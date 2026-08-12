# Code write-up — iGEM soil chamber
Connor Ng

What every file does, how the pieces fit, and how the software lines up against
the circuit in [circuit_image.svg](circuit_image.svg).

Read this before changing anything that can energize a load.

---

## 1. The shape of the repo

There are **two control programs**, not one. They drive overlapping hardware
through different libraries and are not meant to run at the same time on the
same Pi.

| File | Role |
|---|---|
| `chamber.py` | UV + sensor + cameras, driven from an interactive `chamber>` shell over SSH. Uses `lgpio` and raw `spidev`. |
| `bapp.py` | Pumps + UV + camera streams, driven from a web UI on port 5000. Uses `gpiozero`, Flask and `picamera2`. |
| `killswitch/` | Safety package imported by both. Watchdog, event log, latch. |
| `app.py`, `app.py.save` | Byte-identical copies of an early prototype, superseded by `bapp.py`. Not live. |
| `2pumps` | An Arduino sketch toggling two relay pins. Unrelated to the Pi code. |

Everything the rig produces — CSVs, photos, kill-switch logs, the latch file —
lands in `~/uv_chamber_data`, outside the repo. `KILL_SWITCH_DATA_DIR` moves it.

**Neither control program can be imported off a Pi.** Both open GPIO, SPI and
cameras at module scope. Only `killswitch/` runs on a laptop.

---

## 2. `chamber.py`

A single-threaded command shell with a background logging thread.

**Setup, at import time.** Opens gpiochip 0, claims `UV_PIN = 18` as an output
held low, opens SPI0 at 1 MHz, then enumerates cameras through
`Picamera2.global_camera_info()`. A missing `lgpio` or `spidev` is a hard exit; a
missing `picamera2` only disables the camera commands.

**Sensor path.** `read_adc(channel)` speaks the MCP3008 protocol directly —
`spi.xfer2([1, (8 + channel) << 4, 0])`, then reassembles the 10-bit result from
the two returned bytes. `read_uv()` averages `SENSOR_SAMPLES = 16` of those,
converts to volts against `ADC_VREF = 3.3`, and divides by
`GUVA_V_PER_MW = 0.1` to get mW/cm². The 0.1 V per mW/cm² figure is the
approximate GUVA-S12SD response and is the weakest number in the file — it is a
datasheet typical, not a calibration.

**`SensorLogger`** is a `threading.Thread` that samples on a fixed schedule,
correcting drift by advancing an absolute `next_t` rather than sleeping a fixed
interval. It appends CSV rows (`timestamp,uv_on,adc,volts,mw_cm2,label`) and
flushes each one, so a kill mid-run loses nothing. It calls
`kill_switch.heartbeat("sensor-logger")` on every sample — that is what proves
the acquisition loop is alive.

**Commands.** `on`/`off`/`read`/`log`/`expose`/`snap`/`run`/`status`/`kill`/
`interlock`/`help`/`quit`, dispatched through the `COMMANDS` dict in `shell()`.
`cmd_expose` is the interesting one: it starts a 1 Hz logger, drives UV on,
counts down in ≤1 s slices while heart-beating, and turns UV off in a `finally`
so a `KeyboardInterrupt` still ends dark. `cmd_run` wraps it with before/after
photos from both cameras.

**Shutdown.** `_cleanup()` is registered with `atexit`. If the kill switch is
armed it calls `disarm()`, which runs the same hooks as a trip but writes no
latch; otherwise it stops logging, drops UV and closes cameras by hand. Then it
closes the gpiochip and SPI handles.

---

## 3. `bapp.py`

A threaded Flask app. All UI is one HTML string returned by `index()`; its
JavaScript polls `/status` every 1.5 s and repaints pills, timers and readings.

**Pumps.** `PUMP_PINS = {"pump1": 4, "pump2": 27, "pump3": 22}` become
`PWMOutputDevice`s. `PUMP_CALIBRATION` maps duty percent to measured mL/s per
pump, and `flow_to_percent()` inverts it by linear interpolation between the two
bracketing points — returning `None` below the pump's minimum achievable flow, so
the caller can reject a request rather than silently run at zero.

