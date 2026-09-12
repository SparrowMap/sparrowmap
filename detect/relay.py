"""The business bridge: an IP camera feeds SparrowMap without sending video.

    python -m detect.relay --source rtsp://USER:PASS@CAMERA:554/stream1 \\
        --node n_xxxx --token TOKEN --hub https://map.sparrowmap.com

    # a Raspberry Pi in a car: a USB camera and a GPS dongle, one command
    python3 relay.py --source /dev/video0 --gps gpsd://127.0.0.1:2947 \\
        --enroll "Dashcam"          # first run registers it; later runs just --source and --gps

🚨 WHY THIS IS A RELAY AND NOT A NODE.
`run_live.py` classifies on the spot, which needs CLIP and the trained head -
a reasonable ask for the machine that already runs the project, and an
unreasonable one for the PC behind a shop counter. So this does what a PHONE
node does: find a vehicle, send a crop that cannot carry a plate, and let the
home GPU decide whether it was a patrol car. That path is already built and
proven end to end (inbox -> box_puller -> head -> review pen), which is the
whole reason to reuse it rather than invent a second one.

🚨 AND WHY IT MUST NEVER SEND VIDEO. The obvious "service" shape is to stream
the camera to a server and classify centrally. That would mean SparrowMap
receiving video from other people's premises - the exact thing this project
promises never to do, and the thing that would make it indistinguishable from
what it opposes. The stream is opened HERE, on the owner's own network, and the
only thing that leaves the building is a crop small enough that no plate
survives it. The server enforces that independently: `snapshot.store_subresolution`
REFUSES anything with a long edge over 200px, so a bug here cannot quietly
become a privacy breach.

The model is the SAME yolo11s.onnx the browser node runs, over onnxruntime on
the CPU. One model, two clients, no torch: a business installs opencv-python,
onnxruntime and numpy, and nothing else.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# 🚨 SET BEFORE cv2 IS IMPORTED, AND NOT THE OPTION THE DOCS SUGGEST.
# Measured on this build: a dead RTSP host blocks for 30.1s and the documented
# env timeouts (`stimeout`, `timeout`, with or without rtsp_transport) are ALL
# ignored. CAP_PROP_OPEN_TIMEOUT_MSEC on the capture does bind - see open_stream.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2                                                     # noqa: E402
import numpy as np                                             # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 🚨 THIS FILE MUST RUN ON ITS OWN, WITH NO REPOSITORY AROUND IT.
# The onboarding page hands a business ONE file. Asking a shop to clone a git
# repository to run a background service is where most of them would stop, and
# every one that did would be a camera the map never got. Nothing here imports
# from the project, so the only thing that tied it to a checkout was the model
# path - resolved below, and fetched from the hub when it is absent.
MODEL_NAME = "yolo11s.onnx"
MODEL_SHA256 = "90905237dd6974ce798f1cae08e2dbd4dac6ca19e6b0183f63b46688f053e5b1"
MODEL_BYTES = 37925610


def model_path(hub: str) -> Path:
    """The detector weights, from the checkout if present, else cached locally.

    Verified by SHA-256 every time, not just on download. A 38 MB file fetched
    over the network into a long-running background service is exactly the thing
    that should not be trusted because it happens to exist - a truncated
    download and a tampered one look identical to `if path.exists()`.
    """
    local = ROOT / "public" / "vendor" / MODEL_NAME          # running in the repo
    if local.exists():
        return local
    cache = Path.home() / ".sparrowmap" / MODEL_NAME
    if cache.exists() and _sha256(cache) == MODEL_SHA256:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    url = hub.rstrip("/") + "/vendor/" + MODEL_NAME
    print(f"  fetching the detector ({MODEL_BYTES // 1024 // 1024} MB) from {url}")
    print("  this happens once")
    tmp = cache.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            print(f"  {got // 1024 // 1024} MB")
    print()
    digest = _sha256(tmp)
    if digest != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"the downloaded detector does not match its expected "
                         f"checksum (got {digest[:16]}...). Refusing to run it.")
    tmp.replace(cache)
    return cache


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()

# Every one of these mirrors the browser node deliberately. Two clients feeding
# one pipeline should differ in how they get frames and in nothing else - if the
# thresholds drift apart, the same car produces a sighting from a phone and
# silence from a shop, and nobody would know which was right.
# ⚠️ READ FROM THE MODEL, NOT TYPED IN. A hardcoded 640 was rejected outright
# ("Got: 640 Expected: 320") because this export is 320x320 - the same size the
# browser node uses. Asking the model removes the chance of the two clients
# drifting apart on the one number they must agree about.
SIZE = 320          # replaced at startup by the model's real input size
CONF = 0.45
IOU_DUP = 0.45
SEND_EDGE = 200     # long edge of what is uploaded: the plate-illegible cap
MIN_FRAMES = 3      # seen this many times before it counts as a real vehicle
GONE_S = 0.9        # unseen this long and the pass is over
SEND_EVERY_S = 5.0  # at most one crop per this often, per camera
VEHICLE = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

OPEN_TIMEOUT_MS = 8000
READ_TIMEOUT_MS = 8000
UA = "SparrowMap-relay/0.1"


# --------------------------------------------------------------------------
# stream
# --------------------------------------------------------------------------
def open_stream(url: str):
    # A camera plugged into THIS machine: a USB webcam (`0`, `/dev/video0`) or a
    # Pi camera module bridged to V4L2. No ffmpeg, no timeouts to bind - the
    # kernel either hands frames over or it does not.
    if url.isdigit() or url.startswith("/dev/video"):
        cap = cv2.VideoCapture(int(url) if url.isdigit() else url, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        # Ask for a modest frame: the detector runs at 320 px anyway, and a Pi
        # decoding 1080p to find a 320 px car is a Pi that runs hot for nothing.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap
    cap = cv2.VideoCapture()
    cap.open(url, cv2.CAP_FFMPEG,
             [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
              cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS])
    if not cap.isOpened():
        return None
    # A camera streams whether or not anyone is reading, so a slow consumer
    # falls further behind for ever. Keep the buffer at one frame: this relay
    # wants the NEWEST frame, and a backlog is worse than a dropped frame -
    # the same lesson run_live.py records about MJPEG queueing.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def redact(url: str) -> str:
    """Never print camera credentials: this output ends up in logs and pastes."""
    if "@" not in url:
        return url
    head, _, tail = url.partition("://")
    creds, _, rest = tail.partition("@")
    user = creds.split(":", 1)[0] if ":" in creds else ""
    return f"{head}://{user}:***@{rest}" if user else f"{head}://***@{rest}"


# --------------------------------------------------------------------------
# detection - the browser node's maths, in python
# --------------------------------------------------------------------------
def letterbox(frame):
    h, w = frame.shape[:2]
    s = min(SIZE / w, SIZE / h)
    dw, dh = int(round(w * s)), int(round(h * s))
    ox, oy = (SIZE - dw) // 2, (SIZE - dh) // 2
    canvas = np.full((SIZE, SIZE, 3), 114, dtype=np.uint8)
    canvas[oy:oy + dh, ox:ox + dw] = cv2.resize(frame, (dw, dh))
    blob = canvas[:, :, ::-1].astype(np.float32) / 255.0      # BGR->RGB, 0..1
    return np.ascontiguousarray(blob.transpose(2, 0, 1)[None]), s, ox, oy


def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if inter <= 0:
        return 0.0
    ar = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ar if ar > 0 else 0.0


def decode(out, s, ox, oy):
    """YOLO11 output (1, 84, N) -> vehicle boxes in FRAME coordinates."""
    d = out[0]
    n = d.shape[1]
    hits = []
    for i in range(n):
        best, bs = -1, 0.0
        for c in VEHICLE:
            v = float(d[4 + c, i])
            if v > bs:
                bs, best = v, c
        if bs < CONF:
            continue
        cx, cy, w, h = (float(d[0, i]), float(d[1, i]),
                        float(d[2, i]), float(d[3, i]))
        hits.append({
            "cls": VEHICLE[best], "score": bs,
            "box": [(cx - w / 2 - ox) / s, (cy - h / 2 - oy) / s,
                    (cx + w / 2 - ox) / s, (cy + h / 2 - oy) / s],
        })
    hits.sort(key=lambda x: -x["score"])
    keep = []
    for hh in hits:
        if not any(iou(k["box"], hh["box"]) > IOU_DUP for k in keep):
            keep.append(hh)
    return keep


def crop_of(frame, box):
    """The vehicle, shrunk below plate legibility BEFORE it leaves this machine."""
    fh, fw = frame.shape[:2]
    x0, y0, x1, y1 = box
    # Asymmetric padding: a roof light bar sits just outside the detector's
    # box, and a crop that clips it removes the feature the classifier decides
    # on. Nothing diagnostic hangs off the bottom of a car, and widening the
    # sides only buys pavement and other people's vehicles. Same numbers as the
    # browser clients - one crop shape across every node kind.
    bw, bh = (x1 - x0), (y1 - y0)
    pad_x, pad_top, pad_bot = bw * 0.10, bh * 0.28, bh * 0.08
    sx, sy = max(0, int(x0 - pad_x)), max(0, int(y0 - pad_top))
    ex, ey = min(fw, int(x1 + pad_x)), min(fh, int(y1 + pad_bot))
    if ex - sx < 8 or ey - sy < 8:
        return None
    sub = frame[sy:ey, sx:ex]
    s = min(1.0, SEND_EDGE / max(sub.shape[0], sub.shape[1]))
    if s < 1.0:
        sub = cv2.resize(sub, (max(1, int(sub.shape[1] * s)),
                               max(1, int(sub.shape[0] * s))),
                         interpolation=cv2.INTER_AREA)
    # 🚨 BLOCK THE PLATE OUT. SEND_EDGE caps the CROP, not the plate, so it only
    # makes a plate illegible when the vehicle is far enough away. A camera
    # looking down at close traffic scales the same 200px over a much larger
    # plate, and it stays readable. Same geometric band as the browser clients:
    # a plate sits in the lower middle of a vehicle from either end, while the
    # light bar, decals and body shape - what the decision actually rests on -
    # are all above it.
    ch, cw = sub.shape[:2]
    vx = (pad_x / (ex - sx)) * cw
    vy = (pad_top / (ey - sy)) * ch
    vw = (bw / (ex - sx)) * cw
    vh = (bh / (ey - sy)) * ch
    px0 = max(0, int(vx + vw * 0.20))
    py0 = max(0, int(vy + vh * 0.56))
    px1 = min(cw, int(vx + vw * 0.80))
    py1 = min(ch, int(vy + vh * 0.96))
    if px1 > px0 and py1 > py0:
        sub[py0:py1, px0:px1] = 0

    ok, buf = cv2.imencode(".jpg", sub, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


# --------------------------------------------------------------------------
# a camera that MOVES: GPS
# --------------------------------------------------------------------------
# A dashcam node posts every crop with the fix it had at that moment, and the
# hub plots a `mobile` node's sightings at the posted point (nodes.sighting_position
# keys on kind). Two sources, both without extra packages:
#   gpsd://host:port   gpsd's JSON stream (the usual Pi setup: a USB dongle,
#                      `sudo apt install gpsd`, and gpsd owns the device)
#   /dev/ttyACM0       raw NMEA straight from the dongle when gpsd is not running
# 🚨 NO FIX, NO DOT. A crop taken while the receiver was still searching is not
# posted at the last known point: a car that has driven two miles since the fix
# would put a patrol sighting on the wrong street, and a wrong street on this
# map is worse than a missing one. FIX_MAX_AGE_S is the whole allowance.
FIX_MAX_AGE_S = 15.0
GPS = {"lat": None, "lon": None, "ts": 0.0, "speed": None, "err": ""}


def _nmea_deg(v: str, hemi: str) -> Optional[float]:
    if not v:
        return None
    d, m = divmod(float(v), 100.0)
    deg = int(d) + m / 60.0
    return -deg if hemi in ("S", "W") else deg


def _gps_nmea(dev: str) -> None:
    """Read $..RMC / $..GGA lines from a serial device, forever."""
    while True:
        try:
            try:
                import serial                                # pyserial, if present
                f = serial.Serial(dev, 9600, timeout=5)
            except Exception:
                # No pyserial, or a device it cannot configure: read it as a
                # file. A USB CDC dongle ignores baud; a UART HAT needs
                # `stty -F /dev/serial0 9600` once (the guide says so).
                f = open(dev, "rb", buffering=0)
            for raw in iter(lambda: f.readline(), b""):
                line = raw.decode("ascii", "ignore").strip()
                if not line.startswith("$") or "*" not in line:
                    continue
                parts = line[1:].split("*")[0].split(",")
                kind = parts[0][2:]
                if kind == "RMC" and len(parts) > 7 and parts[2] == "A":
                    lat, lon = _nmea_deg(parts[3], parts[4]), _nmea_deg(parts[5], parts[6])
                    if lat is not None and lon is not None:
                        GPS.update(lat=lat, lon=lon, ts=time.time(),
                                   speed=float(parts[7] or 0) * 0.514444, err="")
                elif kind == "GGA" and len(parts) > 6 and parts[6] not in ("", "0"):
                    lat, lon = _nmea_deg(parts[2], parts[3]), _nmea_deg(parts[4], parts[5])
                    if lat is not None and lon is not None:
                        GPS.update(lat=lat, lon=lon, ts=time.time(), err="")
            GPS["err"] = "serial closed"
        except Exception as exc:
            GPS["err"] = f"{exc.__class__.__name__}: {exc}"
        time.sleep(3)


def _gps_gpsd(host: str, port: int) -> None:
    """Follow gpsd's JSON stream (TPV reports), forever."""
    import socket
    while True:
        try:
            with socket.create_connection((host, port), timeout=10) as sk:
                sk.sendall(b'?WATCH={"enable":true,"json":true}\n')
                sk.settimeout(30)
                buf = b""
                while True:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("class") == "TPV" and d.get("mode", 0) >= 2 \
                                and "lat" in d and "lon" in d:
                            GPS.update(lat=float(d["lat"]), lon=float(d["lon"]),
                                       ts=time.time(), speed=d.get("speed"), err="")
            GPS["err"] = "gpsd closed"
        except Exception as exc:
            GPS["err"] = f"{exc.__class__.__name__}: {exc}"
        time.sleep(3)


