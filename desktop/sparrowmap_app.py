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
            tk.Button(nav, text=label, command=lambda p=path: self._open(p),
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
                     font=("Segoe UI", 12)).pack(pady=(40, 6))
            tk.Button(self.body, text="Set one up  →", command=lambda: self._open("/IPCamera"),
                      bg="#141c28", fg=INK, relief="flat", cursor="hand2",
                      font=("Segoe UI", 10), activebackground="#1b2432",
                      activeforeground=INK).pack()
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
        d = tk.Toplevel(self)
        d.title("Add a camera")
        d.configure(bg=BG)
        d.geometry("440x400")
        d.transient(self)
        fields = {}
        rows = [("name", "Name (e.g. Front door)"),
                ("source", "Stream address  (rtsp://…  or  0 for a USB webcam)"),
                ("node", "Camera id   (optional - needed only to post to the map)"),
                ("token", "Token   (optional)"),
                ("lat", "Latitude   (optional)"),
                ("lon", "Longitude  (optional)")]
        for key, label in rows:
            tk.Label(d, text=label, bg=BG, fg=DIM, font=("Segoe UI", 9),
                     anchor="w").pack(fill="x", padx=16, pady=(10, 2))
            e = tk.Entry(d, bg="#0a0e15", fg=INK, insertbackground=INK, relief="flat",
                         font=("Consolas", 10), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor="#3d8cff")
            e.pack(fill="x", padx=16, ipady=4)
            fields[key] = e

        def save():
            cam = {
                "id": _new_id(self.cameras),
                "name": fields["name"].get().strip() or f"Camera {len(self.cameras)+1}",
                "source": fields["source"].get().strip(),
                "node": fields["node"].get().strip(),
                "token": fields["token"].get().strip(),
                "lat": float(fields["lat"].get() or 0) if fields["lat"].get().strip() else 0.0,
                "lon": float(fields["lon"].get() or 0) if fields["lon"].get().strip() else 0.0,
            }
            if not cam["source"]:
                return messagebox.showerror(APP, "Enter the camera's stream address.")
            self.cameras.append(cam)
            self._persist()
            d.destroy()
            self._show_grid()
            self._start_one(cam)

        tk.Button(d, text="Add", command=save, bg=GREEN, fg="#0b0e13", relief="flat",
                  cursor="hand2", font=("Segoe UI", 10, "bold")
                  ).pack(fill="x", padx=16, pady=16, ipady=6)

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

    def _persist(self):
        self.cfg["cameras"] = self.cameras
        save_cfg(self.cfg)

    # -- plumbing --------------------------------------------------------
    def _hub(self):
        return self.cfg.get("hub", HUB_DEFAULT)

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
