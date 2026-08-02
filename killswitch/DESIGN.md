# Kill switch — preliminary design

Response to the spec in [README.md](README.md). Diagram:
[docs/kill_switch_architecture.svg](docs/kill_switch_architecture.svg).

Status: preliminary. The software is complete and tested; the wiring below is a
proposal for review before anything is built.

---

## 1. The wiring this software assumes

### Parts (beyond what is already on the bench)

| Part | Note |
|---|---|
| Latching mushroom E-stop, **2× NC contacts** | Two contacts is the one non-negotiable part choice — see below |
| 2× 12 V relay or contactor, coil ≤ 12 V | One per rail, or one DPST switching both |
| 2× flyback diode (1N4007) | Across each coil |
| Logic-level N-MOSFET or opto-isolated driver | Pi-side interlock in the coil loop |
| 10 kΩ, 1 kΩ resistors | Sense pull-up, gate series |
| Optional: contactor auxiliary NO contact | Lets the Pi verify the rails really dropped |

### Two loops

**Coil loop (cuts the power).** A series chain — everything in it must be closed
before either rail can be live:

```
12 V coil supply ─ E-STOP NC-1 ─ K1+K2 coils ─ Q1 (gated by GPIO24) ─ GND
```

Pressing the button breaks it. The Pi driving GPIO24 low breaks it. Any wire in
it falling off breaks it. There is no state in which the rails are live and both
the button and the Pi are not actively permitting it.

**Sense loop (tells the Pi).** Isolated 3.3 V loop on the button's second contact:

```
3V3 ─ 10 kΩ ─┬─ GPIO23 (input, internal pull-up as well)
             └─ E-STOP NC-2 ─ GND
```

Loop closed → GPIO23 reads **0** = healthy. Button pressed, wire broken, or
connector unseated → the pull-up wins → **1** = kill. Signal loss and an active
press are the same reading, deliberately.

### Why the E-stop needs two contacts

The sense loop is 3.3 V logic; the coil loop is 12 V. Sharing one contact would
mean level-shifting a safety signal. A second dry contact on the same mechanism
costs nothing extra and keeps the two galvanically separate.

### Pins

`GPIO23` sense, `GPIO24` arm, `GPIO25` optional rail feedback. All three are free
today: `chamber.py` uses 18 + SPI (8, 9, 10, 11), `bapp.py` uses 17, 4, 27, 22.
They are set in [config.py](config.py), not inline.

---

## 2. One deliberate deviation from the spec

The spec asks that the watchdog *"fire the same GPIO kill line as the physical
button"*. Implemented as written, that cannot be made fail-safe:

- The E-stop line has to be an **input** with a pull-up, so that a broken wire
  reads as a trigger. Software cannot drive an input.
- If the Pi drove that line instead, the pull-up would have to go, and a
  detached connector would then read as *healthy*.

So the two triggers converge one step later, at the relay coil, rather than on
the sense wire: the button opens the coil loop mechanically, the watchdog opens
it through Q1. Same loop, same contactor, same result — both rails dead. The
functional requirement (one common cut path, no separate software-only shutdown)
holds; only the electrical node where they meet differs.

Say the word if you would rather have it literal and I will rework it.

---

## 3. Software

`killswitch/` is a package imported by the control script, not a separate program.

| File | Contents |
|---|---|
| `config.py` | Every pin, threshold, timeout and path |
| `core.py` | `KillSwitch`: listener thread, watchdog thread, shutdown sequence |
| `backends.py` | lgpio / gpiozero / simulation |
| `__main__.py` | `python3 -m killswitch --status \| --clear-latch \| --selftest` |
| `test_kill_switch.py` | 20 tests, no hardware needed |

### Shutdown sequence

1. **GPIO24 low** — rails cut first. Everything after this runs on dead rails.
2. **Snapshot state** — before the hooks change anything.
3. **Shutdown hooks** in priority order: pumps → UV → cameras/files. Each is
   idempotent and capped at 5 s, in its own thread; a wedged camera handle
   cannot stop the log from being written.
4. **Confirm rails dead** — if the aux contact is wired. A welded contactor logs
   `RAILS STILL LIVE` and tells the operator to pull the supplies.
5. **Write** `killswitch.log`, a JSON line to `killswitch_events.jsonl`, and the
   latch file — all fsynced.
6. **`os._exit(42)`** — not `sys.exit`, which from a watchdog thread would end
   only that thread and leave the chamber script running unsupervised.

### The latch

The rig runs overnight from cron, so "no auto-resume" has to survive a cron
restart. Every kill writes `~/uv_chamber_data/KILL_LATCH`; `arm()` refuses while
it exists. Clearing it is a separate deliberate command:

```bash
python3 -m killswitch --status
```

```bash
python3 -m killswitch --clear-latch
```

The clearance is itself logged, with the event it cleared. Exit code 42 lets
cron and systemd distinguish a kill from an ordinary crash.

A clean exit (`quit`, SIGTERM) also de-energizes the rails but writes no latch.

### The heartbeat and the activity gate

