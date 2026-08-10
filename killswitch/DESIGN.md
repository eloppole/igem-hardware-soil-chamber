# Kill switch — design as built

Response to the spec in [README.md](README.md), rebased on the hardware actually
being built. Sources of truth: [docs/circuit_image.svg](docs/circuit_image.svg)
and [docs/killswitch-instructions.txt](docs/killswitch-instructions.txt).
Code walkthrough: [docs/CODE.md](docs/CODE.md).

Status: the software is complete and tested against this design. The wiring is
drawn but the commissioning checklist in §5 has not been signed off.

> **An earlier revision of this document proposed a latching mushroom E-stop
> driving two 12 V contactors, with a sense loop on GPIO23 and a Pi-side
> interlock on GPIO24. That is not what is being built and this document no
> longer describes it.** The rig uses a lid interlock switch. `config.py` still
> carries those pin numbers, unclaimed, behind `INTERLOCK_SENSE_ENABLED` and
> `INTERLOCK_ARM_ENABLED`, both `False`.

---

## 1. What the kill switch actually is

A **lid interlock**, not an emergency stop. A keyboard-style switch is held
closed by the lid; raising the lid opens it. It sits in series with the 12 V
positive feed:

```
12 V (+) ── LID SWITCH ──┬── LM2596 buck (12 V→6 V) ── UV emitters
                         └── pump (+)

each load's return:   load (−) ── MOSFET drain
                      MOSFET source ── common GND
                      MOSFET gate ── [220 Ω] ── Pi GPIO,  [10 kΩ] ── GND
```

Four Violumas UV emitters run two per buck, across two bucks. Each load — the
bucks and each pump — has its own low-side MOSFET, so the Pi can switch loads
individually while the lid switch cuts everything at once.

Opening the lid de-energizes the 12 V rail mechanically, with the Pi
uninvolved. **The Pi cannot see it happen, and cannot cause it.** There is no
sense contact, no relay coil loop, and no Pi-driven device anywhere in that line.

---

## 2. What that leaves the software able to do

The spec asked for three layers. Against this wiring they come out as:

| Layer | Spec | As built |
|---|---|---|
| 1. Hardware cut | Latching E-stop in a contactor coil loop | **Lid switch in the 12 V feed.** Works with the Pi off. |
| 2. GPIO listener | Watch the same signal line | **Not present.** No sense contact to watch, so no listener thread runs. |
| 3. Software watchdog | Fire the same kill line | **Degraded.** It cannot open the 12 V feed. It commands every load's gate low through the shutdown hooks, logs, latches and exits. |

This is a real reduction in coverage and worth stating plainly: a fault that the
watchdog catches ends with the loads switched off but the rail still live. Only
the lid cuts the rail. The three things the software still guarantees in full
are the **event log**, the **latch** (no auto-resume across a cron restart), and
**idempotent, time-boxed shutdown hooks**.

Both flags are one edit away from restoring layers 2 and 3 in full — wire a dry
contact to GPIO23 and/or an opto in the 12 V feed to GPIO24, flip
`INTERLOCK_SENSE_ENABLED` / `INTERLOCK_ARM_ENABLED`, and the listener, the
startup interlock check and the rail cut all come back. Those paths are already
written and tested (`KillSwitchTestCase` runs with both flags on).

---

## 3. Shutdown sequence

1. **Open the Pi-side interlock**, if one exists. On this rig it does not, so
   this step only records that fact.
2. **Snapshot state** — before the hooks change anything.
3. **Shutdown hooks** in priority order: pumps → UV → cameras/files. Each is
   idempotent and capped at 5 s in its own thread, so a wedged camera handle
   cannot stop the log from being written. **On this rig these hooks are the
   entire power-down.**
4. **Confirm the rail dropped** — only if a feedback contact is wired.
5. **Write** `killswitch.log`, a JSON line to `killswitch_events.jsonl`, and the
   latch file — all fsynced.
6. **`os._exit(42)`** — not `sys.exit`, which from a watchdog thread would end
   only that thread and leave the chamber script running unsupervised.

### The latch

The rig runs overnight from cron, so "no auto-resume" has to survive a cron
restart. Every kill writes `~/uv_chamber_data/KILL_LATCH`; `arm()` refuses while
it exists, whether or not any interlock is wired. Clearing it is deliberate:

```bash
python3 -m killswitch --status
python3 -m killswitch --clear-latch
```

The clearance is itself logged, with the event it cleared. Exit code 42 lets
cron and systemd distinguish a kill from an ordinary crash. A clean exit
(`quit`, SIGTERM) runs the same hooks but writes no latch.

### The heartbeat and the activity gate

An unconditional heartbeat would trip whenever someone stood at the `chamber>`
prompt for 15 s, so it is only enforced while something hazardous is energized:

- `chamber.py` — gate is `UV on or exposure running`; heartbeats come from the
  sensor logger, the exposure loop and the shell.
- `bapp.py` — gate is `any timed pump run in flight`; the heartbeat comes from
  the pump run loop itself, which is the code that must eventually stop the
  pump. Its `time.sleep(seconds)` is chunked to 0.5 s so it can tick.

The window restarts each time the gate opens, so switching UV on never trips
instantly on a stale timestamp.

### Fail-safe behaviours

