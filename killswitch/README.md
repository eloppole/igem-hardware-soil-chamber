# Soil Chamber Kill Switch — Project Rundown

## Context
Raspberry Pi 5 controls a wildfire soil simulation chamber: UVB LED strip (fluorescence excitation) and 6x peristaltic pumps (via DRV8833 drivers), on two isolated 12V supplies, switched through MOSFET GPIO. System runs semi-unattended overnight (cron-based). Existing ADC: MCP3008. Cameras: Pi Camera Module 3 NoIR ×2 (not power-switched).

## Goal
Implement a kill switch system with three layers:

1. **Hardware E-stop (primary, non-negotiable)**: Latching NC E-stop button wired in series with relay coil(s) gating the two 12V rails. Pressing it, or any wire coming loose, de-energizes the relay and cuts power — independent of the Pi. This is physical wiring, not something Claude Code implements, but the software must assume it exists and behave accordingly.

2. **GPIO shutdown listener (software)**: Watches the same signal line as the E-stop. On trigger:
   - Stop all pump PWM output cleanly
   - Disable UV strip control pin
   - Close any open camera/file handles safely
   - Log event with timestamp, reason, and system state to file
   - Exit cleanly (no auto-restart)

3. **Software watchdog (software)**: Independently monitors for fault conditions and fires the *same* GPIO kill line as the physical button:
   - Pump flow rate anomaly (zero or out-of-range reading, once calibrated)
   - Sensor/main-loop heartbeat timeout
   - Overcurrent or over-temp reading (if/when sensors added)
   - Should be configurable (thresholds as constants/config, not hardcoded deep in logic)

## Requirements for implementation
- Python, target Raspberry Pi OS (64-bit), Pi 5
- Integrate into existing `uv_chamber.py` — don't create a fully separate control script; add a `kill_switch` module imported by it
- GPIO pin for kill signal should be a config constant, not hardcoded inline
- All shutdowns must be logged (timestamp, trigger source: button vs watchdog vs which watchdog condition)
- No auto-resume after a kill event — requires explicit manual restart of the script
- Fail-safe assumption: treat GPIO signal loss the same as an active trigger (don't assume "no signal = fine")
- Keep pump and UV shutdown functions idempotent (safe to call even if already off)

## Out of scope for this pass
- Actual relay/E-stop wiring (hardware, not code)
- Flow rate calibration values (pending empirical calibration)
- Temperature/overcurrent sensors (not yet installed)