`/pump/run/<name>` clamps the duration to `MAX_RUN_SECS = 300` and spawns
`run_pump_for()` on a daemon thread. That function sleeps in ≤0.5 s slices
instead of one long sleep, because **it is the code responsible for switching the
pump back off** and therefore the code that must prove to the watchdog it is
still alive. Its `finally` turns the pump off and reports the state change.

**Cameras.** Two `Picamera2` instances record MJPEG into a `StreamOutput`, an
in-memory buffer guarded by a `threading.Condition`. `gen(index)` blocks on that
condition and yields multipart frames, so `/feed/<i>` serves each client the
newest frame without re-encoding.

**UV sensor.** `MCP3008(channel=0)` through gpiozero rather than raw SPI;
`read_uv()` returns `(raw, volts, uv_index)`.

**Startup.** `init_kill_switch()` runs before `app.run(...)`, and
`use_reloader=False` is deliberate: the reloader would fork a second process
holding a second kill switch.

---

## 4. The `killswitch` package

### `config.py`

Every pin, threshold, timeout and path. `core.py` hardcodes none of them.
Per-instance overrides go through `KillSwitch(**overrides)`, which raises
`AttributeError` on an unknown key — a typo cannot silently do nothing.

The two flags that describe the rig:

```python
INTERLOCK_SENSE_ENABLED = False   # the lid switch has no sense contact
INTERLOCK_ARM_ENABLED   = False   # nothing Pi-driven sits in the 12 V feed
```

Both are `False` for the hardware as drawn. `KILL_SENSE_PIN = 23`,
`KILL_ARM_PIN = 24` and `RAIL_FEEDBACK_PIN = 25` are reserved for that wiring if
it is ever added; nothing claims them today.

### `core.py`

`KillSwitch` owns the monitoring threads and the shutdown sequence.

**Registration.** `register_shutdown_hook(fn, name, priority)` — lower priority
first, conventionally pumps → UV → cameras. `register_state_provider(fn)` for the
snapshot recorded with each event. `register_watchdog_check(name, fn)` for extra
fault conditions: return `None` when healthy, a string to trip.
`register_activity_gate(fn)` gates the heartbeat.

**The activity gate** is what makes the heartbeat usable. Enforced
unconditionally, it would trip whenever an operator stood at the `chamber>`
prompt for 15 s. So the heartbeat is only checked while the gate says something
hazardous is energized — `UV on or exposing` in `chamber.py`, `any timed pump run
in flight` in `bapp.py` — and the window restarts each time the gate opens, so
switching UV on never trips instantly on a stale timestamp. A gate that *raises*
is treated as a fault: if we cannot prove nothing is energized, assume the worst.

**`arm()`** checks the latch first and unconditionally, then opens a GPIO backend
**only if some interlock pin is actually wired** (`_needs_gpio()`). On this rig
that means the kill switch claims no GPIO at all — it neither competes with the
host for the chip nor needs a GPIO library. It then starts the watchdog thread,
and the listener thread only when a sense wire exists.

**`_watchdog_loop`** runs heartbeat, flow, overcurrent and overtemp checks plus
any custom ones. A check that raises is itself a fault.

**`trip()`** is idempotent and safe from any thread; the first caller wins.
`_execute_shutdown()` then:

1. opens the Pi-side interlock, where one exists — otherwise logs plainly that it
   cannot and that the hooks are the whole power-down;
2. snapshots state, before the hooks change anything;
3. runs the hooks in priority order, each in its own thread with a 5 s cap, so a
   wedged camera handle cannot stop the log being written;
4. confirms the rail dropped, if a feedback contact is wired;
5. writes `killswitch.log`, a JSON line to `killswitch_events.jsonl`, and the
   latch file — all `fsync`ed;
6. calls `os._exit(42)` — **not `sys.exit`**, which from a watchdog thread would
   end only that thread and leave the chamber running unsupervised.

