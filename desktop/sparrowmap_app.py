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
HUB_DEFAULT = "https://map.sparrowmap.com"
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
# the window
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
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
        self._build_chrome()
        self._show_grid()
        self.after(150, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)
        threading.Thread(target=self._load_model, daemon=True).start()

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
            cmd = self._aim_nav if path == "/aim" else (lambda p=path: self._open(p))
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
        status = tk.Label(bar, text="stopped", bg=PANEL, fg=DIM,
                          font=("Segoe UI", 9), anchor="e")
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
                addrstat.configure(text="✓ Location set below - you can still nudge "
                                        "it, or set the facing later in Aim.", fg=GREEN)
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

        # ADD: enrol with the server HERE, store the keys silently, start watching.
        def do_add():
            src = srcentry.get().strip()
            if not src:
                addstat.configure(text="Pick a camera or type its address first.", fg=AMBER)
                return
            name = fields["name"].get().strip() or f"Camera {len(self.cameras) + 1}"
            latv, lonv = fields["lat"].get().strip(), fields["lon"].get().strip()
            if not (latv and lonv):
                addstat.configure(text="Find where the camera is first (step 2), so it "
                                       "has a spot on the map.", fg=AMBER)
                return
            try:
                lat, lon = float(latv), float(lonv)
            except ValueError:
                addstat.configure(text="Latitude and longitude must be numbers - use Find.",
                                  fg=AMBER)
                return
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
                       "lat": lat, "lon": lon}
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
            self._enroll_node(name, lat, lon, done)
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
            seen.add(ip)
            kind, text = hint
            icon = "\U0001f527" if kind == "enable" else "☁️"  # wrench / cloud
            hints.append({"where": "hint", "kind": kind, "source": "",
                          "label": "%s  %s  (at %s)" % (icon, text, ip)})
        return hints

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
        dlg.geometry("500x760")
        dlg.transient(self)

        def close():
            try:
                dlg.destroy()
            except Exception:
                pass

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

        body = tk.Frame(dlg, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=14, pady=12)

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
                setstat.configure(text="Found: " + hit.get("name", q), fg=GREEN)
            self._geocode(q, got)
        tk.Button(loc, text="Find", command=findloc, bg="#141c28", fg=INK, relief="flat",
                  cursor="hand2", font=("Segoe UI", 9), activebackground="#1b2432",
                  activeforeground=INK).pack(side="left", padx=(6, 0), ipadx=6, ipady=2)
        tk.Label(body, text="A small nudge stays the same camera. A big move makes a new "
                 "one on the map.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x", pady=(0, 10))

        # --- Aim --------------------------------------------------------
        tk.Label(body, text="Aim your camera", bg=BG, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Which way it looks, how wide, and how far. The server finds "
                 "the stretch of road under that.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x", pady=(0, 4))

        aim = tk.Frame(body, bg=BG)
        aim.pack(fill="x")
        comp = tk.Canvas(aim, width=120, height=120, bg="#0a0e15", highlightthickness=1,
                         highlightbackground=LINE)
        comp.pack(side="left", padx=(0, 10))
        prev = tk.Label(aim, bg="#000000", fg=DIM, text="● preview…",
                        font=("Segoe UI", 9), width=28, height=7)
        prev.pack(side="left", fill="both", expand=True)

        state = {"heading": float(cam.get("heading", 0) or 0),
                 "fov": float(cam.get("fov", 60) or 60),
                 "reach": float(cam.get("reach_m", 45) or 45),
                 "prevkeep": None}

        def draw_compass():
            comp.delete("all")
            cx, cy, R = 60, 60, 48
            comp.create_oval(cx - R, cy - R, cx + R, cy + R, outline=LINE)
            for lab, ang in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
                ax = cx + (R - 9) * math.sin(math.radians(ang))
                ay = cy - (R - 9) * math.cos(math.radians(ang))
                comp.create_text(ax, ay, text=lab, fill=DIM, font=("Segoe UI", 8))
            h = state["heading"]
            # Field-of-view wedge, then the facing arrow on top.
            half = state["fov"] / 2.0
            pts = [cx, cy]
            a = h - half
            while a <= h + half + 0.01:
                pts += [cx + R * math.sin(math.radians(a)),
                        cy - R * math.cos(math.radians(a))]
                a += 6
            if len(pts) >= 6:
                comp.create_polygon(pts, fill="#14314a", outline="")
            comp.create_line(cx, cy, cx + R * math.sin(math.radians(h)),
                             cy - R * math.cos(math.radians(h)), fill=GREEN, width=3,
                             arrow="last")
            comp.create_text(cx, cy + R + 8, text="%d°" % round(h), fill=INK,
                             font=("Segoe UI", 9, "bold"))

        def set_heading_from(ev):
            dx, dy = ev.x - 60, ev.y - 60
            if dx == 0 and dy == 0:
                return
            state["heading"] = (math.degrees(math.atan2(dx, -dy))) % 360
            draw_compass()
        comp.bind("<Button-1>", set_heading_from)
        comp.bind("<B1-Motion>", set_heading_from)

        sld = tk.Frame(body, bg=BG)
        sld.pack(fill="x", pady=(8, 2))

        def mkscale(row, label, lo, hi, key, unit):
            tk.Label(row, text=label, bg=BG, fg=DIM, font=("Segoe UI", 9), width=12,
                     anchor="w").pack(side="left")
            var = tk.DoubleVar(value=state[key])

            def on(_v):
                state[key] = float(var.get())
                draw_compass()
            s = tk.Scale(row, from_=lo, to=hi, orient="horizontal", variable=var,
                         command=on, bg=BG, fg=INK, troughcolor="#0a0e15",
                         highlightthickness=0, relief="flat", showvalue=True)
            s.pack(side="left", fill="x", expand=True)

        r1 = tk.Frame(body, bg=BG)
        r1.pack(fill="x")
        mkscale(r1, "Facing °", 0, 359, "heading", "°")
        r2 = tk.Frame(body, bg=BG)
        r2.pack(fill="x")
        mkscale(r2, "How wide °", 10, 120, "fov", "°")
        r3 = tk.Frame(body, bg=BG)
        r3.pack(fill="x")
        mkscale(r3, "How far m", 15, 200, "reach", "m")

        # "Point it down the road": a place at the far end of the view -> a bearing
        # from the camera to there, so aiming needs no compass guesswork.
        dr = tk.Frame(body, bg=BG)
        dr.pack(fill="x", pady=(6, 0))
        drq = tk.Entry(dr, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                       font=("Segoe UI", 9), highlightthickness=1, highlightbackground=LINE)
        drq.pack(side="left", fill="x", expand=True, ipady=3)

        def point_down():
            q = drq.get().strip()
            if not q:
                setstat.configure(text="Type what is at the far end of the view, then "
                                  "Point.", fg=AMBER)
                return
            try:
                la, lo = float(late.get()), float(lone.get())
            except ValueError:
                setstat.configure(text="Set the camera's own lat/lon first.", fg=AMBER)
                return

            def got(hit, err):
                if not hit:
                    setstat.configure(text=err or "no match", fg=RED)
                    return
                dla = math.radians(hit["lat"] - la)
                dlo = math.radians(hit["lon"] - lo)
                y = math.sin(dlo) * math.cos(math.radians(hit["lat"]))
                x = (math.cos(math.radians(la)) * math.sin(math.radians(hit["lat"]))
                     - math.sin(math.radians(la)) * math.cos(math.radians(hit["lat"]))
                     * math.cos(dlo))
                state["heading"] = (math.degrees(math.atan2(y, x))) % 360
                draw_compass()
                setstat.configure(text="Aimed toward " + hit.get("name", q), fg=GREEN)
            self._geocode(q, got)
        tk.Button(dr, text="Point", command=point_down, bg="#141c28", fg=INK,
                  relief="flat", cursor="hand2", font=("Segoe UI", 9),
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="left", padx=(6, 0), ipadx=6, ipady=2)
        tk.Label(body, text="e.g. the shop or cross street the camera looks toward",
                 bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w").pack(fill="x",
                                                                       pady=(1, 10))

        # --- Share on the public map -----------------------------------
        shvar = tk.BooleanVar(value=bool(cam.get("publish_span")))
        tk.Checkbutton(body, text="Show the watched road on the public SparrowMap",
                       variable=shvar, bg=BG, fg=INK, selectcolor="#0a0e15",
                       activebackground=BG, activeforeground=INK, anchor="w",
                       font=("Segoe UI", 9)).pack(fill="x")
        tk.Label(body, text="Off by default. The picture and the exact spot are never "
                 "published either way.", bg=BG, fg=DIM, font=("Segoe UI", 8),
                 anchor="w", wraplength=460, justify="left").pack(fill="x")

        draw_compass()

        # Live preview of what the camera sees right now, so aiming is not blind.
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
                            state["prevkeep"] = tk.PhotoImage(data=b64[0])
                            prev.configure(image=state["prevkeep"], text="")
                        except Exception:
                            pass
                    else:
                        prev.configure(text="(no preview)")
                self.after(0, show)
            threading.Thread(target=work, daemon=True).start()
        grab_prev()

        def do_save():
            name = namee.get().strip() or cam.get("name") or "Camera"
            try:
                lat, lon = float(late.get()), float(lone.get())
            except ValueError:
                setstat.configure(text="Latitude and longitude must be numbers - use Find.",
                                  fg=AMBER)
                return
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
                cam["heading"] = round(state["heading"], 1)
                cam["fov"] = round(state["fov"])
                cam["reach_m"] = round(state["reach"])
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
                              token=cam.get("token"), heading=round(state["heading"], 1),
                              fov=round(state["fov"]), reach_m=round(state["reach"]))
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

    def _pump(self):
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
                    if self.cfg.get("autostart"):
                        self._toggle_all()
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
        for w in self.workers.values():
            w.stop.set()
        self.destroy()


# --------------------------------------------------------------------------
# start with the computer
# --------------------------------------------------------------------------
def set_autostart(on: bool) -> str:
    target = sys.executable
    args = "" if getattr(sys, "frozen", False) else f' "{Path(__file__).resolve()}"'
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