| Situation | Result |
|---|---|
| Latch present | `arm()` raises, script refuses to run |
| Watchdog check raises | The exception is itself a fault → kill |
| Activity gate raises | Cannot prove nothing is energized → kill |
| GPIO library missing, when interlock pins are in use | Hard error. Simulation needs `KILL_SWITCH_SIM=1` |
| Sense line unreadable *(only when `INTERLOCK_SENSE_ENABLED`)* | `gpio_read_failure` → kill |
| Loop open at start-up *(only when `INTERLOCK_SENSE_ENABLED`)* | `arm()` raises, script refuses to run |

Note what is **not** in this table: nothing detects the lid opening. From the
software's point of view the loads simply stop responding.

---

## 4. Integration

Both control scripts arm before anything can be energized, and exit rather than
run unsupervised if they cannot:

```python
ks = KillSwitch(gpio_handle=_chip)          # shares chamber.py's gpiochip handle
ks.register_shutdown_hook(uv_off,         "uv-off",       priority=0)
ks.register_shutdown_hook(_stop_logging,  "stop-logging", priority=1)
ks.register_shutdown_hook(_close_cameras, "close-cameras", priority=2)
ks.register_state_provider(_chamber_state)
ks.register_activity_gate(lambda: uv_state() == 1 or state["exposing"])
ks.arm()
```

- **`chamber.py`** — hooks for UV, sensor logging, cameras; `kill` and
  `interlock` commands; `_cleanup()` disarms.
- **`bapp.py`** — hooks for all three pumps, UV LED and camera recording; a
  `POST /kill` route behind a confirm dialog, with a software stop button and a
  status pill. POST so no link prefetch can fire it.

The spec names `uv_chamber.py`; the repo has `chamber.py` (UV, sensor, cameras)
and `bapp.py` (pumps, UV, cameras, web UI). Both were wired up, since the pumps
the watchdog cares about live only in `bapp.py`.

---

## 5. Commissioning

Do these in order, with **no liquid in the lines and the UV emitters
disconnected**.

1. **Lid switch continuity, unpowered.** Meter across the switch: closed with
   the lid shut, open with it raised. Check it opens early enough in the lid's
   travel that the gap is still too small to look into.
2. **Capacitor holdup.** The open question from the instructions file. With the
   bucks powered and the emitters connected, open the lid and time how long they
   stay lit. If it is perceptible, move the buck ahead of the switch and add a
   regulator, as planned.
3. **Software stop.** `python3 -m killswitch --status`, then `--selftest`. With
   no sense contact this reports the watchdog and latch only — there is no lid
   event for the software to catch.
4. **Watchdog test.** Short `HEARTBEAT_TIMEOUT_S`, then stall the main loop, and
   confirm every load goes off and exit code 42 is returned.
5. **Latch test.** After the above, confirm the script refuses to start until
   the latch is cleared.
6. **Only then** connect the 12 V rail and repeat 2 and 4 with real loads.

---

## 6. Known gaps

- **The watchdog cannot cut power.** §2. The cheapest fix that restores it is an
  opto or logic-level MOSFET in the 12 V feed on GPIO24, plus
  `INTERLOCK_ARM_ENABLED = True`; the code path already exists and is tested.
- **Software cannot see the lid.** A dry contact from the lid switch to GPIO23
  with a pull-up, plus `INTERLOCK_SENSE_ENABLED = True`, would log lid openings
  and stop the software mid-run instead of letting it carry on commanding a dead
  rail. Also already written and tested.
- **A hung Pi holds every gate wherever it was.** A frozen kernel cannot drop a
  gate. The lid is the only cut that does not depend on the Pi being alive.
- **Buck capacitor holdup on the emitters.** Recorded in the instructions file;
  see commissioning step 2.
- **The drawn MOSFETs are P-channel.** The diagram places IRF4905s but is
  annotated "should be N-type". The wiring assumes low-side N-channel switching;
  build it that way.
- **The annunciator is not driven.** The diagram has a piezo buzzer and red and
  green LEDs. Nothing in the software touches them. They would be the obvious
  way to signal a watchdog trip on a rig where software cannot cut power.
- **Flow thresholds are placeholders.** `FLOW_CHECK_ENABLED = False`. The logic
  is written and tested; fill in `FLOW_LIMITS_ML_PER_S` after calibration and
  flip the flag.
- **No current or temperature sensors yet.** Same arrangement — code paths
  tested, checks disabled, limits already in config.
- **A manually toggled pump has no timeout.** `bapp.py`'s `/pump/toggle` runs a
  pump indefinitely with no supervision. Until flow sensing is live, a
  max-runtime check via `register_watchdog_check()` is the cheap interim fix.
- **Single-channel interlock.** A properly safety-rated build uses dual-channel
  monitoring and a safety relay. This is appropriate for a bench rig, not for
  anything a person can reach into while it is live.
- **The diagram shows two pumps; `bapp.py` defines three.** Worth reconciling
  before calibration.

---

## 7. Testing

```bash
python3 -m killswitch.test_kill_switch
```

24 tests, run from the repo root, no Pi required. `KillSwitchTestCase` (20)
covers the fully wired interlock — arming and refusal, loop trips, GPIO read
failure, hook ordering, hung hooks, heartbeat and activity gate, flow anomalies,
overcurrent/overtemp, custom checks, event log contents, latch cycle.
`AsBuiltRigTestCase` (4) covers this rig: arming with no GPIO at all, telling
"unwired" apart from "unhealthy", the watchdog still tripping and running hooks
in order, and the latch still blocking the next start.
