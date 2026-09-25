"""The camera node. Watches a live stream until stopped.

Different from run_video.py in the way that matters for a real node: a stream
never ends, so frames must be DROPPED rather than queued when the pipeline
falls behind, or the node drifts further behind reality every second.

⚠️ THAT SENTENCE USED TO BE A CLAIM RATHER THAN A FACT. `cv2.VideoCapture`
queues on an HTTP MJPEG source, so this loop analysed a backlog for as long as
it existed - the overlay lag was queue depth, not processing time. Frames are
now actually dropped, by `detect/grabber.py`, and it was measured rather than
asserted (`tools/probe_latency.py`).

    python detect\\run_live.py --post --visual --node n_xxx --token ...
    python detect\\run_live.py --seconds 180        # bounded, for a test run

⚠️ IT RUNS UNTIL STOPPED, AND THAT IS THE POINT. `--seconds` defaulted to 180
and every "the map has gone quiet / 0 cameras online" report so far turned out
to be that timer expiring rather than a fault. A camera that switches itself
off after three minutes is not watching anything. Use `--seconds` only for a
deliberate bounded test.

Consumes camctl's MJPEG stream by default, so it does not fight the control app
for the camera - one process owns the device, everyone else reads HTTP. It also
posts its live boxes back to camctl so the owner can watch the detector work
(operator-only; see `overlay_loop`) and banks every vehicle crop for training
(see detect/bank.py - runtime should make the system BETTER, not just busier).
"""

from __future__ import annotations

import argparse
import json
import socket
import signal
import sys
import threading
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classify              # noqa: E402
import node_key              # noqa: E402
import snapshot              # noqa: E402
from core import DATA, NODE_UA, ROOT  # noqa: E402
from detect import bank, visual    # noqa: E402
from detect.grabber import FrameGrabber   # noqa: E402
from detect.pipeline import (MIN_TRACK_FRAMES, TRACK_TIMEOUT_FRAMES,  # noqa: E402
                             PlateReader, VehiclePass, VehicleTracker, frame_quality)
from detect import priority   # noqa: E402

EVAL = DATA / "eval" / "live"

# How often a still-moving vehicle is re-classified for the live overlay.
# Every 8th processed frame is ~2 looks per second at the current rate, which
# is far more than a human watching a box needs and cheap now the models are
# on the GPU. Verdicts are cached per track id between looks.
LIVE_ID_EVERY = 8

# How long the pipeline may go without processing a frame before this node stops
# claiming to be online. Generous next to the hub's 90s online window, so a slow
# model or a brief camera hiccup does not flap the map - but a wedged pipeline
# goes dark within a couple of minutes instead of lying for hours.
STALL_AFTER_S = 120
LIVE_ID: dict = {}
VehicleIdentifier = None


