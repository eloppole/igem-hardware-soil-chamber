from flask import Flask, Response, redirect, url_for, jsonify
from gpiozero import LED, OutputDevice, MCP3008
from picamera2 import Picamera2
import io, time

app = Flask(__name__)

# ── PIN SETUP ─────────────────────────────────────────────────────
led  = LED(17)
pump = OutputDevice(4)

# ── UV SENSOR (MCP3008, hardware SPI) ─────────────────────────────
# Now on the Pi's hardware SPI0 bus, so NO pin arguments are needed --
# gpiozero uses the dedicated SPI peripheral automatically.
#   MCP CLK  (pin 13) -> SCLK  (GPIO11 / header pin 23)
#   MCP DOUT (pin 12) -> MISO  (GPIO9  / header pin 21)
#   MCP DIN  (pin 11) -> MOSI  (GPIO10 / header pin 19)
#   MCP CS   (pin 10) -> CE0   (GPIO8  / header pin 24)
# Requires SPI enabled:  sudo raspi-config -> Interface Options -> SPI -> reboot
# More sensors later = just more channels on the same bus: channel=1, 2, 3 ...
VREF = 3.3
uv = MCP3008(channel=0)          # CE0 by default; use device=1 only if on CE1

def read_uv():
    """Return (raw 0-1, volts, approx UV index)."""
    raw = uv.value
    volts = raw * VREF
    # GUVA-S12SD rough mapping: UV index ~= Vout / 0.1. Module-dependent -- calibrate.
    uv_index = volts / 0.1
    return raw, volts, uv_index

# ── CAMERA SETUP ──────────────────────────────────────────────────
cam = Picamera2()
cam.configure(cam.create_video_configuration(main={"size": (640, 480)}))
cam.start()
time.sleep(1)
print("Camera ready.")

# ── VIDEO STREAM ──────────────────────────────────────────────────
def gen():
    while True:
        buf = io.BytesIO()
        cam.capture_file(buf, format="jpeg")
        frame = buf.getvalue()
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

# ── ROUTES ────────────────────────────────────────────────────────
@app.route("/")
def index():
    led_state  = "ON" if led.is_lit else "OFF"
    pump_state = "ON" if pump.value else "OFF"
    page = f"""
    <html><head><title>Pi Control</title>
    <style>
        body {{ font-family: Arial; text-align: center; background: #1a1a1a; color: white; padding: 20px; }}
        img {{ border-radius: 10px; max-width: 100%; }}
        .btn {{ padding: 15px 30px; margin: 10px; font-size: 18px; border: none; border-radius: 8px;
                cursor: pointer; color: white; }}
        .on  {{ background: #22c55e; }}
        .off {{ background: #ef4444; }}
        .photo {{ background: #3b82f6; }}
        .status {{ font-size: 14px; color: #aaa; margin-top: 5px; }}
        .uv {{ font-size: 20px; color: #a78bfa; margin: 15px 0; }}
    </style></head><body>
        <h1>Pi Control Panel</h1>
        <img src="/feed"><br><br>
        <a href="/toggle/led"><button class="btn {'on' if led.is_lit else 'off'}">LED: {led_state}</button></a>
        <a href="/toggle/pump"><button class="btn {'on' if pump.value else 'off'}">Pump: {pump_state}</button></a>
        <a href="/photo"><button class="btn photo">Take Photo</button></a>
        <div class="uv" id="uv">UV sensor: reading...</div>
        <div class="status">Photos saved to Pi in current directory</div>
    """
    # Kept as a plain (non-f) string so the JS braces don't need escaping.
    script = """
        <script>
        async function pollUV() {
            try {
                const r = await fetch('/uv');
                const d = await r.json();
                document.getElementById('uv').textContent =
                    'UV sensor: ' + d.volts + ' V  (~UV index ' + d.uv_index + ')';
            } catch (e) {
                document.getElementById('uv').textContent = 'UV sensor: unavailable';
            }
        }
        setInterval(pollUV, 1500);
        pollUV();
        </script>
    </body></html>"""
    return page + script

@app.route("/feed")
def feed():
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/uv")
def uv_reading():
    raw, volts, uv_index = read_uv()
    return jsonify(raw=round(raw, 3), volts=round(volts, 3), uv_index=round(uv_index, 1))

@app.route("/toggle/led")
def toggle_led():
    led.toggle()
    return redirect(url_for("index"))

@app.route("/toggle/pump")
def toggle_pump():
    if pump.value:
        pump.off()
    else:
        pump.on()
    return redirect(url_for("index"))

@app.route("/photo")
def photo():
    ts = time.strftime("%Y%m%d_%H%M%S")
    cam.capture_file(f"photo_{ts}.jpg")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
