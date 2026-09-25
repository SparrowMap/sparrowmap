#!/usr/bin/env python3
"""Does the ruler measure a car whose speed we already know?

    python tools/test_calib.py

🚨 GROUND TRUTH, NOT EYEBALLING. A speed published against a named officer is
an accusation, so the only acceptable evidence that this maths works is a
scene where the right answer is known in advance and the code is asked to
recover it. This builds one with a real pinhole projection - a camera at a
height, looking down a road at an angle, lane dashes at the MUTCD period, and
a car driving at an exactly known speed - then checks what comes back.

It also checks the REFUSALS, which matter more than the successes: a camera
with no dashes, a track that barely moves, and a car that changes speed must
all decline to produce a number rather than produce a confident wrong one.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from detect import calib  # noqa: E402

# ---- a synthetic camera ---------------------------------------------------
# Pinhole, 1280x720, 60 degree horizontal field, mounted 6 m up, tilted down
# 15 degrees, looking along a straight road. The road runs away from it in +Z.
W, H = 1280, 720
FOV_DEG = 60.0
FX = (W / 2) / math.tan(math.radians(FOV_DEG / 2))
FY = FX
CAM_H = 6.0
TILT = math.radians(15.0)


def project(d_m: float, lateral_m: float = 0.0) -> tuple:
    """A point d_m down the road, lateral_m to the side, in pixels."""
    # camera coords: x right, y down, z forward
    y = CAM_H
    z = d_m
    yc = y * math.cos(TILT) - z * math.sin(TILT)
    zc = y * math.sin(TILT) + z * math.cos(TILT)
    if zc <= 0.01:
        return None
    return (W / 2 + FX * lateral_m / zc, H / 2 + FY * yc / zc)


def dash_edges(n: int = 4, first_m: float = 12.0) -> list:
    """Painted dash edges down the road, at the MUTCD period."""
    out = []
    for i in range(n):
        start = first_m + i * calib.US_LANE_PERIOD_M
        for off, kind in ((0.0, "start"), (3.048, "end")):
            p = project(start + off)
            if p:
                out.append((p[0], p[1], kind))
    return out


def drive(mph: float, seconds: float, fps: float = 12.0,
          start_m: float = 14.0) -> list:
    """A car at a constant speed, as VehiclePass.motion(full=True) banks it."""
    mps = mph / 2.236936
    track = []
    t = 0.0
    while t <= seconds:
        d = start_m + mps * t
        p = project(d)
        if p:
            # a 1.8 m wide car, so the box narrows with distance like a real one
            wpx = FX * 1.8 / (CAM_H * math.sin(TILT) + d * math.cos(TILT))
            track.append([round(t, 3), p[0] / W, p[1] / H,
                          wpx / W, wpx * 0.8 / H])
        t += 1.0 / fps
    return track


def main() -> int:
    bad = 0
    cal = calib.fit(dash_edges(5))
    print(f"calibration: {cal.n_edges} dash edges, "
          f"fit error {cal.fit_err:.2%}, usable={cal.usable()}")
    if not cal.usable():
        print("  FAIL: the fit should be usable on a clean synthetic road")
        bad += 1
    print()

    # ---- does it recover a known speed? ----------------------------------
    print("known speed -> measured")
    for truth in (25.0, 35.0, 45.0, 60.0):
        r = calib.speed(drive(truth, 2.5), cal, W, H)
        if not r.get("ok"):
            print(f"  FAIL  {truth:5.1f} mph -> refused: {r.get('why')}")
            bad += 1
            continue
        err = abs(r["mph"] - truth) / truth
        ok = err <= 0.10           # within 10% of truth
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {truth:5.1f} mph -> "
              f"{r['mph']:5.1f} mph  ({err:+.1%})  "
              f"range {r['lo_mph']}-{r['hi_mph']}  "
              f"spread {r['spread']:.0%}  over {r['metres']}m")

    # ---- do the refusals refuse? -----------------------------------------
    print()
    print("refusals (a wrong number is worse than no number)")
    checks = [
        ("no dashes at all", calib.fit([]), drive(35, 2.5)),
        ("only 3 dash edges", calib.fit(dash_edges(2)[:3]), drive(35, 2.5)),
        ("car barely moves", cal, drive(35, 0.25)),
    ]
    for label, c, tr in checks:
        r = calib.speed(tr, c, W, H)
        refused = not r.get("ok")
        bad += not refused
        print(f"  {'ok  ' if refused else 'FAIL'}  {label:22s} -> "
              f"{r.get('why', 'PRODUCED ' + str(r.get('mph')))}")

    # a car that accelerates hard must be reported as disagreeing
    tr = drive(25, 1.2) + [[1.2 + t[0], t[1], t[2], t[3], t[4]]
                           for t in drive(55, 1.2, start_m=25.0)]
    r = calib.speed(tr, cal, W, H)
    flagged = not r.get("ok")
    bad += not flagged
    print(f"  {'ok  ' if flagged else 'FAIL'}  {'car changes speed':22s} -> "
          f"{r.get('why', 'PRODUCED ' + str(r.get('mph')))}")

    print(f"\n{'ALL PASS' if not bad else str(bad) + ' FAILED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
