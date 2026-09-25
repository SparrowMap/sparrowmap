#!/usr/bin/env python3
"""Point the dash detector at REAL camera frames and report what it does.

    D:\\LLM\\.venv\\Scripts\\python.exe tools/try_dashes_live.py --source ia --cams 6
    ... --interval 20 --frames 15      # DOT snapshots refresh slowly; respect that
    ... --keep data/dashtest           # write the median + an overlay to look at

🚨 THIS IS THE TEST THAT DECIDES WHETHER ANY OF THIS MAY PUBLISH A NUMBER.
tools/test_calib.py proves the geometry and tools/test_dashes.py proves the
image processing, both against scenes this project rendered itself - which is
proof that the code does what it was designed to do, and no evidence whatever
about rain, worn paint, snow, a low sun on wet tarmac, or a camera pointed at
a junction rather than down a lane.

So this reports honestly rather than scoring: for each real camera it prints
what was found, what was rejected and why. A high refusal rate is a RESULT,
not a failure - most cameras in this network watch junctions and residential
streets, and the correct answer there is no ruler and no speed.

It is deliberately gentle on the source: the same public snapshot URLs the
network already polls, one request per camera per interval.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import calib, dashes  # noqa: E402

UA = {"User-Agent": "SparrowMap/1.0 (+https://sparrowmap.com) dash-calibration-test"}


def grab(url: str, timeout: float = 20.0):
    import cv2
    import numpy as np
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = np.frombuffer(r.read(), np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="ia")
    ap.add_argument("--cams", type=int, default=6)
    ap.add_argument("--frames", type=int, default=15)
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between snapshots; DOT feeds refresh slowly "
                         "and asking faster just returns the same picture")
    ap.add_argument("--match", default="I-80",
                    help="only cameras whose name contains this")
    ap.add_argument("--keep", default="",
                    help="directory to write the median road + overlay into")
    a = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except ImportError:
        print("needs cv2: run with D:\\LLM\\.venv\\Scripts\\python.exe")
        return 2
    import public_cams as pc

    cams = [c for c in pc.SOURCES[a.source]()
            if a.match.lower() in (c.get("name") or "").lower() and c.get("url")]
    # One camera per distinct URL, and spread across the list rather than
    # taking the first N - consecutive entries are often the same junction
    # from three angles, which would test one scene three times.
    seen, picked = set(), []
    step = max(1, len(cams) // max(1, a.cams))
    for c in cams[::step]:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        picked.append(c)
        if len(picked) >= a.cams:
            break
    if not picked:
        print(f"no cameras matched {a.match!r} in {a.source}")
        return 1

    print(f"{len(picked)} camera(s), {a.frames} frames each at "
          f"{a.interval:.0f}s = ~{a.frames * a.interval / 60:.0f} min\n")
    for c in picked:
        print(f"  {c['name'][:60]}")
    print()

    frames = {c["url"]: [] for c in picked}
    t0 = time.time()
    for i in range(a.frames):
        for c in picked:
            try:
                img = grab(c["url"])
                if img is not None:
                    frames[c["url"]].append(img)
            except Exception as e:
                print(f"    ! {c['name'][:36]}: {e.__class__.__name__}")
        done = i + 1
        print(f"  frame {done}/{a.frames}  ({time.time() - t0:.0f}s)", flush=True)
        if done < a.frames:
            time.sleep(a.interval)

    print()
    usable = 0
    for c in picked:
        fs = frames[c["url"]]
        name = c["name"][:52]
        if len(fs) < 4:
            print(f"  {name:54s} only {len(fs)} frames fetched")
            continue
        # How much actually MOVED between frames: if a feed is static the
        # median is meaningless and so is everything after it.
        diff = int((abs(fs[0].astype(int) - fs[-1].astype(int)) > 40).sum())
        edges, info = dashes.find(fs, debug=True)
        line = (f"  {name:54s} {len(fs):2d}f  moved={diff:>7,}  "
                f"{info.get('components', 0):2d} marks")
        if len(edges) >= calib.MIN_EDGES:
            cal = calib.fit(edges)
            usable += cal.usable()
            line += (f" -> {info.get('dashes', 0)} dashes, fit "
                     f"{cal.fit_err:.1%}, "
                     f"{'USABLE' if cal.usable() else 'not usable'}")
        else:
            line += f" -> no ruler: {info.get('why', 'too few edges')}"
        print(line)

        if a.keep:
            out = Path(a.keep)
            out.mkdir(parents=True, exist_ok=True)
            stem = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
            road = dashes.median_road(fs)
            cv2.imwrite(str(out / f"{stem}_median.png"), road)
            ov = cv2.cvtColor(road, cv2.COLOR_GRAY2BGR)
            for (x, y, kind) in edges:
                cv2.circle(ov, (int(x), int(y)), 4,
                           (0, 0, 255) if kind == "start" else (0, 255, 0), -1)
            cv2.imwrite(str(out / f"{stem}_marks.png"), ov)

    print(f"\n{usable} of {len(picked)} cameras produced a usable ruler.")
    print("A low number is a finding, not a bug: most cameras in this network "
          "watch junctions, not lanes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
