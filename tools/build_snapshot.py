"""Bake the map's opening view into one cacheable file.

    python tools/build_snapshot.py          # writes public/static/snapshot.json

🚨 WHY. Every visitor currently waits on TWO live API calls before a single dot
appears - measured 57.8 KB and 175.5 KB, about 0.7s - and both answer
`cache-control: max-age=4` with `cf-cache-status: DYNAMIC`, so the edge caches
neither. Thousands of viewers are thousands of database queries for an answer
that is identical for all of them, and the visitor stares at an empty map while
it happens.

A snapshot is a static file under /static/, which the edge DOES cache, so the
first paint costs one cached fetch and no origin work at all.

🚨 IT GOES THROUGH THE SAME REDACTION AS THE API, AND THAT IS THE WHOLE RISK.
This file bypasses the request path, so every guard living in that path would be
bypassed with it - private plate text, the per-day node alias, private
photographs. It calls privacy.redact with viewer="anon", exactly as
hub._public_rows does. ⚠️ If a new rule is ever added to the read path, it has
to be added there and NOT here, or the two will drift and the static copy will
be the one that leaks.

⚠️ AND IT IS LABELLED STALE, BECAUSE IT IS.
The file carries the moment it was built. The map draws it immediately, then
replaces it the instant live data lands, and does not claim to be live until
that happens. A stale snapshot presented as live would be the project telling a
small lie on its own front page every few minutes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                       # noqa: E402
import privacy                  # noqa: E402

# ⚠️ public/, NOT public/static/. The hub maps /static/<name> to
# PUBLIC/<name> and takes only the BASENAME (hub.py: PUBLIC / Path(p[8:]).name),
# so a file in a subdirectory is served as a 404 - which is exactly what
# happened on the first attempt.
OUT = Path(__file__).resolve().parent.parent / "public" / "snapshot.json"

# What the map asks for on boot, so the snapshot answers the same questions.
# ⚠️ Keep in step with load() in public/app.js - a snapshot of something the
# map does not draw is dead weight in every visitor's first request.
PUBLIC_LIMIT = 2000
TRAFFIC_WINDOW_S = 45           # TRAFFIC_FADE_S: a private pass fades this fast
TRAFFIC_LIMIT = 400


def main() -> int:
    now = time.time()
    pub = db.recent_sightings(since=0, limit=PUBLIC_LIMIT, vclass="public")

    # 🚨 THE SAME FUNCTION THE API USES. Not a copy of its rules.
    # And the same null-drop as hub._public_rows (10-17): without it the first build on the box was
    # 1,268 KB against the API's 599 KB for the same 2,000 rows - 12 always-empty keys (plate_text,
    # plate_hash, make, speed_mph...) on every row. Compared row by row: no value differed.
    pub = [{k: v for k, v in privacy.redact(r, "anon").items() if v is not None} for r in pub]

    # 🚨 NO LIVE TRAFFIC IN HERE, AND THE FIRST VERSION HAD IT WRONG.
    #
    # A private pass fades from the map after TRAFFIC_FADE_S - 45 seconds - and
    # this file is rebuilt every two minutes. So every traffic row it could
    # carry is ALREADY EXPIRED before the first visitor sees it: the map would
    # paint dots and immediately age them out, having claimed for a moment that
    # cars were passing places they had long since left.
    #
    # It also doubled the file, 58 KB to 115 KB, to do it. A public sighting is
    # a RECORD and stays true; a live pass is an EVENT and cannot be cached.
    live: list = []

    doc = {
        "generated": now,
        # Spelled out so anything reading this file - including a human opening
        # it - knows what it is without having to infer it.
        "note": ("A snapshot of the public map, built periodically and served "
                 "from cache so the map is not empty on arrival. It is not "
                 "live; the page replaces it with live data on load."),
        "public": pub,
        "traffic": live,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and moved into place, so a visitor never receives a
    # half-written file - this is served straight off disk by the hub.
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)

    size = OUT.stat().st_size
    print(f"{len(pub)} public + {len(live)} live -> {OUT} "
          f"({size / 1024:.0f} KB)")
    # A snapshot nobody can draw is worse than none: it costs a request and
    # returns nothing. Say so rather than exiting quietly.
    if not pub and not live:
        print("⚠ EMPTY - the map would still open blank; check the database")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
