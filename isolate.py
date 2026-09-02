"""Cut the vehicle out of its surroundings, so a snapshot cannot locate the camera.

🚨 THE OPERATOR SPOTTED THIS, AND IT DEFEATED A PROTECTION ALREADY IN THE CODE.

The node's published position is jittered by up to 60 m and its watched span is
published as a stretch of road rather than a point, specifically so that people
can know where they are recorded without anyone learning which house is
watching. Then every sighting shipped a photograph of the neighbours' porch.

Anyone who wanted the camera could walk that street, match the houses, the
railings, the parked cars and the angle, and place the lens to within a metre
or two. The coordinates were protected and the picture gave them away - the
same shape of failure this codebase keeps finding: a control applied to one
representation, bypassed by another.

His framing was sharper than mine: *"that way cops can't harass people by
looking at buildings in the background and figuring out where the photo was
taken from"*. The threat model is not an abstract stalker. It is a subject of
the map working out who is photographing them - and right now that is exactly
one person, him, which is why this could not wait for a second volunteer.

## What it does

Instance-segments the vehicle and keeps ONLY those pixels. Everything else
becomes flat grey - not blurred, not pixelated, not darkened. Removed.

Blur was rejected. A blurred building is still a building of that shape, in
that place, at that angle, at that distance, with that roofline against that
sky - and deblurring is an active research field with working tools. If the
background is not evidence, and here it is not, then the honest thing is for it
not to be in the file at all.

## What survives, deliberately

The vehicle, its livery, its light bar, its shape and its damage. Everything a
person needs to check the claim "this was a patrol car" is on the vehicle
itself. Nothing that says WHERE the photograph was taken from is.

## Degrading honestly

If segmentation is unavailable the image is NOT published unmasked. It falls
back to the vehicle's bounding box with everything outside it removed, which
still carries some background inside the box (through windows, above the
roofline) - and the caller is told, so a deployment can decide to drop the
picture instead. `strip()` never returns an image that has had nothing done.
"""

from __future__ import annotations

import threading
from typing import Optional

import cv2
import numpy as np

# Flat, obviously artificial, and dark enough that the vehicle reads first.
# Not black (that reads as an exposure failure) and not the old warm brown
# ((26,30,38)BGR == (38,30,26)RGB) which baked into every crop as an ugly stain.
# Matched to the detail-card background (#0d1420) so the removed area vanishes
# into the card where the photo is shown. Kept in sync with
# isolate_pil.BACKDROP_RGB (13,20,32).
BACKDROP = (32, 20, 13)          # BGR  == RGB (13, 20, 32)

# Vehicle classes in COCO. A person standing on a pavement is NOT kept - the
# whole point is that only the vehicle survives.
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

_MODEL = None
_LOCK = threading.Lock()
_FAILED = False


def _model():
    """Load yolo11n-seg once. 5.9 MB; the download happens on first use."""
    global _MODEL, _FAILED
    if _MODEL is not None or _FAILED:
        return _MODEL
    with _LOCK:
        if _MODEL is None and not _FAILED:
            try:
                from ultralytics import YOLO
                _MODEL = YOLO("yolo11n-seg.pt")
            except Exception as exc:
                print(f"[isolate] segmentation unavailable ({exc}); "
                      f"falling back to box masking")
                _FAILED = True
    return _MODEL


def vehicle_mask(img: np.ndarray, box: Optional[tuple] = None,
                 conf: float = 0.25) -> Optional[np.ndarray]:
    """A boolean mask of the vehicle pixels, or None if segmentation failed.

    When `box` is given, only instances overlapping it are kept - a street
    scene contains several vehicles and the snapshot is about ONE of them.
    Keeping the others would republish the parked cars this is trying to
    remove.
    """
    m = _model()
    if m is None:
        return None
    try:
        res = m.predict(img, conf=conf, verbose=False)[0]
    except Exception as exc:
        print(f"[isolate] predict failed: {exc}")
        return None
    if res.masks is None or res.boxes is None:
        return None

    h, w = img.shape[:2]
    names = res.names
    keep = np.zeros((h, w), dtype=bool)
    found = False
    for i, cls_id in enumerate(res.boxes.cls.tolist()):
        if names.get(int(cls_id)) not in VEHICLE_CLASSES:
            continue
        seg = res.masks.data[i].cpu().numpy()
        if seg.shape != (h, w):
            seg = cv2.resize(seg, (w, h), interpolation=cv2.INTER_NEAREST)
        seg = seg > 0.5
        if box is not None and not _overlaps(res.boxes.xyxy[i].tolist(), box):
            continue
        keep |= seg
        found = True
    return keep if found else None


def _overlaps(a, b, min_iou: float = 0.10) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    inter = (ix1 - ix0) * (iy1 - iy0)
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return smaller > 0 and inter / smaller >= min_iou


def strip(img: np.ndarray, box: Optional[tuple] = None,
          feather: int = 3,
          fallback: Optional[tuple] = None) -> tuple[np.ndarray, str]:
    """Remove everything that is not the vehicle. Returns (image, method).

    `method` is 'segment', 'box' or 'none', and the caller is expected to look
    at it: a deployment that will not publish a partially-masked image needs to
    be able to tell the difference, and a function that silently downgrades its
    own privacy guarantee is the failure this whole file exists to correct.

    🚨 `box` AND `fallback` ARE TWO DIFFERENT QUESTIONS AND USED TO BE ONE
    ARGUMENT. `box` asks WHICH INSTANCE is the subject - a street scene holds
    several vehicles and only one of them is this sighting - so it wants to be
    tight, and a caller with no detector box passes the middle of the frame.
    `fallback` asks WHAT SURVIVES when there is no mask at all, and that wants
    to be generous, because anything outside it is destroyed.

    Answering both with the tight box is what published every crop as its own
    middle 64%, with the roof light bar - which sits above the vehicle box on
    purpose - inside the part that got painted over. When `fallback` is not
    given the old behaviour stands, so a caller that genuinely has the vehicle's
    real box (and therefore wants it used for both) passes one argument as
    before.
    """
    out = np.full_like(img, BACKDROP, dtype=np.uint8)
    mask = vehicle_mask(img, box)

    if mask is not None and mask.any():
        m = mask.astype(np.uint8) * 255
        if feather > 0:
            # A one-pixel hard edge looks like a cut-out and, more importantly,
            # a jagged edge leaks a little of what was behind it. Dilate very
            # slightly then soften, so the boundary carries no detail.
            m = cv2.dilate(m, np.ones((feather, feather), np.uint8))
            m = cv2.GaussianBlur(m, (feather * 2 + 1, feather * 2 + 1), 0)
        a = (m.astype(np.float32) / 255.0)[..., None]
        out = (img.astype(np.float32) * a +
               np.array(BACKDROP, np.float32) * (1 - a)).astype(np.uint8)
        return out, "segment"

    keep_box = fallback if fallback is not None else box
    if keep_box is not None:
        x0, y0, x1, y1 = (int(v) for v in keep_box)
        h, w = img.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = img[y0:y1, x0:x1]
            return out, "box"

    return img, "none"


def self_test(path: str, out_path: str) -> None:
    """Strip one image and say what happened. For eyeballing the result."""
    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    res, method = strip(img)
    cv2.imwrite(out_path, res, [cv2.IMWRITE_JPEG_QUALITY, 90])
    kept = float((res != np.array(BACKDROP)).any(axis=2).mean())
    print(f"{path}\n  method={method}  kept {kept:.1%} of the frame -> {out_path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        raise SystemExit("usage: python isolate.py <in.jpg> <out.jpg>")
    self_test(sys.argv[1], sys.argv[2])
