"""The business bridge: an IP camera feeds SparrowMap without sending video.

    python -m detect.relay --source rtsp://USER:PASS@CAMERA:554/stream1 \\
        --node n_xxxx --token TOKEN --hub https://map.sparrowmap.com

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
UA = "RavenMap-relay/0.1"


# --------------------------------------------------------------------------
# stream
# --------------------------------------------------------------------------
def open_stream(url: str):
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
    ap.add_argument("--source", required=True, help="rtsp:// or http:// stream")
    ap.add_argument("--node", required=True, help="camera id from enrolment")
    ap.add_argument("--token", required=True, help="that camera's token")
    ap.add_argument("--hub", default=os.environ.get("RAVEN_HUB") or os.environ.get("SPARROW_HUB"),
                    help="hub URL (or set RAVEN_HUB / legacy SPARROW_HUB)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--every", type=float, default=SEND_EVERY_S,
                    help="minimum seconds between uploads")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and report, but send nothing")
    a = ap.parse_args()
    if not a.hub:
        ap.error(
            "No RavenMap hub configured. Set RAVEN_HUB to your hub URL, "
            "or pass --hub explicitly. Legacy SPARROW_HUB is also accepted."
        )

    mp = model_path(a.hub)
    import onnxruntime as ort
    sess = ort.InferenceSession(str(mp), providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    global SIZE
    shape = sess.get_inputs()[0].shape          # [1, 3, H, W]
    if isinstance(shape[-1], int) and shape[-1] > 0:
        SIZE = int(shape[-1])
    print(f"  model input {SIZE}x{SIZE}")
    print(f"relay: {redact(a.source)} -> {a.hub} as {a.node}"
          f"{' (dry run)' if a.dry_run else ''}")

    cap = open_stream(a.source)
    if cap is None:
        print("could not open the stream. Run tools/test_stream.py on this URL.")
        return 1

    tracks: list = []
    nid = 0
    last_send = 0.0
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
                if a.dry_run:
                    print(f"  would send {t['cls']} (seen {t['seen']}x)")
                    last_send = now
                    continue
                try:
                    r = post(a.hub, a.node, a.token, a.lat, a.lon,
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
