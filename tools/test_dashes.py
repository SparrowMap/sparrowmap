#!/usr/bin/env python3
"""Can it find the lane dashes in a rendered road, and still get the speed?

    D:\\LLM\\.venv\\Scripts\\python.exe tools/test_dashes.py

🚨 END TO END, AGAINST GROUND TRUTH. tools/test_calib.py proves the geometry
when the dash positions are handed to it. This proves the step before that:
given PIXELS - a road with traffic driving over it, a shadow across it and a
kerb running beside it - does detection recover dashes good enough to measure
a car whose speed is known in advance?

It renders with the same pinhole camera the calibration test uses, so the two
agree about what the world looks like, and a failure here is a failure of the
image processing rather than of the maths.

⚠️ RENDERED IS NOT REAL. Passing this does NOT mean it survives rain, worn
paint, snow or a low sun on wet tarmac. It means the approach is sound enough
to point at real footage, which is the next step and the one that decides
whether any of this may publish a number.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from detect import calib, dashes  # noqa: E402

W, H = 1280, 720
FOV_DEG, CAM_H, TILT = 60.0, 6.0, math.radians(15.0)
FX = (W / 2) / math.tan(math.radians(FOV_DEG / 2))


def project(d_m, lateral_m=0.0):
    yc = CAM_H * math.cos(TILT) - d_m * math.sin(TILT)
    zc = CAM_H * math.sin(TILT) + d_m * math.cos(TILT)
    if zc <= 0.01:
        return None
    return (W / 2 + FX * lateral_m / zc, H / 2 + FX * yc / zc)


def render(frame_i, np, cv2, with_traffic=True, with_shadow=True,
           with_kerb=True, paint=235, asphalt=95):
    """One frame of a road: dashes, optional traffic, shadow and kerb."""
    img = np.full((H, W), asphalt, dtype=np.uint8)
    img += np.random.default_rng(frame_i).integers(-6, 7, (H, W)).astype(np.int16).astype(np.uint8)

    # the road surface, lighter than the surround, so the frame is not all road
    for d in np.arange(6.0, 90.0, 0.25):
        p0, p1 = project(d, -4.2), project(d, 4.2)
        if p0 and p1:
            cv2.line(img, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])),
                     int(asphalt), 2)

    if with_kerb:   # a continuous line parallel to the dashes - must be rejected
        pts = [project(d, -4.0) for d in np.arange(6.0, 90.0, 0.5)]
        pts = [(int(p[0]), int(p[1])) for p in pts if p]
        for i in range(1, len(pts)):
            cv2.line(img, pts[i - 1], pts[i], 205, 2)

    # The dashes themselves: 10 ft painted, 30 ft gap.
    #
    # ⚠️ THE WIDTH IS PHYSICAL, NOT PICKED TO LOOK RIGHT. A US lane line is
    # 0.10-0.15 m wide, which at this camera is 11.9 px at 10 m and 1.9 px at
    # 71 m. An earlier version of this test drew 1 px everywhere beyond the
    # nearest dash - three to six times too thin - so the morphological open
    # erased them and only two dashes ever survived. The detector was blamed
    # for a fault in the scene it was being shown, which is the whole reason a
    # synthetic test has to be built from the same physics as the thing it
    # tests.
    d = 10.0
    while d < 80.0:
        a, b = project(d), project(d + 3.048)
        if a and b:
            zc = CAM_H * math.sin(TILT) + d * math.cos(TILT)
            wpx = max(1, int(round(FX * 0.12 / zc)))
            cv2.line(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])),
                     paint, wpx)
        d += calib.US_LANE_PERIOD_M

    if with_shadow:
        # A shadow across the road. Blurred on purpose: a real shadow edge is
        # soft, and a perfectly hard one is a rendering artefact that produces
        # a 800-pixel "mark" no camera ever sees.
        sh = np.zeros((H, W), np.uint8)
        cv2.fillPoly(sh, [np.array([[0, 300], [W, 340], [W, 420], [0, 380]])], 255)
        sh = cv2.GaussianBlur(sh, (0, 0), 9)
        img = (img.astype(np.float32) *
               (1.0 - 0.45 * (sh.astype(np.float32) / 255.0))).astype(np.uint8)

    if with_traffic:  # cars at different places each frame, covering dashes
        rng = np.random.default_rng(1000 + frame_i)
        for _ in range(3):
            dd = float(rng.uniform(8.0, 70.0))
            lat = float(rng.uniform(-2.0, 2.0))
            p = project(dd, lat)
            if not p:
                continue
            wpx = int(FX * 1.8 / (CAM_H * math.sin(TILT) + dd * math.cos(TILT)))
            cv2.rectangle(img, (int(p[0] - wpx / 2), int(p[1] - wpx * 0.9)),
                          (int(p[0] + wpx / 2), int(p[1])),
                          int(rng.integers(40, 200)), -1)
    return img


def drive(mph, seconds, fps=12.0, start_m=14.0):
    mps = mph / 2.236936
    out, t = [], 0.0
    while t <= seconds:
        d = start_m + mps * t
        p = project(d)
        if p:
            wpx = FX * 1.8 / (CAM_H * math.sin(TILT) + d * math.cos(TILT))
            out.append([round(t, 3), p[0] / W, p[1] / H, wpx / W, wpx * 0.8 / H])
        t += 1.0 / fps
    return out


def main() -> int:
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("needs cv2 + numpy: run with D:\\LLM\\.venv\\Scripts\\python.exe")
        return 2
    bad = 0

    # ---- 1. does the median remove the traffic? -------------------------
    frames = [render(i, np, cv2) for i in range(dashes.MEDIAN_FRAMES)]
    road = dashes.median_road(frames)
    one = frames[0].astype(int)
    med = road.astype(int)
    moved = int((abs(one - med) > 40).sum())
    print(f"median of {len(frames)} frames: {moved:,} px differ from a single "
          f"frame (the traffic)")

    # ---- 2. detection ----------------------------------------------------
    edges, info = dashes.find(frames, debug=True)
    print(f"detection: {info}")
    ok = len(edges) >= calib.MIN_EDGES
    bad += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {len(edges)} dash edges found")

    # ---- 3. straight into the ruler, and a known speed ------------------
    if edges:
        cal = calib.fit(edges)
        print(f"  calibration: fit error {cal.fit_err:.2%}, usable={cal.usable()}")
        bad += not cal.usable()
        if cal.usable():
            print("  known speed -> measured (from PIXELS, not from given dashes)")
            for truth in (25.0, 40.0, 55.0):
                r = calib.speed(drive(truth, 2.5), cal, W, H)
                if not r.get("ok"):
                    print(f"    FAIL {truth} mph -> refused: {r.get('why')}")
                    bad += 1
                    continue
                err = abs(r["mph"] - truth) / truth
                good = err <= 0.15
                bad += not good
                print(f"    {'ok  ' if good else 'FAIL'} {truth:5.1f} -> "
                      f"{r['mph']:5.1f} mph ({err:+.1%}) "
                      f"range {r['lo_mph']}-{r['hi_mph']}")

    # ---- 4. the refusals -------------------------------------------------
    print("\n  refusals:")
    blank = [render(i, np, cv2, with_traffic=False, paint=96) for i in range(8)]
    e2, i2 = dashes.find(blank, debug=True)
    r = len(e2) < calib.MIN_EDGES
    bad += not r
    print(f"    {'ok  ' if r else 'FAIL'} no painted line -> {i2.get('why', e2)}")

    print(f"\n{'ALL PASS' if not bad else str(bad) + ' FAILED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
