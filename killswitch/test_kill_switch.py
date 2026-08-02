"""
Offline tests — no Raspberry Pi required. Run from the repo root:

    python3 -m killswitch.test_kill_switch

Every test drives the SimBackend, so the trip paths are exercised for real
(threads, hooks, logging, latch) without any hardware.
"""

import logging
import os
import shutil
import tempfile
import time
import unittest

from .backends import SimBackend
from .core import InterlockOpen, KillSwitch, KillSwitchLatched, TriggerSource

HEALTHY = 0
KILL = 1


class KillSwitchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="killswitch-test-")
        self.backend = SimBackend()
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **overrides):
        defaults = dict(
            DATA_DIR=self.tmp,
            LOG_PATH=os.path.join(self.tmp, "killswitch.log"),
            EVENT_LOG_PATH=os.path.join(self.tmp, "events.jsonl"),
            LATCH_PATH=os.path.join(self.tmp, "KILL_LATCH"),
            EXIT_ON_TRIP=False,          # do not os._exit the test runner
            INSTALL_SIGNAL_HANDLERS=False,
            LOG_TO_STDERR=False,
            SENSE_POLL_INTERVAL_S=0.005,
            WATCHDOG_POLL_INTERVAL_S=0.02,
            HEARTBEAT_GRACE_S=0.0,
        )
        defaults.update(overrides)
        ks = KillSwitch(backend=self.backend, **defaults)
        for handler in list(ks.log.handlers):   # keep test output quiet
            ks.log.removeHandler(handler)
            handler.close()
        ks.log.addHandler(logging.NullHandler())
        # A fresh SimBackend leaves the pulled-up sense pin reading KILL, which
        # is the correct model of an unconnected pin. Start from a closed loop.
        self.backend.levels.setdefault(ks.cfg.KILL_SENSE_PIN, HEALTHY)
        return ks

    def hook(self, name):
        def fn():
            self.calls.append(name)
        return fn

    # -- arming -------------------------------------------------------------

    def test_arm_asserts_interlock_and_disarm_releases_it(self):
        ks = self.make()
        ks.register_shutdown_hook(self.hook("pumps"), "pumps", priority=0)
        ks.arm()
        self.assertTrue(ks.armed)
        self.assertEqual(self.backend.levels[ks.cfg.KILL_ARM_PIN], 1)

        ks.disarm()
        self.assertFalse(ks.armed)
        self.assertEqual(self.backend.levels[ks.cfg.KILL_ARM_PIN], 0)
        self.assertEqual(self.calls, ["pumps"])
        # A clean exit must not latch.
        self.assertFalse(os.path.exists(ks.cfg.LATCH_PATH))

    def test_arm_refused_when_loop_already_open(self):
        ks = self.make()
        self.backend.levels[ks.cfg.KILL_SENSE_PIN] = KILL
        with self.assertRaises(InterlockOpen):
            ks.arm()
        self.assertFalse(ks.armed)
        # Interlock must be left de-asserted after a refused arm.
        self.assertEqual(self.backend.levels.get(ks.cfg.KILL_ARM_PIN, 0), 0)

    def test_arm_refused_while_latched(self):
        ks = self.make()
        ks.arm()
        ks.trip(TriggerSource.MANUAL, "test")
        self.assertTrue(os.path.exists(ks.cfg.LATCH_PATH))

        again = self.make()
        with self.assertRaises(KillSwitchLatched):
            again.arm()

    # -- layer 2: the E-stop listener ---------------------------------------

    def test_estop_line_trips_and_runs_hooks_in_priority_order(self):
        ks = self.make()
        ks.register_shutdown_hook(self.hook("cameras"), "cameras", priority=2)
        ks.register_shutdown_hook(self.hook("pumps"), "pumps", priority=0)
        ks.register_shutdown_hook(self.hook("uv"), "uv", priority=1)
        ks.register_state_provider(lambda: {"uv_pin": 1, "logger": "running"})
        ks.arm()

        self.backend.levels[ks.cfg.KILL_SENSE_PIN] = KILL
        self.assertTrue(ks.wait_for_trip(timeout=2), "listener did not trip")

        self.assertEqual(self.calls, ["pumps", "uv", "cameras"])
        self.assertEqual(self.backend.levels[ks.cfg.KILL_ARM_PIN], 0)
        event = ks.event
        self.assertEqual(event.source, TriggerSource.ESTOP_LOOP)
        self.assertEqual(event.state["host"], {"uv_pin": 1, "logger": "running"})
        self.assertTrue(all(h["ok"] for h in event.hooks))

    def test_gpio_read_failure_is_treated_as_a_trigger(self):
        ks = self.make()
        ks.arm()
        self.backend.fail_reads = True
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.GPIO_FAULT)

    def test_trip_is_idempotent(self):
        ks = self.make()
        ks.register_shutdown_hook(self.hook("pumps"), "pumps")
        ks.arm()
        first = ks.trip(TriggerSource.MANUAL, "one")
        second = ks.trip(TriggerSource.MANUAL, "two")
        self.assertIs(first, second)
        self.assertEqual(self.calls, ["pumps"])

    def test_hanging_hook_does_not_block_the_rest(self):
        ks = self.make(SHUTDOWN_HOOK_TIMEOUT_S=0.1)
        ks.register_shutdown_hook(lambda: time.sleep(5), "wedged-camera", priority=0)
        ks.register_shutdown_hook(self.hook("uv"), "uv", priority=1)
        ks.arm()
        ks.trip(TriggerSource.MANUAL, "hang test")
        self.assertEqual(self.calls, ["uv"])
        hooks = {h["name"]: h for h in ks.event.hooks}
        self.assertFalse(hooks["wedged-camera"]["ok"])
        self.assertTrue(hooks["uv"]["ok"])

    # -- layer 3: the watchdog ----------------------------------------------

    def test_heartbeat_timeout_trips(self):
        ks = self.make(HEARTBEAT_TIMEOUT_S=0.15)
        ks.arm()
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_HEARTBEAT)

    def test_heartbeat_keeps_the_watchdog_quiet(self):
        ks = self.make(HEARTBEAT_TIMEOUT_S=0.3)
        ks.arm()
        for _ in range(10):
            ks.heartbeat()
            time.sleep(0.05)
        self.assertFalse(ks.tripped)
        ks.disarm()

    def test_activity_gate_suppresses_the_heartbeat_while_idle(self):
        """An operator idling at the chamber> prompt with the UV off must not
        trip the watchdog; the same idle with the UV on must."""
        energized = {"value": False}
        ks = self.make(HEARTBEAT_TIMEOUT_S=0.15)
        ks.register_activity_gate(lambda: energized["value"])
        ks.arm()

        time.sleep(0.4)                       # idle at the prompt, nothing on
        self.assertFalse(ks.tripped)

        energized["value"] = True             # UV strip switched on, loop stalls
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_HEARTBEAT)

    def test_activity_gate_failure_is_a_fault(self):
        ks = self.make(HEARTBEAT_TIMEOUT_S=0.15)

        def broken():
            raise RuntimeError("uv_state() unreadable")

        ks.register_activity_gate(broken)
        ks.arm()
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_HEARTBEAT)
        self.assertIn("uv_state() unreadable", ks.event.detail)

    def test_flow_anomaly_trips_after_persisting(self):
        ks = self.make(
            HEARTBEAT_ENABLED=False,
            FLOW_CHECK_ENABLED=True,
            FLOW_STARTUP_GRACE_S=0.05,
            FLOW_FAULT_PERSIST_S=0.1,
            FLOW_LIMITS_ML_PER_S={"pump1": (0.10, 0.85)},
        )
        ks.arm()
        ks.report_pump_state("pump1", True, commanded_flow=0.5)
        ks.report_flow("pump1", 0.5)
        time.sleep(0.15)
        self.assertFalse(ks.tripped, "healthy flow should not trip")

        ks.report_flow("pump1", 0.0)   # dry line / stalled peristaltic head
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_FLOW)
        self.assertIn("pump1", ks.event.detail)

    def test_running_pump_that_stops_reporting_flow_trips(self):
        ks = self.make(
            HEARTBEAT_ENABLED=False,
            FLOW_CHECK_ENABLED=True,
            FLOW_STARTUP_GRACE_S=0.05,
            FLOW_REPORT_TIMEOUT_S=0.1,
            FLOW_FAULT_PERSIST_S=0.05,
            FLOW_LIMITS_ML_PER_S={"pump2": (0.13, 1.25)},
        )
        ks.arm()
        ks.report_pump_state("pump2", True, commanded_flow=0.6)
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_FLOW)

    def test_idle_pump_is_not_checked(self):
        ks = self.make(
            HEARTBEAT_ENABLED=False,
            FLOW_CHECK_ENABLED=True,
            FLOW_STARTUP_GRACE_S=0.0,
            FLOW_FAULT_PERSIST_S=0.05,
            FLOW_LIMITS_ML_PER_S={"pump1": (0.10, 0.85)},
        )
        ks.arm()
        ks.report_pump_state("pump1", False)
        time.sleep(0.2)
        self.assertFalse(ks.tripped)
        ks.disarm()

    def test_overcurrent_and_overtemp(self):
        for kwargs, value, reporter, source in (
            (dict(OVERCURRENT_CHECK_ENABLED=True, CURRENT_LIMIT_A=4.0),
             9.0, "report_current", TriggerSource.WATCHDOG_OVERCURRENT),
            (dict(OVERTEMP_CHECK_ENABLED=True, TEMP_LIMIT_C=55.0),
             80.0, "report_temperature", TriggerSource.WATCHDOG_OVERTEMP),
        ):
            with self.subTest(source=source):
                self.backend = SimBackend()
                ks = self.make(HEARTBEAT_ENABLED=False, ANALOG_FAULT_PERSIST_S=0.05,
                               LATCH_ENABLED=False, **kwargs)
                ks.arm()
                getattr(ks, reporter)(value)
                self.assertTrue(ks.wait_for_trip(timeout=2))
                self.assertEqual(ks.event.source, source)

    def test_custom_watchdog_check(self):
        ks = self.make(HEARTBEAT_ENABLED=False)
        ks.register_watchdog_check("uv-runtime", lambda: "UV on longer than 4 h")
        ks.arm()
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertEqual(ks.event.source, TriggerSource.WATCHDOG_CUSTOM)
        self.assertIn("uv-runtime", ks.event.detail)

    def test_check_that_raises_is_itself_a_fault(self):
        ks = self.make(HEARTBEAT_ENABLED=False)

        def broken():
            raise ValueError("sensor bus gone")

        ks.register_watchdog_check("flow-sensor", broken)
        ks.arm()
        self.assertTrue(ks.wait_for_trip(timeout=2))
        self.assertIn("sensor bus gone", ks.event.detail)

    # -- logging ------------------------------------------------------------

    def test_event_log_and_latch_record_the_trigger(self):
        import json

        ks = self.make()
        ks.arm()
        ks.trip(TriggerSource.MANUAL, "operator pressed the UI stop button")

        with open(ks.cfg.EVENT_LOG_PATH) as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["source"], "manual_software_request")
        self.assertIn("timestamp_local", lines[0])
        self.assertIn("uptime_s", lines[0])

        with open(ks.cfg.LATCH_PATH) as fh:
            latch = json.load(fh)
        self.assertEqual(latch["source"], "manual_software_request")

    def test_clear_latch_allows_a_new_arm(self):
        from .core import clear_latch

        ks = self.make()
        ks.arm()
        ks.trip(TriggerSource.MANUAL, "test")
        self.assertTrue(clear_latch(ks.cfg))

        self.backend = SimBackend()
        again = self.make()
        again.arm()
        self.assertTrue(again.armed)
        again.disarm()

    def test_status_reports_the_loop(self):
        ks = self.make()
        ks.arm()
        status = ks.status()
        self.assertTrue(status["armed"])
        self.assertTrue(status["sense_healthy"])
        self.assertFalse(status["latched"])
        ks.disarm()


if __name__ == "__main__":
    unittest.main(verbosity=2)
