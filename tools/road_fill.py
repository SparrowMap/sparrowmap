"""Resolve the road cells the box needs, here, and copy them to the box.

    python tools/road_fill.py --box root@HOST --key PATH          # fill
    python tools/road_fill.py --box ... --key ... --dry-run       # just look
    python tools/road_fill.py --box ... --key ... --limit 50      # bounded

WHY THIS EXISTS

Road snapping needs Overpass, and Overpass answers this machine while refusing
the public box. Measured 2026-08-19: overpass-api.de returns roads here in
1.8-3.2s, and from the box the same request gets `Connection refused` on both
IPv4 and IPv6 - ICMP to it succeeds at 25ms and github/openstreetmap/cloudflare
all return 200 from there, so it is them refusing that host, almost certainly
because it sits in a Hetzner range.

The only public instance that DID answer the box was maps.mail.ru, and buying
snapping with it meant telling a large Russian service which ~400 m squares a
project that tracks police vehicles keeps asking about. This file is the reason
that trade is not necessary: the cells get resolved on the machine Overpass is
willing to talk to, and copied to the one that needs them.

🚨 NOTHING NEW IS DISCLOSED BY MOVING THE WORK. The query is the same
grid-snapped bbox road.py already sends - never a camera's true position, which
is what GRID_DEG exists for. Only the machine doing the asking changes.

⚠️ OVERPASS RATE LIMITS HARD, and politeness here is not optional - a sweep of
18 nodes earned a 429 in under two minutes. So this sleeps between queries,
stops on the first sustained refusal, and is meant to be run on a timer with a
small --limit rather than as a backfill of everything at once. The box needs
about 350 new cells a day; a handful per run keeps up easily.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 🪟 NO POPUP WINDOW. This runs from a scheduled task (~every 10 min) under
# pythonw, so python itself is hidden - but each ssh/scp child still flashes a
# CMD window unless told not to. CREATE_NO_WINDOW is Windows-only; getattr keeps
# this a no-op elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

import road                                             # noqa: E402

REMOTE_CACHE = "/opt/sparrowmap/data/roadcache"


# 🚨 ASK OVERPASS WHEN IT IS READY INSTEAD OF GUESSING.
#
# road.py cools a mirror for 600s after any failure, which is right for the HUB
# - a request handler cannot sit and wait. It is wrong here. Measured: a run
# earned one 429, road.py then cooled every mirror for ten minutes, and the run
# resolved nothing and exited 0. Meanwhile /api/status reported "2 slots
# available now" seconds later.
#
# Overpass publishes its own rate-limit state, so this asks rather than assumes:
# it waits for a slot, then queries. That is both faster and politer than a
# blind backoff, and it is what the endpoint is documented to want.
STATUS_URL = "https://overpass-api.de/api/status"
_SLOT_RE = __import__("re").compile(r"Slot available after:.*?in (\d+) seconds")


def wait_for_slot(max_wait: float = 90.0, quiet: bool = False) -> bool:
    """Block until Overpass has a free slot. False if it never frees up."""
    import re
    import urllib.request
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                STATUS_URL,
                headers={"User-Agent": "SparrowMap/0.1 (citizen ALPR; road snapping)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode("utf-8", "replace")
        except Exception:
            return True          # cannot ask: proceed and let the query decide
        m = re.search(r"(\d+) slots? available now", txt)
        if m and int(m.group(1)) > 0:
            return True
        waits = [int(x) for x in _SLOT_RE.findall(txt)] or [5]
        nap = min(max(min(waits), 2), max(1.0, deadline - time.time()))
        if not quiet:
            print("   (overpass busy, waiting %ds for a slot)" % int(nap))
        time.sleep(nap)
    return False


def sh(box: str, key: str, cmd: str, timeout: int = 300) -> str:
    r = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", box, cmd],
                       capture_output=True, timeout=timeout,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise SystemExit("ssh failed: %s" % r.stderr.decode()[:300])
    return r.stdout.decode("utf-8", "replace")


# The query the box actually needs answered, derived the same way road.py
# derives it, so a file written here is one the box will accept as its own.
REMOTE_WANTED = r'''
import json, sqlite3, math, time, os
GRID = 0.004
def cell(lat, lon):
    dlon = GRID / max(math.cos(math.radians(lat)), 1e-6)
    return (int(math.floor(lat / GRID)), int(math.floor(lon / dlon)))
db = sqlite3.connect("/opt/sparrowmap/data/sparrow.db")
db.row_factory = sqlite3.Row
want = {}
# Volunteer cameras are the ones snapping is FOR. A public_cam publishes its
# true position from the transport department's own feed and needs no span.
for r in db.execute("SELECT id,lat,lon,kind FROM nodes WHERE status='active' "
                    "AND kind<>'public_cam' AND lat IS NOT NULL"):
    want.setdefault(cell(r["lat"], r["lon"]), 0)
    want[cell(r["lat"], r["lon"])] += 1
# ...and wherever sightings are actually landing, since that is what gets drawn.
cut = time.time() - 7 * 86400
for r in db.execute("SELECT lat,lon FROM sightings WHERE ts>? AND lat IS NOT NULL "
                    "LIMIT 40000", (cut,)):
    k = cell(r["lat"], r["lon"])
    want[k] = want.get(k, 0) + 1
db.close()
have = set()
d = "/opt/sparrowmap/data/roadcache"
if os.path.isdir(d):
    for f in os.listdir(d):
        if f.endswith(".v2.json"):
            a, _, b = f[:-8].partition("_")
            try:
                have.add((int(a), int(b)))
            except ValueError:
                pass
missing = [[k[0], k[1], n] for k, n in want.items() if k not in have]
missing.sort(key=lambda x: -x[2])
print(json.dumps({"missing": missing, "have": len(have), "want": len(want)}))
'''


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", required=True)
    ap.add_argument("--key", required=True)
    # ⚠️ THREE, NOT TWENTY-FIVE. Measured: overpass-api.de answered two cells
    # (285 and 120 ways) and returned 429 on the third, after which road.py
    # correctly cooled every mirror down for ten minutes. So a big --limit does
    # not fill faster, it just earns a rate limit and then idles. Small and
    # often beats large and rare: at 3 per run every 10 minutes this resolves
    # ~430 cells a day against the ~350 the box needs, and clears a backlog.
    ap.add_argument("--limit", type=int, default=3,
                    help="cells to resolve this run (Overpass rate limits hard)")
    ap.add_argument("--sleep", type=float, default=8.0,
                    help="seconds between queries, to stay polite")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp())
    q = tmp / "wanted.py"
    q.write_text(REMOTE_WANTED, encoding="utf-8")
    subprocess.run(["scp", "-i", a.key, "-o", "BatchMode=yes", "-q",
                    str(q), "%s:/tmp/_road_wanted.py" % a.box], check=True,
                   creationflags=_NO_WINDOW)
    info = json.loads(sh(a.box, a.key,
                         "cd /opt/sparrowmap && .venv/bin/python /tmp/_road_wanted.py"))
    missing = info["missing"]
    print("box has %d cells cached, wants %d, missing %d"
          % (info["have"], info["want"], len(missing)))
    if not missing:
        print("nothing to do.")
        return
    if a.dry_run:
        for y, x, n in missing[:a.limit]:
            print("   cell %s_%s  (%d nodes/sightings depend on it)" % (y, x, n))
        return

    # Resolve here, where Overpass answers. Ordered by how many things depend on
    # the cell, so a bounded run fixes the busiest places first.
    done = fail = 0
    out = tmp / "out"
    out.mkdir()
    for y, x, n in missing[:a.limit]:
        # Centre of the cell, which is all fetch_ways needs - it re-snaps to the
        # same grid internally, so this cannot widen what gets asked.
        lat = (y + 0.5) * road.GRID_DEG
        dlon = road.GRID_DEG / max(math.cos(math.radians(lat)), 1e-6)
        lon = (x + 0.5) * dlon
        # Wait for a slot, and clear the hub-style cooldown first: this tool
        # manages its own rate against /api/status, so the 600s blanket that is
        # correct inside a request handler would only make it idle here.
        if not wait_for_slot():
            print("   overpass has no free slot; stopping politely.")
            break
        road._MIRROR_DOWN.clear()
        try:
            ways = road.fetch_ways(lat, lon, timeout=25.0)
        except Exception as exc:
            fail += 1
            print("   %s_%s FAILED %s" % (y, x, str(exc)[:60]))
            # 🚨 STOP ON A RUN OF FAILURES rather than hammering a rate limiter.
            # Overpass answers 429 and then stays angry; continuing turns one
            # polite tool into the thing that gets this address blocked too.
            if fail >= 3 and done == 0:
                print("   three failures and nothing succeeded - stopping.")
                break
            time.sleep(a.sleep * 2)
            continue
        (out / ("%s_%s.v2.json" % (y, x))).write_text(
            json.dumps([[nm, [list(p) for p in pts]] for nm, pts in ways]),
            encoding="utf-8")
        done += 1
        print("   %s_%s  %d ways  (%d dependents)" % (y, x, len(ways), n))
        time.sleep(a.sleep)

    if not done:
        print("resolved nothing; leaving the box alone.")
        return
    subprocess.run(["scp", "-i", a.key, "-o", "BatchMode=yes", "-q",
                    "-r"] + [str(p) for p in out.iterdir()]
                   + ["%s:%s/" % (a.box, REMOTE_CACHE)], check=True,
                   creationflags=_NO_WINDOW)
    sh(a.box, a.key, "chown -R sparrow:sparrow %s" % REMOTE_CACHE)
    print("copied %d cell(s) to the box (%d failed)" % (done, fail))
    print("box cache now: %s"
          % sh(a.box, a.key, "ls %s | wc -l" % REMOTE_CACHE).strip())


if __name__ == "__main__":
    # The timer runs this under pythonw, where stdout/stderr are None: a crash
    # showed only as task result 1 with no reason (10-30). Keep a short log.
    if sys.stdout is None:
        _log = Path(__file__).with_name(".road_fill.log")
        if _log.exists() and _log.stat().st_size > 200_000:
            _log.write_text("", encoding="utf-8")
        sys.stdout = sys.stderr = open(_log, "a", encoding="utf-8", buffering=1)
        print("--- %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    main()
