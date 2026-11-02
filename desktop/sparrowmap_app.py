"""SparrowMap: the LIGHT node, now for one camera or many, with a live wall.

    python desktop\\sparrowmap_app.py          # run it
    python desktop\\build.py                   # build SparrowMap.exe

🚨 THIS IS THE LIGHT NODE. NO CLIP, NO HEAD, NO TORCH.
It finds vehicles and posts a crop too small to carry a plate; the classifier
that decides "is this a patrol car" runs at home on the project's GPU. That is
the whole reason this can be a small download that runs on a shop PC instead of
a machine-learning install. Anything that wants to add the head here should
build a second app rather than grow this one.

WHY A WINDOW. The relay already worked from a command line, and that is the
wrong ask for the people this is aimed at: a business with cameras on the
street and no interest in terminals. The window exists so they can SEE it
working - the live wall shows every camera at once with a box drawn on each car
as it is tracked, and a click opens one full size. No video ever leaves this
computer; only a plate-less crop of a detected vehicle is ever sent, and only
for a camera that has been given an id and placed on the map.

🚨 ONE DETECTOR, SHARED. The ONNX session is loaded once and every camera runs
against it. Loading one per camera would multiply the memory on a shop PC for
no gain - the session is safe to call from several threads.
"""

from __future__ import annotations

import base64
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# Pillow makes the preview cheap: hand the UI thread a resized array and let it
# build the image directly, instead of PNG-encoding + base64 every frame. If it
# is missing we fall back to the slower PNG path so the app still runs.
try:
    from PIL import Image as PILImage, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

APP = "SparrowMap"
APP_VERSION = "1.0.0"
HUB_DEFAULT = "https://map.sparrowmap.com"
# The version file the app checks at start and once a day. SPARROW_UPDATE_URL
# lets a test point it at a local server; in the shipped exe it is the hub.
UPDATE_URL = os.environ.get("SPARROW_UPDATE_URL") or (HUB_DEFAULT + "/app/version.json")
CFG = Path.home() / ".sparrowmap" / "desktop.json"
BG, PANEL, INK, DIM, LINE = "#0a0d12", "#111621", "#e6ecf5", "#8794a8", "#2a3547"
RED, GREEN, AMBER = "#ff3b47", "#3ddc97", "#f5b73d"
BOX_BGR = (97, 220, 61)                 # the green used for tracking boxes


def _resource(name: str) -> str:
    """Absolute path to a bundled asset, in the exe (sys._MEIPASS) or the repo.

    Lets the window wear the real SparrowMap mark whether it runs from source or
    from the PyInstaller bundle (favicon.ico / icon-192.png are added to datas)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / name
        if p.exists():
            return str(p)
    here = Path(__file__).resolve().parent
    for cand in (here / name, here.parent / "public" / name):
        if cand.exists():
            return str(cand)
    return name


# --------------------------------------------------------------------------
# version check + in-place updater
#
# The app asks the hub for a small version.json (version, notes, url, sha256) at
# start and once a day. When it is newer than this build it offers an update;
# nothing downloads or swaps without a click. "Update now" fetches the exe over
# HTTPS, VERIFIES the sha256 before trusting a byte of it, then a tiny .bat waits
# for SparrowMap to exit, copies the new exe over the SAME path (so his Defender
# path-allow still covers it) and relaunches it hidden in the tray with the
# cameras resuming. These are module-level and side-effect-free so they can be
# tested end to end without the window.
# --------------------------------------------------------------------------
def _version_tuple(s):
    """'v1.2.3' -> (1, 2, 3); junk parts become 0 so a bad string never wins."""
    out = []
    for part in str(s).strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def _version_newer(remote, local):
    try:
        return _version_tuple(remote) > _version_tuple(local)
    except Exception:
        return False


def fetch_version_info(url=UPDATE_URL, timeout=12):
    """Return the parsed version.json dict, or None on any failure. Never raises -
    a down server must not break startup."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "SparrowMap/%s" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("version"):
            return data
    except Exception:
        pass
    return None


def download_and_verify(url, sha256_hex, dest, timeout=180):
    """Download url to dest, then check sha256. Returns (ok, message). On ANY
    mismatch or error the partial file is deleted - an unverified binary is never
    left where the swap helper could pick it up."""
    import hashlib
    h = hashlib.sha256()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "SparrowMap/%s" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
    except Exception as ex:
        try:
            os.remove(dest)
        except OSError:
            pass
        return False, "download failed (%s)" % ex.__class__.__name__
    got = h.hexdigest()
    if sha256_hex and got.lower() != str(sha256_hex).lower():
        try:
            os.remove(dest)
        except OSError:
            pass
        return False, "checksum did not match - update refused"
    return True, got