def ensure_signing_key(args) -> None:
    """Give this camera an ed25519 identity, once, at startup.

    🚨 THIS IS WHAT MAKES THE FEATURE REACH EXISTING CAMERAS. Enrolment only
    runs when somebody SAVES a placement, so registering the key there alone
    would have left every camera already in the field unsigned for ever - the
    change would have shipped and altered nothing, which is exactly how pubkey
    came to be NULL on 158 nodes.

    Failure is never fatal. An unsigned camera works; a camera that refuses to
    start because it could not register a key does not, and this runs before
    the first frame.
    """
    if not (args.node and args.token):
        return
    import urllib.request        # imported per-function throughout this file
    try:
        if node_key.load(ROOT, args.node):
            print(f"  signing as {args.node} (key already registered)")
            return
        _priv, pub = node_key.create(ROOT, args.node)
        req = urllib.request.Request(
            f"{args.hub}/api/node/key", method="POST",
            data=json.dumps({"node_id": args.node, "pubkey": pub}).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": NODE_UA,
                     "Authorization": f"Bearer {args.token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read())
        print(f"  registered a signing key for {args.node}; "
              f"sightings from here are signed")
    except Exception as exc:
        # ⚠️ AND REMOVE THE KEY WE JUST MADE. If the hub did not record the
        # public half, keeping the private half achieves nothing and guarantees
        # a mismatch the next time this runs - it would find a local key, skip
        # registration for ever, and sign against a pubkey the hub never got.
        try:
            node_key._key_path(ROOT, args.node).unlink(missing_ok=True)
        except Exception:
            pass
        print(f"  could not register a signing key ({exc}); "
              f"posting unsigned, which still works")


def main() -> None:
    # 🚨 CLAIM IT HERE, NOT FROM THE LAUNCHER. The launcher restarts
    # this process in a loop and each restart inherits the launcher's
    # priority, so a value set from outside is gone by the next crash.
    priority.claim("detector")
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="http://localhost:8160/stream.mjpg")
    # 0 = run until stopped. That is the normal mode for a real node: a camera
    # that switches itself off after three minutes is not watching anything,
    # and every "the map has gone quiet" report so far has turned out to be
    # this timer expiring rather than a fault.
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds; 0 (default) runs until stopped")
    ap.add_argument("--plate-every", type=int, default=2)
    ap.add_argument("--post", action="store_true",
                    help="send sightings to the hub so they appear on the map")
    ap.add_argument("--hub", default="http://localhost:8150")
    ap.add_argument("--node", default="", help="node id from /api/enroll")
    ap.add_argument("--token", default="")
    ap.add_argument("--visual", action="store_true",
                    help="run the CLIP vehicle identifier (police / gov / fleet)")
    ap.add_argument("--overlay", default="http://localhost:8160",
                    help="camera-control app to send live boxes to (operator only)")
    ap.add_argument("--no-overlay", action="store_true",
                    help="do not publish live detection boxes")
    ap.add_argument("--no-bank", action="store_true",
                    help="do not keep vehicle crops for training (see detect/bank.py)")
    args = ap.parse_args()

    if args.post and not args.node:
        raise SystemExit(
            "--post needs --node (and --token). Place the camera at "
            "http://localhost:8160/place, or enroll one directly.")

    # Before anything expensive is loaded. See claim_single_instance.
    _lock = claim_single_instance(args.node)      # noqa: F841 - held for life

    vid = None
    if args.visual:
        global VehicleIdentifier
        from detect.vehicle_id import VehicleIdentifier
        print("loading vehicle identifier...")
        vid = VehicleIdentifier()

    # A bounded run keeps its evidence crops for review. An unbounded one must
    # not: at the observed traffic that is thousands of JPEGs a day written to
    # a folder nobody reads.
    forever = args.seconds <= 0
    keep_eval = not forever
    if keep_eval:
        EVAL.mkdir(parents=True, exist_ok=True)
        for old in EVAL.glob("*.jpg"):
            old.unlink()

    print(f"opening {args.source}")
    try:
        grab = FrameGrabber(args.source).start()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    probe, _ = grab.latest(wait_for_new=False)
    print(f"  {probe.shape[1]}x{probe.shape[0]}  (latest-frame-wins reader)")

    print("loading models...")
    tracker, reader = VehicleTracker(), PlateReader()
    # Say which processor is ACTUALLY doing the work, in plain words. The old
    # line printed a list that included CUDA on a machine that had silently
    # fallen back to CPU for months. See detect/cuda_setup.py.
    print(f"  running on: {reader.device}   ({reader.cuda_note})")

    stop = threading.Event()
    if args.post:
        ensure_signing_key(args)
        threading.Thread(target=heartbeat_loop, args=(args, stop),
                         daemon=True).start()
        print(f"  heartbeat -> {args.hub} every 30s as {args.node}")
    if not args.no_overlay:
        threading.Thread(target=overlay_loop, args=(args, stop),
                         daemon=True).start()
        print(f"  live boxes -> {args.overlay}  (operator view only)")

    print("\nwatching until stopped (ctrl-c)...\n" if forever
          else f"\nwatching for {args.seconds:.0f}s...\n")

    # Ctrl-C sets a flag rather than unwinding the stack, so a long run still
    # releases the camera, flushes the vehicles it was mid-track on and prints
    # its summary instead of dying in the middle of a frame.
    stopping = threading.Event()

    def _sigint(_sig, _frm):
        if stopping.is_set():
            raise KeyboardInterrupt      # second ctrl-c: give up immediately
        print("\n  stopping after this frame...")
        stopping.set()

    signal.signal(signal.SIGINT, _sigint)

    passes: dict[int, VehiclePass] = {}
    done: list[VehiclePass] = []
    last_seen: dict[int, int] = {}
    idx = 0
    n_done = 0
    t0 = time.time()
    last_note = t0
    dead_reads = 0

    while not stopping.is_set() and (forever or time.time() - t0 < args.seconds):
        # ⚠️ ALWAYS THE NEWEST FRAME, NEVER THE NEXT ONE IN A QUEUE.
        # cap.read() on an HTTP MJPEG source returns from a backlog - measured
        # at 13ms of wait while the stream ran 5x faster than this loop, which
        # means every frame analysed had been captured while the previous one
        # was still being processed. The overlay lag was queue depth, not
        # processing time. See detect/grabber.py and tools/probe_latency.py.
        frame, captured_at = grab.latest()
        if frame is None:
            dead_reads += 1
            if dead_reads % 20 == 1:
                print(f"  [{time.time()-t0:6.1f}s] no frames from {args.source} "
                      f"({grab.reopens} reopens so far)")
            continue
        dead_reads = 0
        idx += 1
        LAST_WORK[0] = time.time()      # a real frame reached the pipeline
        # Judge the picture itself now and then - once a second is plenty, and
        # it costs one greyscale median rather than a model pass.
        if idx % 20 == 0:
            ok, why = frame_usable(frame)
            if ok:
                LAST_SEEING[0] = time.time()
                FRAME_STATE[0] = ""
            else:
                FRAME_STATE[0] = why
                if LAST_SEEING[0] == 0.0:
                    LAST_SEEING[0] = time.time()   # start the clock, do not trip on frame 20

        tracked = tracker.track(frame)
        fh, fw = frame.shape[:2]

        # ---- live government-vehicle call, while the car is still there ----
        # The overlay used to colour boxes by COCO class ('car', 'truck'),
        # because the police classification only ran once a pass COMPLETED -
        # so the operator learned what had just gone by after it had gone by.
        # With the detector on the GPU there is headroom to ask CLIP during the
        # pass instead, throttled and cached per track so it costs one
        # classification every LIVE_ID_EVERY frames rather than one per frame.
        if vid is not None and idx % LIVE_ID_EVERY == 0:
            for tid, _cn, b, _c in tracked:
                x0, y0, x1, y1 = (int(v) for v in b)
                if (x1 - x0) < 60 or (y1 - y0) < 40:
                    continue        # too small to judge; leave it unclassified
                crop = frame[max(0, y0):y1, max(0, x0):x1]
                if not crop.size:
                    continue
                r = vid.classify(crop)
                # The SAME bar the published tier uses. A live box that lights
                # up on evidence too weak to publish would be a second, looser
                # classifier that nobody calibrated - and the operator would
                # reasonably read it as what the map is about to say.
                # ONE implementation - see VehicleIdentifier.gov_call. This
                # duplicated the zero-shot gate inline, so the moment the
                # trained head started deciding what publishes, the box on the
                # camera page began answering a different question from the
                # map. The comment above says it must be the same bar; it now
                # actually is.
                call = VehicleIdentifier.gov_call(r)
                LIVE_ID[tid] = {"gov": call["gov"], "conf": call["conf"],
                                "vclass": r["vclass"], "src": call["source"]}
                # Remember it ON THE PASS. LIVE_ID is popped as soon as the
                # track disappears, which is BEFORE handle_pass runs, so the
                # verdict the operator was shown would otherwise be gone by the
                # time we decide whether to ask them about it. See
                # VehiclePass.gov_live.
                _vp = passes.get(tid)
                if _vp is not None and call["gov"]:
                    _vp.gov_live = True
                    _vp.gov_live_conf = max(_vp.gov_live_conf,
                                            float(call["conf"] or 0.0))
            # Tracks that ended should not accumulate forever.
            live_ids = {t for t, _c, _b, _cf in tracked}
            for gone in [t for t in LIVE_ID if t not in live_ids]:
                LIVE_ID.pop(gone, None)
        # Normalised, so the browser can scale them to whatever size the video
        # is rendered at - and so a zoom or a mode change on the camera does
        # not silently put every box in the wrong place.
        overlay_publish([{
            "id": t, "cls": cn,
            "x": round(b[0] / fw, 4), "y": round(b[1] / fh, 4),
            "w": round((b[2] - b[0]) / fw, 4), "h": round((b[3] - b[1]) / fh, 4),
            "conf": round(float(c), 3),
            # "plates" = LEGIBLE reads, not raw reads. The OCR always emits
            # SOMETHING for any plate-shaped box, so counting reads lit up "plate
            # legible" on cars whose plate cannot actually be read. A plate is
            # legible only when it is wide enough to resolve (>=60px, the OCR
            # floor from tools/feature_budget) AND read with real confidence.
            # This is the viewfinder's AIMING signal: it stays off - correctly -
            # on a camera too far back, whatever garbage the reader prints.
            "plates": (sum(1 for r in passes[t].reads
                           if r.conf >= 0.5 and (r.box[2] - r.box[0]) >= 60)
                       if t in passes else 0),
            # Is this a publicly owned vehicle? Drives the red box and the
            # GOVERNMENT VEHICLE label in the operator's viewfinder.
            "gov": bool(LIVE_ID.get(t, {}).get("gov")),
            "gov_conf": round(float(LIVE_ID.get(t, {}).get("conf") or 0), 3),
        } for t, cn, b, c in tracked], fw, fh, captured_at)

        for tid, cls_name, vbox, _c in tracked:
            last_seen[tid] = idx
            vp = passes.get(tid)
            if vp is None:
                vp = passes[tid] = VehiclePass(track_id=tid, cls_name=cls_name,
                                               first_frame=idx,
                                               first_ts=captured_at)
                print(f"  [{time.time()-t0:6.1f}s] new {cls_name} track {tid}")
            vp.last_ts = captured_at
            vp.last_frame, vp.frames_seen = idx, vp.frames_seen + 1
            vp.boxes.append(vbox)
            if idx % args.plate_every:
                continue
            reads = list(reader.read(frame, vbox))
            for pr in reads:
                pr.frame_idx = idx
                vp.reads.append(pr)
            if reads:
                top = max(reads, key=lambda r: r.conf * (r.area ** 0.5))
                # Pick the published frame on VEHICLE FRAMING (whole car, big,
                # centred, sharp), with plate readability only as a tie-breaker.
                # The old rule saved the crispest PLATE even with the car half
                # out of frame. Only plate-detected frames are candidates, so the
                # published crop is still always redactable (best_plate_boxes).
                pbonus = min(top.conf * (top.area ** 0.5), 25.0)
                if frame_quality(vbox, fw, fh) * 100 + pbonus > vp.best_score - 8:
                    score = frame_quality(vbox, fw, fh, frame) * 100 + pbonus
                    if score > vp.best_score:
                        vp.best_score, vp.best_frame = score, frame.copy()
                        vp.best_vehicle_box, vp.best_plate_box = vbox, top.box
                        # Keep EVERY box from this frame. See VehiclePass - the
                        # winner is often the livery rather than the plate.
                        vp.best_plate_boxes = [r.box for r in reads]

        expired = [t for t, f in last_seen.items()
                   if idx - f > TRACK_TIMEOUT_FRAMES]
        for tid in expired:
            # ⚠️ PRUNE THE INDEX TOO, not just `passes`. last_seen was never
            # cleaned, so it grew one entry per vehicle for the life of the
            # process and this list comprehension re-scanned all of them on
            # every single frame - quietly quadratic on a node meant to run for
            # weeks. Same shape as the Sentinel refresh bug.
            last_seen.pop(tid, None)
            vp = passes.pop(tid, None)
            if vp is None or vp.frames_seen < MIN_TRACK_FRAMES:
                continue
            n_done += 1
            plate, conf, agree = vp.vote()
            print(f"  [{time.time()-t0:6.1f}s] track {tid} left after "
                  f"{vp.frames_seen} frames, {len(vp.reads)} plate reads"
                  f"{f' -> {plate} (agree {agree:.2f})' if plate else ''}")
            # Evaluate, bank for training, and post it NOW. A map called
            # live should not be a batch job that reports the last five
            # minutes of traffic in one lump when the run happens to end.
            handle_pass(vp, args, vid)
            if keep_eval:
                done.append(vp)
            else:
                # 🚨 THE FRAME MUST GO OR A LONG RUN EATS THE MACHINE.
                # best_frame is a full ~1.4 MB image and every completed pass
                # was being retained. At the traffic actually measured on his
                # street - about 40 passes per 3 minutes - that is roughly a
                # gigabyte an hour, which a 180 s run never revealed and a
                # 24/7 node would die of.
                vp.best_frame = None
                vp.reads.clear()
                vp.boxes.clear()

        if time.time() - last_note > 30:
            last_note = time.time()
            print(f"  [{time.time()-t0:6.1f}s] {idx} frames, "
                  f"{len(passes)} tracking, {n_done} completed, "
                  f"frame age {grab.age*1000:.0f}ms, {grab.dropped} dropped"
                  + (f", {POSTED[0]} posted ({POSTED[1]} failed)" if args.post else ""))

    grab.stop()
    stragglers = [v for v in passes.values() if v.frames_seen >= MIN_TRACK_FRAMES]
    done += stragglers
    n_done += len(stragglers)
    # Tracks still open when the clock ran out were never posted by the
    # completion path above, so they are flushed here - once, and only these.
    for vp in stragglers:
        handle_pass(vp, args, vid)
    stop.set()

    dt = time.time() - t0
    print(f"\n{idx} frames in {dt:.0f}s ({idx/dt:.1f} fps processed)")
    print(f"{n_done} vehicle passes\n")

    rows = []
    hdr = f"{'trk':>4} {'type':<9} {'frm':>4} {'rds':>4} {'plate':<10} {'ocr':>5} {'agree':>6} tier"
    if done:
        print(hdr); print("-" * len(hdr))
    for vp in sorted(done, key=lambda v: v.first_frame):
        plate, conf, agree = vp.vote()
        ev = {}
        crop = None
        if vp.best_frame is not None and vp.best_vehicle_box:
            ev = visual.signals(vp.best_frame, vp.best_vehicle_box)
            if vid is not None:
                x0, y0, x1, y1 = (int(v) for v in vp.best_vehicle_box)
                crop = vp.best_frame[max(0, y0):y1, max(0, x0):x1]
                if crop.size:
                    vres = vid.evidence(crop)
                    ev.update({k: v for k, v in vres.items()
                               if not k.startswith("_")})
                    # A confident 'ordinary vehicle' call vetoes the crude
                    # colour heuristics. See visual.arbitrate.
                    ev = visual.arbitrate(ev, vres.get("_visual"))
        ev["plate_text"] = plate
        c = classify.classify(ev)
        # A public SIGHTING needs no plate; publishing plate TEXT still needs
        # the strict gate. See classify.py rule 4.
        tier = "public" if (c["tierable"] or c["sightable"]) else "private"
        publish_plate = c["tierable"]
        print(f"{vp.track_id:>4} {vp.cls_name:<9} {vp.frames_seen:>4} "
              f"{len(vp.reads):>4} {plate or '-':<10} {conf:>5.2f} {agree:>6.2f} {tier}")
        rows.append({"track": vp.track_id, "type": vp.cls_name,
                     "frames": vp.frames_seen, "reads": len(vp.reads),
                     "plate": plate, "ocr_conf": round(conf, 3),
                     "agreement": round(agree, 3), "tier": tier})

        if vp.best_frame is not None:
            meta = {"ts": time.time(), "node_id": "live", "node_name": "live",
                    "tier": tier, "plate_text": plate, "vclass": c["vclass"]}
            try:
                img = snapshot.Image.fromarray(
                    cv2.cvtColor(vp.best_frame, cv2.COLOR_BGR2RGB))
                name, _ = snapshot.store_crop(img, vp.best_vehicle_box, meta,
                                              plate_box=vp.best_plate_box)
                (EVAL / f"track{vp.track_id:03d}_{tier}.jpg").write_bytes(
                    (snapshot.SNAPS / name).read_bytes())
                (snapshot.SNAPS / name).unlink()
            except Exception as exc:
                print(f"  ! track {vp.track_id}: {exc}")

    with_plate = sum(1 for r in rows if r["plate"])
    print(f"\npasses={len(rows)}  with a plate read={with_plate}")
    print(f"crops -> {EVAL}")
    (EVAL / "report.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    if args.post:
        print(f"\nposted {POSTED[0]} sightings ({POSTED[1]} failed) to {args.hub}")


POSTED = [0, 0]      # sent, failed

# 🚨 FULL-RES ON CONFIRM (client side). The hub caps every upload at 200px so a
# private plate cannot survive the trip. Once the hub PUBLISHES a government
# vehicle it belongs in the public tier, where the plate is legible ON PURPOSE -
# and by then the only copy the hub has is the 200px one. The camera that took
# it is the only device that still holds the original, so the hub asks for it on
# the heartbeat (`want_full`). This holds the full-resolution vehicle crop of
# every posted sighting, keyed by id, and hands it back when asked. It is only
# ever uploaded for a row the hub has ALREADY made public, so no plate is ever
# sent for a private-tier vehicle - the 200px guarantee is untouched.
FULLRES_HOLD: dict = {}
_FULLRES_LOCK = threading.Lock()
FULLRES_TTL_S = 3600.0        # match db.FULLRES_WINDOW_S; drop what the hub can no longer ask for
FULLRES_MAX = 300
FULLRES_EDGE = 900            # long-edge cap of the good crop


def _hold_fullres(sid, frame, vbox) -> None:
    """Keep the full-resolution vehicle crop of a just-posted sighting."""
    if not sid or frame is None or not vbox:
        return
    try:
        x0, y0, x1, y1 = (int(v) for v in vbox)
        h, w = frame.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        crop = frame[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        scale = min(1.0, FULLRES_EDGE / float(max(ch, cw)))   # never upscale
        if scale < 1.0:
            crop = cv2.resize(crop, (max(1, int(cw * scale)), max(1, int(ch * scale))),
                              interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return
        now = time.time()
        with _FULLRES_LOCK:
            FULLRES_HOLD[int(sid)] = (buf.tobytes(), now)
            for k in [k for k, (_, t) in list(FULLRES_HOLD.items())
                      if now - t > FULLRES_TTL_S]:
                FULLRES_HOLD.pop(k, None)
            if len(FULLRES_HOLD) > FULLRES_MAX:
                stale = sorted(FULLRES_HOLD, key=lambda k: FULLRES_HOLD[k][1])
                for k in stale[:len(FULLRES_HOLD) - FULLRES_MAX]:
                    FULLRES_HOLD.pop(k, None)
    except Exception:
        pass


def _send_fullres(args, sids) -> None:
    """Answer the hub's want_full: upload the good crop for each id still held."""
    import base64
    import urllib.request
    for sid in sids or []:
        try:
            key = int(sid)
        except (TypeError, ValueError):
            continue
        with _FULLRES_LOCK:
            held = FULLRES_HOLD.get(key)
        if not held:
            continue
        try:
            body = json.dumps({
                "node_id": args.node, "id": key,
                "snap_b64": "data:image/jpeg;base64,"
                + base64.b64encode(held[0]).decode()}).encode()
            req = urllib.request.Request(
                f"{args.hub}/api/sighting/fullres", method="POST", data=body,
                headers={"Content-Type": "application/json", "User-Agent": NODE_UA,
                         "Authorization": f"Bearer {args.token}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            # Whether it stored it or already had it, stop holding it.
            with _FULLRES_LOCK:
                FULLRES_HOLD.pop(key, None)
            print(f"    full-res sent for {key}")
        except Exception as exc:
            print(f"    ! full-res upload failed for {key}: {exc}")


# The last moment this detector actually PROCESSED a frame. The heartbeat reads
# it, so "online" means the work is happening rather than merely that a thread
# is alive. See heartbeat_loop.
LAST_WORK = [0.0]

# 🚨 FRAMES FLOWING IS NOT THE SAME AS SEEING.
#
# The stall gate above asks whether frames reach the pipeline. A camera pinned
# at pure white passes that test perfectly - it delivered thousands of frames,
# every one of them blank - and it reported itself online for six hours while
# finding nothing, because "no vehicles" is also what a quiet street looks like.
#
# Traffic volume cannot separate those two: an empty road at 4am is healthy. The
# PICTURE can. A frame that is entirely clipped or entirely black is unusable
# whatever the traffic is doing, so that is what gets measured - and a node that
# cannot see stops claiming to be watching.
LAST_SEEING = [0.0]        # last time the frame was actually usable
FRAME_STATE = [""]         # why it is not, when it is not
BLIND_MEDIAN_HIGH = 245    # the whole frame has clipped
BLIND_MEDIAN_LOW = 6       # the whole frame is black
BLIND_AFTER_S = 180        # tolerate a passing headlight or a lens wipe


def frame_usable(frame) -> tuple[bool, str]:
    """Can anything be seen in this frame at all?

    Deliberately crude and deliberately not about vehicles: it answers "is this
    picture capable of showing a car", not "is there a car in it".
    """
    try:
        import cv2
        import numpy as np
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        med = float(np.median(g))
    except Exception:
        return True, ""        # cannot judge; never fail a node on that
    if med >= BLIND_MEDIAN_HIGH:
        return False, f"washed out (median {med:.0f}/255) - exposure too long"
    if med <= BLIND_MEDIAN_LOW:
        return False, f"black (median {med:.0f}/255) - no light reaching the sensor"
    return True, ""


def claim_single_instance(node_id: str, port_base: int = 47800) -> "socket.socket":
    """Refuse to start if this node is already being watched by another process.

    🚨 FOUR DETECTORS WERE RUNNING AT ONCE AND NOTHING SAID SO.
    Every restart during development started an additional node rather than
    replacing the previous one, because `pkill -f run_live` in Git Bash matches
    nothing against native Windows processes and exits 0 while doing nothing -
    the project's signature failure, a command reporting success having done
    none of the work. The result: the same vehicle posted up to four times, the
    traffic count inflated by the same factor, four copies of every crop
    entering the training bank, and four processes fighting for the CPU that
    the frame rate depends on.

    None of that is a development-only accident. A volunteer double-clicking
    the shortcut twice, or a scheduled task overlapping a manual start, hits it
    identically - and the failure is silent and looks like success, because the
    map fills up faster than usual.

    A bound socket is the check rather than a PID file: the bind is atomic, the
    OS releases it when the process dies however it dies, and there is no stale
    lock to reason about after a crash. Keyed on the node id, so a box running
    two cameras is still fine.
    """
    import hashlib
    # ⚠️ NOT builtin hash(). Python randomises string hashing per process
    # (PYTHONHASHSEED), so two processes asked for the same node id computed
    # two DIFFERENT ports, both bound happily, and the guard silently passed -
    # which is how this exact function failed its own first test.
    digest = hashlib.blake2b((node_id or "default").encode(), digest_size=2).digest()
    port = port_base + (int.from_bytes(digest, "big") % 200)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        # 🚨 EXIT 3, NOT 1, AND THAT DISTINCTION IS LOAD-BEARING.
        # SystemExit("some message") exits 1 - the same code Python uses for an
        # unhandled exception. The launcher keyed "already running" off exit 1,
        # so when a NameError killed the detector the wrapper announced
        # "another detector is already running - close that window first" and
        # STOPPED retrying. Every fixed camera stayed dark behind a confident,
        # wrong diagnosis, and the one message a human would read pointed away
        # from the fault. A refusal and a crash must never share an exit code.
        print(
            f"\n  A detector is ALREADY RUNNING for node {node_id or '(default)'}.\n"
            f"  Starting a second one would double-count every vehicle, so this\n"
            f"  one is stopping.\n\n"
            f"  Close the other window first, or find it with:\n"
            f"    powershell \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | \"\n"
            f"      \"Where CommandLine -match 'run_live' | Select ProcessId,CommandLine\"\n", file=sys.stderr)
        raise SystemExit(3)

# Latest detections, for the operator's live overlay. One slot, overwritten:
# the viewer wants the CURRENT boxes, so a backlog of old ones is worse than
# useless. Published by a background thread so an unreachable viewer can never
# slow the capture loop down - the detector's job is to watch the road, and
# nothing decorative gets to interfere with that.
OVERLAY: dict = {"boxes": [], "ts": 0.0, "seq": 0}
_OVERLAY_LOCK = threading.Lock()


def overlay_publish(boxes: list, w: int, h: int, captured_at: float) -> None:
    """Hand the newest boxes to the publisher thread. Never blocks.

    `captured_at` is when the FRAME was taken, not when this ran. The browser
    predicts each box forward by its age, and stamping that age with "now"
    would hide exactly the processing latency the prediction has to cancel -
    the boxes would claim to be 0ms old while describing a frame from a
    quarter of a second ago.
    """
    with _OVERLAY_LOCK:
        OVERLAY["boxes"] = boxes
        OVERLAY["w"], OVERLAY["h"] = w, h
        OVERLAY["ts"] = captured_at
        OVERLAY["seq"] += 1


def overlay_loop(args, stop) -> None:
    """Push detections to camctl so the owner can watch the detector work.

    Operator-only by construction: camctl is the local camera-control app,
    bound to this machine, and these boxes never touch the public hub. Nobody
    but the person whose camera it is sees a live view of their own street.
    """
    import urllib.request
    last = -1
    while not stop.is_set():
        with _OVERLAY_LOCK:
            seq, payload = OVERLAY["seq"], dict(OVERLAY)
        if seq != last:
            last = seq
            try:
                req = urllib.request.Request(
                    f"{args.overlay}/api/detections", method="POST",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                 "User-Agent": NODE_UA})
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass        # the overlay is a convenience; never a failure
        stop.wait(0.08)


def heartbeat_loop(args, stop) -> None:
    """Tell the hub this camera is awake, every 30 s, until the run ends.

    🚨 WHY THIS EXISTS. A node's liveness used to be inferred from its last
    SIGHTING: online meant "a vehicle passed in the last 15 minutes". A camera
    pointed at a quiet street is working perfectly and has nothing to report,
    so the map showed 0/1 cameras online and looked broken - and the harder the
    node's job (night, low traffic, a residential street), the more confidently
    the map called it dead. Liveness is a thing a node knows and should state,
    not a thing a hub should guess from traffic.

    🚨 BUT IT MUST NOT STATE MORE THAN IT KNOWS. This loop is its own thread, so
    it kept beating happily while the capture/detection pipeline was wedged: a
    detector ran 18 hours, stopped processing frames entirely, and the map still
    showed the camera ONLINE because a timer in a separate thread was ticking.
    A liveness signal that does not depend on the work it reports on cannot
    detect the work stopping - it only proves the process has not exited.

    So the beat is now CONDITIONAL on recent work (run_live.LAST_WORK, stamped
    every processed frame). If no frame has been through the pipeline in
    STALL_AFTER_S the beat is withheld, the hub's window lapses, and the camera
    goes offline - which is the truth. A quiet street still beats: an empty road
    still produces frames. Only a stalled pipeline goes dark.
    """
    import urllib.request
    body = json.dumps({"node_id": args.node}).encode()
    warned = False
    blind_warned = False
    while not stop.is_set():
        # A camera that cannot SEE must not report itself as watching, even
        # though frames are arriving perfectly.
        blind_for = time.time() - (LAST_SEEING[0] or time.time())
        if FRAME_STATE[0] and blind_for > BLIND_AFTER_S:
            if not blind_warned:
                print(f"  ! the camera cannot see: {FRAME_STATE[0]} "
                      f"({blind_for:.0f}s) - withholding the heartbeat; this "
                      f"camera will show OFFLINE until the picture recovers")
                blind_warned = True
            stop.wait(30)
            continue
        if blind_warned:
            print("  the picture recovered - resuming heartbeat")
            blind_warned = False
        idle = time.time() - (LAST_WORK[0] or time.time())
        if idle > STALL_AFTER_S:
            # Say it once, loudly, rather than every 30s forever.
            if not warned:
                print(f"  ! no frame processed in {idle:.0f}s - withholding the "
                      f"heartbeat; this camera will show OFFLINE until it "
                      f"recovers")
                warned = True
            stop.wait(30)
            continue
        if warned:
            print("  frames flowing again - resuming heartbeat")
            warned = False
        try:
            req = urllib.request.Request(
                f"{args.hub}/api/heartbeat", method="POST", data=body,
                headers={"Content-Type": "application/json",
                 "User-Agent": NODE_UA,
                         "Authorization": f"Bearer {args.token}"})
            raw = urllib.request.urlopen(req, timeout=10).read()
            # 🚨 THE HUB ASKS FOR THE GOOD PICTURE HERE. If it has published any
            # of this camera's vehicles, `want_full` names them; hand back the
            # full-resolution crop we held at post time.
            try:
                resp = json.loads(raw or b"{}")
            except Exception:
                resp = {}
            want = resp.get("want_full")
            if want:
                _send_fullres(args, want)
        except Exception as exc:
            print(f"  ! heartbeat failed: {exc}")
        stop.wait(30)


def evaluate(vp, vid) -> tuple[dict, dict, str, float]:
    """Everything we can say about one finished pass. Computed ONCE.

    Both the poster and the training bank need this, and running the models
    twice per vehicle on a node that is already CPU-bound would halve the frame
    rate to produce identical answers.
    """
    plate, _conf, agree = vp.vote()
    ev: dict = {}
    if vp.best_frame is not None and vp.best_vehicle_box:
        ev = visual.signals(vp.best_frame, vp.best_vehicle_box)
        if vid is not None:
            x0, y0, x1, y1 = (int(v) for v in vp.best_vehicle_box)
            c = vp.best_frame[max(0, y0):y1, max(0, x0):x1]
            if c.size:
                vres = vid.evidence(c)
                ev.update({k: v for k, v in vres.items() if not k.startswith("_")})
                ev = visual.arbitrate(ev, vres.get("_visual"))
                ev["_visual"] = vres.get("_visual")
    # How it crossed the frame. Banked now, unused for now: see
    # VehiclePass.motion - this is the TIME half of a speed, and the metres
    # arrive when a camera is calibrated against its own road markings.
    # Recording it from today means every pass in between can still have a
    # speed computed afterwards.
    verdict = classify.classify({**ev, "plate_text": plate})
    # How it crossed the frame. Banked now, unused until a camera is
    # calibrated against its own road markings - see VehiclePass.motion.
    #
    # 🚨 THE FULL TRACK ONLY FOR WHAT GETS PUBLISHED. A government pass is a
    # public record and its speed claim has to be checkable frame by frame;
    # anonymous traffic fades in forty-five seconds and keeping a
    # sample-by-sample path of a private car past somebody's house is the
    # trace this project exists to refuse. Measured on the live fleet: ~79
    # published rows a day against ~327,000 passes, so the expensive version
    # is paid for 0.02% of them.
    fh, fw = (vp.best_frame.shape[:2] if vp.best_frame is not None else (0, 0))
    # ⚠️ THE VERDICT HAS NO "tier" KEY - it has `tierable` and `sightable`,
    # and the poster derives the tier from them (see the tier line below).
    # Asking for verdict["tier"] returns None, which is falsy, so the full
    # track would simply never have been kept and nothing would have said so.
    mo = vp.motion(fw, fh,
                   full=bool(verdict.get("tierable") or verdict.get("sightable")))
    if mo:
        ev["pass_motion"] = json.dumps(mo, separators=(",", ":"))
    return ev, verdict, plate, agree


# A pass this confident gets put in front of the operator immediately. Below
# the publishing bar on purpose: a near miss is exactly the case worth a human
# glance, and the whole bottleneck is confirmed government vehicles.
# ~~CONFIRM_AT~~ removed. The popup now fires on VehicleIdentifier.gov_call,
# the same judgement that decides publication, so a second hand-picked
# threshold here would only be a way for the two to disagree.


def push_confirm(args, stem: str, conf: float, published: bool) -> None:
    """Ask the owner to confirm, now, while the vehicle is still in sight."""
    if args.no_overlay or not stem:
        return
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{args.overlay}/api/confirm", method="POST",
            data=json.dumps({"day": time.strftime("%Y-%m-%d"), "stem": stem,
                             "conf": round(conf, 3), "ts": time.time(),
                             "published": bool(published)}).encode(),
            headers={"Content-Type": "application/json",
                 "User-Agent": NODE_UA})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass        # a prompt nobody sees must never disturb the node


def handle_pass(vp, args, vid) -> None:
    """One completed vehicle: evaluate it, keep it for training, publish it."""
    ev, verdict, plate, agree = evaluate(vp, vid)
    stem = None
    if not args.no_bank:
        stem = bank.bank_pass(vp, ev, verdict, node_id=args.node)
        # Prompt on the SAME judgement that decides publication. This read
        # CLIP's confidence against a fixed 0.85, so once the head took over it
        # would have asked about cars the map was not going to publish and
        # stayed silent about ones it was.
        vis = ev.get("_visual") or {}
        call = VehicleIdentifier.gov_call(vis) if vis else {"gov": False,
                                                            "conf": 0.0}
        # 🚨 ASK ABOUT WHAT THE OPERATOR WAS SHOWN, NOT ONLY ABOUT THE CROP WE
        # KEPT. `call` judges the pass's chosen crop; `vp.gov_live` is true if
        # ANY live frame of this same vehicle read government - which is exactly
        # when the viewfinder drew a red box around it. Firing only on the
        # former meant a patrol car could light up red as it went past and
        # produce no prompt at all, which reads as the popup being broken and
        # is the one failure that costs a confirmation nobody can get back: the
        # car is gone.
        #
        # This can only ever ADD prompts, and the cost of a surplus one is a
        # keypress. The cost of the missing one is a patrol car nobody was
        # asked about while they were watching it.
        if call["gov"] or vp.gov_live:
            push_confirm(args, stem,
                         call["conf"] if call["gov"] else vp.gov_live_conf,
                         bool(verdict.get("sightable")))
    if args.post:
        sid = post_one(vp, args, vid, ev, verdict, plate, agree, stem)
        # Tie the published sighting to the crop it came from, both ways. A
        # correction on the map then lands as a LABEL on real camera-view data
        # instead of evaporating as a one-off fix - which is the whole point of
        # reviewing at all.
        if sid and stem:
            bank.link_sighting(stem, sid)


def post_one(vp, args, vid, ev=None, verdict=None, plate=None, agree=None,
             stem=None):
    """Send one finished pass to the hub, as it finishes."""
    import base64
    import urllib.request

    if verdict is None:
        ev, verdict, plate, agree = evaluate(vp, vid)
    # Only send something the hub can do anything with: either a publishable
    # sighting, or a plate good enough to clear the gate.
    # Each of these returns leaves a crop banked with no sighting_id, and they
    # mean completely different things - a declined gate is the system working,
    # a failed post is the system losing a patrol car. Say which. See
    # bank.note_not_posted.
    if not (verdict["sightable"] or verdict["tierable"] or agree >= 0.55):
        bank.note_not_posted(stem, "gate: not sightable/tierable and plate "
                                   f"agreement {agree:.2f} < 0.55")
        return
    if vp.best_frame is None:
        bank.note_not_posted(stem, "no best_frame to encode")
        return

    ok, buf = cv2.imencode(".jpg", vp.best_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        bank.note_not_posted(stem, "jpeg encode failed")
        return
    body = {
        "node_id": args.node,
        # The middle of the pass, not the moment of the POST. See
        # VehiclePass.first_ts.
        "ts": (vp.first_ts + vp.last_ts) / 2 if vp.last_ts else time.time(),
        # Send the read whenever the sighting is publishable at all, not only
        # when `tierable`. The node discarding it here is why no sighting has
        # EVER carried a plate: visual identification alone tops out at 0.802
        # against a 0.85 bar, so tierable was unreachable for a marked patrol
        # car and the text was thrown away before the hub could decide.
        # The hub still decides; it just has something to decide about now.
        "plate_text": plate if (verdict["tierable"] or verdict["sightable"]) else "",
        "plate_state": "MI",
        "plate_conf": agree,
        "body": vp.cls_name, "heading": vp.heading(),
        "evidence": {k: v for k, v in ev.items() if not k.startswith("_")},
        "source": "camera",
        "snap_b64": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode(),
        "plate_box": list(vp.best_plate_box) if vp.best_plate_box else None,
        # ALL plate-like boxes from this frame, not just the winner. The winner
        # is picked by area and on a marked patrol car the door livery beats the
        # plate - so redacting only it blacked out the livery and left the plate
        # readable. See VehiclePass.best_plate_boxes.
        "plate_boxes": [list(b) for b in (vp.best_plate_boxes or [])],
        # ⚠️ REQUIRED. Without it the hub has nothing to crop to and stores the
        # whole street. See snapshot.store_submitted.
        "vehicle_box": list(vp.best_vehicle_box) if vp.best_vehicle_box else None,
        # The training crop this sighting came from, so the hub can join the
        # two without searching the bank. See db.FIELDS.
        "bank_ref": stem,
    }
    # ⚠️ BUILDING THE REQUEST IS INSIDE THE try, AND THAT IS NOT PEDANTRY.
    # It used to sit outside it, so anything that went wrong while ASSEMBLING
    # the request - a bad header value, an un-encodable body, a missing name -
    # escaped this function and killed the detector outright instead of costing
    # one pass. That is exactly what happened when NODE_UA was added to a
    # comment rather than the import list: a NameError on this line took the
    # whole camera down, and the launcher then misreported it as "another
    # detector is already running" because the exit code collided with the
    # single-instance guard. One pass is an acceptable loss; the camera is not.
    try:
        # 🚨 SIGN THE CLAIM, NOT JUST THE CONNECTION.
        # The bearer token proves a secret was replayed. A signature proves
        # THIS claim came from the camera that owns the key, so altering a
        # plate hash or a position in flight stops it verifying - which is the
        # tamper the project has to be able to rule out when it says a
        # government vehicle was somewhere.
        #
        # Only signs if this node has a key. A node with none is the normal
        # supported case (browser and phone nodes cannot hold one), and
        # _ingest demands a signature only from a node that has REGISTERED a
        # public key, so an unsigned post from an unsigned node is accepted
        # exactly as before. Never fatal: a signing failure loses a signature,
        # never the sighting.
        sig = node_key.sign(ROOT, body.get("node_id") or "", body)
        if sig:
            body["sig"] = sig
        req = urllib.request.Request(
            f"{args.hub}/api/sightings", method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": NODE_UA,
                     "Authorization": f"Bearer {args.token}"})
        with urllib.request.urlopen(req, timeout=25) as r:
            out = json.loads(r.read())
            print("    posted:", out)
            POSTED[0] += 1
            sid = out.get("id")
            # Hold the FULL-resolution crop so the hub can ask for it if it
            # publishes this vehicle. Costs nothing until want_full names it.
            _hold_fullres(sid, vp.best_frame, vp.best_vehicle_box)
            return sid
    except Exception as exc:
        # 🚨 THE EXPENSIVE ONE. There is no retry and no queue behind this, so
        # a transient network error permanently loses a pass the detector had
        # already decided was worth reporting. Recording it on the crop is the
        # minimum: it makes the loss COUNTABLE, and a lost patrol car becomes
        # something that can be found later instead of a gap nobody can see.
        # A real retry/dead-letter is the follow-up; this is the diagnosis.
        print("    post failed:", exc)
        bank.note_not_posted(stem, f"POST FAILED: {exc}")
        POSTED[1] += 1
    return None


if __name__ == "__main__":
    main()
