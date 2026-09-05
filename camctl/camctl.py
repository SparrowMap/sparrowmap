r"""SparrowMap Camera Control - a PTZ and imaging console for a USB camera.

Separate from the SparrowMap site on purpose: this is the installer's tool for
aiming and tuning a node, not something the public map should carry.

    python camctl\camctl.py            ->  http://example-host:8160/

WHY THIS ALSO SOLVES A REAL PROBLEM
On Windows exactly one process can hold a webcam. If this app owns the camera,
the SparrowMap node cannot open it, and vice versa. Rather than make them
mutually exclusive, this app OWNS the camera and re-serves it as MJPEG - so the
node consumes it exactly like any other network camera:

    CameraSource(kind="mjpeg", target="http://localhost:8160/stream.mjpg")

One process on the hardware, any number of consumers, and the local webcam
becomes an IP camera without buying one.

CONTROLS THAT ONLY WORK IN THE RIGHT ORDER
UVC has interlocks that silently swallow writes:
  * FOCUS is read-only until AUTOFOCUS is turned off.
  * GAIN and EXPOSURE are read-only while auto-exposure is running.
  * PAN and TILT do nothing at zoom 100, because there is nothing to pan
    within - the crop is the whole sensor.
The UI enforces the order rather than letting you set a value that appears to
take and does not.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

HERE = Path(__file__).resolve().parent

# The labelling queue lives in the project root; camctl is the operator's own
# app and localhost-bound, which is exactly where labelling belongs.
import sys                                        # noqa: E402
sys.path.insert(0, str(HERE.parent))
import db                                         # noqa: E402
import labelbank                                  # noqa: E402
import node_key                                   # noqa: E402
# The key store lives in the PROJECT ROOT, not in camctl/. camctl enrols
# the camera and run_live signs with the key it made - two processes in
# different directories, and a store that moved with the caller would give
# them one key store each. The detector would then find no key, post
# unsigned, and be 401'd by the pubkey camctl had just registered.
ROOT_DIR = HERE.parent
from core import is_operator_addr, NODE_UA        # noqa: E402
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
from detect import priority
PRESETS = HERE / "presets.json"
PORT = 8160

# ---------------------------------------------------------------------------
# ⚠️ UVC ZOOM / PAN / TILT ARE NOT USED. They are replaced by a software crop.
#
# The C920 does expose them, and OpenCV's DSHOW backend will happily accept a
# value and echo it back from cap.get() - but cap.get() is reading a CACHE of
# what we wrote, not the device, and the image does not move. cap.set(PAN, ...)
# also raises a hard "Unknown C++ exception" for out-of-range values rather
# than returning False.
#
# Rather than fight that, the crop moved into our own code, and it is the
# better design regardless:
#
#   * The C920's zoom is DIGITAL anyway - it crops its own sensor. Cropping in
#     software uses the identical pixels, and skips the camera's upscale back
#     to full frame, which only ever destroys detail.
#   * It works on EVERY camera, not just this one. A $20 fixed camera gets pan,
#     tilt and zoom it does not have in hardware, which matters a great deal
#     for a volunteer network built from whatever people own.
#   * It is deterministic. No driver, no backend, no silent no-op.
#
# The ROI is the aiming mechanism. UVC keeps only the controls that verifiably
# work: exposure, focus, and the image adjustments.
# ---------------------------------------------------------------------------

# name -> (cv2 prop, min, max, step, label, group)
CONTROLS = {
    "focus":      (cv2.CAP_PROP_FOCUS, 0, 255, 5, "Focus", "aim"),
    "autofocus":  (cv2.CAP_PROP_AUTOFOCUS, 0, 1, 1, "Autofocus", "aim"),
    # ⚠️ KEEP THIS. Without an auto-exposure toggle there is no way BACK from a
    # manual value, and the camera remembers manual across restarts - so a
    # night-time test can leave the node dark in broad daylight with no
    # obvious cause. Every manual override needs a documented way home.
    "auto_exposure": (cv2.CAP_PROP_AUTO_EXPOSURE, 0, 1, 1, "Auto exposure", "light"),
    "exposure":   (cv2.CAP_PROP_EXPOSURE, -11, -2, 1, "Exposure", "light"),
    "gain":       (cv2.CAP_PROP_GAIN, 0, 255, 5, "Gain", "light"),
    "brightness": (cv2.CAP_PROP_BRIGHTNESS, 0, 255, 5, "Brightness", "light"),
    "contrast":   (cv2.CAP_PROP_CONTRAST, 0, 255, 5, "Contrast", "light"),
    "backlight":  (cv2.CAP_PROP_BACKLIGHT, 0, 1, 1, "Backlight comp", "light"),
    "saturation": (cv2.CAP_PROP_SATURATION, 0, 255, 5, "Saturation", "image"),
    "sharpness":  (cv2.CAP_PROP_SHARPNESS, 0, 255, 5, "Sharpness", "image"),
    "wb":         (cv2.CAP_PROP_WB_TEMPERATURE, 2000, 6500, 100, "White balance", "image"),
    "auto_wb":    (cv2.CAP_PROP_AUTO_WB, 0, 1, 1, "Auto white balance", "image"),
}

# 2560x1472 is NOT native on a C920 - DirectShow inserts a scaler and reports a
# mode the hardware does not have. Kept, flagged, and not the default.
MODES = [(1920, 1080), (2560, 1472), (1600, 896), (1280, 720), (800, 600), (640, 480)]

# What the detector last reported seeing, for the owner's live overlay.
# In memory only, overwritten each time, never persisted: it is a viewfinder.
DETECTIONS: dict = {"boxes": [], "w": None, "h": None, "ts": 0.0}

# Government vehicles the detector has just finished tracking, waiting for the
# owner to confirm while the car is still in sight. In memory, bounded, and
# dropped on restart - it is a prompt, not a record. The record is the label it
# produces and the sighting that label moves.
CONFIRMS: list = []
CONFIRM_MAX = 6

# 🚨 A PROMPT HAS A SHELF LIFE, AND IT DID NOT HAVE ONE.
#
# The whole justification for interrupting the operator is in the line above:
# "while the car is still in sight". Nothing enforced it. A prompt sat in this
# list until it was answered here or camctl restarted, so the operator came back
# to the desk and was asked to identify vehicles that had passed hours earlier -
# including ones they had already judged on the review page, which clears the
# card on the box and knows nothing about this in-memory list.
#
# Answering late is not merely annoying, it is worse evidence: the honest answer
# to "what was that car" ten minutes later is "I do not remember", and the value
# of this prompt over the review queue is entirely that you just watched it go
# past. If it has expired, the review queue is the right place for it and the
# crop is already there.
CONFIRM_TTL_S = 180.0


class CameraBusy(Exception):
    """The grabber did not answer in time. Never block an HTTP request on it."""


class Camera:
    """Owns the device. THE GRABBER THREAD IS THE ONLY THING THAT TOUCHES cap.

    ⚠️ THIS REPLACED A DEADLOCK, and the reason is worth keeping.

    The first version guarded `cap` with a mutex and took that mutex inside the
    grabber loop, AROUND `cap.read()`. That is fine until the camera stalls -
    and USB cameras stall. `cap.read()` then blocks indefinitely while holding
    the lock, so every API request piles up behind it and the whole server
    hangs. The symptom was maddeningly indirect: the MJPEG endpoint does not
    take the lock, so it kept serving the last good frame, and the app looked
    like "the camera froze" while the real fault was a wedged HTTP server.

    Rule: never hold a lock across a blocking I/O call on a device.

    So there is no shared lock now. `cap` belongs to the grabber thread alone.
    Everything else posts a callable to a queue and waits with a TIMEOUT, so a
    stalled camera degrades to "controls unavailable" instead of taking the
    process down. A watchdog reopens the device when frames stop arriving.
    """

    STALL_SECONDS = 6.0        # no frame for this long -> reopen
    CMD_TIMEOUT = 4.0          # how long an API call waits for the grabber

    def __init__(self, index: int = 0, width: int = 1920, height: int = 1080):
        self.index, self.width, self.height = index, width, height
        # Region of interest as fractions of the frame: this IS the pan / tilt /
        # zoom. cx, cy are the centre; z is the zoom factor (1 = whole frame).
        self.roi = {"cx": 0.5, "cy": 0.5, "z": 1.0}
        self.cap: cv2.VideoCapture | None = None
        self.frame = None
        self.jpeg = None
        self.running = False
        self.blank = False
        self.fps = 0.0
        self.last_frame_at = time.time()
        self.reopens = 0
        self.error = ""
        self.backend = "-"
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._open()
        threading.Thread(target=self._grab, daemon=True).start()

    # -- device, grabber thread only ------------------------------------
    def _open(self) -> bool:
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        # ⚠️ BACKEND CHOICE IS WORTH 6x THE FRAME RATE. MSMF FIRST.
        #
        # Measured on this C920 at 1920x1080:
        #     DSHOW  ->  5.0 fps      (and 10.0 fps at 1280x720)
        #     MSMF   -> 29.8 fps
        #
        # DSHOW stays in an uncompressed mode no matter what FOURCC you set -
        # 5 fps at 1080p and 10 at 720p is exactly the YUY2 bandwidth ladder,
        # so the format request is being ignored. MSMF negotiates a compressed
        # mode by itself and needs no FOURCC at all.
        #
        # I originally picked DSHOW believing MSMF was slow to open and flaky
        # on cheap webcams. That cost six times the frame rate and sent me
        # chasing MJPG, USB bandwidth, exposure and hub topology first. Try
        # MSMF, fall back to DSHOW, and record which one is live so the app can
        # say so.
        for backend, name in ((cv2.CAP_MSMF, "MSMF"), (cv2.CAP_DSHOW, "DSHOW")):
            self.cap = cv2.VideoCapture(self.index, backend)
            if self.cap.isOpened():
                self.backend = name
                break
            self.cap.release()
        else:
            self.backend = "-"
            self.error = "cannot open device (is another app using the camera?)"
            return False

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        ok, _ = self.cap.read()
        if not ok and self.backend == "MSMF":
            # MSMF sometimes opens a wedged device and then cannot read.
            self.cap.release()
            self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
            self.backend = "DSHOW"
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.read()
        if not self.cap.isOpened():
            self.error = "cannot open device"
            return False
        self.width = int(self.cap.get(3))
        self.height = int(self.cap.get(4))
        self.last_frame_at = time.time()
        self.error = ""
        return True

    def _call(self, fn, timeout: float | None = None):
        """Run `fn(cap)` on the grabber thread. Raises CameraBusy on timeout."""
        box: "queue.Queue" = queue.Queue(maxsize=1)
        self._cmds.put((fn, box))
        try:
            ok, val = box.get(timeout=timeout or self.CMD_TIMEOUT)
        except queue.Empty:
            raise CameraBusy("camera did not respond; it may have stalled")
        if not ok:
            raise val
        return val

    def _grab(self):
        self.running = True
        n, t0 = 0, time.time()
        while self.running:
            # 1. apply queued commands - this thread owns cap, so no lock
            for _ in range(8):
                try:
                    fn, box = self._cmds.get_nowait()
                except queue.Empty:
                    break
                try:
                    box.put((True, fn(self.cap)))
                except Exception as exc:
                    box.put((False, exc))

            # 2. read, WITHOUT holding anything
            try:
                ok, f = self.cap.read() if self.cap else (False, None)
            except Exception:
                ok, f = False, None

            # 3. watchdog: a stalled USB camera never errors, it just goes quiet
            if not ok or f is None:
                if self.cap is None or not self.cap.isOpened():
                    # ⚠️ BACK OFF. Retrying every 2 seconds is what wedges this
                    # camera: rapid VideoCapture open/release cycles put a C920
                    # into a state where every subsequent open fails for
                    # minutes and only a physical replug clears it. A tight
                    # retry loop does not recover a busy camera, it breaks a
                    # working one. Back off to a minute and stay there.
                    wait = min(60.0, 3.0 * (2 ** min(self.reopens, 5)))
                    time.sleep(wait)
                    self.reopens += 1
                    self._open()
                    continue
                if time.time() - self.last_frame_at > self.STALL_SECONDS:
                    self.reopens += 1
                    print(f"[camera] no frames for {self.STALL_SECONDS:.0f}s, "
                          f"reopening (attempt {self.reopens})")
                    self._open()
                time.sleep(0.15)
                continue

            self.last_frame_at = time.time()

            # ON WINDOWS A SECOND OPENER GETS BLACK FRAMES, NOT AN ERROR.
            # cap.isOpened() is True, read() succeeds, and every frame is
            # identically zero. Name the failure instead of showing a black
            # rectangle - it cost a long detour once already.
            self.blank = bool(f.std() < 0.5)
            self.frame = f
            view = self.crop(f)
            enc, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if enc:
                self.jpeg = buf.tobytes()
            n += 1
            if n % 30 == 0:
                now = time.time()
                self.fps = 30.0 / max(now - t0, 1e-6)
                t0 = now

    def open(self) -> bool:
        """Reopen from another thread, via the grabber."""
        try:
            return self._call(lambda cap: self._open(), timeout=8.0)
        except CameraBusy:
            return False

    # -- the aim, in software -------------------------------------------
    def crop(self, f):
        """Apply the ROI. Returns the region 1:1 - never upscaled.

        Deliberately NOT resized back to full frame. Upscaling would make the
        stream look like the camera zoomed optically while adding no detail,
        which is exactly the illusion the hardware zoom was selling. What comes
        out is smaller and real.
        """
        r = self.roi
        if r["z"] <= 1.001:
            return f
        h, w = f.shape[:2]
        cw, ch = w / r["z"], h / r["z"]
        cx, cy = r["cx"] * w, r["cy"] * h
        x0 = int(max(0, min(w - cw, cx - cw / 2)))
        y0 = int(max(0, min(h - ch, cy - ch / 2)))
        return f[y0:y0 + int(ch), x0:x0 + int(cw)]

    def set_roi(self, cx=None, cy=None, z=None) -> dict:
        r = self.roi
        if z is not None:
            r["z"] = max(1.0, min(8.0, float(z)))
        if cx is not None:
            r["cx"] = max(0.0, min(1.0, float(cx)))
        if cy is not None:
            r["cy"] = max(0.0, min(1.0, float(cy)))
        # Keep the window inside the frame so panning stops at the edge instead
        # of silently clamping the picture without moving the reported centre.
        half = 0.5 / r["z"]
        r["cx"] = max(half, min(1 - half, r["cx"]))
        r["cy"] = max(half, min(1 - half, r["cy"]))
        out = dict(r)
        out["view_w"] = int(self.width / r["z"])
        out["view_h"] = int(self.height / r["z"])
        return out

    # -- controls, all marshalled onto the grabber thread ----------------
    def get_all(self) -> dict:
        def read(cap):
            out = {}
            for name, (prop, *_rest) in CONTROLS.items():
                try:
                    v = cap.get(prop)
                except Exception:
                    v = -1
                out[name] = None if v == -1 else round(v, 2)
            return out
        try:
            return self._call(read)
        except CameraBusy:
            return {}

    def set(self, name: str, value: float) -> dict:
        if name not in CONTROLS:
            return {"error": f"unknown control {name}"}
        prop, lo, hi, *_ = CONTROLS[name]
        value = max(lo, min(hi, float(value)))

        def apply(cap):
            # Interlocks, applied here so the UI cannot produce a silent no-op.
            if name == "focus":
                cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            if name in ("gain", "exposure"):
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # manual
            if name == "wb":
                cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            if name == "auto_exposure":
                # DSHOW/MSMF encode this as 0.75 = auto, 0.25 = manual rather
                # than 1/0, so a plain 1 does nothing at all.
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75 if value >= 0.5 else 0.25)
                return cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            cap.set(prop, value)
            return cap.get(prop)

        try:
            got = self._call(apply)
        except CameraBusy as exc:
            return {"name": name, "error": str(exc)}
        return {"name": name, "asked": value, "got": round(got, 2),
                "applied": abs(got - value) <= max(1.0, abs(value) * 0.02)}

    def set_mode(self, w: int, h: int) -> dict:
        self.width, self.height = w, h
        ok = self.open()
        return {"ok": ok, "width": self.width, "height": self.height,
                "note": ("2560x1472 is upscaled by DirectShow, not native to the "
                         "C920 - verify it carries real detail before relying on it"
                         if (self.width, self.height) == (2560, 1472) else "")}


CAM: Camera | None = None


PLACEMENT = HERE / "placement.json"


def load_placement() -> dict:
    if PLACEMENT.exists():
        try:
            return json.loads(PLACEMENT.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_placement(p: dict) -> None:
    PLACEMENT.write_text(json.dumps(p, indent=2), encoding="utf-8")


# Where a camera enrolls by default. SparrowMap is the point, so the public
# network is the default; a self-hoster sets SPARROW_HUB to their own hub
# (e.g. http://localhost:8150) and everything else is unchanged.
PUBLIC_HUB = os.environ.get("SPARROW_HUB", "https://map.sparrowmap.com").rstrip("/")


def enroll_with_hub(p: dict, hub: str = None) -> dict:
    """Register (or re-register) this camera as a node on the hub.

    The node id and token are kept here, on the machine that owns the camera,
    so re-saving a placement updates the same node instead of littering the map
    with duplicates every time someone nudges the heading.
    """
    hub = (hub or PUBLIC_HUB)
    import urllib.request

    def _post(b):
        req = urllib.request.Request(
            hub.rstrip("/") + "/api/enroll", method="POST",
            data=json.dumps(b).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": NODE_UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    body = {
        "name": p.get("name") or "Camera node",
        "lat": p["lat"], "lon": p["lon"],
        "heading": p.get("heading", 0), "fov": p.get("fov", 60),
        "reach_m": p.get("reach_m", 40), "kind": "fixed",
        "node_id": p.get("node_id") or "",
    }
    # An existing camera re-registers with the key it already has, so a nudge
    # from /aim never looks like a key rotation.
    known = p.get("node_id") or ""
    if known:
        pub = node_key.public_for(ROOT_DIR, known)
        if pub:
            body["pubkey"] = pub
        if p.get("token"):
            body["token"] = p["token"]
    out = _post(body)

    # 🚨 THE KEY IS NAMED AFTER THE NODE, AND THE NODE ID COMES FROM THE SERVER.
    # A brand-new camera has no id to name a key file after until this call
    # answers, and a camera that MOVED far enough comes back with a different id
    # than it sent (nodes.enroll splits past 60 m). Both land here with a node
    # that has no registered key, so the key is made now and registered by one
    # more call - which is a no-op move of 0 m and cannot split again.
    #
    # It converges rather than assuming: if the node already has the right key,
    # nothing else is sent.
    nid, tok = out.get("id"), out.get("token") or p.get("token")
    if nid and tok and not node_key.load(ROOT_DIR, nid):
        try:
            _priv, pub = node_key.create(ROOT_DIR, nid)
            body.update({"node_id": nid, "token": tok, "pubkey": pub})
            out2 = _post(body)
            out.setdefault("token", tok)
            out["pubkey_registered"] = bool(out2.get("id"))
        except Exception as exc:
            # An unsigned camera works. A camera that fails to start because it
            # could not register a key does not, so this can never be fatal.
            print(f"[camctl] could not register a signing key: {exc}")
            out["pubkey_registered"] = False
    return out


def load_presets() -> dict:
    if PRESETS.exists():
        try:
            return json.loads(PRESETS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_presets(p: dict) -> None:
    PRESETS.write_text(json.dumps(p, indent=2), encoding="utf-8")


def _already_judged(c: dict) -> bool:
    """Has this crop already been given a label by a human, anywhere?

    The prompt and the review page are two front ends onto the same crop, and
    only one of them knew that. Answering on /rv writes a label through
    labelbank (set_label, and the box's verdicts come back the same way), so the
    sidecar is the one place both can see. Without this the operator answered a
    card on the review page and was then asked about the same vehicle here.

    Fails OPEN - a crop we cannot read is still worth asking about. The cost of
    a wrong answer here is one extra prompt; the cost of the opposite default is
    a government vehicle nobody is ever asked about.
    """
    try:
        p = labelbank._sidecar(c.get("day", ""), c.get("stem", ""))
        if not p.exists():
            return False
        return bool(json.loads(p.read_text(encoding="utf-8")).get("label"))
    except Exception:
        return False


def _live_confirms() -> list:
    """The prompts still worth showing, pruning the list as it goes.

    Pruning on READ rather than on a timer: this is the only thing that looks at
    the list, so a timer would be a second moving part to keep correct for no
    benefit.
    """
    now = time.time()
    CONFIRMS[:] = [c for c in CONFIRMS
                   if now - float(c.get("ts") or 0) <= CONFIRM_TTL_S
                   and not _already_judged(c)]
    return CONFIRMS


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html")
        if p == "/place":
            return self._send(200, (HERE / "place.html").read_bytes(), "text/html")
        # Every machine-written label on one page, grouped by call, so a person
        # can audit a first pass by glancing rather than by clicking through it.
        # Regenerated by tools/proof_sheet.py; a plain file, so it is honest
        # about being a snapshot rather than pretending to be live.
        # 🚨 THE AUDIT HAS TO BE ACTIONABLE, NOT JUST READABLE.
        # The first proof page could only be looked at, so finding a wrong
        # machine label meant describing it in a message and hoping the right
        # crop was found from two decimal places. Three were reported that way
        # and several crops matched each description. A reviewer who can see a
        # mistake has to be able to fix it in the same glance.
        if p == "/api/proof/list":
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            import bank_index
            db = bank_index.read()
            rows = [dict(r) for r in db.execute(
                "SELECT day, stem, label, clip_vclass, clip_conf, head_conf, sampling "
                "FROM crops WHERE sampling IN ('machine','confirmed') "
                "ORDER BY sampling, head_conf DESC")]
            db.close()
            return self._json({"items": rows})
        if p == "/proof":
            f = HERE / "proof_app.html"
            if not f.exists():
                return self._send(200, b"<body style='background:#0a0d12;color:#8794a8;"
                                       b"font:14px system-ui;padding:40px'>"
                                       b"No proof sheet built yet. Run: "
                                       b"<code>python tools/proof_sheet.py</code></body>",
                                  "text/html")
            return self._send(200, f.read_bytes(), "text/html")
        if p == "/label":
            return self._send(200, (HERE / "label.html").read_bytes(), "text/html")
        if p == "/grid":
            return self._send(200, (HERE / "grid.html").read_bytes(), "text/html")
        if p == "/api/bank/stats":
            return self._json(labelbank.stats())
        if p == "/api/bank/grid":
            # A whole screen of crops to label at once. Click the police ones;
            # the rest save as not-police. Far faster than one crop at a time
            # for a class as rare as police.
            mode = q.get("mode", ["likely"])[0]
            try:
                n = max(6, min(48, int(q.get("n", ["24"])[0])))
            except (TypeError, ValueError):
                n = 24
            return self._json({"items": labelbank.next_batch(mode, n),
                               "mode": mode, **labelbank.stats()})
        if p == "/api/bank/next":
            bank_mode = q.get("mode", ["review"])[0]
            it = labelbank.next_item(bank_mode)
            if not it:
                return self._json({"done": True, **labelbank.stats()})
            # ⚠️ THE MODEL'S GUESS IS STRIPPED HERE.
            # Showing it beside the image anchors the human onto it, the labels
            # drift toward the model, and the model then scores well against
            # labels it partly wrote. It is returned by the LABEL call instead,
            # so the page can reveal it only after a decision exists.
            # The other crops that are probably this same vehicle, so one
            # decision can cover the whole pass instead of one per frame.
            it["group"] = labelbank.same_pass(it["day"], it["stem"],
                                              mode=bank_mode)
            return self._json({**{k: v for k, v in it.items()
                                  if not k.startswith("_")},
                               **labelbank.stats()})
        if p.startswith("/api/bank/img/"):
            parts = p[len("/api/bank/img/"):].split("/")
            f = labelbank.image_path(*parts) if len(parts) == 2 else None
            if not f:
                return self._json({"error": "not found"}, 404)
            return self._send(200, f.read_bytes(), "image/jpeg")
        if p.startswith("/static/"):
            # Shared with the hub's public folder so ONE copy of a shared
            # control (sitenav.js, style.css) serves both apps. A control
            # copied into two files gets fixed in one of them.
            f = HERE.parent / "public" / Path(p[8:]).name
            if f.is_file():
                import mimetypes as _m
                return self._send(200, f.read_bytes(),
                                  _m.guess_type(str(f))[0] or "application/octet-stream")
            return self._json({"error": "not found"}, 404)
        if p.startswith("/vendor/"):
            # Leaflet is already vendored for the map; share it rather than
            # keeping a second copy that can drift.
            f = HERE.parent / "public" / "vendor" / Path(p[8:]).name
            if not f.is_file():
                f = HERE.parent / "public" / "vendor" / "images" / Path(p).name
            if f.is_file():
                import mimetypes as _m
                return self._send(200, f.read_bytes(),
                                  _m.guess_type(str(f))[0] or "application/octet-stream")
            return self._json({"error": "not found"}, 404)
        if p == "/api/placement":
            return self._json(load_placement())
        if p == "/stream.mjpg":
            return self._stream()
        if p == "/snapshot.jpg":
            return self._send(200, CAM.jpeg or b"", "image/jpeg")
        if p == "/api/state":
            return self._json({
                "controls": CAM.get_all(),
                "spec": {k: {"min": v[1], "max": v[2], "step": v[3],
                             "label": v[4], "group": v[5]}
                         for k, v in CONTROLS.items()},
                "width": CAM.width, "height": CAM.height,
                "fps": round(CAM.fps, 1),
                "blank": getattr(CAM, "blank", False),
                "stalled": (time.time() - CAM.last_frame_at) > CAM.STALL_SECONDS,
                "silent_for": round(time.time() - CAM.last_frame_at, 1),
                "reopens": CAM.reopens,
                "error": CAM.error,
                "backend": getattr(CAM, "backend", "-"),
                "roi": CAM.set_roi(),
                "view": [int(CAM.width / CAM.roi["z"]), int(CAM.height / CAM.roi["z"])],
                "modes": [f"{w}x{h}" for w, h in MODES],
                "presets": sorted(load_presets()),
            })
        if p == "/api/presets":
            return self._json(load_presets())
        if p == "/api/confirm":
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            return self._json({"pending": _live_confirms()})
        if p == "/api/detections":
            # What the detector is seeing right now. OPERATOR ONLY - this app
            # is the camera owner's own control panel and never leaves the
            # machine; the public hub has no equivalent and must not get one.
            # A live box feed is a live view of a street, which is exactly the
            # thing the two-tier design refuses to publish.
            d = dict(DETECTIONS)
            d["age"] = round(time.time() - d.get("ts", 0), 2) if d.get("ts") else None
            return self._json(d)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")

        if u.path == "/api/set":
            return self._json(CAM.set(body["name"], body["value"]))
        # Confirming or correcting a machine label. The result is tagged
        # `confirmed` rather than `review`: a human has now looked at it, so it
        # is trustworthy enough to TRAIN on and to use as a gold crop for the
        # community task - but the crop was chosen by a biased queue, so it must
        # still never enter `measurable`. Only crops drawn at random in review
        # mode may measure anything.
        if u.path == "/api/proof/set":
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            try:
                res = labelbank.set_label(body["day"], body["stem"],
                                          body["label"], "confirmed")
                return self._json({"ok": True, "clip": res.get("clip")})
            except Exception as exc:                       # noqa: BLE001
                return self._json({"error": str(exc)}, 400)

        if u.path == "/api/bank/label":
            # Same check as the hub, from the same function. `example-host`
            # is IPv6-first on this box, so an IPv4-only test 403'd every
            # labelling click made through the Pages hub link.
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            try:
                if body.get("clear"):
                    undone = labelbank.clear_label(body["day"], body["stem"])
                    # An undo must reverse the WHOLE action. When the label was
                    # applied to a same-vehicle group (`also`), clearing only the
                    # primary left the siblings mislabelled - the exact hole that
                    # let two civilian cars keep a `police` label after an undo.
                    also_cleared = 0
                    for g in (body.get("also") or [])[:7]:
                        try:
                            labelbank.clear_label(g["day"], g["stem"])
                            also_cleared += 1
                        except (KeyError, ValueError, FileNotFoundError,
                                TypeError):
                            pass
                    return self._json({"ok": True, "undone": undone,
                                       "also_cleared": also_cleared,
                                       **labelbank.stats()})
                res = labelbank.set_label(body["day"], body["stem"],
                                          body["label"],
                                          body.get("mode", "review"))
                # `also` carries the rest of the vehicle pass. The page only
                # sends it when he chose to label the group, and he was looking
                # at every crop in it when he did - see labelbank.same_pass for
                # why this is never inferred server-side.
                extra = 0
                for g in (body.get("also") or [])[:7]:
                    try:
                        labelbank.set_label(g["day"], g["stem"], body["label"],
                                            body.get("mode", "review"))
                        extra += 1
                    except (KeyError, ValueError, FileNotFoundError, TypeError):
                        pass
                return self._json({"ok": True, "also_labelled": extra,
                                   **res, **labelbank.stats()})
            except (KeyError, ValueError, FileNotFoundError) as exc:
                return self._json({"error": str(exc)}, 400)

        if u.path == "/api/bank/grid_label":
            # Batch label from the grid picker: the crops he clicked are police,
            # the rest of the shown grid are not-police (civilian). One request
            # instead of one per tile. Best-effort per crop - a single bad stem
            # never loses the rest of the screen's work.
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            mode = body.get("mode", "likely")
            # ⚡ ONE shared index connection for the whole batch, sequential.
            # The real cost was NOT the network: set_label opened a fresh
            # bank_index connection per crop and connect() re-runs the schema
            # executescript (~100 ms), so 24 crops = ~2 s of pure overhead. With
            # one reused connection the not-police writes are a few ms each.
            # Not-police labels also skip the box round-trip (sync=False) unless
            # the crop was actually published, and police are few per grid, so a
            # thread pool is no longer worth its foot-guns (a SQLite connection
            # is not shareable across threads anyway).
            # 🚀 SIDECARS NOW, INDEX LATER. Measured: a sidecar write is ~11 ms
            # but one bank_index INSERT+commit is ~160 ms on the 260 MB index, so
            # the index update - NOT the sidecar and NOT the network - was the
            # whole 2-8 s of a grid save. The sidecar is the truth, so write all
            # of them (fast, and police still sync to the map), return at once,
            # and fold the slow index updates into a background thread. The page
            # de-dups the crops it just saved, so a momentarily-stale index never
            # re-shows them.
            import bank_index
            import threading as _th
            jobs = ([(it, "police") for it in (body.get("police") or [])[:64]]
                    + [(it, "civilian") for it in (body.get("other") or [])[:64]])
            done = {"police": 0, "civilian": 0, "published": 0, "failed": 0}
            written = []
            for it, lab in jobs:
                try:
                    res = labelbank.set_label(it["day"], it["stem"], lab, mode,
                                              sync=(lab == "police"),
                                              defer_index=True)
                    done[lab] += 1
                    written.append((it["day"], it["stem"]))
                    syn = str(res.get("synced") or "")
                    if syn.startswith("promot") or syn.startswith("reclassif"):
                        done["published"] += 1
                except (KeyError, ValueError, FileNotFoundError, TypeError):
                    done["failed"] += 1

            def _reindex(rows):
                try:
                    db = bank_index.connect()
                    try:
                        for day, stem in rows:
                            try:
                                bank_index.update_one(db, day, stem)
                            except Exception:
                                pass
                    finally:
                        db.close()
                except Exception:
                    pass
            if written:
                _th.Thread(target=_reindex, args=(written,), daemon=True).start()
            # stats() reads the index, which is a beat behind now - fine, it
            # catches up. The client tracks its own saved crops for the queue.
            return self._json({"ok": True, **done, **labelbank.stats()})

        if u.path == "/api/confirm":
            # Pushed by the node the moment a government-looking pass ends, so
            # the owner is asked while the vehicle is still in sight rather than
            # hours later on a review page.
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            if body.get("clear"):
                stem = body.get("stem")
                CONFIRMS[:] = [c for c in CONFIRMS if c.get("stem") != stem]
                return self._json({"ok": True, "pending": len(CONFIRMS)})
            if body.get("stem"):
                # 🚨 ONE VEHICLE PASS = ONE PROMPT, NOT ONE PER TRACK.
                # The tracker loses and re-acquires a car crossing the view, so a
                # single cop driving by ends several tracks seconds apart, each
                # with its OWN stem, each POSTing a confirm here. Deduping by stem
                # made every fragment its own prompt - so the operator clicked
                # "confirm" three to six times for the same car before it finally
                # advanced to the next vehicle. Collapse anything within
                # PASS_GAP seconds of a pending prompt into that one prompt.
                PASS_GAP = 15.0
                ts = float(body.get("ts") or 0)
                same = next((c for c in CONFIRMS
                             if abs(float(c.get("ts") or 0) - ts) <= PASS_GAP), None)
                if same:
                    # Keep the STRONGEST evidence on the single prompt, and chain
                    # the timestamp forward so a longer pass stays one prompt.
                    if body.get("published"):
                        same["published"] = True
                    if float(body.get("conf") or 0) >= float(same.get("conf") or 0):
                        same.update({k: body.get(k) for k in
                                     ("day", "stem", "conf", "published")})
                    same["ts"] = ts
                else:
                    CONFIRMS.append({k: body.get(k) for k in
                                     ("day", "stem", "conf", "ts", "published")})
                    # Bounded: a prompt nobody answered must not become a backlog.
                    del CONFIRMS[:-CONFIRM_MAX]
            return self._json({"ok": True, "pending": len(CONFIRMS)})

        if u.path == "/api/detections":
            # The detector runs as a separate process (one owner of the device,
            # everyone else reads HTTP) so it posts what it sees here. Bounded
            # on purpose: a fixed number of boxes, held in memory, never
            # written to disk. This is a viewfinder, not a record.
            if not is_operator_addr(self.client_address[0]):
                return self._json({"error": "not an operator address"}, 403)
            DETECTIONS.update({
                "boxes": (body.get("boxes") or [])[:32],
                "w": body.get("w"), "h": body.get("h"),
                "ts": time.time(),
            })
            return self._json({"ok": True})
        if u.path == "/api/shutdown":
            # A clean way to stop. Force-killing a process that holds a USB
            # capture graph wedges the device, so there has to be a door.
            def bye():
                time.sleep(0.3)
                CAM.running = False
                time.sleep(0.5)
                try:
                    if CAM.cap:
                        CAM.cap.release()
                except Exception:
                    pass
                import os
                os._exit(0)
            threading.Thread(target=bye, daemon=True).start()
            return self._json({"stopping": True})

        if u.path == "/api/placement":
            p = load_placement()
            p.update({k: v for k, v in body.items() if k != "hub"})
            try:
                r = enroll_with_hub(p, body.get("hub") or PUBLIC_HUB)
                p["node_id"] = r["id"]
                if r.get("token"):
                    p["token"] = r["token"]
                # A new camera comes back with its own reviewer token, scoped to
                # just this camera - kept so the launcher can show the owner
                # where to review their own catches.
                if r.get("review_token"):
                    p["review_token"] = r["review_token"]
                    p["review_url"] = r.get("review_url") or "/rv"
                p["enrolled"] = True
                p["error"] = ""
            except Exception as exc:
                p["enrolled"] = False
                p["error"] = f"{type(exc).__name__}: {exc}"
            save_placement(p)
            return self._json(p)

        if u.path == "/api/roi":
            return self._json(CAM.set_roi(body.get("cx"), body.get("cy"),
                                          body.get("z")))
        if u.path == "/api/mode":
            w, h = (int(x) for x in body["mode"].split("x"))
            return self._json(CAM.set_mode(w, h))
        if u.path == "/api/preset/save":
            ps = load_presets()
            ps[body["name"]] = {"controls": CAM.get_all(),
                                "mode": f"{CAM.width}x{CAM.height}",
                                "roi": dict(CAM.roi)}
            save_presets(ps)
            return self._json({"saved": body["name"], "presets": sorted(ps)})
        if u.path == "/api/preset/load":
            ps = load_presets()
            pr = ps.get(body["name"])
            if not pr:
                return self._json({"error": "no such preset"}, 404)
            if pr.get("mode"):
                w, h = (int(x) for x in pr["mode"].split("x"))
                if (w, h) != (CAM.width, CAM.height):
                    CAM.set_mode(w, h)
            # autofocus/auto-wb first, so the manual values that follow stick
            for k in ("autofocus", "auto_wb"):
                if pr["controls"].get(k) is not None:
                    CAM.set(k, pr["controls"][k])
            for k, v in pr["controls"].items():
                if v is not None and k not in ("autofocus", "auto_wb"):
                    CAM.set(k, v)
            if pr.get("roi"):
                CAM.set_roi(**pr["roi"])
            return self._json({"loaded": body["name"], "controls": CAM.get_all(),
                               "roi": dict(CAM.roi)})
        if u.path == "/api/preset/delete":
            ps = load_presets()
            ps.pop(body["name"], None)
            save_presets(ps)
            return self._json({"presets": sorted(ps)})
        return self._json({"error": "not found"}, 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                j = CAM.jpeg
                if j:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: " + str(len(j)).encode() +
                                     b"\r\n\r\n" + j + b"\r\n")
                time.sleep(1 / 20)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    # 🚨 CLAIM IT HERE, NOT FROM THE LAUNCHER. The launcher restarts
    # this process in a loop and each restart inherits the launcher's
    # priority, so a value set from outside is gone by the next crash.
    priority.claim("camctl")
    global CAM
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()

    CAM = Camera(a.index, a.width, a.height)
    # Start the server even if the camera is busy. A node may boot before the
    # camera is ready, or a browser tab may be holding it, and refusing to
    # start means the operator cannot even open the page to find out why. The
    # watchdog keeps retrying and the UI says what is wrong.
    if CAM.cap is None or not CAM.cap.isOpened():
        print(f"! camera {a.index} is not available yet - something else may "
              f"have it (Windows Camera, Teams, Zoom, OBS, or a browser tab).")
        print("  Serving anyway; the watchdog will keep retrying.")
    else:
        print(f"camera {a.index} open at {CAM.width}x{CAM.height}")
    print(f"SparrowMap Camera Control -> http://localhost:{a.port}/")
    print(f"  node source: CameraSource(kind='mjpeg', "
          f"target='http://localhost:{a.port}/stream.mjpg')")
    import sys
    sys.path.insert(0, str(HERE.parent))
    from dualstack import serve
    srv = serve(Handler, a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        CAM.running = False
        print("\nstopping")


if __name__ == "__main__":
    main()
