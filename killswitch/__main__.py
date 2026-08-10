"""
Operator CLI:  python3 -m killswitch [--status | --clear-latch | --selftest]

Run from the repo root (the directory holding chamber.py).
"""

import argparse
import json
import sys

from . import config as cfg
from .core import KillSwitch, TriggerSource, clear_latch, latch_info


def cmd_status() -> int:
    info = latch_info()
    sense = (f"GPIO{cfg.KILL_SENSE_PIN} (kill level {cfg.SENSE_KILL_LEVEL})"
             if cfg.INTERLOCK_SENSE_ENABLED else "not wired — lid switch has no sense contact")
    arm = (f"GPIO{cfg.KILL_ARM_PIN} (assert {cfg.ARM_ASSERT_LEVEL})"
           if cfg.INTERLOCK_ARM_ENABLED else "not wired — software cannot cut the 12 V rail")
    print(f"interlock   : sense={sense}")
    print(f"              arm  ={arm}")
    if not (cfg.INTERLOCK_SENSE_ENABLED or cfg.INTERLOCK_ARM_ENABLED):
        print("              a trip commands the loads off via the shutdown hooks,")
        print("              logs, latches and exits; the lid is the only rail cut.")
    print(f"watchdog    : heartbeat {cfg.HEARTBEAT_TIMEOUT_S}s, "
          f"flow check {'on' if cfg.FLOW_CHECK_ENABLED else 'OFF (pending calibration)'}, "
          f"overcurrent {'on' if cfg.OVERCURRENT_CHECK_ENABLED else 'off'}, "
          f"overtemp {'on' if cfg.OVERTEMP_CHECK_ENABLED else 'off'}")
    print(f"logs        : {cfg.LOG_PATH}\n              {cfg.EVENT_LOG_PATH}")
    if info is None:
        print("latch       : clear — the chamber script may be started")
        return 0
    print(f"latch       : SET at {cfg.LATCH_PATH}")
    print(json.dumps(info, indent=2)[:2000])
    print("\nClear it only after fixing the cause:  python3 -m killswitch --clear-latch")
    return 1


def cmd_clear_latch() -> int:
    if clear_latch():
        print("Latch cleared. The chamber script may now be started manually.")
        return 0
    print("No latch was set.")
    return 0


def cmd_selftest() -> int:
    """Arms against the real hardware, reads whatever interlock exists, then
    disarms. Does not touch pumps or UV — nothing is registered as a shutdown
    hook."""
    ks = KillSwitch()
    try:
        ks.arm()
    except Exception as exc:  # noqa: BLE001
        print(f"ARM FAILED: {exc}")
        return 1
    print(json.dumps(ks.status(), indent=2, default=str))

    if not cfg.INTERLOCK_SENSE_ENABLED:
        print("\nNo sense contact is wired, so there is no lid event for the listener")
        print("to catch and nothing to press. Verify the lid switch with a meter")
        print("instead: continuity closed with the lid shut, open with it raised.")
        print("What is armed above is the watchdog, the event log and the latch.")
        ks.disarm(reason="selftest complete")
        return 0

    print("\nOpen the lid within 20 s to verify the listener, or Ctrl-C to stop.")
    tripped = ks.wait_for_trip(timeout=20)
    if not tripped:
        print("No trigger seen in 20 s.")
        ks.disarm(reason="selftest complete")
        return 0
    return cfg.KILL_EXIT_CODE


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m killswitch", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show config and latch state")
    group.add_argument("--clear-latch", action="store_true", help="clear a latched kill event")
    group.add_argument("--selftest", action="store_true",
                       help="arm on real hardware and report the interlock state")
    args = parser.parse_args(argv)

    if args.clear_latch:
        return cmd_clear_latch()
    if args.selftest:
        return cmd_selftest()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