def write_swap_helper(new_file, target, relaunch=True):
    """Write a .bat that waits for `target` to unlock (the running exe to exit),
    copies new_file over it IN PLACE, relaunches it hidden in the tray, then
    deletes the temp file and itself. No PID is used: copy simply fails while the
    exe holds the file, so it retries until the file is free - PID reuse can never
    make it swap the wrong thing. It gives up after ~2 min and relaunches whatever
    is at `target` (new if the copy landed, old if not) so the app is never left
    closed. `relaunch=False` is for tests only (skips the start line)."""
    bat = str(Path(new_file).with_suffix(".swap.bat"))
    start_line = ('start "" "%s" --tray\r\n' % target) if relaunch else ""
    script = (
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        "set /a n=0\r\n"
        ":loop\r\n"
        'copy /y "%s" "%s" >nul 2>&1\r\n' % (new_file, target) +
        "if not errorlevel 1 goto done\r\n"
        "set /a n+=1\r\n"
        "if !n! geq 120 goto done\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto loop\r\n"
        ":done\r\n" +
        start_line +
        'del /q "%s" >nul 2>&1\r\n' % new_file +
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(bat, "w", encoding="ascii") as f:
        f.write(script)
    return bat


def _spawn_detached(cmd):
    """Launch cmd so it outlives this process and shows no console window - the
    swap helper must keep running after the app exits."""
    import subprocess
    flags = 0
    kw = {}
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        kw["close_fds"] = True
    return subprocess.Popen(cmd, creationflags=flags, **kw)

# Cloud / Wi-Fi cameras that offer no RTSP still announce their maker in the MAC
# address. BRAND_HINTS turns a maker into a plain next step. kind: "enable" = you
# can turn RTSP on in the camera's own app and then Scan again; "cloud" = there
# is no local RTSP, it needs RTSP AnyCam (coming). Hints are drawn from
# D:/LLM/rtsp-anycam/BRANDS.md (researched 2026-11-01).
BRAND_HINTS = {
    "wyze":      ("enable", "Wyze camera: turn on RTSP in the Wyze app (Cam v3 / Pan v3, firmware 2/2026+), then Scan again. OG / Pan v4 have no RTSP."),
    "ring":      ("cloud",  "Ring camera: no local RTSP. Needs RTSP AnyCam (coming), through your Ring login."),
    "nest":      ("cloud",  "Google Nest camera: no local RTSP. Needs RTSP AnyCam (coming), Google's camera API."),
    "google":    ("cloud",  "Google / Nest device: if it is a Nest camera, RTSP needs AnyCam (coming)."),
    "arlo":      ("cloud",  "Arlo camera: no RTSP. Needs RTSP AnyCam (coming), cloud login."),
    "blink":     ("cloud",  "Blink camera (Amazon): no RTSP. Needs RTSP AnyCam (coming), cloud login."),
    "amazon":    ("cloud",  "Amazon device: could be a Ring or Blink camera (or an Echo). A Ring/Blink cam needs RTSP AnyCam (coming)."),
    "eufy":      ("enable", "Eufy / Anker camera: wired / HomeBase models can turn on RTSP in the app (under storage); battery models need AnyCam (coming)."),
    "tplink":    ("enable", "TP-Link camera (Tapo/Kasa): wired Tapo - turn on a Camera Account in the Tapo app for RTSP; Kasa needs AnyCam (coming)."),
    "reolink":   ("enable", "Reolink camera: turn on RTSP/ONVIF in the Reolink app (wired/PoE), then Scan again; battery needs a Hub/NVR."),
    "hikvision": ("enable", "Hikvision camera: turn on RTSP (port 554) in the camera, then Scan again."),
    "ezviz":     ("enable", "EZVIZ camera: turn on RTSP under LAN Live View in the EZVIZ app, then Scan again."),
    "dahua":     ("enable", "Dahua camera: turn on RTSP (port 554) in the camera, then Scan again."),
    "amcrest":   ("enable", "Amcrest camera: RTSP is usually on (port 554). Scan again, or type rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0."),
    "ubiquiti":  ("enable", "UniFi Protect: turn on RTSP(S) per camera in the console, then add the rtsps:// URL."),
    "xiaomi":    ("cloud",  "Xiaomi (Mi Home) camera: no local RTSP. Needs RTSP AnyCam (coming)."),
    "tuya":      ("cloud",  "Tuya / Smart Life camera: no local RTSP. Needs RTSP AnyCam (coming)."),
    "zmodo":     ("enable", "Zmodo device: some older models offer RTSP (port 10554); newer firmware disables it."),
    "swann":     ("enable", "Swann camera: RTSP on selected models only - check the manual, then Scan again."),
    "lorex":     ("enable", "Lorex camera: wired models often expose RTSP (port 554); Wi-Fi models may not."),
    "roku":      ("cloud",  "Roku camera: no confirmed RTSP route yet; AnyCam (coming) will try."),
}

_OUI_CACHE = None

def _camera_ouis() -> dict:
    """The MAC-prefix -> brand map (camera_ouis.json), loaded once. Empty if the
    file is missing, so the scan degrades to RTSP-only rather than crashing."""
    global _OUI_CACHE
    if _OUI_CACHE is None:
        try:
            with open(_resource("camera_ouis.json"), encoding="utf-8") as f:
                _OUI_CACHE = json.load(f)
        except Exception:
            _OUI_CACHE = {}
    return _OUI_CACHE

# 🚨 IMPORTED, NOT SHELLED OUT TO. Bundled by PyInstaller into one exe, there is
# no `python -m detect.relay` to run: the interpreter IS this program.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_cfg() -> dict:
    try:
        d = json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return _migrate(d)


def _migrate(d: dict) -> dict:
    """Old single-camera config -> the cameras list, so an existing install
    keeps its camera instead of starting blank."""
    if "cameras" not in d:
        cams = []
        if d.get("source") or d.get("node"):
            cams.append({
                "id": "cam1", "name": "Camera 1",
                "source": d.get("source", ""), "node": d.get("node", ""),
                "token": d.get("token", ""),
                "lat": d.get("lat", 0.0), "lon": d.get("lon", 0.0),
            })
        d["cameras"] = cams
    d.setdefault("autostart", False)
    d.setdefault("run_at_login", False)   # the "Start with Windows" checkbox
    d.setdefault("settings_win", "")      # remembered Aim & settings size "WxH"
    d.setdefault("tray_notice_shown", False)
    d.setdefault("last_update_check", "")  # ISO date of the last version check
    d.setdefault("hub", HUB_DEFAULT)
    return d


def save_cfg(d: dict) -> None:
    try:
        CFG.parent.mkdir(parents=True, exist_ok=True)
        CFG.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


def _new_id(existing) -> str:
    n = 1
    have = {c["id"] for c in existing}
    while f"cam{n}" in have:
        n += 1
    return f"cam{n}"


# --------------------------------------------------------------------------
# one camera
# --------------------------------------------------------------------------
class CameraWorker(threading.Thread):
    """Reads one stream, detects vehicles, draws boxes for the preview, and
    (when the camera has an id + token + location) posts a crop when a vehicle
    leaves frame. Communicates only through the queue - never touches tkinter.
    """

    def __init__(self, cam: dict, shared: dict, out: "queue.Queue"):
        super().__init__(daemon=True)
        self.cam, self.shared, self.out = cam, shared, out
        self.cid = cam["id"]
        self.stop = threading.Event()
        self.sent = 0
        self.preview_w = 300            # the UI updates this on view change

    def emit(self, kind, val):
        self.out.put((kind, self.cid, val))

    def _preview(self, frame, hits):
        try:
            import cv2
            img = frame
            if hits:
                img = frame.copy()
                for h in hits:
                    x1, y1, x2, y2 = (int(v) for v in h["box"])
                    cv2.rectangle(img, (x1, y1), (x2, y2), BOX_BGR, 3)
            ht, wd = img.shape[:2]
            w = max(120, int(self.preview_w))
            img = cv2.resize(img, (w, max(1, int(ht * w / wd))))
            if HAVE_PIL:
                # hand the UI thread the RGB array; it builds the image (cheap)
                self.emit("frame", cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            else:
                ok, buf = cv2.imencode(".png", img)
                if ok:
                    self.emit("frame", base64.b64encode(buf).decode())
        except Exception:
            pass

    def run(self):
        from detect import relay
        sess, iname = self.shared["sess"], self.shared["iname"]
        hub = self.shared["hub"]
        src = self.cam["source"]
        if isinstance(src, str) and src.strip().isdigit():
            src = int(src.strip())

        cap = relay.open_stream(src)
        if cap is None:
            self.emit("log", "could not open the camera - check the address")
            self.emit("state", "error")
            return
        self.emit("state", "running")
        self.emit("log", "watching")

        can_post = bool(self.cam.get("node") and self.cam.get("token"))
        # 🚨 A FIXED CAMERA SENDS NO GPS. The hub plots a fixed camera's sightings
        # on the span it was given in Aim and uses any event GPS only to REJECT a
        # moving phone reusing a fixed id - so posting 0,0 (the old default) made
        # the hub think the camera was 11,000 km away and refuse every sighting
        # with a 409. Send None and let the Aim placement rule; only pass a real
        # value if the owner deliberately set one.
        lat = float(self.cam["lat"]) if self.cam.get("lat") else None
        lon = float(self.cam["lon"]) if self.cam.get("lon") else None
        tracks, nid, last_send, last_det, last_prev = [], 0, 0.0, 0.0, 0.0
        hits = []
        while not self.stop.is_set():
            ok, frame = cap.read()
            if not ok:
                self.emit("log", "camera dropped; reconnecting…")
                cap.release()
                time.sleep(3)
                cap = relay.open_stream(src)
                if cap is None:
                    time.sleep(7)
                continue
            now = time.time()
            # Detect ~10x/sec (the CPU-heavy part), reuse the last boxes for the
            # frames in between so the video stays smooth without 10 cameras
            # pinning the CPU.
            if now - last_det > 0.1:
                last_det = now
                blob, s, ox, oy = relay.letterbox(frame)
                hits = relay.decode(sess.run(None, {iname: blob})[0], s, ox, oy)
                for h in hits:
                    m = next((t for t in tracks if relay.iou(t["box"], h["box"]) > 0.3), None)
                    if m is None:
                        nid += 1
                        m = {"id": nid, "seen": 0, "best": 0.0, "crop": None, "sent": False}
                        tracks.append(m)
                    m.update(box=h["box"], last=now, cls=h["cls"])
                    m["seen"] += 1
                    area = (h["box"][2] - h["box"][0]) * (h["box"][3] - h["box"][1])
                    if area > m["best"]:
                        m["best"], m["crop"] = area, relay.crop_of(frame, h["box"])
                for t in list(tracks):
                    if now - t.get("last", 0) < relay.GONE_S:
                        continue
                    tracks.remove(t)
                    if not can_post or t["seen"] < relay.MIN_FRAMES or not t["crop"] \
                            or t["sent"] or now - last_send < 5.0:
                        continue
                    try:
                        r = relay.post(hub, self.cam["node"], self.cam["token"],
                                       lat, lon, t["crop"], t["cls"], 0.9)
                        last_send, t["sent"] = now, True
                        if r.get("error"):
                            self.emit("log", f"hub refused: {r['error']}")
                        else:
                            self.sent += 1
                            self.emit("sent", self.sent)
                            self.emit("log", f"sent a {t['cls']}")
                    except urllib.error.HTTPError as e:
                        last_send, t["sent"] = now, True
                        detail = ""
                        try:
                            detail = json.loads(e.read() or b"{}").get("error", "")
                        except Exception:
                            pass
                        hint = "  — no location set; open Aim to place it." if not (lat or lon) else ""
                        self.emit("log", f"hub refused ({e.code}): {detail or e.reason}{hint}")
                    except Exception as exc:
                        self.emit("log", f"could not send ({exc.__class__.__name__})")
            # Preview up to ~30 fps (the UI coalesces to the newest, and the
            # camera's own rate is usually the real ceiling).
            if now - last_prev > 0.033:
                last_prev = now
                self._preview(frame, hits)
            time.sleep(0.005)
        cap.release()
        self.emit("state", "stopped")


# --------------------------------------------------------------------------
# the aim map: a slippy OpenStreetMap on a plain Tk canvas
# --------------------------------------------------------------------------
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_UA = "SparrowMap-desktop/1.0 (+https://map.sparrowmap.com)"


class AimMap(tk.Frame):
    """A small slippy map (OpenStreetMap tiles drawn on a Tk canvas) for placing
    AND aiming a camera the way the website does - with no browser engine and no
    image library, because Tk 8.6 decodes the PNG tiles itself. Drag the dot to
    MOVE the camera, drag the arrow's tip to REPOINT it; How-wide / How-far are
    extras. Only map tiles are ever fetched (and a tile names an area, not a
    point) - the camera's own position is never sent anywhere."""

    def __init__(self, parent, app, lat, lon, heading=0.0, fov=60.0, reach=45.0,
                 on_change=None, width=452, height=264):
        super().__init__(parent, bg=BG)
        self.app = app
        self.on_change = on_change
        self._has_fix = lat is not None and lon is not None
        self.lat = float(lat) if lat is not None else 39.5
        self.lon = float(lon) if lon is not None else -98.35
        self.heading = float(heading or 0.0)
        self.fov = float(fov or 60.0)
        self.reach = float(reach or 45.0)
        self.zoom = 17 if self._has_fix else 4
        self.clat, self.clon = self.lat, self.lon
        self.cw, self.ch = width, height
        self._tiles = {}
        self._inflight = set()
        self._drag = None
        self._in_redraw = False
        self._pin = (width / 2.0, height / 2.0)
        self._tip = (width / 2.0, height / 2.0)

        self.canvas = tk.Canvas(self, width=width, height=height, bg="#0e1622",
                                highlightthickness=1, highlightbackground=LINE,
                                cursor="fleur")
        self.canvas.pack(fill="x")
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        tk.Button(row, text="＋", command=lambda: self._zoom(1), width=2, bg="#141c28",
                  fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"),
                  activebackground="#1b2432", activeforeground=INK).pack(side="left")
        tk.Button(row, text="－", command=lambda: self._zoom(-1), width=2, bg="#141c28",
                  fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"),
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="left", padx=(4, 10))
        self.locbtn = tk.Button(row, text="\U0001f4cd  Use my location",
                                command=self._use_location, bg="#141c28", fg=INK,
                                relief="flat", cursor="hand2", font=("Segoe UI", 9),
                                activebackground="#1b2432", activeforeground=INK)
        self.locbtn.pack(side="left")
        self.hint = tk.Label(self, text="Drag the dot to place it, drag the arrow tip to "
                             "aim. + / − to zoom.", bg=BG, fg=DIM,
                             font=("Segoe UI", 8), anchor="w", wraplength=440,
                             justify="left")
        self.hint.pack(fill="x", pady=(2, 0))

        ex = tk.Frame(self, bg=BG)
        ex.pack(fill="x", pady=(2, 0))
        self._mkscale(ex, "How wide °", 10, 120, "fov")
        ex2 = tk.Frame(self, bg=BG)
        ex2.pack(fill="x")
        self._mkscale(ex2, "How far m", 15, 200, "reach")

        self.after(0, self.redraw)

    # -- values ----------------------------------------------------------
    def get(self):
        return {"lat": self.lat, "lon": self.lon, "heading": round(self.heading, 1),
                "fov": round(self.fov), "reach": round(self.reach),
                "has_fix": self._has_fix}

    def _emit(self):
        if self.on_change:
            try:
                self.on_change(self.get())
            except Exception:
                pass

    def set_camera(self, lat, lon, recenter=True):
        self.lat, self.lon = float(lat), float(lon)
        self._has_fix = True
        if recenter:
            self.clat, self.clon = self.lat, self.lon
            if self.zoom < 15:
                self.zoom = 17
        self.redraw()
        self._emit()

    def set_heading(self, h):
        self.heading = float(h) % 360
        self.redraw()
        self._emit()

    def _mkscale(self, row, label, lo, hi, attr):
        tk.Label(row, text=label, bg=BG, fg=DIM, font=("Segoe UI", 9), width=11,
                 anchor="w").pack(side="left")
        var = tk.DoubleVar(value=getattr(self, attr))

        def on(_v):
            setattr(self, attr, float(var.get()))
            self.redraw()
            self._emit()
        tk.Scale(row, from_=lo, to=hi, orient="horizontal", variable=var, command=on,
                 bg=BG, fg=INK, troughcolor="#0a0e15", highlightthickness=0,
                 relief="flat", showvalue=True).pack(side="left", fill="x", expand=True)

    # -- projection (web mercator, pixels at self.zoom) ------------------
    def _proj(self, lat, lon):
        n = 2.0 ** self.zoom
        x = (lon + 180.0) / 360.0 * n * 256.0
        s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
        y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n * 256.0
        return x, y

    def _unproj(self, px, py):
        n = 2.0 ** self.zoom
        lon = px / (n * 256.0) * 360.0 - 180.0
        ty = py / (n * 256.0)
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty))))
        return lat, lon

    # -- drawing ---------------------------------------------------------
    def redraw(self):
        if self._in_redraw:
            return
        self._in_redraw = True
        try:
            c = self.canvas
            c.delete("all")
            cw, ch = self.cw, self.ch
            cworld = self._proj(self.clat, self.clon)
            ox = cworld[0] - cw / 2.0
            oy = cworld[1] - ch / 2.0
            n = int(2 ** self.zoom)
            ftx = int(math.floor(ox / 256.0))
            fty = int(math.floor(oy / 256.0))
            tx = ftx
            while tx * 256 - ox < cw:
                ty = fty
                while ty * 256 - oy < ch:
                    sx, sy = tx * 256 - ox, ty * 256 - oy
                    if 0 <= ty < n:
                        key = (self.zoom, tx % n, ty)
                        img = self._tiles.get(key)
                        if img is not None:
                            c.create_image(sx, sy, anchor="nw", image=img)
                        else:
                            c.create_rectangle(sx, sy, sx + 256, sy + 256,
                                               fill="#0e1622", outline="")
                            self._fetch(key)
                    ty += 1
                tx += 1
            c.create_text(cw - 3, ch - 3, anchor="se", text="© OpenStreetMap",
                          fill=DIM, font=("Segoe UI", 7))
            # the camera: field-of-view wedge, facing arrow, and two grab points
            pwx, pwy = self._proj(self.lat, self.lon)
            pinx, piny = pwx - ox, pwy - oy
            self._pin = (pinx, piny)
            mpp = 156543.03392 * math.cos(math.radians(self.lat)) / (2 ** self.zoom)
            conelen = max(24.0, (self.reach / mpp) if mpp > 0 else 40.0)
            half = self.fov / 2.0
            pts = [pinx, piny]
            a = self.heading - half
            while a <= self.heading + half + 0.001:
                pts += [pinx + conelen * math.sin(math.radians(a)),
                        piny - conelen * math.cos(math.radians(a))]
                a += 4
            if len(pts) >= 6:
                c.create_polygon(pts, fill=GREEN, stipple="gray25", outline="")
            tipx = pinx + conelen * math.sin(math.radians(self.heading))
            tipy = piny - conelen * math.cos(math.radians(self.heading))
            self._tip = (tipx, tipy)
            c.create_line(pinx, piny, tipx, tipy, fill=GREEN, width=2, arrow="last")
            c.create_oval(tipx - 7, tipy - 7, tipx + 7, tipy + 7, fill="#0b0e13",
                          outline=GREEN, width=2)
            c.create_oval(pinx - 8, piny - 8, pinx + 8, piny + 8, fill=GREEN,
                          outline="#07120c", width=2)
            c.create_oval(pinx - 2, piny - 2, pinx + 2, piny + 2, fill="#07120c",
                          outline="")
            if not self._has_fix:
                c.create_text(cw / 2.0, 13, text="Place it: Find an address, Use my "
                              "location, or drag the dot", fill=AMBER,
                              font=("Segoe UI", 8))
        finally:
            self._in_redraw = False

    def _fetch(self, key):
        if key in self._inflight or key in self._tiles:
            return
        self._inflight.add(key)
        z, x, y = key
        url = TILE_URL.format(z=z, x=x, y=y)

        def work():
            data = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": TILE_UA})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data = r.read()
            except Exception:
                data = None

            def done():
                self._inflight.discard(key)
                if data:
                    try:
                        self._tiles[key] = tk.PhotoImage(
                            data=base64.b64encode(data).decode())
                        self.redraw()
                    except Exception:
                        pass
            try:
                self.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # -- interaction -----------------------------------------------------
    def _press(self, ev):
        tx, ty = self._tip
        px, py = self._pin
        if (ev.x - tx) ** 2 + (ev.y - ty) ** 2 <= 196:
            self._drag = "repoint"
        elif (ev.x - px) ** 2 + (ev.y - py) ** 2 <= 256:
            self._drag = "move"
        else:
            self._drag = ("pan", ev.x, ev.y, self.clat, self.clon)

    def _motion(self, ev):
        d = self._drag
        if not d:
            return
        if d == "repoint":
            px, py = self._pin
            dx, dy = ev.x - px, ev.y - py
            if dx or dy:
                self.heading = math.degrees(math.atan2(dx, -dy)) % 360
                self.redraw()
                self._emit()
        elif d == "move":
            cworld = self._proj(self.clat, self.clon)
            ox = cworld[0] - self.cw / 2.0
            oy = cworld[1] - self.ch / 2.0
            self.lat, self.lon = self._unproj(ev.x + ox, ev.y + oy)
            self._has_fix = True
            self.redraw()
            self._emit()
        elif isinstance(d, tuple) and d[0] == "pan":
            _, sx, sy, clat, clon = d
            sw = self._proj(clat, clon)
            self.clat, self.clon = self._unproj(sw[0] - (ev.x - sx),
                                                sw[1] - (ev.y - sy))
            self.redraw()

    def _zoom(self, dz):
        self.zoom = max(3, min(19, self.zoom + dz))
        self.redraw()

    def _use_location(self):
        self.locbtn.configure(state="disabled", text="\U0001f4cd  Locating…")

        def done(lat, lon, err):
            self.locbtn.configure(state="normal", text="\U0001f4cd  Use my location")
            if err or lat is None:
                self.hint.configure(text="Could not get your location (" + (err or "off")
                                    + "). Use the address Find instead.", fg=AMBER)
                return
            self.set_camera(lat, lon)
            self.hint.configure(text="Placed at your location. Drag the dot to "
                                "fine-tune, drag the arrow tip to aim.", fg=GREEN)
        self.app._win_location(done)


