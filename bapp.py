from flask import Flask, Response, redirect, url_for, jsonify, request
from gpiozero import OutputDevice, PWMOutputDevice, MCP3008
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
import io, time, threading

app = Flask(__name__)

# ── PIN SETUP ─────────────────────────────────────────────────────
uv_led = OutputDevice(17)

PUMP_PINS   = {"pump1": 4, "pump2": 27, "pump3": 22}
PUMP_LABELS = {"pump1": "H₂O₂", "pump2": "NaCl", "pump3": "H₂O₂ (2)"}
pumps = {name: PWMOutputDevice(pin) for name, pin in PUMP_PINS.items()}

# ── CALIBRATION ───────────────────────────────────────────────────
PUMP_CALIBRATION = {
    "pump1": [
        (0, 0.0), (10, 0.0), (20, 0.1333), (30, 0.4333), (40, 0.5167),
        (50, 0.5833), (60, 0.6417), (70, 0.6667), (80, 0.7167),
        (90, 0.7333), (100, 0.75),
    ],
    "pump2": [
        (0, 0.0), (10, 0.0), (20, 0.1667), (30, 0.6333), (40, 0.8),
        (50, 0.9), (60, 0.9667), (70, 1.0), (80, 1.0333),
        (90, 1.0833), (100, 1.1333),
    ],
    "pump3": [
        (0, 0.0), (10, 0.0), (20, 0.1667), (30, 0.6333), (40, 0.8),
        (50, 0.9), (60, 0.9667), (70, 1.0), (80, 1.0333),
        (90, 1.0833), (100, 1.1333),
    ],
}

def pump_range(name):
    flows = [f for _, f in PUMP_CALIBRATION[name]]
    return min(f for f in flows if f > 0), max(flows)

def flow_to_percent(name, target_flow):
    table = PUMP_CALIBRATION[name]
    min_flow, max_flow = pump_range(name)
    if target_flow <= 0:
        return 0.0
    if target_flow >= max_flow:
        return 100.0
    if target_flow < min_flow:
        return None
    for i in range(1, len(table)):
        p0, f0 = table[i - 1]
        p1, f1 = table[i]
        if f1 > f0 and f0 <= target_flow <= f1:
            frac = (target_flow - f0) / (f1 - f0)
            return p0 + frac * (p1 - p0)
    return 100.0

# ── TIMED PUMP RUN ────────────────────────────────────────────────
MAX_RUN_SECS  = 300
pump_running  = {name: False for name in pumps}
pump_end_time = {name: 0.0   for name in pumps}

def run_pump_for(name, seconds, duty):
    try:
        pumps[name].value = duty
        time.sleep(seconds)
    finally:
        pumps[name].off()
        pump_running[name]  = False
        pump_end_time[name] = 0.0

# ── UV SENSOR ─────────────────────────────────────────────────────
VREF = 3.3
uv_sensor = MCP3008(channel=0)

def read_uv():
    raw   = uv_sensor.value
    volts = raw * VREF
    return round(raw, 3), round(volts, 3), round(volts / 0.1, 1)

# ── CAMERA SETUP ──────────────────────────────────────────────────
class StreamOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame     = None
        self.condition = threading.Condition()
    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

cameras    = []
outputs    = []
cam_running  = []
cam_end_time = []

for i in range(2):
    try:
        cam = Picamera2(i)
        cam.configure(cam.create_video_configuration(main={"size": (640, 480)}))
        out = StreamOutput()
        cam.start_recording(MJPEGEncoder(), FileOutput(out))
        cameras.append(cam)
        outputs.append(out)
        cam_running.append(True)
        cam_end_time.append(0.0)
        print(f"Camera {i} ready.")
    except Exception as e:
        print(f"Camera {i} unavailable: {e}")
        break

def stop_camera_after(index, seconds):
    time.sleep(seconds)
    try:
        cameras[index].stop_recording()
        cam_running[index]  = False
        cam_end_time[index] = 0.0
    except Exception:
        pass

def gen(index):
    out = outputs[index]
    while True:
        with out.condition:
            out.condition.wait()
            frame = out.frame
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