An unconditional heartbeat would trip the watchdog whenever someone stood at the
`chamber>` prompt for 15 s. So the heartbeat is only enforced while something
hazardous is actually energized, via `register_activity_gate()`:

- `chamber.py` — gate is `UV on or exposure running`; heartbeats come from the
  sensor logger, the exposure loop and the shell.
- `bapp.py` — gate is `any timed pump run in flight`; the heartbeat comes from
  the pump run loop itself, which is the code that must eventually stop the
  pump. Its `time.sleep(seconds)` was chunked to 0.5 s so it can tick.

The window restarts each time the gate opens, so switching the UV on never trips
instantly on a stale timestamp.

### Fail-safe behaviours

| Situation | Result |
|---|---|
| Sense line unreadable | `gpio_read_failure` → kill |
| Loop open at start-up | `arm()` raises, script refuses to run |
| Latch present | `arm()` raises, script refuses to run |
| Kill switch cannot arm for any reason | Script exits; no UV, no pumps |
| GPIO library missing | Hard error. Simulation needs `KILL_SWITCH_SIM=1` |
| Watchdog check raises | The exception is itself a fault → kill |
| Activity gate raises | Cannot prove nothing is energized → kill |

---

## 4. Integration

Both control scripts arm before anything can be energized, and refuse to start
if they cannot:

```python
ks = KillSwitch(gpio_handle=_chip)          # shares chamber.py's gpiochip handle
ks.register_shutdown_hook(uv_off,         "uv-off",       priority=0)
ks.register_shutdown_hook(_stop_logging,  "stop-logging", priority=1)
ks.register_shutdown_hook(_close_cameras, "close-cameras", priority=2)
ks.register_state_provider(_chamber_state)
ks.register_activity_gate(lambda: uv_state() == 1 or state["exposing"])
ks.arm()
```

- **`chamber.py`** — hooks for UV, sensor logging, cameras; new `kill` and
  `interlock` commands; `_cleanup()` now disarms.
- **`bapp.py`** — hooks for all three pumps, UV LED, camera recording; a
  `POST /kill` route behind a confirm dialog, with a red software E-stop button
  and an interlock indicator in the UI. POST so no link prefetch can fire it.

The spec names `uv_chamber.py`; the repo has `chamber.py` (UV, sensor, cameras)
and `bapp.py` (pumps, UV, cameras, web UI). Both were wired up, since the pumps
the watchdog cares about live only in `bapp.py`.

---

## 5. Commissioning

Do these in order, with **no liquid in the lines and the UV strip disconnected**.

1. **Loop continuity, unpowered.** Meter across the sense contact: closed with
   the button out, open with it in. Same for the coil contact.
2. **Sense polarity.** `python3 -m killswitch --status`, then `--selftest`.
   Press the button; the listener should trip within ~20 ms.
3. **Break test.** With the rig armed, unplug the sense connector. It must kill.
   *This is the test worth repeating* — it is the one people skip.
4. **Watchdog test.** `KILL_SWITCH_SIM=1` and a short `HEARTBEAT_TIMEOUT_S`,
   then stall the main loop.
5. **Latch test.** After any of the above, confirm the script refuses to start
   until the latch is cleared.
6. **Only then** connect the 12 V rails and repeat 2 and 3 with real loads,
   watching that both rails actually die.

---

## 6. Known gaps

- **A hung Pi holds GPIO24 high.** A frozen kernel keeps the interlock closed
  indefinitely. The hardware E-stop is the only cut that does not depend on the
  Pi being alive. The fix, if you want it: drive the interlock with a
  retriggerable monostable or charge pump fed by a *pulsed* GPIO24, so silence
  drops the rails. That is a hardware change; the software would need a pulse
  loop in place of the static hold.
- **Flow thresholds are placeholders.** `FLOW_CHECK_ENABLED = False`. The logic
  is written and tested; fill in `FLOW_LIMITS_ML_PER_S` after calibration and
  flip the flag. Nothing else changes.
- **No current or temperature sensors yet.** Same arrangement — code paths
  tested, checks disabled, limits already in config.
- **A manually toggled pump has no timeout.** `bapp.py`'s `/pump/toggle` runs a
  pump indefinitely with no supervision beyond the E-stop. Once flow sensing is
  live the watchdog covers it; until then, a max-runtime check via
  `register_watchdog_check()` would be the cheap interim fix.
- **Single-channel interlock.** A properly safety-rated build uses dual-channel
  monitoring and a safety relay. This is appropriate for a bench rig, not for
  anything a person can reach into while it is live.
- **`chamber.py` and `bapp.py` disagree on the UV pin** (18 vs 17). Unrelated to
  the kill switch, but worth resolving before they are ever run on the same Pi.

---

## 7. Testing

```bash
python3 -m killswitch.test_kill_switch
```

20 tests, run from the repo root, no Pi required. Covers arming and refusal,
E-stop trips, GPIO read failure, hook ordering, hung hooks, heartbeat and
activity gate, flow anomalies, overcurrent/overtemp, custom checks, event log
contents, and the latch cycle.
