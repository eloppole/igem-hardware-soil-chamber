"""
Kill-switch configuration — every tunable lives here.

Nothing in core.py hardcodes a pin, threshold, timeout, or path. Change a value
here (or pass an override to KillSwitch(...)) rather than editing logic.

Pin numbering is BCM, matching chamber.py / bapp.py.
See DESIGN.md for the wiring these pins assume.
"""

import os

# ---------------------------------------------------------------------------
# GPIO — kill line
# ---------------------------------------------------------------------------

# Input. Second (dry) NC contact of the latching E-stop, wired as a loop:
#   3V3 --[10k]--+--> KILL_SENSE_PIN
#                |
#          E-stop NC contact (2-wire run out to the panel)
#                |
#               GND
# Loop closed (button released) -> pin pulled to GND -> reads 0 = HEALTHY.
# Button pressed OR either wire broken -> pull-up wins -> reads 1 = KILL.
# That asymmetry is the fail-safe: signal loss is indistinguishable from a
# press, and both are treated as an active trigger.
KILL_SENSE_PIN = 23
SENSE_PULL = "up"          # "up" | "down" | "none" (internal pull; keep the external one too)
SENSE_KILL_LEVEL = 1       # pin level that means KILL

# Output. Drives the opto/MOSFET that sits in series with the E-stop contacts
# in the 12 V contactor coil loop. HIGH = permissive ("armed"), the contactor
# can pull in. LOW or released = coil de-energized = both 12 V rails cut.
# This is how the software watchdog cuts power through the same coil loop the
# physical button breaks.
KILL_ARM_PIN = 24
ARM_ASSERT_LEVEL = 1       # level that keeps the interlock closed

# Optional. Auxiliary contact on the main contactor, so the Pi can verify the
# rails actually dropped after a kill (catches a welded relay). Off until the
# aux contact is wired. Aux NO closes to GND while the contactor is pulled in.
RAIL_FEEDBACK_ENABLED = False
RAIL_FEEDBACK_PIN = 25
RAIL_LIVE_LEVEL = 0        # level that means the 12 V rails are still live

# lgpio chip number for the 40-pin header. chamber.py uses 0; some older Pi 5
# Bookworm images expose it as 4.
GPIO_CHIP = 0

# "auto" | "lgpio" | "gpiozero" | "sim"
GPIO_BACKEND = "auto"

# Simulation backend never engages by accident: a missing GPIO library on the
# Pi is a hard error, not a silent no-op. Opt in with KILL_SWITCH_SIM=1 when
# developing off-Pi.
ALLOW_SIMULATION = os.environ.get("KILL_SWITCH_SIM") == "1"

# ---------------------------------------------------------------------------
# Layer 2 — GPIO shutdown listener
# ---------------------------------------------------------------------------

SENSE_POLL_INTERVAL_S = 0.01   # 10 ms; the hardware relay has already cut power
SENSE_CONFIRM_SAMPLES = 2      # consecutive kill-level reads before tripping
REQUIRE_HEALTHY_LOOP_TO_ARM = True   # refuse to start with the E-stop engaged

# ---------------------------------------------------------------------------
# Layer 3 — software watchdog
# ---------------------------------------------------------------------------

WATCHDOG_ENABLED = True
WATCHDOG_POLL_INTERVAL_S = 0.5

# --- main-loop heartbeat ---
HEARTBEAT_ENABLED = True
HEARTBEAT_TIMEOUT_S = 15.0     # no KillSwitch.heartbeat() for this long -> kill
HEARTBEAT_GRACE_S = 30.0       # startup slack (camera init is slow)

# --- pump flow rate ---
# Disabled until the flow sensors are calibrated (README: out of scope for this
# pass). The plumbing below is live and tested; only the thresholds are pending.
FLOW_CHECK_ENABLED = False
# pump name -> (min_ml_per_s, max_ml_per_s) expected while that pump is running.
# Placeholder envelopes derived from bapp.py's PUMP_CALIBRATION tables.
FLOW_LIMITS_ML_PER_S = {
    "pump1": (0.10, 0.85),
    "pump2": (0.13, 1.25),
    "pump3": (0.13, 1.25),
}
FLOW_STARTUP_GRACE_S = 2.0     # ignore flow right after a pump is commanded on
FLOW_REPORT_TIMEOUT_S = 5.0    # a running pump must report flow this often
FLOW_FAULT_PERSIST_S = 3.0     # fault must hold this long before tripping

# --- overcurrent / over-temperature (sensors not installed yet) ---
OVERCURRENT_CHECK_ENABLED = False
CURRENT_LIMIT_A = 5.0
OVERTEMP_CHECK_ENABLED = False
TEMP_LIMIT_C = 60.0
ANALOG_REPORT_TIMEOUT_S = 10.0
ANALOG_FAULT_PERSIST_S = 2.0

# ---------------------------------------------------------------------------
# Shutdown behaviour
# ---------------------------------------------------------------------------

SHUTDOWN_HOOK_TIMEOUT_S = 5.0  # per hook; a hung camera close cannot block the rest
STATE_SNAPSHOT_TIMEOUT_S = 2.0
EXIT_ON_TRIP = True            # hard-exit the process after a kill
KILL_EXIT_CODE = 42            # distinct code so cron/systemd can tell kills apart
INSTALL_SIGNAL_HANDLERS = True # SIGINT/SIGTERM -> clean powered-down exit (no latch)
DISARM_ON_EXIT = True          # normal exit also de-energizes the rails

# ---------------------------------------------------------------------------
# Logging and the no-auto-resume latch
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get(
    "KILL_SWITCH_DATA_DIR", os.path.expanduser("~/uv_chamber_data")
)
LOG_PATH = os.path.join(DATA_DIR, "killswitch.log")           # human readable
EVENT_LOG_PATH = os.path.join(DATA_DIR, "killswitch_events.jsonl")  # machine readable
LOG_TO_STDERR = True

# Presence of this file blocks arm(). Written on every kill so that the
# overnight cron job cannot silently relaunch into a fault condition; clearing
# it is a deliberate human act:  python3 -m killswitch --clear-latch
LATCH_PATH = os.path.join(DATA_DIR, "KILL_LATCH")
LATCH_ENABLED = True
