"""
Layered kill switch for the iGEM wildfire soil chamber (Raspberry Pi 5).

Layer 1 (hardware, not implemented here): a latching 2NC E-stop in series with
the 12 V contactor coil loop. It cuts power with no involvement from the Pi.
This module assumes it exists and never assumes power is still present.

Layer 2 (this module): a listener watching the E-stop's second contact. On an
active trigger it stops pumps, drops the UV pin, closes cameras and files,
logs the event, latches, and exits without restarting.

Layer 3 (this module): a watchdog that independently checks heartbeat, flow,
current and temperature, and cuts the rails through the same contactor coil
loop that the physical button breaks.

Wiring model and the reasoning behind it: see DESIGN.md.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as _config_module
from .backends import GpioBackend, GpioUnavailable, open_backend

__all__ = [
    "KillSwitch",
    "KillEvent",
    "TriggerSource",
    "KillSwitchLatched",
    "InterlockOpen",
    "GpioUnavailable",
]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class TriggerSource(str, Enum):
    """What fired the kill. Recorded verbatim in every log line."""

    # A wire break is indistinguishable from a press by design -- both mean
    # "the interlock loop is open", and both are treated as an active trigger.
    ESTOP_LOOP = "estop_button_or_loop_open"
    WATCHDOG_HEARTBEAT = "watchdog_heartbeat_timeout"
    WATCHDOG_FLOW = "watchdog_flow_anomaly"
    WATCHDOG_OVERCURRENT = "watchdog_overcurrent"
    WATCHDOG_OVERTEMP = "watchdog_overtemp"
    WATCHDOG_CUSTOM = "watchdog_custom_check"
    GPIO_FAULT = "gpio_read_failure"
    MANUAL = "manual_software_request"


class KillSwitchLatched(RuntimeError):
    """A previous kill has not been cleared; arming is refused."""


class InterlockOpen(RuntimeError):
    """The E-stop loop is already open at startup (button in, or wire off)."""


@dataclass
class KillEvent:
    source: TriggerSource
    detail: str
    timestamp_utc: str
    timestamp_local: str
    uptime_s: float
    state: Dict[str, Any] = field(default_factory=dict)
    hooks: List[Dict[str, Any]] = field(default_factory=list)
    rails_confirmed_dead: Optional[bool] = None

    def as_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["source"] = self.source.value
        return d


@dataclass(order=True)
class _Hook:
    priority: int
    seq: int
    name: str = field(compare=False)
    func: Callable[[], Any] = field(compare=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_with_timeout(func, timeout: float, name: str) -> Tuple[bool, Any, Optional[str]]:
    """Run func in a throwaway thread. A hung shutdown step must not block the
    steps after it -- closing a wedged camera cannot stop the log from being
    written."""
    box: Dict[str, Any] = {}

    def runner():
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - never let a hook escape
            box["error"] = repr(exc)

    thread = threading.Thread(target=runner, name=f"killswitch-hook-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None, f"timed out after {timeout}s"
    return "error" not in box, box.get("value"), box.get("error")


def _resolve_config(config=None, overrides: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
    base = {k: v for k, v in vars(_config_module).items() if k.isupper()}
    if config is not None:
        src = vars(config) if not isinstance(config, dict) else config
        base.update({k: v for k, v in src.items() if k.isupper()})
    for key, value in (overrides or {}).items():
        if key not in base:
            raise AttributeError(f"unknown kill switch config option: {key!r}")
        base[key] = value
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------


class KillSwitch:
    """Owns the kill line, the listener thread and the watchdog thread.

    Typical host usage::

        ks = KillSwitch()
        ks.register_shutdown_hook(all_pumps_off, "pumps", priority=0)
        ks.register_shutdown_hook(uv_off,        "uv",    priority=1)
        ks.register_shutdown_hook(close_cameras, "cameras", priority=2)
        ks.register_state_provider(snapshot)
        ks.arm()
        while running:
            ks.heartbeat()
            ...
    """

    def __init__(
        self,
        config=None,
        gpio_handle: Optional[int] = None,
        backend: Optional[GpioBackend] = None,
        **overrides,
    ):
        self.cfg = _resolve_config(config, overrides)
        self._gpio_handle = gpio_handle
        self._backend = backend
        self._backend_injected = backend is not None

        self._armed = False
        self._tripped = False
        self._event: Optional[KillEvent] = None
        self._trip_lock = threading.RLock()
        self._tripped_flag = threading.Event()
        self._stop_threads = threading.Event()
        self._threads: List[threading.Thread] = []
        self._armed_at = 0.0

        self._hooks: List[_Hook] = []
        self._hook_seq = 0
        self._state_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._custom_checks: List[Tuple[str, Callable[[], Optional[str]]]] = []

        self._last_heartbeat = 0.0
        self._heartbeat_source = ""
        self._activity_gate: Optional[Callable[[], bool]] = None
        self._gate_active = False
        self._pumps: Dict[str, Dict[str, Any]] = {}
        self._analog: Dict[str, Dict[str, Any]] = {}
        self._prev_signal_handlers: Dict[int, Any] = {}

        self.log = self._build_logger()

    # -- properties ---------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def event(self) -> Optional[KillEvent]:
        return self._event

    # -- registration -------------------------------------------------------

    def register_shutdown_hook(self, func: Callable[[], Any], name: str, priority: int = 100) -> None:
        """Register an idempotent power-down action. Lower priority runs first;
        put pumps before UV before cameras. Hooks must be safe to call when the
        thing is already off -- the rails may already be dead."""
        self._hook_seq += 1
        self._hooks.append(_Hook(priority=priority, seq=self._hook_seq, name=name, func=func))
        self._hooks.sort()

    def register_state_provider(self, func: Callable[[], Dict[str, Any]]) -> None:
        """Supply the system-state snapshot recorded with each kill event."""
        self._state_provider = func

    def register_activity_gate(self, func: Callable[[], bool]) -> None:
        """Enforce the heartbeat only while func() is True — that is, only while
        something hazardous is energized. Without a gate the heartbeat is always
        enforced, which would trip on an operator idling at an interactive
        prompt. The heartbeat window restarts each time the gate opens."""
        self._activity_gate = func

    def register_watchdog_check(self, name: str, func: Callable[[], Optional[str]]) -> None:
        """Add a fault condition without touching core logic. Return None when
        healthy, or a string describing the fault to trigger a kill."""
        self._custom_checks.append((name, func))

    # -- inputs from the host -----------------------------------------------

    def heartbeat(self, source: str = "main") -> None:
        """Call from the main loop. Silence for HEARTBEAT_TIMEOUT_S is a kill."""
        self._last_heartbeat = time.monotonic()
        self._heartbeat_source = source

    def report_pump_state(self, pump: str, running: bool, commanded_flow: Optional[float] = None) -> None:
        entry = self._pumps.setdefault(pump, {})
        entry.update(
            running=bool(running),
            commanded_flow=commanded_flow,
            changed_at=time.monotonic(),
            fault_since=None,
        )
        if not running:
            entry["measured_flow"] = None

    def report_flow(self, pump: str, ml_per_s: float) -> None:
        entry = self._pumps.setdefault(pump, {"running": True, "changed_at": time.monotonic()})
        entry["measured_flow"] = float(ml_per_s)
        entry["reported_at"] = time.monotonic()

    def report_current(self, amps: float, rail: str = "main") -> None:
        self._analog.setdefault(f"current:{rail}", {}).update(
            value=float(amps), reported_at=time.monotonic()
        )

    def report_temperature(self, celsius: float, probe: str = "main") -> None:
        self._analog.setdefault(f"temp:{probe}", {}).update(
            value=float(celsius), reported_at=time.monotonic()
        )

    # -- arming -------------------------------------------------------------

    def arm(self) -> "KillSwitch":
        """Claim the kill line, energize the interlock, start both threads."""
        if self._armed:
            return self
        cfg = self.cfg

        self._check_latch()

        if self._backend is None:
            self._backend = open_backend(cfg, self._gpio_handle)
        if self._backend.name == "sim":
            self.log.warning(
                "SIMULATED GPIO backend — no hardware is being protected. "
                "This must never be the case on the chamber Pi."
            )

        self._backend.claim_input(cfg.KILL_SENSE_PIN, cfg.SENSE_PULL)
        # Claim the arm line de-asserted, so a crash between here and the
        # assert below leaves the rails down rather than up.
        self._backend.claim_output(cfg.KILL_ARM_PIN, 1 - cfg.ARM_ASSERT_LEVEL)
        if cfg.RAIL_FEEDBACK_ENABLED:
            self._backend.claim_input(cfg.RAIL_FEEDBACK_PIN, "up")

        level = self._read_sense_or_fail()
        if level == cfg.SENSE_KILL_LEVEL and cfg.REQUIRE_HEALTHY_LOOP_TO_ARM:
            self._backend.close()
            self._backend = None
            raise InterlockOpen(
                f"E-stop loop is open at startup (GPIO{cfg.KILL_SENSE_PIN} reads "
                f"{level}). Release the E-stop and check the two sense wires, "
                "then start again."
            )

        self._backend.write(cfg.KILL_ARM_PIN, cfg.ARM_ASSERT_LEVEL)
        time.sleep(0.05)  # let the contactor settle before trusting the loop
        if self._read_sense_or_fail() == cfg.SENSE_KILL_LEVEL and cfg.REQUIRE_HEALTHY_LOOP_TO_ARM:
            self._backend.write(cfg.KILL_ARM_PIN, 1 - cfg.ARM_ASSERT_LEVEL)
            self._backend.close()
            self._backend = None
            raise InterlockOpen("E-stop loop opened while arming; refusing to run.")

        now = time.monotonic()
        self._armed_at = now
        self._last_heartbeat = now
        self._armed = True
        self._stop_threads.clear()

        self._start_thread(self._listener_loop, "killswitch-listener")
        if cfg.WATCHDOG_ENABLED:
            self._start_thread(self._watchdog_loop, "killswitch-watchdog")

        self._install_signal_handlers()
        self.log.info(
            "ARMED  backend=%s sense=GPIO%d arm=GPIO%d heartbeat=%.1fs watchdog=%s "
            "flow_check=%s exit_on_trip=%s",
            self._backend.name,
            cfg.KILL_SENSE_PIN,
            cfg.KILL_ARM_PIN,
            cfg.HEARTBEAT_TIMEOUT_S,
            cfg.WATCHDOG_ENABLED,
            cfg.FLOW_CHECK_ENABLED,
            cfg.EXIT_ON_TRIP,
        )
        if not cfg.EXIT_ON_TRIP:
            self.log.warning("EXIT_ON_TRIP is disabled — the host must exit on its own.")
        return self

    def _check_latch(self) -> None:
        cfg = self.cfg
        if not cfg.LATCH_ENABLED or not os.path.exists(cfg.LATCH_PATH):
            return
        detail = ""
        try:
            with open(cfg.LATCH_PATH) as fh:
                data = json.load(fh)
            detail = f" ({data.get('source')} at {data.get('timestamp_local')}: {data.get('detail')})"
        except Exception:
            pass
        raise KillSwitchLatched(
            f"A kill event is latched{detail}.\n"
            f"Inspect {cfg.LOG_PATH}, fix the cause, then clear it deliberately:\n"
            f"    python3 -m killswitch --clear-latch"
        )

    def _start_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _install_signal_handlers(self) -> None:
        if not self.cfg.INSTALL_SIGNAL_HANDLERS:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, _frame):
            name = signal.Signals(signum).name
            self.log.info("%s received — powering down cleanly.", name)
            self.disarm(reason=f"signal {name}")
            previous = self._prev_signal_handlers.get(signum)
            if callable(previous) and previous not in (signal.SIG_IGN, signal.SIG_DFL):
                previous(signum, _frame)
            else:
                raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._prev_signal_handlers[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    # -- monitoring threads -------------------------------------------------

    def _read_sense_or_fail(self) -> int:
        return self._backend.read(self.cfg.KILL_SENSE_PIN)

    def _listener_loop(self) -> None:
        """Layer 2. Watches the E-stop sense line; a read failure is itself a
        trigger (we cannot prove the loop is healthy, so assume it is not)."""
        cfg = self.cfg
        consecutive = 0
        while not self._stop_threads.is_set() and not self._tripped:
            try:
                level = self._read_sense_or_fail()
            except Exception as exc:  # noqa: BLE001
                self.trip(TriggerSource.GPIO_FAULT,
                          f"cannot read sense line GPIO{cfg.KILL_SENSE_PIN}: {exc!r}")
                return
            if level == cfg.SENSE_KILL_LEVEL:
                consecutive += 1
                if consecutive >= cfg.SENSE_CONFIRM_SAMPLES:
                    self.trip(
                        TriggerSource.ESTOP_LOOP,
                        f"GPIO{cfg.KILL_SENSE_PIN} read kill level {level} on "
                        f"{consecutive} consecutive samples — E-stop pressed or "
                        "interlock wiring open",
                    )
                    return
            else:
                consecutive = 0
            self._stop_threads.wait(cfg.SENSE_POLL_INTERVAL_S)

    def _watchdog_loop(self) -> None:
        """Layer 3. Every check returns None when healthy or a detail string."""
        cfg = self.cfg
        checks = (
            (TriggerSource.WATCHDOG_HEARTBEAT, self._check_heartbeat),
            (TriggerSource.WATCHDOG_FLOW, self._check_flow),
            (TriggerSource.WATCHDOG_OVERCURRENT, self._check_overcurrent),
            (TriggerSource.WATCHDOG_OVERTEMP, self._check_overtemp),
        )
        while not self._stop_threads.is_set() and not self._tripped:
            for source, check in checks:
                try:
                    detail = check()
                except Exception as exc:  # noqa: BLE001
                    detail = f"watchdog check raised {exc!r}"
                if detail:
                    self.trip(source, detail)
                    return
            for name, check in self._custom_checks:
                try:
                    detail = check()
                except Exception as exc:  # noqa: BLE001
                    detail = f"raised {exc!r}"
                if detail:
                    self.trip(TriggerSource.WATCHDOG_CUSTOM, f"{name}: {detail}")
                    return
            self._stop_threads.wait(cfg.WATCHDOG_POLL_INTERVAL_S)

    def _check_heartbeat(self) -> Optional[str]:
        cfg = self.cfg
        if not cfg.HEARTBEAT_ENABLED:
            return None
        now = time.monotonic()
        if now - self._armed_at < cfg.HEARTBEAT_GRACE_S:
            return None

        if self._activity_gate is not None:
            try:
                active = bool(self._activity_gate())
            except Exception as exc:  # noqa: BLE001
                # Cannot tell whether anything is energized -- assume the worst.
                return f"activity gate raised {exc!r}"
            if not active:
                self._gate_active = False
                return None
            if not self._gate_active:
                self._gate_active = True
                self._last_heartbeat = now  # fresh window as the hazard starts
                return None

        age = now - self._last_heartbeat
        if age > cfg.HEARTBEAT_TIMEOUT_S:
            return (f"no heartbeat for {age:.1f}s (limit {cfg.HEARTBEAT_TIMEOUT_S}s, "
                    f"last source {self._heartbeat_source!r}) — control loop is stalled")
        return None

    def _check_flow(self) -> Optional[str]:
        cfg = self.cfg
        if not cfg.FLOW_CHECK_ENABLED:
            return None
        now = time.monotonic()
        for pump, entry in self._pumps.items():
            if not entry.get("running"):
                entry["fault_since"] = None
                continue
            if now - entry.get("changed_at", now) < cfg.FLOW_STARTUP_GRACE_S:
                continue

            limits = cfg.FLOW_LIMITS_ML_PER_S.get(pump)
            if limits is None:
                continue
            low, high = limits

            reported_at = entry.get("reported_at")
            if reported_at is None or now - reported_at > cfg.FLOW_REPORT_TIMEOUT_S:
                fault = (f"{pump} is running but has not reported flow for "
                         f"{'ever' if reported_at is None else f'{now - reported_at:.1f}s'} "
                         f"(limit {cfg.FLOW_REPORT_TIMEOUT_S}s)")
            else:
                flow = entry.get("measured_flow")
                if flow is None or flow < low or flow > high:
                    fault = (f"{pump} flow {flow} mL/s outside [{low}, {high}] "
                             f"(commanded {entry.get('commanded_flow')})")
                else:
                    fault = None

            if fault is None:
                entry["fault_since"] = None
                continue
            if entry.get("fault_since") is None:
                entry["fault_since"] = now
                continue
            held = now - entry["fault_since"]
            if held >= cfg.FLOW_FAULT_PERSIST_S:
                return f"{fault}; persisted {held:.1f}s"
        return None

    def _check_analog(self, key_prefix: str, enabled: bool, limit: float,
                      unit: str, label: str) -> Optional[str]:
        cfg = self.cfg
        if not enabled:
            return None
        now = time.monotonic()
        for key, entry in self._analog.items():
            if not key.startswith(key_prefix):
                continue
            age = now - entry.get("reported_at", 0.0)
            if age > cfg.ANALOG_REPORT_TIMEOUT_S:
                return f"{label} sensor {key} stale for {age:.1f}s (limit {cfg.ANALOG_REPORT_TIMEOUT_S}s)"
            value = entry.get("value")
            if value is not None and value > limit:
                if entry.get("fault_since") is None:
                    entry["fault_since"] = now
                elif now - entry["fault_since"] >= cfg.ANALOG_FAULT_PERSIST_S:
                    return (f"{label} {key} at {value}{unit} over limit {limit}{unit} "
                            f"for {now - entry['fault_since']:.1f}s")
            else:
                entry["fault_since"] = None
        return None

    def _check_overcurrent(self) -> Optional[str]:
        return self._check_analog("current:", self.cfg.OVERCURRENT_CHECK_ENABLED,
                                  self.cfg.CURRENT_LIMIT_A, "A", "current")

    def _check_overtemp(self) -> Optional[str]:
        return self._check_analog("temp:", self.cfg.OVERTEMP_CHECK_ENABLED,
                                  self.cfg.TEMP_LIMIT_C, "C", "temperature")

    # -- tripping -----------------------------------------------------------

    def trip(self, source: TriggerSource = TriggerSource.MANUAL, detail: str = "") -> Optional[KillEvent]:
        """Fire the kill. Idempotent and safe from any thread; the first caller
        wins and later callers are no-ops."""
        with self._trip_lock:
            if self._tripped:
                return self._event
            self._tripped = True
        return self._execute_shutdown(source, detail)

    def _execute_shutdown(self, source: TriggerSource, detail: str) -> KillEvent:
        cfg = self.cfg
        now = datetime.now()
        event = KillEvent(
            source=source,
            detail=detail,
            timestamp_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            timestamp_local=now.isoformat(timespec="milliseconds"),
            uptime_s=round(time.monotonic() - self._armed_at, 3) if self._armed_at else 0.0,
        )
        self.log.critical("KILL [%s] %s", source.value, detail)

        # 1. De-energize the contactor coil first — the fastest path to safe.
        #    Everything after this is tidy-up on already-dead rails.
        self._drop_arm_line()

        # 2. Snapshot before the hooks change anything.
        event.state = self._snapshot_state()

        # 3. Idempotent power-down hooks, each individually time-boxed.
        event.hooks = self._run_hooks()

        # 4. Confirm the rails actually dropped, if the aux contact is wired.
        event.rails_confirmed_dead = self._verify_rails_dead()

        # 5. Persist. Do this before releasing GPIO or exiting.
        self._stop_threads.set()
        self._write_event(event)
        self._write_latch(event)

        self._event = event
        self._armed = False
        self._tripped_flag.set()
        self._release_gpio()

        self.log.critical(
            "Kill complete. No auto-resume: clear the latch and restart manually. "
            "log=%s events=%s", cfg.LOG_PATH, cfg.EVENT_LOG_PATH
        )
        for handler in list(self.log.handlers):
            try:
                handler.flush()
            except Exception:
                pass

        if cfg.EXIT_ON_TRIP:
            # os._exit, not sys.exit: this often runs on a watchdog thread,
            # where SystemExit would only end that thread and leave the
            # chamber script running unsupervised.
            os._exit(cfg.KILL_EXIT_CODE)
        return event

    def _drop_arm_line(self) -> None:
        cfg = self.cfg
        try:
            self._backend.write(cfg.KILL_ARM_PIN, 1 - cfg.ARM_ASSERT_LEVEL)
            self.log.critical("Interlock released: GPIO%d -> %d (12 V rails cut)",
                              cfg.KILL_ARM_PIN, 1 - cfg.ARM_ASSERT_LEVEL)
        except Exception as exc:  # noqa: BLE001
            self.log.critical("FAILED to release interlock GPIO%d: %r — the hardware "
                              "E-stop is now the only power cut.", cfg.KILL_ARM_PIN, exc)

    def _snapshot_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "pumps": {k: dict(v) for k, v in self._pumps.items()},
            "analog": {k: dict(v) for k, v in self._analog.items()},
            "seconds_since_heartbeat": (
                round(time.monotonic() - self._last_heartbeat, 3) if self._last_heartbeat else None
            ),
            "heartbeat_source": self._heartbeat_source,
        }
        try:
            state["sense_pin_level"] = self._backend.read(self.cfg.KILL_SENSE_PIN)
        except Exception as exc:  # noqa: BLE001
            state["sense_pin_level"] = f"unreadable: {exc!r}"
        if self._state_provider is not None:
            ok, value, error = _run_with_timeout(
                self._state_provider, self.cfg.STATE_SNAPSHOT_TIMEOUT_S, "state-provider"
            )
            state["host"] = value if ok else {"error": error}
        return state

    def _run_hooks(self) -> List[Dict[str, Any]]:
        results = []
        for hook in self._hooks:
            started = time.monotonic()
            ok, _value, error = _run_with_timeout(
                hook.func, self.cfg.SHUTDOWN_HOOK_TIMEOUT_S, hook.name
            )
            entry = {
                "name": hook.name,
                "priority": hook.priority,
                "ok": ok,
                "duration_s": round(time.monotonic() - started, 3),
            }
            if not ok:
                entry["error"] = error
                self.log.error("shutdown hook %r failed: %s", hook.name, error)
            else:
                self.log.info("shutdown hook %r ok (%.3fs)", hook.name, entry["duration_s"])
            results.append(entry)
        return results

    def _verify_rails_dead(self) -> Optional[bool]:
        cfg = self.cfg
        if not cfg.RAIL_FEEDBACK_ENABLED:
            return None
        time.sleep(0.1)  # contactor drop-out time
        try:
            live = self._backend.read(cfg.RAIL_FEEDBACK_PIN) == cfg.RAIL_LIVE_LEVEL
        except Exception as exc:  # noqa: BLE001
            self.log.error("rail feedback unreadable: %r", exc)
            return None
        if live:
            self.log.critical(
                "RAILS STILL LIVE after kill — suspect a welded contactor. "
                "Press the hardware E-stop and disconnect the 12 V supplies now."
            )
        return not live

    # -- clean shutdown -----------------------------------------------------

    def disarm(self, reason: str = "normal shutdown") -> None:
        """Normal exit path: stop the threads, power everything down, release
        the interlock. No latch is written, so the next run may start."""
        if not self._armed or self._tripped:
            return
        self._armed = False
        self._stop_threads.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads.clear()
        self.log.info("DISARM (%s) — running shutdown hooks and cutting the rails.", reason)
        self._run_hooks()
        self._drop_arm_line()
        self._release_gpio()

    def _release_gpio(self) -> None:
        if self._backend is None or self._backend_injected:
            return
        try:
            self._backend.close()
        except Exception:
            pass

    # -- misc ---------------------------------------------------------------

    def wait_for_trip(self, timeout: Optional[float] = None) -> bool:
        """Block until a kill fires. Useful when EXIT_ON_TRIP is disabled."""
        return self._tripped_flag.wait(timeout)

    def status(self) -> Dict[str, Any]:
        cfg = self.cfg
        try:
            sense = self._backend.read(cfg.KILL_SENSE_PIN) if self._backend else None
        except Exception as exc:  # noqa: BLE001
            sense = f"unreadable: {exc!r}"
        return {
            "armed": self._armed,
            "tripped": self._tripped,
            "backend": self._backend.name if self._backend else None,
            "sense_pin": cfg.KILL_SENSE_PIN,
            "sense_level": sense,
            "sense_healthy": (sense != cfg.SENSE_KILL_LEVEL) if isinstance(sense, int) else False,
            "arm_pin": cfg.KILL_ARM_PIN,
            "latched": bool(cfg.LATCH_ENABLED and os.path.exists(cfg.LATCH_PATH)),
            "watchdog_enabled": cfg.WATCHDOG_ENABLED,
            "flow_check_enabled": cfg.FLOW_CHECK_ENABLED,
            "seconds_since_heartbeat": (
                round(time.monotonic() - self._last_heartbeat, 2) if self._last_heartbeat else None
            ),
            "pumps": {k: dict(v) for k, v in self._pumps.items()},
            "hooks": [h.name for h in self._hooks],
        }

    # -- persistence --------------------------------------------------------

    def _build_logger(self) -> logging.Logger:
        cfg = self.cfg
        log = logging.getLogger("killswitch")
        log.setLevel(logging.INFO)
        log.propagate = False
        # Reuse the handlers only if they already point at this config's log
        # file; an instance built with a different LOG_PATH must not keep
        # writing to the previous one.
        if log.handlers:
            if any(getattr(h, "baseFilename", None) == os.path.abspath(cfg.LOG_PATH)
                   for h in log.handlers):
                return log
            for handler in list(log.handlers):
                log.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s [killswitch] %(message)s")
        try:
            os.makedirs(cfg.DATA_DIR, exist_ok=True)
            file_handler = logging.FileHandler(cfg.LOG_PATH)
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: kill switch file log unavailable ({exc!r})", file=sys.stderr)
        if cfg.LOG_TO_STDERR:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(fmt)
            log.addHandler(stream_handler)
        return log

    def _write_event(self, event: KillEvent) -> None:
        try:
            os.makedirs(self.cfg.DATA_DIR, exist_ok=True)
            with open(self.cfg.EVENT_LOG_PATH, "a") as fh:
                fh.write(json.dumps(event.as_dict(), default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:  # noqa: BLE001
            self.log.error("could not append to event log: %r", exc)

    def _write_latch(self, event: KillEvent) -> None:
        if not self.cfg.LATCH_ENABLED:
            return
        try:
            os.makedirs(self.cfg.DATA_DIR, exist_ok=True)
            with open(self.cfg.LATCH_PATH, "w") as fh:
                json.dump(event.as_dict(), fh, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            self.log.critical("Latched: %s blocks the next arm().", self.cfg.LATCH_PATH)
        except Exception as exc:  # noqa: BLE001
            self.log.error("could not write latch file: %r", exc)


# ---------------------------------------------------------------------------
# Latch helpers (also exposed through `python3 -m killswitch`)
# ---------------------------------------------------------------------------


def latch_info(cfg=None) -> Optional[Dict[str, Any]]:
    cfg = _resolve_config(cfg)
    if not os.path.exists(cfg.LATCH_PATH):
        return None
    try:
        with open(cfg.LATCH_PATH) as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"latch present but unreadable: {exc!r}"}


def clear_latch(cfg=None, operator: Optional[str] = None) -> bool:
    """Deliberate human act — the clearance itself is logged."""
    cfg = _resolve_config(cfg)
    if not os.path.exists(cfg.LATCH_PATH):
        return False
    previous = latch_info(cfg)
    os.remove(cfg.LATCH_PATH)
    who = operator or os.environ.get("USER", "unknown")
    log = logging.getLogger("killswitch")
    if not log.handlers:
        logging.basicConfig(level=logging.INFO)
    log.warning("LATCH CLEARED by %s (previous event: %s)", who,
                json.dumps(previous, default=str) if previous else "unreadable")
    try:
        with open(cfg.EVENT_LOG_PATH, "a") as fh:
            fh.write(json.dumps({
                "source": "latch_cleared",
                "operator": who,
                "timestamp_local": datetime.now().isoformat(timespec="milliseconds"),
                "cleared_event": previous,
            }, default=str) + "\n")
    except Exception:
        pass
    return True