def start_gps(spec: str) -> None:
    import threading
    if spec.startswith("gpsd://"):
        hp = spec[len("gpsd://"):] or "127.0.0.1:2947"
        host, _, port = hp.partition(":")
        t = threading.Thread(target=_gps_gpsd, args=(host or "127.0.0.1", int(port or 2947)),
                             daemon=True)
    else:
        t = threading.Thread(target=_gps_nmea, args=(spec,), daemon=True)
    t.start()


def gps_fix() -> Optional[tuple]:
    if GPS["lat"] is None or time.time() - GPS["ts"] > FIX_MAX_AGE_S:
        return None
    return GPS["lat"], GPS["lon"]


# --------------------------------------------------------------------------
# enrolment - so a Pi in a car is ONE command, not a form on another device
# --------------------------------------------------------------------------
NODE_FILE = Path.home() / ".sparrowmap" / "node.json"


def enroll(hub: str, name: str, kind: str, lat: float, lon: float) -> dict:
    """Register this camera with the hub once and remember the id + token."""
    body = {"name": name[:80], "kind": kind, "lat": lat, "lon": lon}
    req = urllib.request.Request(
        hub.rstrip("/") + "/api/enroll", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read() or b"{}")
    if not d.get("id") or not d.get("token"):
        raise RuntimeError(f"hub did not enrol the camera: {d.get('error') or d}")
    NODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    NODE_FILE.write_text(json.dumps({"id": d["id"], "token": d["token"],
                                     "name": name, "kind": kind, "hub": hub}))
    try:
        NODE_FILE.chmod(0o600)          # the token is the camera's identity
    except OSError:
        pass
    return d


