from flask import Flask, Response, redirect, url_for
from gpiozero import LED, OutputDevice
from picamera2 import Picamera2
import io, time

app = Flask(__name__)

# ── PIN SETUP ─────────────────────────────────────────────────────
led  = LED(17)
pump = OutputDevice(4)

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
    return f"""
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
    </style></head><body>
        <h1>Pi Control Panel</h1>
        <img src="/feed"><br><br>
        <a href="/toggle/led"><button class="btn {'on' if led.is_lit else 'off'}">LED: {led_state}</button></a>
        <a href="/toggle/pump"><button class="btn {'on' if pump.value else 'off'}">Pump: {pump_state}</button></a>
        <a href="/photo"><button class="btn photo">Take Photo</button></a>
        <div class="status">Photos saved to Pi in current directory</div>
    </body></html>
    """

@app.route("/feed")
def feed():
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

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