# ── ROUTES ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>iGEM Chamber Controller</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 24px; }
h1 { font-size: 20px; font-weight: 500; color: #f8fafc; margin-bottom: 4px; }
.subtitle { font-size: 13px; color: #64748b; margin-bottom: 24px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.full { grid-column: span 2; }
.card { background: #1e2130; border: 0.5px solid #2d3148; border-radius: 12px; padding: 20px; }
.card-title { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em; color: #64748b; margin-bottom: 16px; }
.cameras-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.cam-feed { width: 100%; aspect-ratio: 4/3; object-fit: cover; border-radius: 8px; background: #0f1117; display: block; }
.cam-off  { width: 100%; aspect-ratio: 4/3; background: #0f1117; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: #334155; font-size: 13px; border: 0.5px solid #1e293b; }
.cam-label { font-size: 12px; color: #64748b; margin-top: 6px; }
.divider { border: none; border-top: 0.5px solid #2d3148; margin: 16px 0; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
.row:last-child { margin-bottom: 0; }
.btn { padding: 9px 16px; border-radius: 8px; border: 0.5px solid #2d3148; background: #252838; color: #e2e8f0; font-size: 13px; cursor: pointer; text-decoration: none; white-space: nowrap; display: inline-block; }
.btn:hover { background: #2d3148; }
.btn-blue { background: #1e3a5f; border-color: #1d4ed8; color: #93c5fd; }
.pill { font-size: 11px; font-weight: 500; padding: 2px 9px; border-radius: 20px; }
.pill-on   { background: #14532d; color: #86efac; }
.pill-off  { background: #1c1917; color: #78716c; }
.pill-busy { background: #431407; color: #fb923c; }
input[type=number] { padding: 8px 10px; border-radius: 8px; border: 0.5px solid #2d3148; background: #0f1117; color: #e2e8f0; font-size: 13px; width: 110px; }
input[type=number]::placeholder { color: #475569; }
.pump-name { font-size: 14px; font-weight: 500; min-width: 80px; color: #cbd5e1; }
.range-hint { font-size: 11px; color: #475569; }
.timer { font-size: 11px; color: #fb923c; }
.uv-big  { font-size: 36px; font-weight: 500; color: #a78bfa; line-height: 1; }
.uv-unit { font-size: 13px; color: #64748b; margin-top: 4px; margin-bottom: 12px; }
.uv-meta { display: flex; gap: 20px; }
.uv-meta-item { font-size: 12px; color: #64748b; }
.uv-meta-item span { color: #94a3b8; }
.pump-row { padding: 12px 0; border-bottom: 0.5px solid #2d3148; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.pump-row:last-child { border-bottom: none; padding-bottom: 0; }
</style>
</head>
<body>
<h1>iGEM Chamber Controller</h1>
<p class="subtitle">Raspberry Pi 5 — UV Soil Simulation System</p>
<div class="grid">

  <!-- Cameras -->
  <div class="card full">
    <div class="card-title">Live feed</div>
    <div class="cameras-grid">
      <div>
        <img src="/feed/0" class="cam-feed" id="cam0-img" onerror="this.style.display='none';document.getElementById('cam0-off').style.display='flex'">
        <div class="cam-off" id="cam0-off" style="display:none">camera off</div>
        <div class="cam-label">Side View, Camera 0 — primary &nbsp;<span class="timer" id="cam0-timer"></span></div>
      </div>
      <div>
        <img src="/feed/1" class="cam-feed" id="cam1-img" onerror="this.style.display='none';document.getElementById('cam1-off').style.display='flex'">
        <div class="cam-off" id="cam1-off" style="display:none">camera off</div>
        <div class="cam-label">Top view, Camera 1 — secondary &nbsp;<span class="timer" id="cam1-timer"></span></div>
      </div>
    </div>
    <hr class="divider">
    <div class="row">
      <button class="btn btn-blue" onclick="fetch('/snap/0')">Capture cam 0</button>
      <button class="btn btn-blue" onclick="fetch('/snap/1')">Capture cam 1</button>
      <button class="btn btn-blue" onclick="fetch('/snap/both')">Capture both</button>
    </div>
    <div class="row" style="margin-top:10px;">
      <button class="btn" id="cam0-toggle" onclick="fetch('/cam/toggle/0').then(poll)">Cam 0 on/off</button>
      <input type="number" id="cam0-secs" placeholder="seconds" min="1" step="1" style="width:100px">
      <button class="btn btn-blue" onclick="timedCam(0)">Timed run</button>
      <span class="pill" id="cam0-pill">ON</span>
      &nbsp;&nbsp;
      <button class="btn" id="cam1-toggle" onclick="fetch('/cam/toggle/1').then(poll)">Cam 1 on/off</button>
      <input type="number" id="cam1-secs" placeholder="seconds" min="1" step="1" style="width:100px">
      <button class="btn btn-blue" onclick="timedCam(1)">Timed run</button>
      <span class="pill" id="cam1-pill">ON</span>
    </div>
  </div>

  <!-- UV LED -->
  <div class="card">
    <div class="card-title">UV LED strip</div>
    <div class="row">
      <a href="/toggle/led" class="btn">Toggle UV strip</a>
      <span class="pill pill-off" id="led-pill">OFF</span>
    </div>
  </div>

  <!-- UV Sensor -->
  <div class="card">
    <div class="card-title">UV sensor (GUVA)</div>
    <div class="uv-big" id="uv-idx">—</div>
    <div class="uv-unit">UV index</div>
    <div class="uv-meta">
      <div class="uv-meta-item">Voltage &nbsp;<span id="uv-v">—</span> V</div>
      <div class="uv-meta-item">Raw &nbsp;<span id="uv-raw">—</span></div>
    </div>
  </div>

  <!-- Pumps -->
  <div class="card full">
    <div class="card-title">Pumps</div>
    <div class="pump-row">
      <span class="pump-name">H&#x2082;O&#x2082;</span>
      <a href="/pump/toggle/pump1" class="btn">Toggle</a>
      <span class="pill pill-off" id="pump1-pill">OFF</span>
      <span class="timer" id="pump1-timer"></span>
      <span style="flex:1"></span>
      <input type="number" id="pump1-flow" placeholder="mL/sec" min="0" step="0.01">
      <input type="number" id="pump1-secs" placeholder="seconds" min="0.1" step="0.1">
      <button class="btn btn-blue" onclick="runPump('pump1')">Run</button>
      <span class="range-hint">0.13–0.75 mL/s</span>
    </div>
    <div class="pump-row">
      <span class="pump-name">NaCl</span>
      <a href="/pump/toggle/pump2" class="btn">Toggle</a>
      <span class="pill pill-off" id="pump2-pill">OFF</span>
      <span class="timer" id="pump2-timer"></span>
      <span style="flex:1"></span>
      <input type="number" id="pump2-flow" placeholder="mL/sec" min="0" step="0.01">
      <input type="number" id="pump2-secs" placeholder="seconds" min="0.1" step="0.1">
      <button class="btn btn-blue" onclick="runPump('pump2')">Run</button>
      <span class="range-hint">0.17–1.13 mL/s</span>
    </div>
    <div class="pump-row">
      <span class="pump-name">NaCl (2)</span>
      <a href="/pump/toggle/pump3" class="btn">Toggle</a>
      <span class="pill pill-off" id="pump3-pill">OFF</span>
      <span class="timer" id="pump3-timer"></span>
      <span style="flex:1"></span>
      <input type="number" id="pump3-flow" placeholder="mL/sec" min="0" step="0.01">
      <input type="number" id="pump3-secs" placeholder="seconds" min="0.1" step="0.1">
      <button class="btn btn-blue" onclick="runPump('pump3')">Run</button>
      <span class="range-hint">0.17–1.13 mL/s</span>
    </div>
  </div>

</div>
<script>
function setPill(id, on, busy) {
  const el = document.getElementById(id);
  if (!el) return;
  if (busy)      { el.textContent = 'RUNNING'; el.className = 'pill pill-busy'; }
  else if (on)   { el.textContent = 'ON';      el.className = 'pill pill-on';   }
  else           { el.textContent = 'OFF';     el.className = 'pill pill-off';  }
}
function setTimer(id, secs) {
  const el = document.getElementById(id);
  if (el) el.textContent = secs > 0 ? secs + 's left' : '';
}
function runPump(name) {
  const flow = document.getElementById(name + '-flow').value;
  const secs = document.getElementById(name + '-secs').value;
  if (!flow || !secs) return;
  fetch(`/pump/run/${name}?flow=${flow}&seconds=${secs}`);
}
function timedCam(i) {
  const secs = document.getElementById('cam' + i + '-secs').value;
  if (!secs) return;
  fetch(`/cam/run/${i}?seconds=${secs}`).then(poll);
}
async function poll() {
  try {
    const d = await fetch('/status').then(r => r.json());
    setPill('led-pill',   d.led,   false);
    ['pump1','pump2','pump3'].forEach(n => {
      setPill(n + '-pill', d[n] > 0, d[n + '_running']);
      setTimer(n + '-timer', d[n + '_remaining']);
    });
    document.getElementById('uv-idx').textContent = d.uv_index;
    document.getElementById('uv-v').textContent   = d.volts;
    document.getElementById('uv-raw').textContent = d.raw;
    for (let i = 0; i < 2; i++) {
      setPill('cam' + i + '-pill', d['cam' + i], false);
      setTimer('cam' + i + '-timer', d['cam' + i + '_remaining']);
    }
  } catch(e) {}
}
setInterval(poll, 1500);
poll();
</script>
</body>
</html>"""

@app.route("/feed/<int:index>")
def feed(index):
    if index >= len(outputs):
        return "Camera not available", 404
    return Response(gen(index), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/status")
def status():
    raw, volts, uv_index = read_uv()
    now  = time.time()
    data = dict(led=int(uv_led.value), raw=raw, volts=volts, uv_index=uv_index)
    for name in pumps:
        data[name]                = round(pumps[name].value, 3)
        data[name + "_running"]   = pump_running[name]
        data[name + "_remaining"] = round(max(0.0, pump_end_time[name] - now), 1) if pump_running[name] else 0.0
    for i in range(len(cameras)):
        data[f"cam{i}"]             = cam_running[i]
        data[f"cam{i}_remaining"]   = round(max(0.0, cam_end_time[i] - now), 1) if cam_end_time[i] > now else 0.0
    return jsonify(data)

@app.route("/toggle/led")
def toggle_led():
    if uv_led.value: uv_led.off()
    else: uv_led.on()
    return redirect(url_for("index"))

@app.route("/pump/toggle/<name>")
def toggle_pump(name):
    p = pumps.get(name)
    if p:
        if p.value: p.off()
        else: p.on()
    return redirect(url_for("index"))

@app.route("/pump/run/<name>")
def pump_run(name):
    if name not in pumps:
        return ("", 404)
    try: secs = float(request.args.get("seconds", 0))
    except: secs = 0
    try: flow = float(request.args.get("flow", 0))
    except: flow = 0
    secs = max(0.0, min(secs, MAX_RUN_SECS))
    pct  = flow_to_percent(name, flow)
    if secs > 0 and pct and pct > 0 and not pump_running[name]:
        pump_running[name]  = True
        pump_end_time[name] = time.time() + secs
        threading.Thread(target=run_pump_for, args=(name, secs, pct / 100.0), daemon=True).start()
    return ("", 204)

@app.route("/cam/toggle/<int:index>")
def cam_toggle(index):
    if index >= len(cameras):
        return ("", 404)
    if cam_running[index]:
        cameras[index].stop_recording()
        cam_running[index]  = False
        cam_end_time[index] = 0.0
    else:
        cameras[index].start_recording(MJPEGEncoder(), FileOutput(outputs[index]))
        cam_running[index] = True
    return ("", 204)

@app.route("/cam/run/<int:index>")
def cam_run(index):
    if index >= len(cameras):
        return ("", 404)
    try: secs = float(request.args.get("seconds", 0))
    except: secs = 0
    secs = max(0.0, min(secs, 3600))
    if secs > 0:
        if not cam_running[index]:
            cameras[index].start_recording(MJPEGEncoder(), FileOutput(outputs[index]))
            cam_running[index] = True
        cam_end_time[index] = time.time() + secs
        threading.Thread(target=stop_camera_after, args=(index, secs), daemon=True).start()
    return ("", 204)

@app.route("/snap/<target>")
def snap(target):
    ts = time.strftime("%Y%m%d_%H%M%S")
    if target == "both":
        for i, cam in enumerate(cameras):
            if cam_running[i]:
                cam.capture_file(f"photo_cam{i}_{ts}.jpg")
    else:
        i = int(target)
        if i < len(cameras) and cam_running[i]:
            cameras[i].capture_file(f"photo_cam{i}_{ts}.jpg")
    return ("", 204)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
