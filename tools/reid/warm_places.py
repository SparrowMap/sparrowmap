#!/usr/bin/env python3
"""Fill the place cache for sightings whose camera has no town of its own.

    python tools/reid/warm_places.py            # what is missing
    python tools/reid/warm_places.py --apply    # ask Nominatim, ~1.2 s each

🚨 SEPARATE FROM THE PIPELINE ON PURPOSE. link.py only ever reads the CACHE
(places.known), never the network, because a run that blocks a second per
sighting stops being a four-hourly job. The network cost is paid here, by a
tool somebody chose to run, where the rate limit is visible and interruptible.

Rounded to ~1 km before asking, so a dashcam that drove down one street is one
lookup rather than forty - and so the cache never becomes a list of the exact
places a volunteer has been.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import places  # noqa: E402

DATA = HERE.parent.parent / "data" / "reid"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()

    feats = json.load(open(DATA / "features.json"))
    # Only rows whose camera has no town: a fixed camera already answers this
    # question and asking again would spend the budget on what is known.
    need = [f for f in feats
            if not (f.get("place") or "").strip()
            and f.get("lat") is not None and f.get("lon") is not None]
    keys = Counter(f"{round(float(f['lat']), places.ROUND)},"
                   f"{round(float(f['lon']), places.ROUND)}" for f in need)
    cache = places._load()
    missing = [k for k in keys if k not in cache]
    print(f"{len(feats):,} sightings, {len(need):,} from a camera with no town")
    print(f"{len(keys):,} distinct ~1km cells, {len(missing):,} not yet cached")
    print(f"cache now: {places.stats()}")
    if not a.apply:
        print(f"\n--apply would ask for {min(len(missing), a.limit)} of them "
              f"at {places.DELAY_S}s each "
              f"= ~{min(len(missing), a.limit) * places.DELAY_S / 60:.0f} min")
        return 0

    done = 0
    for k in missing[:a.limit]:
        lat, lon = (float(x) for x in k.split(","))
        town, state = places.near(lat, lon)
        done += 1
        print(f"  {k:24s} -> {town or '-'}, {state or '-'}", flush=True)
    print(f"\nasked {done}; cache now {places.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
