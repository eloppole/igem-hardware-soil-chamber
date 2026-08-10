# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Control software for an iGEM wildfire soil-simulation chamber on a Raspberry Pi 5: four Violumas
UV emitters behind two LM2596 bucks, peristaltic pumps, two Pi Camera Module 3 units, and a GUVA
UV sensor read through an MCP3008 — plus `killswitch/`, a safety package both control programs
import.

No packaging, dependency manifest, CI, or linter config. Dependencies are Raspberry Pi OS apt
packages (`python3-lgpio`, `python3-spidev`, `python3-picamera2`, gpiozero, Flask); tests are
stdlib `unittest`.

## Commands

Run on the Pi (over SSH):

```bash
python3 chamber.py      # interactive `chamber>` shell
python3 bapp.py         # Flask web UI on 0.0.0.0:5000
```

Run anywhere, from the repo root:

```bash
python3 -m killswitch.test_kill_switch                                          # full suite (24 tests)
python3 -m unittest killswitch.test_kill_switch.KillSwitchTestCase.<name> -v    # single test
python3 -m killswitch --status                                                  # config, interlock wiring, latch
python3 -m killswitch --clear-latch                                             # clear a latched kill
```

`--selftest` arms against real GPIO and needs the Pi. `KILL_SWITCH_SIM=1` is only needed when an
interlock pin is configured and no GPIO library is present — with the rig as built the kill
switch claims no GPIO, so the commands above run on a laptop unmodified.

**Only `killswitch/` is testable off-Pi.** `chamber.py` and `bapp.py` open GPIO, SPI and cameras
at module scope; they cannot even be imported on a laptop. Don't try to run them locally to check
a change.

## Architecture

Two independent control programs, never run at the same time on one Pi, sharing one safety
package:

- **`chamber.py`** — direct `lgpio` + raw `spidev` MCP3008 reads (`xfer2`), an interactive command
  shell (`on`/`off`/`expose`/`run`/`snap`/`kill`/`interlock`), and a `SensorLogger` thread writing
  CSVs to `~/uv_chamber_data`. UV on GPIO18.
- **`bapp.py`** — `gpiozero` + Flask + `picamera2` MJPEG streams. Three PWM pumps (GPIO 4/27/22)
  driven through `PUMP_CALIBRATION`, a per-pump flow→duty table interpolated by
  `flow_to_percent()`. The whole UI is one HTML string returned by `index()`; its JS polls
  `/status` every 1.5 s. UV on GPIO17.
- `app.py` and `app.py.save` are byte-identical copies of an early prototype superseded by
  `bapp.py`. `2pumps` is an unrelated Arduino sketch. None of the three are live code.

Runtime artifacts — CSVs, photos, kill-switch logs, the latch file — all land in
`~/uv_chamber_data` (`KILL_SWITCH_DATA_DIR` overrides it), outside the repo.

`killswitch/docs/CODE.md` is a full walkthrough of all of the above; start there for anything
non-trivial.

## The hardware, and what the kill switch can actually do

The wiring is `killswitch/docs/circuit_image.svg` (a Cirkit Designer export; render it, the
annotations on it are the authority). The chain is:

```
12 V (+) ── LID SWITCH ──┬── LM2596 buck (12 V→6 V) ── UV emitters
                         └── pump (+)
each load's return: load (−) ── MOSFET drain; source ── GND; gate ← [220 Ω] ← Pi GPIO, [10 kΩ] ← GND
```

**The kill switch is a lid interlock, not an E-stop.** A switch held closed by the lid, in series
with the 12 V feed. Opening the lid cuts the rail mechanically. There is no sense contact, no
relay coil loop, and nothing Pi-driven in that line — so **software can neither see the lid open
nor cut the rail.** A watchdog trip commands every load's gate low through the shutdown hooks,
logs, latches and exits with code 42.

`config.py` reflects this with `INTERLOCK_SENSE_ENABLED = False` and `INTERLOCK_ARM_ENABLED =
False`. `KILL_SENSE_PIN = 23` / `KILL_ARM_PIN = 24` / `RAIL_FEEDBACK_PIN = 25` are reserved for
that wiring if it is ever added, and nothing claims them today. The listener-thread and rail-cut
code paths exist and stay covered by tests behind those flags — flipping one on is the whole
change needed.

