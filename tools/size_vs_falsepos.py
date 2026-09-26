#!/usr/bin/env python3
r"""Does the police call survive a SMALL vehicle, and what does it cost?

    D:\LLM\.venv\Scripts\python.exe tools/size_vs_falsepos.py
    ... --widths 40,60,80,100,120,160 --neg-cams 40 --pos 250

🚨 THIS EXISTS TO TEST A NUMBER NOBODY EVER MEASURED.

`MIN_VEHICLE_PX = 120` is the bar that decides which cameras this project will
poll at all, and it is the single biggest lever on fleet size: measured
2026-09-26, WSDOT has 1,630 traffic cameras of which 4% clear 120 px, and a
wall of them sit between 59 and 113. Michigan, Texas, Colorado and Idaho all
have the same shape. Drop the bar and the network could roughly triple.

His question, and it is the right one: *"are you sure it cant detect police at
60 pixel cars? i feel like it could. we should test how often we get false
positives so we can make use of as many cameras as possible no matter the
quality."*

The comment beside the constant says "below this the head is guessing". That
was a judgement, not a measurement, and this is the measurement.

## What is measured

Both halves of the trade, because recall alone would be a sales pitch:

  * RECALL - crops of vehicles a HUMAN confirmed as patrol cars, downscaled so
    the vehicle is W pixels wide. How many does the live decision path still
    call police?
  * FALSE POSITIVES - vehicle crops taken from live DOT cameras at the same
    widths. Essentially all of them are ordinary traffic, so anything called
    police is a false positive.

⚠️ THE NEGATIVES HAVE A REAL BASE RATE AND IT IS NOT ZERO. Roughly one vehicle
in a thousand on a public road is a patrol car, so a measured false-positive
rate below ~0.1% is at the floor of what this method can see. Above that, the
number is real.

⚠️ IT USES THE LIVE DECISION PATH, not a re-implementation: VehicleIdentifier
.classify + gov_call, the same two calls the node makes. A bespoke threshold
here would measure something this project does not actually do.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

UA = {"User-Agent": "SparrowMap/1.0 (+https://sparrowmap.com) bar-calibration"}


def fetch(url: str, timeout: float = 25.0) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def to_width(img, w: int):
    """Resize a crop so its LONG EDGE is w, keeping aspect.

    The bar is expressed as a vehicle's pixel width, and these crops are the
    vehicle, so the crop's own long edge is the thing to set.
    """
    from PIL import Image
    if img.width <= 0 or img.height <= 0:
        return None
    scale = w / float(max(img.width, img.height))
    nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    return img.resize((nw, nh), Image.LANCZOS)


def positives(limit: int) -> list:
    """Crops of vehicles a human confirmed as patrol cars."""
    from PIL import Image
    snaps = ROOT / "data" / "reid" / "snaps"
    rows = json.loads((ROOT / "data" / "reid" / "reid_rows.json").read_text())
    want = {str(r["snap"]) for r in rows if r.get("snap")
            and r.get("vclass") == "police" and r.get("reviewed") == "confirmed"}
    out = []
    for f in snaps.iterdir():
        if f.name not in want:
            continue
        try:
            out.append(Image.open(f).convert("RGB"))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def negatives(n_cams: int, per_cam: int, limit: int) -> list:
    """Vehicle crops from live DOT cameras - ordinary traffic."""
    from PIL import Image
    import public_cams as pc
    sess, size = pc.load_model()
    # 🚨 THE NEGATIVES COME FROM THE CAMERAS THE DECISION IS ABOUT.
    # Michigan publishes 804 cameras and exactly 18 clear the 1280px pre-filter,
    # so it IS the population that a lower bar would admit. Measuring false
    # positives on `atx`-grade HD cameras would flatter the answer and tell us
    # nothing about the choice actually in front of us.
    #
    # ⚠️ measured_only=False on purpose: probe_filter would hand back only the
    # 18 already-usable cameras, which is the opposite of what is needed here.
    # Michigan is the population a lower bar would admit, but its 320px frames
    # yield few confident detections, so ordinary traffic is drawn from cameras
    # where the DETECTOR works and then downscaled - exactly as the positives
    # are. Both sides meet at the same width, which is the only way the
    # comparison is fair.
    cams = []
    # Georgia publishes 7,083 cameras at ~480px - plenty for the DETECTOR to
    # find ordinary traffic - and Michigan is the population a lower bar would
    # actually admit. Crops from both are downscaled to the test width exactly
    # as the positives are; meeting at the same width is the only fair test.
    for src in ("ga", "mi"):
        try:
            cams += (pc.michigan_index(measured_only=False) if src == "mi"
                     else pc.arcgis_index(src, measured_only=False))
        except Exception as exc:
            print(f"  {src} unavailable: {type(exc).__name__}: {exc}")
    random.seed(13)
    random.shuffle(cams)
    out = []
    for c in cams:
        if len(out) >= limit:
            break
        try:
            img = Image.open(io.BytesIO(fetch(c["url"]))).convert("RGB")
        except Exception:
            continue
        try:
            boxes = pc.detect(sess, size, img)
        except Exception:
            continue
        for b in boxes[:per_cam]:
            # detect() returns {"cls","conf","box":(x0,y0,x1,y1),"w"}
            x0, y0, x1, y1 = b["box"]
            if b["w"] < 40:       # too small to downscale meaningfully
                continue
            crop = img.crop((max(0, int(x0)), max(0, int(y0)),
                             min(img.width, int(x1)),
                             min(img.height, int(y1))))
            if crop.width >= 32 and crop.height >= 32:
                out.append(crop)
        n_cams -= 1
        if n_cams <= 0:
            break
    return out[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--widths", default="40,60,80,100,120,160")
    ap.add_argument("--pos", type=int, default=250)
    ap.add_argument("--neg", type=int, default=250)
    ap.add_argument("--neg-cams", type=int, default=120)
    a = ap.parse_args()
    widths = [int(x) for x in a.widths.split(",")]

    import numpy as np
    from detect import vehicle_id

    print("loading the live classifier ...", flush=True)
    vid = vehicle_id.get()

    print("gathering confirmed patrol crops ...", flush=True)
    pos = positives(a.pos)
    print(f"  {len(pos)} positives", flush=True)
    print("gathering ordinary traffic crops from live cameras ...", flush=True)
    neg = negatives(a.neg_cams, 3, a.neg)
    print(f"  {len(neg)} negatives", flush=True)
    if not pos or not neg:
        print("not enough data")
        return 1

    def called_police(img) -> bool:
        bgr = np.array(img)[:, :, ::-1].copy()      # PIL RGB -> cv2 BGR
        r = vid.classify(bgr)
        return bool(vehicle_id.VehicleIdentifier.gov_call(r).get("gov"))

    print()
    print(f"{'width':>6}  {'recall':>18}  {'false positives':>20}")
    rows = []
    # 🚨 A NATIVE-SIZE BASELINE OR THE COLUMN MEANS NOTHING. These crops were
    # called police by this very system at their own resolution, so whatever
    # recall they score untouched is the ceiling every downscaled row is
    # measured against - not 100%.
    tp0 = sum(1 for im in pos if called_police(im))
    fp0 = sum(1 for im in neg if called_police(im))
    print(f"{'native':>6}  {tp0:>5}/{len(pos):<5} {100.0*tp0/len(pos):>5.1f}%  "
          f"{fp0:>5}/{len(neg):<5} {100.0*fp0/len(neg):>6.2f}%   <- ceiling",
          flush=True)
    for w in widths:
        tp = sum(1 for im in pos if (s := to_width(im, w)) and called_police(s))
        fp = sum(1 for im in neg if (s := to_width(im, w)) and called_police(s))
        rec = 100.0 * tp / len(pos)
        fpr = 100.0 * fp / len(neg)
        rows.append((w, rec, fpr))
        print(f"{w:>6}  {tp:>5}/{len(pos):<5} {rec:>5.1f}%  "
              f"{fp:>5}/{len(neg):<5} {fpr:>6.2f}%", flush=True)

    print()
    print("Read it as a trade, not a score: a lower bar buys cameras and pays")
    print("in false positives, and every false positive is a REVIEW-QUEUE ITEM")
    print("and a wrong red dot until somebody clears it.")
    print("⚠️ ~0.1% of real traffic IS a patrol car, so a false-positive rate")
    print("   near or below that is at the floor of what this can measure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