# --------------------------------------------------------------------------
# posting
# --------------------------------------------------------------------------
def post(hub: str, node: str, token: str, lat: float, lon: float,
         crop: str, cls_name: str, score: float) -> dict:
    body = {"node_id": node, "ts": time.time(), "source": "phone_node",
            "body": cls_name, "det_conf": round(float(score), 3),
            "plate_text": "", "plate_conf": 0, "evidence": {},
            "snap_b64": crop, "lat": lat, "lon": lon}
    req = urllib.request.Request(
        hub.rstrip("/") + "/api/sightings", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token,
                 # 🚨 REQUIRED. Cloudflare 1010s a request with no User-Agent,
                 # which is how desktop ingest died silently once already.
                 "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True,
                    help="rtsp:// or http:// stream, or a local camera: 0, /dev/video0")
    ap.add_argument("--node", help="camera id from enrolment (or use --enroll once)")
    ap.add_argument("--token", help="that camera's token")
    ap.add_argument("--hub", default="https://map.sparrowmap.com")
    ap.add_argument("--lat", type=float, help="fixed camera: where it is")
    ap.add_argument("--lon", type=float)
    ap.add_argument("--gps", metavar="SRC",
                    help="moving camera: gpsd://127.0.0.1:2947 or a serial device "
                         "like /dev/ttyACM0. Each crop is posted at the current fix.")
    ap.add_argument("--enroll", metavar="NAME",
                    help="register this camera with the hub on first run and remember "
                         "it in ~/.sparrowmap/node.json; later runs need no --node/--token")
    ap.add_argument("--kind", default=None, choices=["fixed", "mobile"],
                    help="with --enroll: 'mobile' for a dashcam (default when --gps is set)")
    ap.add_argument("--every", type=float, default=SEND_EVERY_S,
                    help="minimum seconds between uploads")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and report, but send nothing")
    a = ap.parse_args()

    # Where am I? A fixed camera is told once; a dashcam asks the receiver.
    if a.gps:
        start_gps(a.gps)
    elif a.lat is None or a.lon is None:
        ap.error("give --lat and --lon for a fixed camera, or --gps for a moving one")

    # Who am I? Remembered from a previous --enroll, given on the command line,
    # or registered right now.
    if not a.node and not a.enroll and NODE_FILE.exists():
        try:
            saved = json.loads(NODE_FILE.read_text())
            if saved.get("hub", a.hub) == a.hub:
                a.node, a.token = saved["id"], saved["token"]
                print(f"  this camera is {a.node} ({saved.get('name')}, {saved.get('kind')})")
        except (ValueError, KeyError):
            pass
    if a.enroll and not a.dry_run:
        kind = a.kind or ("mobile" if a.gps else "fixed")
        if a.gps:
            print("  waiting for a GPS fix to enrol at...", flush=True)
            deadline = time.time() + 180
            while gps_fix() is None and time.time() < deadline:
                time.sleep(1)
            fix = gps_fix()
            if fix is None:
                print(f"  no GPS fix in 3 minutes ({GPS['err'] or 'receiver still searching'}). "
                      "Put the antenna where it can see the sky and try again.")
                return 1
            lat0, lon0 = fix
        else:
            lat0, lon0 = a.lat, a.lon
        d = enroll(a.hub, a.enroll, kind, lat0, lon0)
        a.node, a.token = d["id"], d["token"]
        print(f"  enrolled as {a.node} ({kind}); remembered in {NODE_FILE}")
    if not a.dry_run and (not a.node or not a.token):
        ap.error("no camera identity: pass --node and --token, or --enroll NAME once")

    mp = model_path(a.hub)
    import onnxruntime as ort
    sess = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    global SIZE
    shape = sess.get_inputs()[0].shape          # [1, 3, H, W]
    if isinstance(shape[-1], int) and shape[-1] > 0:
        SIZE = int(shape[-1])
    print(f"  model input {SIZE}x{SIZE}")
    print(f"relay: {redact(a.source)} -> {a.hub} as {a.node or '(dry run)'}"
          f"{' via ' + a.gps if a.gps else ''}{' (dry run)' if a.dry_run else ''}")

    cap = open_stream(a.source)
    if cap is None:
        print("could not open the stream. Run tools/test_stream.py on this URL.")
        return 1

    tracks: list = []
    nid = 0
    last_send = last_nofix = 0.0
    sent = seen = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                # A camera that drops out must not take the relay with it: an
                # outdoor camera reboots, loses PoE, or the wifi dips, and the
                # bridge has to still be running when it comes back.
                print("stream dropped; reopening in 3s")
                cap.release()
                time.sleep(3)
                cap = open_stream(a.source)
                if cap is None:
                    time.sleep(7)
                continue

            blob, s, ox, oy = letterbox(frame)
            out = sess.run(None, {iname: blob})[0]
            hits = decode(out, s, ox, oy)
            now = time.time()

            for h in hits:
                match = None
                for t in tracks:
                    if iou(t["box"], h["box"]) > 0.3:
                        match = t
                        break
                if match is None:
                    nid += 1
                    match = {"id": nid, "seen": 0, "best": 0.0, "crop": None,
                             "sent": False}
                    tracks.append(match)
                match["box"] = h["box"]
                match["last"] = now
                match["seen"] += 1
                match["cls"] = h["cls"]
                # Keep the LARGEST view of this vehicle, which is the one with
                # the most pixels on the car - the same rule the pass pipeline
                # uses to pick what it banks.
                area = (h["box"][2] - h["box"][0]) * (h["box"][3] - h["box"][1])
                if area > match["best"]:
                    match["best"] = area
                    match["crop"] = crop_of(frame, h["box"])

            for t in list(tracks):
                if now - t.get("last", 0) < GONE_S:
                    continue
                tracks.remove(t)
                # MIN_FRAMES kills the one-frame flickers that would otherwise
                # each become a sighting.
                if t["seen"] < MIN_FRAMES or not t["crop"] or t["sent"]:
                    continue
                if now - last_send < a.every:
                    continue
                seen += 1
                # A moving camera posts where it IS, or not at all.
                if a.gps:
                    fix = gps_fix()
                    if fix is None:
                        if now - last_nofix > 30:
                            print(f"  no GPS fix ({GPS['err'] or 'searching'}); "
                                  f"a {t['cls']} went unposted")
                            last_nofix = now
                        continue
                    lat, lon = fix
                else:
                    lat, lon = a.lat, a.lon
                if a.dry_run:
                    print(f"  would send {t['cls']} (seen {t['seen']}x) at {lat:.5f},{lon:.5f}")
                    last_send = now
                    continue
                try:
                    r = post(a.hub, a.node, a.token, lat, lon,
                             t["crop"], t["cls"], 0.9)
                    last_send = now
                    t["sent"] = True
                    sent += 1
                    if r.get("error"):
                        print(f"  hub refused: {r['error']}")
                    else:
                        print(f"  sent {t['cls']} -> sighting {r.get('id')}"
                              f"  (total {sent})")
                except urllib.error.HTTPError as e:
                    print(f"  HTTP {e.code}: {e.read()[:120]!r}")
                except Exception as exc:
                    print(f"  send failed ({exc.__class__.__name__}); "
                          f"the camera keeps running")
    except KeyboardInterrupt:
        print(f"\nstopped. {seen} vehicles completed, {sent} crops sent.")
    finally:
        if cap is not None:
            cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