Pin assignments in `chamber.py` and `bapp.py` are confirmed by the diagram's own annotations
(GP18 gate through 220 Ω with a 10 kΩ pulldown; MCP3008 on CLK GP11 / DOUT GP9 / DIN GP10 /
CS GP8 with CH0 from the GUVA). **Treat them as correct.** The UV pin differing between the two
programs is how they are, not a defect to fix.

### Host integration contract

```python
ks = KillSwitch(gpio_handle=_chip)          # or KillSwitch(GPIO_BACKEND="gpiozero")
ks.register_shutdown_hook(all_pumps_off, "pumps-off", priority=0)   # lower priority runs first
ks.register_shutdown_hook(uv_off,        "uv-off",    priority=1)
ks.register_shutdown_hook(stop_cameras,  "cameras",   priority=2)
ks.register_state_provider(chamber_state)   # snapshot recorded in the kill event
ks.register_activity_gate(lambda: ...)      # enforce the heartbeat only while a hazard is live
ks.arm()
```

Then `ks.heartbeat(source)` must be called from whichever loop is responsible for eventually
turning a hazard *off* — that's the loop whose stall is dangerous. It's why `bapp.py`'s
`run_pump_for()` sleeps in 0.5 s chunks instead of one long sleep.

`arm()` happens before anything can be energized, and failure to arm is fatal: both entry points
`sys.exit` rather than fall through to running unsupervised. Preserve that.

## Conventions that are load-bearing

These are safety invariants — breaking one degrades the system silently rather than failing a
test.

- **Fail-safe polarity.** Unreadable, ambiguous, or absent signal means kill. Never write a path
  where "no signal = fine". A watchdog check or activity gate that *raises* is itself a fault.
- **"Unwired" is not "healthy", and not "faulted".** `status()["sense_healthy"]` is `None` when no
  sense contact exists. Don't collapse that to a boolean — the UI and the shell both render it as
  its own state, because claiming the interlock is "OK" would be a claim we can't make.
- **Shutdown hooks must be idempotent** and safe to call when the thing is already off. On this
  rig they are the *entire* power-down, so treat them as the safety mechanism they are.
- **Every tunable lives in `killswitch/config.py`.** `core.py` hardcodes no pin, threshold,
  timeout, or path. Per-instance overrides go through `KillSwitch(**overrides)`, which raises
  `AttributeError` on an unknown key.
- **`os._exit`, not `sys.exit`, in the trip path.** It usually runs on a watchdog thread, where
  `SystemExit` would kill only that thread and leave the chamber running unsupervised.
- **Simulation is opt-in.** A missing GPIO library where a pin is actually needed is a hard error,
  never a silent no-op. `SimBackend` requires `KILL_SWITCH_SIM=1`.
- **`bapp.py` must keep `use_reloader=False`** — the reloader forks a second process holding a
  second kill switch. `/kill` is POST-only so no link prefetch or reload can fire it.

Disabled watchdog checks (`FLOW_CHECK_ENABLED`, `OVERCURRENT_CHECK_ENABLED`,
`OVERTEMP_CHECK_ENABLED`) are off for want of calibration and sensors, not because the code is
unfinished — those paths are written and tested. Enabling one means filling in limits and
flipping the flag.

## Docs

- `killswitch/docs/CODE.md` — walkthrough of every file, and how the software maps onto the circuit.
- `killswitch/docs/circuit_image.svg`, `killswitch/docs/killswitch-instructions.txt` — the hardware
  ground truth, in the team's own hands.
- `killswitch/DESIGN.md` — the design as built, commissioning checklist, and known gaps (the
  watchdog cannot cut power; the buzzer and status LEDs on the diagram are undriven; the diagram
  shows two pumps where `bapp.py` defines three).
- `killswitch/README.md` — the team's original spec. It predates the hardware settling on a lid
  interlock, so where it and DESIGN.md disagree, DESIGN.md is current.
