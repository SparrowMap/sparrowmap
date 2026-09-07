"""RavenMap4Biz: the LIGHT node for a business, with a window instead of a terminal.

    python desktop\\sparrowmap_app.py          # run it
    python desktop\\build.py                   # build SparrowMap.exe

🚨 THIS IS THE LIGHT NODE. NO CLIP, NO HEAD, NO TORCH.
It finds vehicles and posts a crop too small to carry a plate; the classifier
that decides "is this a patrol car" runs at home on the project's GPU. That is
the whole reason this can be a 60 MB download that runs on a shop PC instead of
a machine-learning install. Anything that wants to add the head here should
build a second app rather than grow this one - the moment torch is a dependency,
the audience for it disappears.

WHY A WINDOW AT ALL. The relay already worked from a command line, and that is
the wrong ask for the people this is aimed at: a business with a camera on the
street and no interest in terminals. Everything here exists because it is the
part somebody would otherwise get wrong - the stream URL, keeping the process
alive, and finding the pages that go with the camera.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# The BUSINESS build. Named apart from the phone/webcam node so the two
# downloads on the release page cannot be confused for each other - one
# is for a shop with an IP camera, the other is not.
APP = "RavenMap4Biz"
HUB_DEFAULT = os.environ.get("RAVEN_HUB") or os.environ.get("SPARROW_HUB") or ""
CFG = Path.home() / ".sparrowmap" / "desktop.json"
BG, PANEL, INK, DIM, LINE = "#0a0d12", "#111621", "#e6ecf5", "#8794a8", "#2a3547"
RED, GREEN = "#ff3b47", "#3ddc97"

# 🚨 IMPORTED, NOT SHELLED OUT TO. Bundled by PyInstaller into one exe, there is
# no `python -m detect.relay` to run: the interpreter IS this program. Keeping
# the relay as a module the app imports means the exe and the command line share
# one implementation rather than drifting into two.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(d: dict) -> None:
    try:
        CFG.parent.mkdir(parents=True, exist_ok=True)
        CFG.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except Exception:
        pass


class RelayThread(threading.Thread):
    """Runs the relay loop and reports back through a queue.

    A THREAD, not a subprocess, so the window can show what it is doing without
    parsing text out of a pipe - and so closing the window actually stops it.
    """

    def __init__(self, args, out: "queue.Queue"):
        super().__init__(daemon=True)
        self.args, self.out, self.stop = args, out, threading.Event()
        self.sent = 0

    def log(self, msg):
        self.out.put(("log", msg))

    def run(self):
        try:
            from detect import relay
        except Exception as exc:
            self.log(f"could not load the detector: {exc}")
            self.out.put(("state", "stopped"))
            return
        try:
            import onnxruntime as ort
            self.log("loading the detector…")
            mp = relay.model_path(self.args["hub"])
            sess = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
            iname = sess.get_inputs()[0].name
            relay.SIZE = int(sess.get_inputs()[0].shape[-1])
            self.log(f"detector ready ({relay.SIZE}x{relay.SIZE})")
        except Exception as exc:
            self.log(f"detector failed to start: {exc}")
            self.out.put(("state", "stopped"))
            return

        src = self.args["source"]
        cap = relay.open_stream(src)
        if cap is None:
            self.log("could not open the camera. Check the address.")
            self.out.put(("state", "stopped"))
            return
        self.log("watching")
        self.out.put(("state", "running"))

        tracks, nid, last_send = [], 0, 0.0
        while not self.stop.is_set():
            ok, frame = cap.read()
            if not ok:
                self.log("camera dropped; reconnecting…")
                cap.release()
                time.sleep(3)
                cap = relay.open_stream(src)
                if cap is None:
                    time.sleep(7)
                continue
            blob, s, ox, oy = relay.letterbox(frame)
            hits = relay.decode(sess.run(None, {iname: blob})[0], s, ox, oy)
            now = time.time()
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
                if (t["seen"] < relay.MIN_FRAMES or not t["crop"] or t["sent"]
                        or now - last_send < self.args["every"]):
                    continue
                try:
                    r = relay.post(self.args["hub"], self.args["node"],
                                   self.args["token"], self.args["lat"],
                                   self.args["lon"], t["crop"], t["cls"], 0.9)
                    last_send, t["sent"] = now, True
                    if r.get("error"):
                        self.log(f"hub refused: {r['error']}")
                    else:
                        self.sent += 1
                        self.out.put(("sent", self.sent))
                        self.log(f"sent a {t['cls']}")
                except Exception as exc:
                    self.log(f"could not send ({exc.__class__.__name__}); still watching")
        cap.release()
        self.log("stopped")
        self.out.put(("state", "stopped"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.configure(bg=BG)
        self.geometry("560x600")
        self.minsize(520, 560)
        self.cfg = load_cfg()
        self.q: "queue.Queue" = queue.Queue()
        self.thread = None
        self._build()
        self.after(150, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # -- layout ----------------------------------------------------------
    def _label(self, parent, text, size=10, color=DIM, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", BG), fg=color,
                        font=("Segoe UI", size), anchor="w", justify="left", **kw)

    def _entry(self, parent, value=""):
        e = tk.Entry(parent, bg="#0a0e15", fg=INK, insertbackground=INK,
                     relief="flat", font=("Consolas", 10),
                     highlightthickness=1, highlightbackground=LINE,
                     highlightcolor="#3d8cff")
        e.insert(0, value)
        return e

    def _build(self):
        head = tk.Frame(self, bg=PANEL, height=58)
        head.pack(fill="x")
        tk.Label(head, text="RAVENMAP", bg=PANEL, fg=INK,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=14, pady=(12, 0))
        self.dot = tk.Label(head, text="●", bg=PANEL, fg=DIM, font=("Segoe UI", 13))
        self.dot.pack(side="right", padx=(0, 6), pady=12)
        self.state = tk.Label(head, text="stopped", bg=PANEL, fg=DIM,
                              font=("Segoe UI", 10))
        self.state.pack(side="right", pady=12)
        tk.Label(self, text="This computer watches your camera. No video leaves it.",
                 bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=14, pady=(0, 10))

        # 🚨 THE NAV IS PACKED BEFORE THE BODY, AND THAT IS NOT COSMETIC.
        # tkinter gives space to widgets in pack order, so a body packed with
        # expand=True first takes everything and a later side="bottom" strip is
        # pushed off the window entirely. That is exactly what happened: the row
        # of links to every mode - the reason this is an app and not just a
        # relay - was invisible in the built exe.
        nav = tk.Frame(self, bg=PANEL)
        nav.pack(fill="x", side="bottom")
        for label, path in (("Map", "/"), ("Driving", "/drive"), ("Review", "/rv"),
                            ("Aim", "/aim"), ("Status", "/status")):
            tk.Button(nav, text=label, command=lambda p=path: self._open(p),
                      bg=PANEL, fg=GREEN, relief="flat", cursor="hand2",
                      font=("Segoe UI", 9, "bold"), activebackground="#1b2432",
                      activeforeground=INK).pack(side="left", expand=True,
                                                 fill="x", pady=8)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14)

        self._label(body, "Camera stream address  (rtsp://… or 0 for a USB webcam)").pack(fill="x", pady=(8, 3))
        self.e_src = self._entry(body, self.cfg.get("source", ""))
        self.e_src.pack(fill="x", ipady=5)

        row = tk.Frame(body, bg=BG); row.pack(fill="x", pady=(10, 0))
        left = tk.Frame(row, bg=BG); left.pack(side="left", fill="x", expand=True)
        right = tk.Frame(row, bg=BG); right.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._label(left, "Camera id").pack(fill="x", pady=(0, 3))
        self.e_node = self._entry(left, self.cfg.get("node", "")); self.e_node.pack(fill="x", ipady=5)
        self._label(right, "Token").pack(fill="x", pady=(0, 3))
        self.e_tok = self._entry(right, self.cfg.get("token", "")); self.e_tok.pack(fill="x", ipady=5)

        row2 = tk.Frame(body, bg=BG); row2.pack(fill="x", pady=(10, 0))
        l2 = tk.Frame(row2, bg=BG); l2.pack(side="left", fill="x", expand=True)
        r2 = tk.Frame(row2, bg=BG); r2.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._label(l2, "Latitude").pack(fill="x", pady=(0, 3))
        self.e_lat = self._entry(l2, str(self.cfg.get("lat", ""))); self.e_lat.pack(fill="x", ipady=5)
        self._label(r2, "Longitude").pack(fill="x", pady=(0, 3))
        self.e_lon = self._entry(r2, str(self.cfg.get("lon", ""))); self.e_lon.pack(fill="x", ipady=5)

        tk.Button(body, text="I don't have a camera id yet  —  set one up",
                  command=lambda: self._open("/IPCamera"), bg="#141c28", fg=INK,
                  relief="flat", font=("Segoe UI", 9), cursor="hand2",
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(fill="x", pady=(10, 0), ipady=6)

        btns = tk.Frame(body, bg=BG); btns.pack(fill="x", pady=(14, 0))
        self.b_start = tk.Button(btns, text="Start watching", command=self._toggle,
                                 bg=RED, fg="#0b0e13", relief="flat", cursor="hand2",
                                 font=("Segoe UI", 11, "bold"), activebackground="#ff6b74")
        self.b_start.pack(side="left", fill="x", expand=True, ipady=9)
        tk.Button(btns, text="Test camera", command=self._test, bg="#141c28", fg=INK,
                  relief="flat", font=("Segoe UI", 10), cursor="hand2",
                  activebackground="#1b2432", activeforeground=INK
                  ).pack(side="left", padx=(8, 0), ipady=9, ipadx=12)

        self.autostart = tk.BooleanVar(value=bool(self.cfg.get("autostart")))
        tk.Checkbutton(body, text="Start automatically when this computer starts",
                       variable=self.autostart, command=self._autostart,
                       bg=BG, fg=DIM, selectcolor=PANEL, activebackground=BG,
                       activeforeground=INK, font=("Segoe UI", 9), anchor="w"
                       ).pack(fill="x", pady=(10, 0))

        self._label(body, "Sent nothing yet", color=DIM).pack(fill="x", pady=(12, 2))
        self.lbl_sent = body.winfo_children()[-1]

        self.log = tk.Text(body, height=9, bg="#080b10", fg="#b9c6d8", relief="flat",
                           font=("Consolas", 9), highlightthickness=1,
                           highlightbackground=LINE, wrap="word")
        self.log.pack(fill="both", expand=True, pady=(4, 10))
        self.log.configure(state="disabled")


    # -- actions ---------------------------------------------------------
    def _hub(self):
        return self.cfg.get("hub", HUB_DEFAULT)

    def _open(self, path):
        webbrowser.open(self._hub().rstrip("/") + path)

    def _say(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _values(self):
        src = self.e_src.get().strip()
        # A bare number means a USB webcam, which cv2 wants as an int.
        if src.isdigit():
            src = int(src)
        return {"source": src, "node": self.e_node.get().strip(),
                "token": self.e_tok.get().strip(), "hub": self._hub(),
                "every": 5.0,
                "lat": float(self.e_lat.get() or 0), "lon": float(self.e_lon.get() or 0)}

    def _test(self):
        try:
            v = self._values()
        except ValueError:
            return messagebox.showerror(APP, "Latitude and longitude must be numbers.")
        if not v["source"] and v["source"] != 0:
            return messagebox.showerror(APP, "Enter the camera address first.")
        self._say("testing the camera…")

        def work():
            try:
                from detect import relay
                cap = relay.open_stream(v["source"])
                if cap is None:
                    self.q.put(("log", "could not open it. Check the address, the "
                                       "username and password, and that RTSP is on."))
                    return
                ok, frame = cap.read()
                cap.release()
                if not ok:
                    self.q.put(("log", "it opened but sent no pictures. Try the "
                                       "camera's SUB stream."))
                    return
                self.q.put(("log", f"camera works: {frame.shape[1]}x{frame.shape[0]}"))
            except Exception as exc:
                self.q.put(("log", f"test failed: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _toggle(self):
        if self.thread and self.thread.is_alive():
            self.thread.stop.set()
            self.b_start.configure(text="Stopping…", state="disabled")
            return
        try:
            v = self._values()
        except ValueError:
            return messagebox.showerror(APP, "Latitude and longitude must be numbers.")
        if not v["node"] or not v["token"]:
            return messagebox.showerror(
                APP, "This camera needs an id and a token.\n\n"
                     "Use the button below the boxes to create one.")
        if not v["hub"]:
            return messagebox.showerror(
                APP, "No RavenMap hub configured.\n\n"
                     "Set RAVEN_HUB (or legacy SPARROW_HUB) before starting "
                     "RavenMap4Biz.")
        self.cfg.update({k: v[k] for k in ("node", "token", "lat", "lon")})
        self.cfg["source"] = self.e_src.get().strip()
        save_cfg(self.cfg)
        self.thread = RelayThread(v, self.q)
        self.thread.start()
        self.b_start.configure(text="Stop", bg="#141c28", fg=INK)

    def _autostart(self):
        on = self.autostart.get()
        self.cfg["autostart"] = on
        save_cfg(self.cfg)
        try:
            msg = set_autostart(on)
            self._say(msg)
        except Exception as exc:
            self._say(f"could not change startup: {exc}")
            self.autostart.set(not on)

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._say(val)
                elif kind == "sent":
                    self.lbl_sent.configure(
                        text=f"Sent {val} vehicle{'s' if val != 1 else ''} to the map",
                        fg=GREEN)
                elif kind == "state":
                    running = val == "running"
                    self.state.configure(text="watching" if running else "stopped",
                                         fg=GREEN if running else DIM)
                    self.dot.configure(fg=GREEN if running else DIM)
                    if not running:
                        self.b_start.configure(text="Start watching", bg=RED,
                                               fg="#0b0e13", state="normal")
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _close(self):
        if self.thread and self.thread.is_alive():
            self.thread.stop.set()
        self.destroy()


# --------------------------------------------------------------------------
# start with the computer
# --------------------------------------------------------------------------
def set_autostart(on: bool) -> str:
    """Run at login, using whatever the platform actually honours.

    ⚠️ THE REGISTRY Run KEY, NOT A SERVICE. A Windows service runs as SYSTEM in
    session 0, which has no desktop and frequently no route to a camera on the
    user's network - and this app has a window. Run-at-login is what matches
    what the thing actually is: a program the owner started, that comes back
    after a reboot without them thinking about it.
    """
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
    # Linux: a user unit, so it needs no root and stops when the user logs out.
    unit = Path.home() / ".config" / "systemd" / "user" / "sparrowmap.service"
    if on:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=RavenMap relay\n\n"
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