# --------------------------------------------------------------------------
# the system tray (pure ctypes - no extra package in the light download)
# --------------------------------------------------------------------------
class _Tray:
    """A tray icon with a right-click menu, driven from the main thread. A
    message-only window receives the icon's callbacks; App.pump() drains them
    every tick, so there is no second thread touching Tk. Windows only; any
    failure is caught by the caller, which then just closes normally."""

    def __init__(self, icon_path, tip, on_open, on_toggle, on_quit):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.on_open, self.on_toggle, self.on_quit = on_open, on_toggle, on_quit
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        self.s = ctypes.windll.shell32

        LRESULT = ctypes.c_ssize_t
        HWND, UINT, WPARAM, LPARAM = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        DWORD, HICON, LPCWSTR = wintypes.DWORD, wintypes.HICON, wintypes.LPCWSTR
        WCHAR = ctypes.c_wchar

        # res/argtypes matter on 64-bit: a handle returned OR passed as the
        # default c_int is truncated, and every call on it then fails.
        vp = ctypes.c_void_p
        self.k.GetModuleHandleW.restype = wintypes.HMODULE
        self.u.DefWindowProcW.restype = LRESULT
        self.u.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        self.u.CreateWindowExW.restype = HWND
        self.u.CreateWindowExW.argtypes = [DWORD, LPCWSTR, LPCWSTR, DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, HWND, vp, vp, vp]
        self.u.LoadImageW.restype = wintypes.HANDLE
        self.u.LoadImageW.argtypes = [vp, LPCWSTR, UINT, ctypes.c_int, ctypes.c_int, UINT]
        self.u.CreatePopupMenu.restype = wintypes.HMENU
        self.u.AppendMenuW.argtypes = [vp, UINT, vp, LPCWSTR]
        self.u.TrackPopupMenu.argtypes = [vp, UINT, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, vp, vp]
        self.u.DestroyMenu.argtypes = [vp]
        self.u.SetForegroundWindow.argtypes = [vp]
        self.u.PostMessageW.argtypes = [vp, UINT, vp, vp]
        self.u.GetCursorPos.argtypes = [vp]
        self.u.PeekMessageW.argtypes = [vp, vp, UINT, UINT, UINT]
        self.s.Shell_NotifyIconW.argtypes = [DWORD, vp]

        self.WM_TRAY = 0x0400 + 1
        self.ID_OPEN, self.ID_TOGGLE, self.ID_QUIT = 1, 2, 3

        self.WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
        self._proc_ref = self.WNDPROCTYPE(self._wndproc)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", UINT), ("lpfnWndProc", self.WNDPROCTYPE),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HMODULE), ("hIcon", HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", LPCWSTR), ("lpszClassName", LPCWSTR)]

        class NID(ctypes.Structure):
            _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT),
                        ("uFlags", UINT), ("uCallbackMessage", UINT), ("hIcon", HICON),
                        ("szTip", WCHAR * 128), ("dwState", DWORD), ("dwStateMask", DWORD),
                        ("szInfo", WCHAR * 256), ("uVersion", UINT),
                        ("szInfoTitle", WCHAR * 64), ("dwInfoFlags", DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", HICON)]
        self._NID = NID

        hinst = self.k.GetModuleHandleW(None)
        self._wc = WNDCLASS()
        self._wc.lpfnWndProc = self._proc_ref
        self._wc.hInstance = hinst
        self._wc.lpszClassName = "SparrowMapTrayWin"
        self.u.RegisterClassW(ctypes.byref(self._wc))
        HWND_MESSAGE = wintypes.HWND(-3)
        self.hwnd = self.u.CreateWindowExW(0, "SparrowMapTrayWin", "SparrowMap",
                                           0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)

        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        self.hicon = self.u.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)

        self.nid = NID()
        self.nid.cbSize = ctypes.sizeof(NID)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = 0x1 | 0x2 | 0x4          # MESSAGE | ICON | TIP
        self.nid.uCallbackMessage = self.WM_TRAY
        self.nid.hIcon = self.hicon
        self.nid.szTip = tip[:127]
        self.s.Shell_NotifyIconW(0, ctypes.byref(self.nid))   # NIM_ADD

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == self.WM_TRAY:
                low = lp & 0xFFFF
                if low == 0x0202:              # WM_LBUTTONUP - open
                    self.on_open()
                elif low in (0x0205, 0x007B):  # WM_RBUTTONUP / WM_CONTEXTMENU - menu
                    self._menu()
                return 0
        except Exception:
            pass
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu(self):
        ct = self.ct
        running = False
        try:
            running = bool(self.on_toggle(peek=True))
        except Exception:
            pass
        hmenu = self.u.CreatePopupMenu()
        MF_STRING = 0x0
        self.u.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN, "Open SparrowMap")
        self.u.AppendMenuW(hmenu, MF_STRING, self.ID_TOGGLE,
                           "Stop all cameras" if running else "Start all cameras")
        self.u.AppendMenuW(hmenu, 0x800, 0, None)      # MF_SEPARATOR
        self.u.AppendMenuW(hmenu, MF_STRING, self.ID_QUIT, "Quit")

        class POINT(ct.Structure):
            _fields_ = [("x", ct.c_long), ("y", ct.c_long)]
        pt = POINT()
        self.u.GetCursorPos(ct.byref(pt))
        self.u.SetForegroundWindow(self.hwnd)
        # TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY
        cmd = self.u.TrackPopupMenu(hmenu, 0x2 | 0x100 | 0x80, pt.x, pt.y, 0, self.hwnd, None)
        self.u.PostMessageW(self.hwnd, 0, 0, 0)        # WM_NULL - dismiss quirk
        self.u.DestroyMenu(hmenu)
        if cmd == self.ID_OPEN:
            self.on_open()
        elif cmd == self.ID_TOGGLE:
            self.on_toggle()
        elif cmd == self.ID_QUIT:
            self.on_quit()

    def notify(self, title, text):
        ct = self.ct
        self.nid.uFlags = 0x1 | 0x2 | 0x4 | 0x10       # + NIF_INFO
        self.nid.szInfo = text[:255]
        self.nid.szInfoTitle = title[:63]
        self.nid.dwInfoFlags = 0x1                      # NIIF_INFO
        self.s.Shell_NotifyIconW(1, ct.byref(self.nid))  # NIM_MODIFY
        self.nid.uFlags = 0x1 | 0x2 | 0x4

    def pump(self):
        ct = self.ct
        msg = self.wt.MSG()
        # Only our own window's messages, so Tk's loop is untouched.
        while self.u.PeekMessageW(ct.byref(msg), self.hwnd, 0, 0, 1):
            self.u.TranslateMessage(ct.byref(msg))
            self.u.DispatchMessageW(ct.byref(msg))

    def remove(self):
        try:
            self.s.Shell_NotifyIconW(2, self.ct.byref(self.nid))   # NIM_DELETE
        except Exception:
            pass
        try:
            self.u.DestroyWindow(self.hwnd)
        except Exception:
            pass


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s %s" % (APP, APP_VERSION))
        self._set_icon()
        self.configure(bg=BG)
        self.geometry("1000x720")
        self.minsize(760, 520)
        self.cfg = load_cfg()
        self.cameras = self.cfg["cameras"]
        self.q: "queue.Queue" = queue.Queue()
        self.workers: dict = {}
        self.shared = {"sess": None, "iname": None, "hub": self.cfg.get("hub", HUB_DEFAULT)}
        self.tiles: dict = {}
        self.imgs: dict = {}          # keep PhotoImage refs alive
        self.view = ("grid", None)
        self.model_ready = False
        self.live = {}                       # cid -> is the source live right now
        self._probing = False
        self.tray = None
        self._launch_tray = ("--tray" in sys.argv)
        self._build_chrome()
        self._show_grid()
        self._init_tray()
        self.after(150, self._pump)
        self.after(1500, self._poll_live)
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        threading.Thread(target=self._load_model, daemon=True).start()
        if getattr(sys, "frozen", False) or os.environ.get("SPARROW_UPDATE_TARGET"):
            # Only a real installed exe can swap itself; running from source has
            # nothing to replace. SPARROW_UPDATE_TARGET lets a test opt in.
            threading.Thread(target=self._check_update, daemon=True).start()
        if self._launch_tray and self.tray:
            # Started with Windows: come up hidden in the tray, cameras resume
            # once the detector has loaded (see the "ready" case in _pump).
            self.after(200, self.withdraw)

    def _init_tray(self):
        """Put the sparrow in the system tray so the X can hide-not-quit and the
        app can live there when it starts with Windows. Any failure leaves
        self.tray None and the X closes the app the old way."""
        if sys.platform != "win32":
            return
        try:
            def toggle(peek=False):
                if peek:
                    return any(w.is_alive() for w in self.workers.values())
                self.after(0, self._toggle_all)
            self.tray = _Tray(_resource("favicon.ico"), "SparrowMap",
                              on_open=lambda: self.after(0, self._show_window),
                              on_toggle=toggle,
                              on_quit=lambda: self.after(0, self._quit))
        except Exception:
            self.tray = None

    def _set_icon(self):
        """Wear the SparrowMap sparrow-in-a-pin, not Tk's default feather."""
        try:
            self.iconbitmap(default=_resource("favicon.ico"))
        except Exception:
            pass
        try:                              # taskbar/alt-tab on top of the title bar
            if HAVE_PIL:
                self._icon_img = ImageTk.PhotoImage(PILImage.open(_resource("icon-192.png")))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    # -- window / tray ---------------------------------------------------
    def _workarea(self):
        """Usable size of the monitor this window is on (minus the taskbar), so
        a dialog never opens bigger than the screen - his touch screen is only
        1024x600. Falls back to the whole screen if the monitor query fails."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                u.MonitorFromWindow.restype = ctypes.c_void_p
                u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_long]

                class RECT(ctypes.Structure):
                    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

                class MI(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]
                hmon = u.MonitorFromWindow(ctypes.c_void_p(self.winfo_id()), 2)   # NEAREST
                mi = MI()
                mi.cbSize = ctypes.sizeof(MI)
                u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    w = mi.rcWork.right - mi.rcWork.left
                    h = mi.rcWork.bottom - mi.rcWork.top
                    if w > 0 and h > 0:
                        return int(w * 0.96), int(h * 0.94)
        except Exception:
            pass
        return (max(360, self.winfo_screenwidth() - 80),
                max(360, self.winfo_screenheight() - 120))

    def _dialog_geometry(self, prefer_w, prefer_h, key):
        """A 'WxH' string: the remembered size if there is one, else the
        preferred size, always clamped to the monitor it opens on."""
        rw, rh = prefer_w, prefer_h
        saved = (self.cfg.get(key) or "").split("x")
        if len(saved) == 2:
            try:
                rw, rh = int(saved[0]), int(saved[1])
            except ValueError:
                rw, rh = prefer_w, prefer_h
        aw, ah = self._workarea()
        return "%dx%d" % (max(360, min(rw, aw)), max(360, min(rh, ah)))

    def _show_window(self):
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    def _hide_to_tray(self):
        """The X hides to the tray and leaves the cameras running. With no tray
        (a tray that failed to start, or non-Windows) it closes for real so the
        X is never a dead button."""
        if not self.tray:
            return self._quit()
        self.withdraw()
        if not self.cfg.get("tray_notice_shown"):
            self.cfg["tray_notice_shown"] = True
            save_cfg(self.cfg)
            try:
                self.tray.notify("SparrowMap is still watching",
                                 "Your cameras keep running. Right-click the tray "
                                 "icon to open it again or quit.")
            except Exception:
                pass

    def _quit(self):
        for w in self.workers.values():
            w.stop.set()
        if self.tray:
            try:
                self.tray.remove()
            except Exception:
                pass
        self.destroy()

    # -- updates ---------------------------------------------------------
    def _check_update(self, force=False):
        """Ask the hub for version.json; offer an update if it is newer. Runs off
        the UI thread. `force` is the manual 'Check for updates' button and skips
        the once-a-day guard."""
        import datetime
        today = datetime.date.today().isoformat()
        if not force and self.cfg.get("last_update_check") == today:
            return
        info = fetch_version_info()
        self.cfg["last_update_check"] = today
        try:
            save_cfg(self.cfg)
        except Exception:
            pass
        if info and _version_newer(info.get("version"), APP_VERSION):
            self.q.put(("update", None, info))
        elif force:
            self.q.put(("banner", None, "SparrowMap is up to date (%s)." % APP_VERSION))

    def _show_update_banner(self, info):
        if getattr(self, "_update_bar", None):
            try:
                self._update_bar.destroy()
            except Exception:
                pass
        bar = tk.Frame(self, bg="#15324a")
        self._update_bar = bar
        notes = (info.get("notes") or "A new version is available.").strip()
        ver = info.get("version", "")
        tk.Label(bar, text="Update available (%s): %s" % (ver, notes), bg="#15324a",
                 fg=INK, font=("Segoe UI", 9), anchor="w").pack(side="left", padx=12, pady=6)
        tk.Button(bar, text="Later", command=bar.destroy, bg="#15324a", fg=DIM,
                  relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  activebackground="#1b3a55", activeforeground=INK
                  ).pack(side="right", padx=(0, 10))
        self._update_btn = tk.Button(bar, text="Update now",
                                     command=lambda: self._do_update(info), bg=GREEN,
                                     fg="#0b0e13", relief="flat", cursor="hand2",
                                     font=("Segoe UI", 9, "bold"),
                                     activebackground="#5fe8ae")
        self._update_btn.pack(side="right", padx=8, pady=5, ipadx=4)
        bar.pack(fill="x", after=self.banner)

    def _do_update(self, info):
        url = info.get("url")
        if not url:
            self.banner.configure(text="This update has no download link.")
            return
        if getattr(self, "_update_btn", None):
            try:
                self._update_btn.configure(state="disabled", text="Updating…")
            except Exception:
                pass
        threading.Thread(target=self._run_update,
                         args=(url, info.get("sha256")), daemon=True).start()

    def _run_update(self, url, sha):
        import tempfile
        target = os.environ.get("SPARROW_UPDATE_TARGET") or sys.executable
        dest = os.path.join(tempfile.gettempdir(), "SparrowMap.update.exe")
        ok, msg = download_and_verify(url, sha, dest)
        if not ok:
            self.q.put(("banner", None, "Update stopped: " + msg))
            self.after(0, self._reenable_update)
            return
        try:
            bat = write_swap_helper(dest, target)
            _spawn_detached(["cmd", "/c", bat])
        except Exception as ex:
            self.q.put(("banner", None,
                        "Update could not start (%s)." % ex.__class__.__name__))
            self.after(0, self._reenable_update)
            return
        # Quit so the exe unlocks; the helper swaps it and relaunches to the tray.
        self.after(0, self._quit)

    def _reenable_update(self):
        if getattr(self, "_update_btn", None):
            try:
                self._update_btn.configure(state="normal", text="Update now")
            except Exception:
                pass

    # -- model -----------------------------------------------------------
    def _load_model(self):
        try:
            from detect import relay
            import onnxruntime as ort
            self.q.put(("banner", None, "loading the detector…"))
            mp = relay.model_path(self.shared["hub"])
            sess = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
            relay.SIZE = int(sess.get_inputs()[0].shape[-1])
            self.shared["sess"] = sess
            self.shared["iname"] = sess.get_inputs()[0].name
            self.model_ready = True
            self.q.put(("banner", None, ""))
            self.q.put(("ready", None, None))
        except Exception as exc:
            self.q.put(("banner", None, f"detector failed to load: {exc}"))

    # -- chrome ----------------------------------------------------------
    def _build_chrome(self):
        head = tk.Frame(self, bg=PANEL, height=56)
        head.pack(fill="x")
        tk.Label(head, text="SPARROWMAP", bg=PANEL, fg=INK,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14, pady=14)
        self.btn_all = tk.Button(head, text="Start all", command=self._toggle_all,
                                 bg=GREEN, fg="#0b0e13", relief="flat", cursor="hand2",
                                 font=("Segoe UI", 10, "bold"), activebackground="#5fe8ae")
        self.btn_all.pack(side="right", padx=(6, 14), pady=11, ipadx=6)
        tk.Button(head, text="+ Add camera", command=self._add_dialog, bg="#141c28",
                  fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="right", padx=6, pady=11, ipadx=4)
        tk.Button(head, text="⚙  Settings", command=self._app_settings_dialog,
                  bg="#141c28", fg=INK, relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), activebackground="#1b2432",
                  activeforeground=INK).pack(side="right", padx=6, pady=11, ipadx=4)
        self.banner = tk.Label(self, text="", bg=PANEL, fg=AMBER,
                               font=("Segoe UI", 9), anchor="w")
        self.banner.pack(fill="x")
        tk.Label(self, text="This computer watches your cameras. No video leaves it.",
                 bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="w"
                 ).pack(fill="x", padx=14, pady=(0, 8))

        nav = tk.Frame(self, bg=PANEL)
        nav.pack(fill="x", side="bottom")
        for label, path in (("Map", "/"), ("Driving", "/drive"), ("Review", "/rv"),
                            ("Aim", "/aim"), ("Status", "/status")):
            if path == "/aim":
                cmd = self._aim_nav
            elif path == "/rv":
                cmd = self._review_dialog
            else:
                cmd = (lambda p=path: self._open(p))
            tk.Button(nav, text=label, command=cmd,
                      bg=PANEL, fg=GREEN, relief="flat", cursor="hand2",
                      font=("Segoe UI", 9, "bold"), activebackground="#1b2432",
                      activeforeground=INK).pack(side="left", expand=True, fill="x", pady=8)

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.tiles.clear()

    # -- grid (the wall) -------------------------------------------------
    def _show_grid(self):
        self.view = ("grid", None)
        self._clear_body()
        if not self.cameras:
            tk.Label(self.body, text="No cameras yet.", bg=BG, fg=DIM,
                     font=("Segoe UI", 12)).pack(pady=(40, 4))
            tk.Label(self.body, text="One button finds your cameras, places them, and "
                     "sets them up here. No website, no copying.", bg=BG, fg=DIM,
                     font=("Segoe UI", 9), wraplength=420, justify="center").pack(pady=(0, 10))
            tk.Button(self.body, text="Set up a camera  →", command=self._add_dialog,
                      bg=GREEN, fg="#0b0e13", relief="flat", cursor="hand2",
                      font=("Segoe UI", 11, "bold"), activebackground="#5fe8ae"
                      ).pack(ipadx=8, ipady=4)
            return
        n = len(self.cameras)
        cols = 1 if n == 1 else (2 if n <= 4 else 3)
        grid = tk.Frame(self.body, bg=BG)
        grid.pack(fill="both", expand=True, padx=10, pady=10)
        for i in range(cols):
            grid.columnconfigure(i, weight=1, uniform="cam")
        for idx, cam in enumerate(self.cameras):
            self._make_tile(grid, cam, idx // cols, idx % cols)

    def _make_tile(self, parent, cam, r, c):
        cid = cam["id"]
        tile = tk.Frame(parent, bg=PANEL, highlightthickness=1,
                        highlightbackground=LINE)
        tile.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
        parent.rowconfigure(r, weight=1)
        img = tk.Label(tile, bg="#000000", fg=DIM, cursor="hand2",
                       text="● connecting…", font=("Segoe UI", 9))
        img.pack(fill="both", expand=True, padx=1, pady=1)
        img.bind("<Button-1>", lambda e, i=cid: self._show_single(i))
        img.bind("<Configure>", lambda e, i=cid: self._fit(i, e.width, e.height))
        bar = tk.Frame(tile, bg=PANEL)
        bar.pack(fill="x")
        name = tk.Label(bar, text=cam.get("name", cid), bg=PANEL, fg=INK,
                        font=("Segoe UI", 9, "bold"), anchor="w")
        name.pack(side="left", padx=8, pady=5)
        on = (cid in self.workers and self.workers[cid].is_alive()) or bool(self.live.get(cid))
        status = tk.Label(bar, text="running" if on else "stopped", bg=PANEL,
                          fg=GREEN if on else DIM, font=("Segoe UI", 9), anchor="e")
        status.pack(side="right", padx=8)
        self.tiles[cid] = {"img": img, "status": status, "w": 300}
        if cid in self.workers and self.workers[cid].is_alive():
            self.workers[cid].preview_w = 300

    # -- single view -----------------------------------------------------
    def _show_single(self, cid):
        cam = next((c for c in self.cameras if c["id"] == cid), None)
        if not cam:
            return
        self.view = ("single", cid)
        self._clear_body()
        top = tk.Frame(self.body, bg=BG)
        top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Button(top, text="← All cameras", command=self._show_grid, bg="#141c28",
                  fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  activebackground="#1b2432", activeforeground=INK).pack(side="left")
        tk.Label(top, text=cam.get("name", cid), bg=BG, fg=INK,
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=12)
        running = cid in self.workers and self.workers[cid].is_alive()
        self.single_btn = tk.Button(
            top, text="Stop" if running else "Watch", command=lambda: self._toggle_one(cid),
            bg=("#141c28" if running else GREEN), fg=(INK if running else "#0b0e13"),
            relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"))
        self.single_btn.pack(side="right")
        tk.Button(top, text="Remove", command=lambda: self._remove(cid), bg="#141c28",
                  fg=RED, relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  activebackground="#1b2432").pack(side="right", padx=8)
        tk.Button(top, text="⚙  Aim & settings", command=lambda: self._settings_dialog(cid),
                  bg="#141c28", fg=INK, relief="flat", cursor="hand2",
                  font=("Segoe UI", 9), activebackground="#1b2432",
                  activeforeground=INK).pack(side="right", padx=8)

        img = tk.Label(self.body, bg="#000000", fg=DIM, text="● connecting…",
                       font=("Segoe UI", 10))
        img.pack(fill="both", expand=True, padx=12, pady=6)
        img.bind("<Configure>", lambda e, i=cid: self._fit(i, e.width, e.height))
        status = tk.Label(self.body, text="", bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w")
        status.pack(fill="x", padx=12)
        logw = tk.Text(self.body, height=6, bg="#080b10", fg="#b9c6d8", relief="flat",
                       font=("Consolas", 9), highlightthickness=1,
                       highlightbackground=LINE, wrap="word")
        logw.pack(fill="x", padx=12, pady=(4, 10))
        logw.configure(state="disabled")
        self.tiles[cid] = {"img": img, "status": status, "log": logw, "w": 760}
        if running:
            self.workers[cid].preview_w = 760

    def _fit(self, cid, w, h):
        """Size the preview to fill its tile (width, capped so a ~16:9 frame
        still fits the height), and rescale live as the window resizes."""
        w = max(160, min(int(w), int(h * 16 / 9)))
        t = self.tiles.get(cid)
        if t:
            t["w"] = w
        wk = self.workers.get(cid)
        if wk and wk.is_alive():
            wk.preview_w = w

    # -- start / stop ----------------------------------------------------
    def _start_one(self, cam):
        if not self.model_ready:
            self.banner.configure(text="still loading the detector…")
            return
        cid = cam["id"]
        if cid in self.workers and self.workers[cid].is_alive():
            return
        w = CameraWorker(cam, self.shared, self.q)
        if cid in self.tiles:
            w.preview_w = self.tiles[cid]["w"]
        self.workers[cid] = w
        w.start()

    def _stop_one(self, cid):
        w = self.workers.get(cid)
        if w and w.is_alive():
            w.stop.set()

    def _toggle_one(self, cid):
        cam = next((c for c in self.cameras if c["id"] == cid), None)
        if not cam:
            return
        if cid in self.workers and self.workers[cid].is_alive():
            self._stop_one(cid)
        else:
            self._start_one(cam)

    def _toggle_all(self):
        any_running = any(w.is_alive() for w in self.workers.values())
        if any_running:
            for cid in list(self.workers):
                self._stop_one(cid)
            self.btn_all.configure(text="Start all", bg=GREEN)
        else:
            for cam in self.cameras:
                self._start_one(cam)
            self.btn_all.configure(text="Stop all", bg="#141c28", fg=INK)

    # -- add / remove ----------------------------------------------------
    def _add_dialog(self):
        """The WHOLE camera setup, inside the app - no website, no copy/paste.
        Find cameras on the network (the local relay's streams + RTSP hosts),
        pick one with a live thumbnail, place it by address, test, and Add. Add
        enrols the camera with the server right here and stores its keys
        silently; they are never shown for copying."""
        d = tk.Toplevel(self)
        d.title("Set up a camera")
        d.configure(bg=BG)
        d.geometry("500x760")
        d.minsize(440, 560)
        d.transient(self)
        try:
            d.iconbitmap(default=_resource("favicon.ico"))
        except Exception:
            pass

        picked = {"source": ""}

        # Add / Cancel sit in a bottom bar packed FIRST, so they stay on screen
        # however tall the found-cameras list grows (the old dialog could push
        # its submit button off the bottom - the bug this whole rewrite fixes).
        bottom = tk.Frame(d, bg=PANEL)
        bottom.pack(side="bottom", fill="x")
        addstat = tk.Label(bottom, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                           anchor="w", wraplength=460, justify="left")
        addstat.pack(fill="x", padx=16, pady=(6, 0))
        addbtn = tk.Button(bottom, text="＋  Add this camera", bg=GREEN, fg="#0b0e13",
                           relief="flat", cursor="hand2", font=("Segoe UI", 11, "bold"),
                           activebackground="#5fe8ae")
        addbtn.pack(fill="x", padx=16, pady=(6, 6), ipady=8)
        tk.Button(bottom, text="Cancel", command=d.destroy, bg=PANEL, fg=DIM,
                  relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  activebackground=PANEL, activeforeground=INK).pack(pady=(0, 8))

        # A scrollable middle, so the found list + preview + address never clip.
        canvas = tk.Canvas(d, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(d, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        def close():
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            d.destroy()
        d.protocol("WM_DELETE_WINDOW", close)

        fields = {}

        def _field(parent, key, label):
            tk.Label(parent, text=label, bg=BG, fg=DIM, font=("Segoe UI", 9),
                     anchor="w").pack(fill="x", padx=16, pady=(10, 2))
            e = tk.Entry(parent, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                         font=("Consolas", 10), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor="#3d8cff")
            e.pack(fill="x", padx=16, ipady=4)
            fields[key] = e
            return e

        _field(body, "name", "Name it  (e.g. Front door)")

        # 1) FIND. One button scans the LAN for RTSP cameras and asks the local
        # relay (go2rtc) for its ready-made streams, then lists them to pick.
        tk.Label(body, text="1 · Find your camera", bg=BG, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w"
                 ).pack(fill="x", padx=16, pady=(14, 2))
        scanbtn = tk.Button(body, text="\U0001f50d  Scan my network", bg="#141c28", fg=INK,
                            relief="flat", cursor="hand2", font=("Segoe UI", 10),
                            activebackground="#1b2432", activeforeground=INK)
        scanbtn.pack(fill="x", padx=16, ipady=4)
        scanstat = tk.Label(body, text="", bg=BG, fg=DIM, font=("Segoe UI", 9),
                            anchor="w", wraplength=460, justify="left")
        scanstat.pack(fill="x", padx=16, pady=(4, 2))
        candf = tk.Frame(body, bg=BG)
        candf.pack(fill="x", padx=16)

        srcentry = _field(body, "source",
                          "Stream address  (filled by a pick above, or type rtsp://… / 0 for USB)")

        thumbwrap = tk.Label(body, bg="#000000", fg=DIM, text="", font=("Segoe UI", 9))

        def set_thumb(b64):
            if not b64:
                thumbwrap.configure(image="", text="(no picture yet - check the login/path)",
                                    height=4, fg=AMBER)
                thumbwrap.pack(fill="x", padx=16, pady=(8, 2))
                return
            try:
                img = tk.PhotoImage(data=b64)
                d._thumb = img
                thumbwrap.configure(image=img, text="", height=0)
                thumbwrap.pack(fill="x", padx=16, pady=(8, 2))
            except Exception:
                pass

        def grab_thumb(src):
            thumbwrap.configure(image="", text="● opening a preview…", height=4, fg=DIM)
            thumbwrap.pack(fill="x", padx=16, pady=(8, 2))

            def work():
                b64 = None
                try:
                    import cv2
                    from detect import relay
                    s = int(src) if str(src).strip().isdigit() else src
                    cap = relay.open_stream(s)
                    if cap is not None:
                        ok, frame = False, None
                        for _ in range(4):
                            ok, frame = cap.read()
                            if ok:
                                break
                        try:
                            cap.release()
                        except Exception:
                            pass
                        if ok and frame is not None:
                            h, w = frame.shape[:2]
                            tw = 320
                            th = max(1, int(h * tw / w))
                            frame = cv2.resize(frame, (tw, th))
                            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            good, buf = cv2.imencode(".png", rgb)
                            if good:
                                b64 = base64.b64encode(buf).decode()
                except Exception:
                    b64 = None
                self.after(0, lambda: set_thumb(b64))
            threading.Thread(target=work, daemon=True).start()

        def pick(cand):
            picked["source"] = cand["source"]
            srcentry.delete(0, "end")
            srcentry.insert(0, cand["source"])
            if not fields["name"].get().strip() and cand.get("name"):
                fields["name"].insert(0, cand["name"])
            grab_thumb(cand["source"])

        def show_cands(cands):
            for w in candf.winfo_children():
                w.destroy()
            # A short note on where each kind came from, so a pick is not a mystery.
            pickable = [c for c in cands if c.get("where") != "hint"]
            brandcams = [c for c in cands if c.get("where") == "hint"]
            nrelay = sum(1 for c in cands if c.get("where") == "relay")
            nnet = sum(1 for c in cands if c.get("where") == "network")
            ncam = sum(1 for c in cands if c.get("where") == "webcam")
            parts = []
            if nrelay:
                parts.append("%d on the relay" % nrelay)
            if nnet:
                parts.append("%d on your network" % nnet)
            parts.append("%d webcam%s" % (ncam, "" if ncam == 1 else "s")
                         if ncam else "no webcam found")
            if brandcams:
                parts.append("%d brand camera%s that need a step"
                             % (len(brandcams), "" if len(brandcams) == 1 else "s"))
            note = "Found: " + ", ".join(parts) + "."
            if not cands:
                scanstat.configure(text=note + " Type the stream address below, or plug "
                                        "in a webcam and scan again.", fg=AMBER)
                return
            if pickable:
                scanstat.configure(text=note + "  Pick one (a preview opens below):",
                                   fg=DIM)
            else:
                scanstat.configure(text=note + "  Each needs a step first, then Scan "
                                        "again:", fg=DIM)
            for c in pickable:
                tk.Button(candf, text=c["label"], anchor="w", bg="#0a0e15", fg=INK,
                          relief="flat", cursor="hand2", justify="left",
                          font=("Segoe UI", 9), activebackground="#1b2432",
                          activeforeground=INK, command=lambda cc=c: pick(cc)
                          ).pack(fill="x", padx=2, pady=1, ipady=3)
            # Cameras we recognised by maker but that give no RTSP yet: show the
            # step to take, not a dead "pick" that cannot open.
            if brandcams:
                tk.Label(candf, text="These are cameras, but need a step first:",
                         bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w"
                         ).pack(fill="x", padx=2, pady=(8, 1))
                for c in brandcams:
                    fg = AMBER if c.get("kind") == "cloud" else "#9fe6c4"
                    tk.Label(candf, text=c["label"], anchor="w", bg="#0a0e15", fg=fg,
                             justify="left", font=("Segoe UI", 8), wraplength=430
                             ).pack(fill="x", padx=2, pady=1, ipady=2)

        def scan():
            scanbtn.configure(state="disabled", text="Scanning…")
            scanstat.configure(text="Looking for cameras on your network and local "
                                    "relay… (a few seconds)", fg=AMBER)

            def work():
                cands = self._scan_candidates()
                self.after(0, lambda: (scanbtn.configure(state="normal",
                           text="\U0001f50d  Scan again"), show_cands(cands)))
            threading.Thread(target=work, daemon=True).start()
        scanbtn.configure(command=scan)

        # An explicit Test too (the thumbnail already proves a pick, but a
        # hand-typed address wants a check before Add).
        def test():
            src = srcentry.get().strip()
            if not src:
                teststat.configure(text="Pick a camera above or type its address first.",
                                   fg=AMBER)
                return
            grab_thumb(src)
            testbtn.configure(state="disabled", text="Testing…")
            teststat.configure(text="Opening the stream… a relay/ONVIF camera can take "
                                    "~10s the first time.", fg=AMBER)

            def work():
                ok = False
                try:
                    from detect import relay
                    s = int(src) if src.isdigit() else src
                    cap = relay.open_stream(s)
                    ok = cap is not None
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                except Exception:
                    ok = False

                def done():
                    testbtn.configure(state="normal", text="Test connection")
                    teststat.configure(
                        text=("✓ It opened - the stream is good." if ok else
                              "✗ Could not open it. Check the login/path, or that the "
                              "camera (or its relay) is on."),
                        fg=(GREEN if ok else RED))
                self.after(0, done)
            threading.Thread(target=work, daemon=True).start()
        testbtn = tk.Button(body, text="Test connection", command=test, bg="#141c28",
                            fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 9),
                            activebackground="#1b2432", activeforeground=INK)
        testbtn.pack(fill="x", padx=16, pady=(8, 0), ipady=3)
        teststat = tk.Label(body, text="", bg=BG, fg=DIM, font=("Segoe UI", 9),
                            anchor="w", wraplength=460, justify="left")
        teststat.pack(fill="x", padx=16, pady=(4, 2))

        # 2) PLACE IT by address (geocode proxied by our hub, same as the site).
        tk.Label(body, text="2 · Where is it?", bg=BG, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w"
                 ).pack(fill="x", padx=16, pady=(14, 2))
        _field(body, "address", "Type an address or place, then Find")
        addrstat = tk.Label(body, text="", bg=BG, fg=DIM, font=("Segoe UI", 9),
                            anchor="w", wraplength=460, justify="left")
        hitsf = tk.Frame(body, bg=BG)

        def find():
            qy = fields["address"].get().strip()
            if len(qy) < 3:
                addrstat.configure(text="Type a bit more of the address.", fg=AMBER)
                return
            findbtn.configure(state="disabled", text="Finding…")
            addrstat.configure(text="Searching…", fg=AMBER)

            def work():
                results, err = [], None
                try:
                    url = (self._hub().rstrip("/") + "/api/geocode?q="
                           + urllib.parse.quote(qy))
                    with urllib.request.urlopen(url, timeout=15) as r:
                        data = json.loads(r.read().decode())
                    results = data.get("results") or []
                    err = data.get("error")
                except Exception:
                    err = "Could not search just now. Type lat/lon below by hand."
                self.after(0, lambda: show(results, err))

            def show(results, err):
                findbtn.configure(state="normal", text="Find")
                for w in hitsf.winfo_children():
                    w.destroy()
                if err:
                    addrstat.configure(text=err, fg=AMBER)
                    return
                if not results:
                    addrstat.configure(text="Nothing found. Try street + town without a "
                                            "unit number, or type lat/lon below.", fg=AMBER)
                    return
                addrstat.configure(text="Pick the right one:", fg=DIM)
                for r in results[:6]:
                    tk.Button(hitsf, text=r.get("name", ""), anchor="w", bg="#0a0e15",
                              fg=INK, relief="flat", cursor="hand2", justify="left",
                              font=("Segoe UI", 9), activebackground="#1b2432",
                              activeforeground=INK, command=lambda rr=r: pickloc(rr)
                              ).pack(fill="x", padx=2, pady=1, ipady=2)

            def pickloc(r):
                for key, v in (("lat", r.get("lat")), ("lon", r.get("lon"))):
                    fields[key].delete(0, "end")
                    if v is not None:
                        fields[key].insert(0, f"{float(v):.6f}")
                for w in hitsf.winfo_children():
                    w.destroy()
                try:
                    amap.set_camera(float(r.get("lat")), float(r.get("lon")))
                except (TypeError, ValueError):
                    pass
                addrstat.configure(text="✓ Placed on the map below - drag the dot to "
                                        "fine-tune and the arrow to aim it.", fg=GREEN)
            threading.Thread(target=work, daemon=True).start()
        findbtn = tk.Button(body, text="Find", command=find, bg="#141c28", fg=INK,
                            relief="flat", cursor="hand2", font=("Segoe UI", 9),
                            activebackground="#1b2432", activeforeground=INK)
        findbtn.pack(fill="x", padx=16, pady=(8, 0), ipady=3)
        addrstat.pack(fill="x", padx=16, pady=(4, 2))
        hitsf.pack(fill="x", padx=16)

        latlon = tk.Frame(body, bg=BG)
        latlon.pack(fill="x", padx=16, pady=(6, 10))
        for key, lab in (("lat", "Latitude"), ("lon", "Longitude")):
            col = tk.Frame(latlon, bg=BG)
            col.pack(side="left", expand=True, fill="x",
                     padx=(0, 8) if key == "lat" else 0)
            tk.Label(col, text=lab, bg=BG, fg=DIM, font=("Segoe UI", 9),
                     anchor="w").pack(fill="x")
            e = tk.Entry(col, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                         font=("Consolas", 10), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor="#3d8cff")
            e.pack(fill="x", ipady=4)
            fields[key] = e

        # 3 · Aim it - the same drag-to-place / drag-to-aim map the website has,
        # right here before Add. Drag the dot to move, the arrow tip to repoint.
        tk.Label(body, text="3 · Aim it", bg=BG, fg=INK,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(
                     fill="x", padx=16, pady=(6, 0))
        tk.Label(body, text="Point the arrow the way the camera looks. Place it first "
                 "(Find, Use my location, or drag the dot).", bg=BG, fg=DIM,
                 font=("Segoe UI", 9), anchor="w", justify="left", wraplength=470
                 ).pack(fill="x", padx=16, pady=(0, 4))

        def on_map(st):
            # keep the lat/lon fields in step with the dot, so do_add and a later
            # edit both read the same spot.
            if st.get("lat") is not None:
                for key, v in (("lat", st["lat"]), ("lon", st["lon"])):
                    fields[key].delete(0, "end")
                    fields[key].insert(0, f"{float(v):.6f}")

        _la = fields["lat"].get().strip()
        _lo = fields["lon"].get().strip()
        try:
            _la, _lo = float(_la), float(_lo)
        except ValueError:
            _la, _lo = None, None
        amap = AimMap(body, self, _la, _lo, on_change=on_map, width=470, height=250)
        amap.pack(fill="x", padx=16, pady=(0, 10))

        def latlon_typed(_=None):
            try:
                amap.set_camera(float(fields["lat"].get().strip()),
                                float(fields["lon"].get().strip()))
            except ValueError:
                pass
        for key in ("lat", "lon"):
            fields[key].bind("<Return>", latlon_typed)
            fields[key].bind("<FocusOut>", latlon_typed)

        # ADD: enrol with the server HERE, store the keys silently, start watching.
        def do_add():
            src = srcentry.get().strip()
            if not src:
                addstat.configure(text="Pick a camera or type its address first.", fg=AMBER)
                return
            name = fields["name"].get().strip() or f"Camera {len(self.cameras) + 1}"
            m = amap.get()
            if not m["has_fix"]:
                addstat.configure(text="Place the camera on the map first (step 3) - "
                                       "Find, Use my location, or drag the dot.", fg=AMBER)
                return
            lat, lon = m["lat"], m["lon"]
            heading, fov, reach = m["heading"], m["fov"], m["reach"]
            addbtn.configure(state="disabled", text="Adding…")
            addstat.configure(text="Registering the camera with SparrowMap…", fg=AMBER)

            def done(res, err):
                if err or not res:
                    addbtn.configure(state="normal", text="＋  Add this camera")
                    addstat.configure(text="Could not register it: "
                                      + (err or "no reply") + ". Try Add again.", fg=RED)
                    return
                cam = {"id": _new_id(self.cameras), "name": name, "source": src,
                       "node": res.get("id", ""), "token": res.get("token", ""),
                       "lat": lat, "lon": lon, "heading": heading, "fov": fov,
                       "reach_m": reach}
                self.cameras.append(cam)
                if res.get("review_token"):
                    self.cfg.setdefault("review_tokens", {})[cam["id"]] = res["review_token"]
                self._persist()
                close()
                self._show_grid()
                self._start_one(cam)
                messagebox.showinfo(APP, 'Added "' + name + '".\n\n'
                    "It is registered, connecting now, and will show up on the wall "
                    "and on the map at %.5f, %.5f.\n\n"
                    "Nothing was copied or pasted - its keys are kept inside the app."
                    % (lat, lon))
            self._enroll_node(name, lat, lon, done, heading=heading, fov=fov,
                              reach_m=reach)
        addbtn.configure(command=do_add)

    def _scan_candidates(self):
        """Cameras we can offer without the user knowing an address: the local
        relay's (go2rtc) ready-made streams, a USB webcam, and any host on the
        LAN answering on an RTSP port. Runs off the UI thread (called in one)."""
        import socket
        import ipaddress
        from concurrent.futures import ThreadPoolExecutor
        cands = []
        # go2rtc / local relay streams - fully-formed URLs, so previews work at
        # once (this is how teru151 / doorbell show up on his own machine).
        for host in ("127.0.0.1",):
            try:
                with urllib.request.urlopen("http://%s:1984/api/streams" % host,
                                            timeout=1.5) as r:
                    streams = json.loads(r.read().decode())
                for nm in (streams or {}):
                    # Only offer a relay stream whose SOURCE answers right now.
                    # go2rtc keeps a configured stream in the list even when its
                    # camera is unplugged; listing one we cannot reach is the
                    # same lie as a stale ARP entry.
                    if not self._relay_source_live(streams.get(nm)):
                        continue
                    cands.append({"label": "\U0001f3a5  %s   (on the local relay)" % nm,
                                  "name": nm, "where": "relay",
                                  "source": "rtsp://%s:8554/%s" % (host, nm)})
            except Exception:
                pass
        # USB webcam(s) actually PLUGGED IN: open each index and read one frame.
        # DirectShow fails fast on an absent/unplugged device, so this does not
        # hang when there is no camera (the point of the probe).
        try:
            import cv2
            for i in range(4):
                cap = None
                try:
                    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                    ok = bool(cap and cap.isOpened())
                    frame = None
                    if ok:
                        ok, frame = cap.read()
                    if ok and frame is not None:
                        cands.append({
                            "label": "\U0001f4f7  USB webcam on this PC (camera %d)" % i,
                            "name": "Webcam %d" % i if i else "Webcam",
                            "where": "webcam", "source": str(i)})
                except Exception:
                    pass
                finally:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
        except Exception:
            pass
        # RTSP cameras on the LAN (host only; a login/path may still be needed)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            myip = s.getsockname()[0]
            s.close()
            net = ipaddress.ip_network(myip + "/24", strict=False)
            hosts = [str(h) for h in net.hosts()]

            def probe(h):
                for port in (554, 8554):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as k:
                            k.settimeout(0.4)
                            if k.connect_ex((h, port)) == 0:
                                return h, port
                    except OSError:
                        pass
                return None
            with ThreadPoolExecutor(max_workers=128) as ex:
                for res in ex.map(probe, hosts):
                    if res:
                        h, port = res
                        cands.append({
                            "label": "\U0001f4f9  %s:%d   (on your network, RTSP - may need login/path)"
                                     % (h, port),
                            "name": "", "where": "network",
                            "source": "rtsp://%s:%d/" % (h, port)})
            # The port sweep above has now ARP-resolved every live host, so the
            # ARP table names them by MAC. Cloud / Wi-Fi cameras that gave no RTSP
            # are still recognisable by their maker's OUI - list each with a step.
            known = {c["source"].split("//", 1)[-1].split(":")[0]
                     for c in cands if c.get("where") == "network"}
            known.add(myip)
            cands.extend(self._scan_lan_vendors(known))
        except Exception:
            pass
        return cands

    def _scan_lan_vendors(self, known_ips):
        """Read the ARP table and, for each device whose MAC maker is a known
        camera brand, return a hint row (where='hint', not pickable) with a plain
        next step. Skips anything already found as an RTSP camera. Never leaves
        the PC - ARP is local, the OUI lookup is a bundled file."""
        import re
        import subprocess
        hints, seen = [], set()
        try:
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                                 timeout=6, creationflags=flags).stdout
        except Exception:
            return hints
        ouis = _camera_ouis()
        for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})", out):
            if ip in known_ips or ip in seen or ip.endswith(".255") or ip.startswith("224."):
                continue
            prefix = re.sub(r"[-:]", "", mac).upper()[:6]
            if prefix in ("FFFFFF", "000000"):
                continue
            brand = ouis.get(prefix)
            hint = BRAND_HINTS.get(brand) if brand else None
            if not hint:
                continue
            # The ARP table keeps a device for minutes after it leaves the
            # network, so a known-brand MAC here is NOT proof it is on. Only
            # hint at one that answers a fresh probe RIGHT NOW - otherwise the
            # scan shows cameras that are unplugged (e.g. his Roku cams, off).
            if not self._host_live_now(ip):
                continue
            seen.add(ip)
            kind, text = hint
            icon = "\U0001f527" if kind == "enable" else "☁️"  # wrench / cloud
            hints.append({"where": "hint", "kind": kind, "source": "",
                          "label": "%s  %s  (at %s)" % (icon, text, ip)})
        return hints

    def _host_live_now(self, ip):
        """True only if the host at ip answers RIGHT NOW. ARP keeps an entry
        for a device for minutes after it drops off the network, so a MAC in
        the table is not proof of presence. A quick TCP knock on the ports a
        camera usually opens catches most; one ICMP echo catches the ones that
        keep every port closed. Short timeouts so a scan stays snappy."""
        import socket
        import subprocess
        for port in (80, 554, 443, 8554, 8080):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as k:
                    k.settimeout(0.3)
                    if k.connect_ex((ip, port)) == 0:
                        return True
            except OSError:
                pass
        try:
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            if sys.platform == "win32":
                cmd = ["ping", "-n", "1", "-w", "500", ip]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", ip]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=3, creationflags=flags).stdout
            return "TTL=" in (out or "").upper()
        except Exception:
            return False

    def _relay_source_live(self, info):
        """True if a go2rtc stream's own source is reachable right now. go2rtc
        leaves a configured stream in /api/streams even when its camera is off,
        so trusting the list alone offers dead cameras. Probe the first
        producer's host:port with a fresh, short TCP connect. A source we
        cannot parse is left listed (fail open) rather than wrongly hidden; a
        local shim source resolves to 127.0.0.1, which answers while it runs."""
        import socket
        prods = (info or {}).get("producers") or []
        if not prods:
            return False
        url = prods[0].get("url") or ""
        host, port = None, 554
        try:
            if "://" in url:
                auth = url.split("://", 1)[1].split("/", 1)[0]
                auth = auth.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
                if ":" in auth:
                    host, ps = auth.rsplit(":", 1)
                    port = int(ps)
                else:
                    host = auth
        except Exception:
            host = None
        if not host:
            return True  # cannot parse the source - do not hide what we cannot test
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as k:
                k.settimeout(0.6)
                return k.connect_ex((host, port)) == 0
        except OSError:
            return False

    def _enroll_node(self, name, lat, lon, cb, node=None, token=None,
                     heading=None, fov=None, reach_m=None):
        """Register (or re-place) a camera with the hub from inside the app and
        hand back its id + token. The token never appears in the UI - it goes
        straight into the saved config. Passing node + token re-saves the SAME
        camera (rename, move, or a new aim) instead of minting a new one; that is
        exactly the loop the website's /aim page runs, done here instead."""
        def work():
            res, err = None, None
            try:
                body = {"name": name, "lat": lat, "lng": lon, "lon": lon,
                        "kind": "fixed"}
                if node:
                    body["node_id"] = node
                if token:
                    body["token"] = token
                if heading is not None:
                    body["heading"] = heading
                if fov is not None:
                    body["fov"] = fov
                if reach_m is not None:
                    body["reach_m"] = reach_m
                req = urllib.request.Request(
                    self._hub().rstrip("/") + "/api/enroll", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read().decode())
                if isinstance(res, dict) and res.get("error"):
                    err, res = res["error"], None
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read() or b"{}").get("error", "")
                except Exception:
                    err = ""
                err = err or ("the server said %d" % e.code)
            except Exception as ex:
                err = "could not reach SparrowMap (%s)" % ex.__class__.__name__
            self.after(0, lambda: cb(res, err))
        threading.Thread(target=work, daemon=True).start()

    def _node_span(self, node, token, publish, cb):
        """Turn the camera's watched stretch of road on or off on the public map
        (the privacy switch the website's aim page carries). POSTs /api/node/span
        with the camera's own token; nothing is shown for copying."""
        def work():
            res, err = None, None
            try:
                body = json.dumps({"node_id": node, "token": token,
                                   "publish": bool(publish)}).encode()
                req = urllib.request.Request(
                    self._hub().rstrip("/") + "/api/node/span", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read() or b"{}").get("error", "")
                except Exception:
                    err = ""
                err = err or ("the server said %d" % e.code)
            except Exception as ex:
                err = "could not reach SparrowMap (%s)" % ex.__class__.__name__
            self.after(0, lambda: cb(res, err))
        threading.Thread(target=work, daemon=True).start()

    def _remove(self, cid):
        cam = next((c for c in self.cameras if c["id"] == cid), None)
        if not cam:
            return
        if not messagebox.askyesno(APP, f"Remove {cam.get('name', cid)}?"):
            return
        self._stop_one(cid)
        self.cameras = [c for c in self.cameras if c["id"] != cid]
        self._persist()
        self._show_grid()

    def _geocode(self, q, cb):
        """Address/place -> lat/lon via the hub's own proxied geocoder (keeps the
        search private, same one the add dialog and the website use)."""
        def work():
            hit, err = None, None
            try:
                url = (self._hub().rstrip("/") + "/api/geocode?q="
                       + urllib.parse.quote(q))
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = json.loads(r.read().decode())
                res = (data.get("results") or []) if isinstance(data, dict) else []
                if res:
                    hit = res[0]
                else:
                    err = "no place matched that"
            except Exception as ex:
                err = "could not look that up (%s)" % ex.__class__.__name__
            self.after(0, lambda: cb(hit, err))
        threading.Thread(target=work, daemon=True).start()

    def _win_location(self, cb):
        """Ask Windows where this PC is (the OS shows its own permission prompt).
        Used ONLY to drop the map pin; the coordinate never leaves the PC. Falls
        back cleanly when location is off or the prompt is declined."""
        def work():
            lat = lon = None
            err = None
            try:
                ps = (
                    "Add-Type -AssemblyName System.Device;"
                    "$w=New-Object System.Device.Location.GeoCoordinateWatcher;"
                    "$w.Start();$n=0;"
                    "while($w.Status -ne 'Ready' -and $n -lt 60 -and "
                    "$w.Position.Location.IsUnknown){Start-Sleep -Milliseconds 100;$n++};"
                    "$c=$w.Position.Location;"
                    "if($c.IsUnknown){'UNKNOWN'}else{'{0},{1}' -f "
                    "$c.Latitude,$c.Longitude};$w.Stop()"
                )
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                    capture_output=True, text=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
                val = lines[0] if lines else ""
                if val and val != "UNKNOWN" and "," in val:
                    a, b = val.split(",", 1)
                    lat, lon = float(a), float(b)
                else:
                    err = "location is off or was not allowed"
            except Exception as ex:
                err = ex.__class__.__name__
            self.after(0, lambda: cb(lat, lon, err))
        threading.Thread(target=work, daemon=True).start()

    def _app_settings_dialog(self):
        """The app's own settings (not a camera's): Start with Windows, and start
        the cameras on launch. 'Start with Windows' just writes a Run entry - it
        is here, in plain sight, usable any time, because the setup wizard was
        the only place it ever lived and nobody could find it again."""
        dlg = tk.Toplevel(self)
        dlg.title("SparrowMap settings")
        dlg.configure(bg=BG)
        dlg.geometry(self._dialog_geometry(460, 360, "app_settings_win"))
        dlg.minsize(360, 300)
        dlg.transient(self)

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(body, text="Settings", bg=BG, fg=INK,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x", pady=(0, 10))

        loginvar = tk.BooleanVar(value=bool(self.cfg.get("run_at_login")))
        autovar = tk.BooleanVar(value=bool(self.cfg.get("autostart")))
        note = tk.Label(body, text="", bg=BG, fg=DIM, font=("Segoe UI", 9),
                        anchor="w", wraplength=400, justify="left")

        def on_login():
            want = bool(loginvar.get())
            try:
                msg = set_autostart(want)
            except Exception as ex:
                loginvar.set(not want)
                note.configure(text="Could not change that: " + ex.__class__.__name__,
                               fg=RED)
                return
            self.cfg["run_at_login"] = want
            if want:
                self.cfg["autostart"] = True   # login start implies cameras resume
                autovar.set(True)
            save_cfg(self.cfg)
            note.configure(text=msg + ".", fg=GREEN)

        def on_auto():
            self.cfg["autostart"] = bool(autovar.get())
            save_cfg(self.cfg)

        tk.Checkbutton(body, text="Start SparrowMap with Windows",
                       variable=loginvar, command=on_login, bg=BG, fg=INK,
                       selectcolor="#0a0e15", activebackground=BG, activeforeground=INK,
                       anchor="w", font=("Segoe UI", 10)).pack(fill="x", pady=(2, 0))
        tk.Label(body, text="Comes up hidden in the system tray and starts your "
                 "cameras, so the computer watches them the moment it is on.",
                 bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w",
                 wraplength=400, justify="left").pack(fill="x", pady=(0, 12))

        tk.Checkbutton(body, text="Start all cameras when the app opens",
                       variable=autovar, command=on_auto, bg=BG, fg=INK,
                       selectcolor="#0a0e15", activebackground=BG, activeforeground=INK,
                       anchor="w", font=("Segoe UI", 10)).pack(fill="x")
        note.pack(fill="x", pady=(10, 0))

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", pady=(14, 10))
        ver = tk.Frame(body, bg=BG)
        ver.pack(fill="x")
        tk.Label(ver, text="SparrowMap %s" % APP_VERSION, bg=BG, fg=DIM,
                 font=("Segoe UI", 9), anchor="w").pack(side="left")

        def check_now():
            note.configure(text="Checking for updates…", fg=DIM)
            threading.Thread(target=lambda: self._check_update(force=True),
                             daemon=True).start()

        tk.Button(ver, text="Check for updates", command=check_now, bg="#141c28",
                  fg=INK, relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="right", ipadx=4)

        tk.Button(body, text="Close", command=dlg.destroy, bg="#141c28", fg=INK,
                  relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="bottom", fill="x", pady=(14, 0), ipady=4)

    def _settings_dialog(self, cid):
        """Aim the camera and change its every setting IN THE APP - no browser.
        Re-saves the SAME node (rename, move, re-aim) through /api/enroll with the
        camera's own token, and flips the public-map share through /api/node/span.
        The aim needs no web map: a compass heading + field of view + reach is what
        the server resolves a watched road from, which is all the website's /aim
        page ever posted."""
        import math
        cam = next((c for c in self.cameras if c["id"] == cid), None)
        if not cam:
            return
        dlg = tk.Toplevel(self)
        dlg.title("Aim & settings - " + cam.get("name", cid))
        dlg.configure(bg=BG)
        # Open at the remembered size, else a sensible default, but NEVER taller
        # or wider than the monitor it lands on - his touch screen is 1024x600.
        dlg.geometry(self._dialog_geometry(500, 820, "settings_win"))
        dlg.minsize(360, 400)
        dlg.transient(self)

        def close():
            try:
                self.cfg["settings_win"] = "%dx%d" % (dlg.winfo_width(), dlg.winfo_height())
                save_cfg(self.cfg)
            except Exception:
                pass
            try:
                canvas.unbind_all("<MouseWheel>")
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass
        dlg.protocol("WM_DELETE_WINDOW", close)

        # Bottom action bar first, so Save never gets clipped by a tall body.
        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(side="bottom", fill="x")
        savebtn = tk.Button(bar, text="Save", bg=GREEN, fg="#0b0e13", relief="flat",
                            cursor="hand2", font=("Segoe UI", 11, "bold"),
                            activebackground="#5fe8ae")
        savebtn.pack(side="top", fill="x", padx=12, pady=(10, 2), ipady=5)
        setstat = tk.Label(bar, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                           anchor="w", wraplength=470, justify="left")
        setstat.pack(side="top", fill="x", padx=12)
        tk.Button(bar, text="Cancel", command=close, bg=PANEL, fg=DIM, relief="flat",
                  cursor="hand2", font=("Segoe UI", 9), activebackground=PANEL
                  ).pack(side="top", pady=(2, 8))

        # Everything below scrolls, so a small screen never clips a control and
        # Save stays pinned to the bottom bar above.
        canvas = tk.Canvas(dlg, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="top", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        _win = canvas.create_window((14, 12), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(_win, width=e.width - 28))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # --- Name -------------------------------------------------------
        tk.Label(body, text="Name", bg=BG, fg=INK, font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(fill="x")
        namee = tk.Entry(body, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                         font=("Segoe UI", 10), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor="#3d8cff")
        namee.pack(fill="x", ipady=4, pady=(2, 10))
        namee.insert(0, cam.get("name", ""))

        # --- Where it is ------------------------------------------------
        tk.Label(body, text="Where it is", bg=BG, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        loc = tk.Frame(body, bg=BG)
        loc.pack(fill="x")
        plq = tk.Entry(loc, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                       font=("Segoe UI", 10), highlightthickness=1,
                       highlightbackground=LINE, highlightcolor="#3d8cff")
        plq.pack(side="left", fill="x", expand=True, ipady=4)
        plq.insert(0, "")

        llf = tk.Frame(body, bg=BG)
        llf.pack(fill="x", pady=(4, 2))
        tk.Label(llf, text="lat", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="left")
        late = tk.Entry(llf, width=12, bg="#0a0e15", fg=INK, insertbackground=INK,
                        relief="flat", font=("Consolas", 10), highlightthickness=1,
                        highlightbackground=LINE)
        late.pack(side="left", padx=(2, 8), ipady=3)
        tk.Label(llf, text="lon", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="left")
        lone = tk.Entry(llf, width=12, bg="#0a0e15", fg=INK, insertbackground=INK,
                        relief="flat", font=("Consolas", 10), highlightthickness=1,
                        highlightbackground=LINE)
        lone.pack(side="left", padx=2, ipady=3)
        if cam.get("lat") is not None:
            late.insert(0, "%.6f" % cam["lat"])
        if cam.get("lon") is not None:
            lone.insert(0, "%.6f" % cam["lon"])

        def findloc():
            q = plq.get().strip()
            if not q:
                setstat.configure(text="Type an address or place, then Find.", fg=AMBER)
                return
            setstat.configure(text="Looking up “" + q + "”…", fg=AMBER)

            def got(hit, err):
                if not hit:
                    setstat.configure(text=err or "no match", fg=RED)
                    return
                late.delete(0, "end"), late.insert(0, "%.6f" % hit["lat"])
                lone.delete(0, "end"), lone.insert(0, "%.6f" % hit["lon"])
                mapw.set_camera(hit["lat"], hit["lon"])
                setstat.configure(text="Found: " + hit.get("name", q), fg=GREEN)
            self._geocode(q, got)
        tk.Button(loc, text="Find", command=findloc, bg="#141c28", fg=INK, relief="flat",
                  cursor="hand2", font=("Segoe UI", 9), activebackground="#1b2432",
                  activeforeground=INK).pack(side="left", padx=(6, 0), ipadx=6, ipady=2)
        tk.Label(body, text="A small nudge stays the same camera. A big move makes a new "
                 "one on the map.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x", pady=(0, 10))

        # --- Aim it on the map -----------------------------------------
        tk.Label(body, text="Aim your camera", bg=BG, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Drag the dot to where the camera is, drag the arrow tip to "
                 "point it down the road. The server finds the stretch of road under "
                 "that wedge.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x", pady=(0, 4))

        hold = {"prev": None}

        def on_map(st):
            # Keep the typed lat/lon in step with the pin, so Save and the boxes
            # always agree with where the dot sits.
            late.delete(0, "end"), late.insert(0, "%.6f" % st["lat"])
            lone.delete(0, "end"), lone.insert(0, "%.6f" % st["lon"])

        mapw = AimMap(body, self, cam.get("lat"), cam.get("lon"),
                      heading=cam.get("heading", 0) or 0, fov=cam.get("fov", 60) or 60,
                      reach=cam.get("reach_m", 45) or 45, on_change=on_map)
        mapw.pack(fill="x", pady=(2, 6))

        def latlon_typed(_e=None):
            try:
                mapw.set_camera(float(late.get()), float(lone.get()))
            except ValueError:
                pass
        for _ent in (late, lone):
            _ent.bind("<Return>", latlon_typed)
            _ent.bind("<FocusOut>", latlon_typed)

        # Live preview of what the camera sees right now, so aiming is not blind.
        prev = tk.Label(body, bg="#000000", fg=DIM, text="● preview…",
                        font=("Segoe UI", 9), height=4)
        prev.pack(fill="x", pady=(0, 8))

        # --- Share on the public map -----------------------------------
        shvar = tk.BooleanVar(value=bool(cam.get("publish_span")))
        tk.Checkbutton(body, text="Show the watched road on the public SparrowMap",
                       variable=shvar, bg=BG, fg=INK, selectcolor="#0a0e15",
                       activebackground=BG, activeforeground=INK, anchor="w",
                       font=("Segoe UI", 9)).pack(fill="x")
        tk.Label(body, text="Off by default. The picture and the exact spot are never "
                 "published either way.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x")

        def grab_prev():
            src = cam.get("source")
            if not src:
                return
            b64 = [None]

            def work():
                try:
                    import cv2
                    import detect.relay as relay
                    s = int(src) if str(src).strip().isdigit() else src
                    cap = relay.open_stream(s)
                    if cap is not None:
                        ok, frame = False, None
                        for _ in range(4):
                            ok, frame = cap.read()
                            if ok:
                                break
                        try:
                            cap.release()
                        except Exception:
                            pass
                        if ok and frame is not None:
                            h, w = frame.shape[:2]
                            tw = 240
                            th = max(1, int(h * tw / w))
                            frame = cv2.resize(frame, (tw, th))
                            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            good, buf = cv2.imencode(".png", rgb)
                            if good:
                                b64[0] = base64.b64encode(buf).decode()
                except Exception:
                    b64[0] = None

                def show():
                    if b64[0]:
                        try:
                            hold["prev"] = tk.PhotoImage(data=b64[0])
                            prev.configure(image=hold["prev"], text="")
                        except Exception:
                            pass
                    else:
                        prev.configure(text="(no preview)")
                self.after(0, show)
            threading.Thread(target=work, daemon=True).start()
        grab_prev()

        def do_save():
            name = namee.get().strip() or cam.get("name") or "Camera"
            m = mapw.get()
            if not m["has_fix"]:
                setstat.configure(text="Place the camera first - drag the dot, Find an "
                                  "address, or Use my location.", fg=AMBER)
                return
            lat, lon = m["lat"], m["lon"]
            savebtn.configure(state="disabled", text="Saving…")
            setstat.configure(text="Saving the aim and settings…", fg=AMBER)

            def after_enroll(res, err):
                if err or not res:
                    savebtn.configure(state="normal", text="Save")
                    setstat.configure(text="Could not save: " + (err or "no reply")
                                      + ". Try again.", fg=RED)
                    return
                cam["name"] = name
                cam["lat"], cam["lon"] = lat, lon
                cam["heading"] = m["heading"]
                cam["fov"] = m["fov"]
                cam["reach_m"] = m["reach"]
                # A big move mints a new node; adopt its id + token or we post as a
                # camera that no longer exists where it claims to.
                if res.get("moved_from") and res.get("id"):
                    cam["node"] = res["id"]
                if res.get("token"):
                    cam["token"] = res["token"]
                self._persist()

                def finish(msg):
                    savebtn.configure(state="normal", text="Save")
                    close()
                    if self.view == ("single", cid):
                        self._show_single(cid)
                    road = res.get("road_name")
                    where = ("\n\nIt watches: " + road) if road else ""
                    messagebox.showinfo(APP, 'Saved "' + name + '".' + where + msg)

                # Then the public-map share, if it changed.
                if bool(shvar.get()) != bool(cam.get("publish_span")):
                    def after_span(sres, serr):
                        cam["publish_span"] = bool(shvar.get())
                        self._persist()
                        extra = ""
                        if sres and not sres.get("has_span"):
                            extra = ("\n\nShare is on, but there is no road resolved yet "
                                     "- nudge the aim until it finds one.")
                        finish(extra)
                    self._node_span(cam.get("node"), cam.get("token"),
                                    bool(shvar.get()), after_span)
                else:
                    finish("")
            self._enroll_node(name, lat, lon, after_enroll, node=cam.get("node"),
                              token=cam.get("token"), heading=m["heading"],
                              fov=m["fov"], reach_m=m["reach"])
        savebtn.configure(command=do_save)

    def _persist(self):
        self.cfg["cameras"] = self.cameras
        save_cfg(self.cfg)

    # -- plumbing --------------------------------------------------------
    def _hub(self):
        return self.cfg.get("hub", HUB_DEFAULT)

    def _aim_nav(self):
        """The Aim tab now aims IN THE APP, never a browser. One camera -> straight
        to its aim; several -> open the one being viewed, else say to pick one."""
        if not self.cameras:
            messagebox.showinfo(APP, "Add a camera first, then you can aim it here.")
            return
        if len(self.cameras) == 1:
            self._settings_dialog(self.cameras[0]["id"])
            return
        if self.view and self.view[0] == "single" and self.view[1]:
            self._settings_dialog(self.view[1])
            return
        messagebox.showinfo(APP, "Click a camera, then “Aim & settings” to aim it.")

    def _review_dialog(self):
        """Review flagged passes IN THE APP - no browser. A camera you add here
        gets its own reviewer token automatically (saved in the config, never
        shown), so the queue below is yours. Crops are fetched with that token,
        shown, never written to disk; your verdict posts straight back."""
        tokens = dict(self.cfg.get("review_tokens", {}))
        dlg = tk.Toplevel(self)
        dlg.title("Review - patrol-car checks")
        dlg.configure(bg=BG)
        dlg.geometry("560x720")
        dlg.transient(self)

        head = tk.Frame(dlg, bg=PANEL)
        head.pack(side="top", fill="x")
        tk.Label(head, text="Review", bg=PANEL, fg=INK, font=("Segoe UI", 13, "bold"),
                 anchor="w").pack(fill="x", padx=14, pady=(10, 0))
        sub = tk.Label(head, text="Checking your queue…", bg=PANEL, fg=DIM,
                       font=("Segoe UI", 9), anchor="w", wraplength=520,
                       justify="left")
        sub.pack(fill="x", padx=14, pady=(2, 10))

        canv = tk.Canvas(dlg, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(dlg, orient="vertical", command=canv.yview)
        inner = tk.Frame(canv, bg=BG)
        inner.bind("<Configure>",
                   lambda e: canv.configure(scrollregion=canv.bbox("all")))
        canv.create_window((0, 0), window=inner, anchor="nw", width=540)
        canv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canv.pack(side="left", fill="both", expand=True)

        if not tokens:
            sub.configure(text="")
            tk.Label(inner, text="No reviewer access yet.\n\nAdd a camera in this app "
                     "and it gets its own reviewer token automatically - then the "
                     "passes it flags show up here for you to confirm. Nothing to "
                     "copy or paste.", bg=BG, fg=DIM, font=("Segoe UI", 10),
                     justify="left", wraplength=500, anchor="w"
                     ).pack(fill="x", padx=16, pady=20)
            return

        self._rv_imgs = {}
        hub = self._hub().rstrip("/")

        def decode(data):
            try:
                if HAVE_PIL:
                    import io
                    return ImageTk.PhotoImage(
                        PILImage.open(io.BytesIO(data)).convert("RGB"))
                import cv2
                import numpy as np
                arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if arr is None:
                    return None
                rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
                good, buf = cv2.imencode(".png", rgb)
                if good:
                    return tk.PhotoImage(data=base64.b64encode(buf).decode())
            except Exception:
                return None
            return None

        def fetch_crop(it, imglab):
            url = hub + (it.get("crop") or "")
            tok = it["_tok"]

            def work():
                photo = None
                try:
                    req = urllib.request.Request(
                        url, headers={"Authorization": "Bearer " + tok})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        photo = decode(r.read())
                except Exception:
                    photo = None

                def fin():
                    if photo is not None:
                        self._rv_imgs[id(imglab)] = photo
                        imglab.configure(image=photo, text="")
                    else:
                        imglab.configure(text="(could not load the image)")
                try:
                    self.after(0, fin)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def verdict(it, v, row):
            tok = it["_tok"]

            def work():
                ok = False
                try:
                    body = json.dumps({"id": it.get("id"), "verdict": v}).encode()
                    req = urllib.request.Request(
                        hub + "/api/rv/verdict", data=body,
                        headers={"Content-Type": "application/json",
                                 "Authorization": "Bearer " + tok})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        r.read()
                    ok = True
                except Exception:
                    ok = False

                def fin():
                    if ok:
                        for w in row.winfo_children():
                            w.destroy()
                        label = {"cop": "patrol car", "gov": "government",
                                 "not": "not one"}.get(v, v)
                        tk.Label(row, text="✓ Marked " + label, bg="#0d1219",
                                 fg=GREEN, font=("Segoe UI", 9), anchor="w"
                                 ).pack(fill="x", padx=8, pady=8)
                    else:
                        sub.configure(text="That verdict did not go through - "
                                           "try again.", fg=AMBER)
                try:
                    self.after(0, fin)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def render_list(items, err):
            if err and not items:
                sub.configure(text="Could not load your review queue: " + err)
                return
            if not items:
                sub.configure(text="Nothing waiting - you are all caught up.")
                return
            sub.configure(text="%d flagged pass%s waiting. Is it a patrol car?"
                          % (len(items), "" if len(items) == 1 else "es"))
            for it in items:
                row = tk.Frame(inner, bg="#0d1219", highlightthickness=1,
                               highlightbackground=LINE)
                row.pack(fill="x", padx=12, pady=6)
                imglab = tk.Label(row, bg="#0d1219", text="loading…", fg=DIM,
                                  font=("Segoe UI", 9))
                imglab.pack(fill="x", padx=6, pady=6)
                meta = "%s · score %s" % (
                    it.get("node_name") or it.get("node_id") or "camera",
                    it.get("score", "?"))
                tk.Label(row, text=meta, bg="#0d1219", fg=DIM,
                         font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=8)
                btns = tk.Frame(row, bg="#0d1219")
                btns.pack(fill="x", padx=6, pady=(4, 8))
                for lab, vv, col in (("Patrol car", "cop", GREEN),
                                     ("Government", "gov", AMBER),
                                     ("Not one", "not", "#8794a8")):
                    tk.Button(btns, text=lab,
                              command=lambda it=it, vv=vv, row=row: verdict(it, vv, row),
                              bg="#141c28", fg=col, relief="flat", cursor="hand2",
                              font=("Segoe UI", 9, "bold"), activebackground="#1b2432",
                              activeforeground=INK).pack(side="left", expand=True,
                                                         fill="x", padx=3, ipady=3)
                fetch_crop(it, imglab)

        def load():
            seen, merged, err = set(), [], None
            for tok in tokens.values():
                try:
                    req = urllib.request.Request(
                        hub + "/api/rv/queue?scope=mine",
                        headers={"Authorization": "Bearer " + tok})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        d = json.loads(r.read().decode())
                    for it in (d.get("items") or []):
                        iid = it.get("id")
                        if iid in seen:
                            continue
                        seen.add(iid)
                        it["_tok"] = tok
                        merged.append(it)
                except Exception as e:
                    err = str(e)
            try:
                self.after(0, lambda: render_list(merged, err))
            except Exception:
                pass
        threading.Thread(target=load, daemon=True).start()

    def _open(self, path):
        webbrowser.open(self._hub().rstrip("/") + path)

    def _say(self, cid, msg):
        t = self.tiles.get(cid)
        if t and "log" in t:
            t["log"].configure(state="normal")
            t["log"].insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
            t["log"].see("end")
            t["log"].configure(state="disabled")

    def _render(self, cid, val):
        t = self.tiles.get(cid)
        if not t:
            return
        try:
            if HAVE_PIL and not isinstance(val, str):
                self.imgs[cid] = ImageTk.PhotoImage(PILImage.fromarray(val))
            else:
                self.imgs[cid] = tk.PhotoImage(data=val)
            t["img"].configure(image=self.imgs[cid], text="")
        except Exception:
            pass

    def _poll_live(self):
        """The tile said 'stopped' for cameras that were plainly on, because it
        only knew about workers THIS app had started - his cameras are watched
        by a detector running outside the app. So, for any camera this app is
        not itself running, ask the real question: is the video source live
        right now? One bounded probe per camera, off the UI thread, every 20s."""
        if not self._probing:
            self._probing = True
            cams = [dict(c) for c in self.cameras
                    if not (c["id"] in self.workers and self.workers[c["id"]].is_alive())]

            def work():
                for c in cams:
                    live = self._source_live(c.get("source"))
                    try:
                        self.q.put(("live", c["id"], live))
                    except Exception:
                        pass
                self._probing = False
            threading.Thread(target=work, daemon=True).start()
        self.after(20000, self._poll_live)

    def _source_live(self, src):
        """True if the camera's stream hands us a frame right now. Mirrors how a
        worker opens it, so 'live' here means the same thing the worker would
        see. Bounded: a few reads then release, whatever happens."""
        if not src:
            return False
        try:
            import cv2  # noqa: F401
            import detect.relay as relay
            s = int(src) if str(src).strip().isdigit() else src
            cap = relay.open_stream(s)
            if cap is None:
                return False
            ok = False
            try:
                for _ in range(4):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        break
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
            return bool(ok)
        except Exception:
            return False

    def _pump(self):
        if self.tray:
            try:
                self.tray.pump()
            except Exception:
                pass
        frames = {}                       # keep only the newest frame per camera
        try:
            while True:
                kind, cid, val = self.q.get_nowait()
                if kind == "frame":
                    frames[cid] = val
                    continue
                if kind == "banner":
                    self.banner.configure(text=val or "")
                elif kind == "ready":
                    if self.cfg.get("autostart") or self._launch_tray:
                        self._toggle_all()
                elif kind == "update":
                    self._show_update_banner(val)
                elif kind == "live":
                    self.live[cid] = val
                    # Only speak for a camera this app is not running itself; a
                    # live worker owns its own status line.
                    if cid in self.tiles and not (
                            cid in self.workers and self.workers[cid].is_alive()):
                        t = self.tiles[cid]
                        t["status"].configure(text="running" if val else "stopped",
                                              fg=GREEN if val else DIM)
                elif cid in self.tiles:
                    t = self.tiles[cid]
                    if kind == "state":
                        col = {"running": GREEN, "error": RED}.get(val, DIM)
                        t["status"].configure(text=val, fg=col)
                        self._say(cid, val)
                    elif kind == "sent":
                        t["status"].configure(text=f"sent {val}", fg=GREEN)
                    elif kind == "log":
                        self._say(cid, val)
        except queue.Empty:
            pass
        for cid, val in frames.items():
            self._render(cid, val)
        self.after(33, self._pump)      # ~30 fps UI repaint

    def _close(self):
        # kept for anything that still calls it; the X now hides to the tray.
        self._hide_to_tray()


# --------------------------------------------------------------------------
# start with the computer
# --------------------------------------------------------------------------
def set_autostart(on: bool) -> str:
    target = sys.executable
    # --tray: a login launch comes up hidden in the tray with cameras running,
    # so starting with Windows is not a window popping open on every boot.
    extra = " --tray"
    args = extra if getattr(sys, "frozen", False) else f' "{Path(__file__).resolve()}"{extra}'
    cmd = f'"{target}"{args}'
    if os.name == "nt":
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            if on:
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, cmd)
                return "will start with Windows"
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass
            return "will no longer start with Windows"
        finally:
            winreg.CloseKey(key)
    unit = Path.home() / ".config" / "systemd" / "user" / "sparrowmap.service"
    if on:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=SparrowMap relay\n\n"
            f"[Service]\nExecStart={cmd}\nRestart=always\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "enable", "--now", "sparrowmap"],
                       capture_output=True)
        return "installed a user service"
    subprocess.run(["systemctl", "--user", "disable", "--now", "sparrowmap"],
                   capture_output=True)
    unit.unlink(missing_ok=True)
    return "removed the user service"


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