**The latch.** `~/uv_chamber_data/KILL_LATCH` blocks the next `arm()`. The rig is
meant to run overnight from cron, so "no auto-resume" has to survive a cron
restart. Clearing it is a separate deliberate act and is itself logged, with the
event it cleared. Exit code 42 lets cron or systemd tell a kill from a crash.

**`status()`** returns `sense_healthy: None` when no sense wire exists. `None`
means *not observable*, which is not the same as *open* — both hosts render those
as different states, and neither claims the interlock is "OK".

### `backends.py`

`claim_input` / `claim_output` / `read` / `write` over three implementations.
`LgpioBackend` can share an already-open chip handle, which is how `chamber.py`
avoids a second `gpiochip` handle on the same chip. `GpiozeroBackend` exists for
`bapp.py`; note its `read()` inverts the value when `pull_up=True`, because
gpiozero reports *active*, not level. `SimBackend` keeps pins in a dict and can
be told to fail reads, for the tests.

`open_backend()` will not fall back to simulation silently: a missing GPIO
library on the Pi is a hard error, and `SimBackend` requires `KILL_SWITCH_SIM=1`.

### `__main__.py` and `test_kill_switch.py`

```bash
python3 -m killswitch --status        # config, interlock wiring, latch state
python3 -m killswitch --clear-latch   # deliberate, logged
python3 -m killswitch --selftest      # arm against real hardware, report, disarm
python3 -m killswitch.test_kill_switch                                        # 24 tests
python3 -m unittest killswitch.test_kill_switch.KillSwitchTestCase.<name> -v  # one test
```

`KillSwitchTestCase` forces both `INTERLOCK_*` flags on, so the sense-line and
arm-line paths stay covered for the day that wiring exists.
`AsBuiltRigTestCase` covers today's rig: arms with no GPIO, distinguishes
unwired from unhealthy, still trips on the watchdog, still latches.

---

## 5. Software against the circuit

The drawing is a Cirkit Designer export. Reading the annotations on it:

**Confirmed by the diagram, matching the code exactly.** MCP3008 pin 16 VDD →
3V3, 15 VREF → 3V3, 14 AGND → GND, 13 CLK → GP11, 12 DOUT → GP9, 11 DIN → GP10,
10 CS → GP8, 9 DGND → GND, 1 CH0 → GUVA OUT, with a 100 nF cap across pins 16 and
14. And for the UV switch: Gate ← [220 Ω] ← GP18, Gate ← [10 kΩ] ← GND, Drain →
UV strip negative, Source → GND. That is `chamber.py`'s `UV_PIN = 18` and its SPI
wiring, unchanged since before the kill-switch work.

**The power chain.** 12 V (+) → lid switch → LM2596 buck (set to 6 V) → the
Violumas UV emitters, two per buck, four in total across two bucks. Each load
returns through its own MOSFET: drain to the load's negative, source to common
GND, gate driven from a Pi pin through 220 Ω with a 10 kΩ pulldown. The pumps
repeat the same pattern with their positive on the 12 V rail.

**What has no software behind it.** The diagram includes a piezo buzzer, a red
LED and a green LED — an annunciator that nothing drives — plus a second Adafruit
UV sensor breakout (only `CH0` is read) and a USB-RS485 adapter. The drawing shows
two pumps; `bapp.py` defines three.

**Notes left on the drawing.** "note: should be N-type" sits beside the MOSFETs:
the placed part is an IRF4905, which is P-channel, standing in for the intended
logic-level N-channel part. Low-side switching is what the wiring assumes.
"Not sure if UV sensors still being used" sits beside the sensor breakouts. The
MCP3008 exists at all because the Pi has no built-in ADC, unlike the ESP32 the
project used before.

**What the software cannot do on this rig.** There is no sense wire and no
Pi-driven device in the 12 V feed, so software cannot see the lid open and cannot
cut the rail. A watchdog trip commands every load's gate low, logs, latches and
exits. Opening the lid is the only real power cut. See
[DESIGN.md](../DESIGN.md) for the consequences and the open hardware questions.
