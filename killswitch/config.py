"""
Kill-switch configuration — every tunable lives here.

Nothing in core.py hardcodes a pin, threshold, timeout, or path. Change a value
here (or pass an override to KillSwitch(...)) rather than editing logic.

Pin numbering is BCM, matching chamber.py / bapp.py.
See DESIGN.md for the wiring, and docs/circuit_image.svg for the drawing.

The rig as built
----------------
The kill switch is a lid interlock, not an emergency stop: a keyboard-style
switch held closed by the lid, wired in series with the 12 V positive feed.

    12 V (+) ── LID SWITCH ──┬── LM2596 buck (12 V->6 V) ── UV emitters
                             └── pump (+)

    each load's return:  load (-) ── MOSFET drain
                         MOSFET source ── common GND
                         MOSFET gate  ── [220R] ── Pi GPIO,  [10k] ── GND

Opening the lid breaks the 12 V rail mechanically, with no involvement from the
Pi. There is no sense wire back to the Pi, no Pi-driven contactor, and no relay
coil loop -- so software cannot observe the lid, and cannot cut the rail. What
software can still do is drop the MOSFET gates (turning every load off) and
refuse to resume. That is what this package does; see INTERLOCK_* below.

Note also: the buck converters hold charge in their output capacitors, so the
emitters can lag the lid opening slightly. See docs/killswitch-instructions.txt.
"""

import os

# ---------------------------------------------------------------------------
# GPIO — kill line
# ---------------------------------------------------------------------------

# Is the lid switch's second contact wired back to the Pi as a sense loop?
# FALSE ON THE RIG AS BUILT -- the switch is a single pole in the 12 V feed and
# has no low-voltage contact. With this off there is nothing to listen to, so
# the listener thread does not run and KILL_SENSE_PIN is not claimed.
INTERLOCK_SENSE_ENABLED = False

# Is there a Pi-driven device (opto/MOSFET/relay coil) in series with the 12 V
# feed, that the watchdog can open to cut the rail itself?
# FALSE ON THE RIG AS BUILT -- the lid switch is the only thing in that line.
# With this off the watchdog still trips, but "cutting power" degrades to
# running the shutdown hooks, i.e. commanding each load's MOSFET gate low.
INTERLOCK_ARM_ENABLED = False

# --- the pins below apply only when the flags above are turned on ---

# Input, if a sense contact is ever added, wired as a loop:
#   3V3 --[10k]--+--> KILL_SENSE_PIN
#                |
#          lid switch dry contact (2-wire run out to the lid)
#                |
#               GND
# Loop closed (lid shut) -> pin pulled to GND -> reads 0 = HEALTHY.
# Lid open OR either wire broken -> pull-up wins -> reads 1 = KILL.
# That asymmetry is the fail-safe: signal loss is indistinguishable from an
# open lid, and both are treated as an active trigger.
KILL_SENSE_PIN = 23
SENSE_PULL = "up"          # "up" | "down" | "none" (internal pull; keep the external one too)
SENSE_KILL_LEVEL = 1       # pin level that means KILL

# Output, if a Pi-side interlock is ever added in series with the 12 V feed.
# HIGH = permissive ("armed"), the rail can be live. LOW or released = rail cut.
KILL_ARM_PIN = 24
ARM_ASSERT_LEVEL = 1       # level that keeps the interlock closed

# Optional. Feedback contact telling the Pi whether the 12 V rail is actually
# live, so a kill can be confirmed (catches a welded relay). Off until wired.
RAIL_FEEDBACK_ENABLED = False
RAIL_FEEDBACK_PIN = 25
RAIL_LIVE_LEVEL = 0        # level that means the 12 V rail is still live

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
# Layer 2 — GPIO shutdown listener  (only runs when INTERLOCK_SENSE_ENABLED)
# ---------------------------------------------------------------------------

SENSE_POLL_INTERVAL_S = 0.01   # 10 ms; the lid switch has already cut power
SENSE_CONFIRM_SAMPLES = 2      # consecutive kill-level reads before tripping
REQUIRE_HEALTHY_LOOP_TO_ARM = True   # refuse to start with the lid already open

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
