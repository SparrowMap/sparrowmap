"""Turn human review-pen verdicts into training labels, without walking the bank.

    python tools\\sync_pen_labels.py --box root@HOST --key KEY [--since 2026-09-02] [--dry-run]

WHY NOT tools/sync_review_labels.py
    That tool finds each verdict's crop by opening EVERY sidecar in the bank
    (BANK.rglob). On 2026-10-07 the bank held ~83 day folders of ~300k crops on
    a spinning disk - most of a day of random reads. And it wrote two things
    that quietly spoiled every retrain:

      * no `label_vocab`, so fit_local.load() dropped EVERY synced `police`
        label as an ambiguous vocab-1 "government" click - the pen's
        confirmations never trained anything;
      * `sampling="review"`, the tag of his RANDOM draw, which fit_local
        MEASURES on. Pen cards are crops the head already picked - measuring on
        them is the selection bias the 2026-09-02 notes warn about.

    This writes `sampling="pen"` (trainable, never measured) and the current
    LABEL_VOCAB.

HOW IT FINDS THE CROP
    box_puller scores an inbox pull in sighting-id order, banks each crop as it
    goes, and prints one line per crop, then `pulled ... at HH:MM:SS`. So a
    reviewed sighting's crop is the k-th sidecar written during that cycle. The
    log gives the cycle and k; one scandir per day folder gives every sidecar's
    mtime for free; then only a handful of files around position k are opened
    to confirm `sighting_id`. Nothing is inferred: a crop is labelled only when
    its own sidecar names the sighting.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import DATA  # noqa: E402
import labelbank       # noqa: E402

BANK = DATA / "training"
LOG = DATA.parent / "logs" / "box_puller.log"
# sid -> "remote_<day>/<stem>" for every crop already found. The runner kills
# this step whenever BeamNG starts and re-runs it later; without this file every
# re-run repeated the whole search (10-07 night: 8 h re-finding crops already
# labelled). With it a re-run resumes where the last one stopped.
CACHE = BANK / "pen_sync_found.json"
VERDICT_LABEL = {"review:confirm": "police", "review:confirm_gov": "gov",
                 "review:reject": "civilian"}
CROP = re.compile(r"^\s+#(\d+): ")
CYCLE = re.compile(r"^pulled (\d+),.* at (\d\d):(\d\d):(\d\d)\s*$")


def ssh(args, py: str) -> str:
    cmd = (f"cd {args.remote} && .venv/bin/python3 - <<'EOF'\n{py}\nEOF")
    return subprocess.run(["ssh", "-i", args.key, "-o", "BatchMode=yes",
                           args.box, cmd], capture_output=True, text=True,
                          timeout=300).stdout


def fetch(args, since: float) -> tuple[dict, list]:
    """Human verdicts (latest per sighting) and (id, ts) anchors."""
    py = f"""
import sqlite3, json
c = sqlite3.connect("file:data/sparrow.db?mode=ro", uri=True)
v = c.execute(\"\"\"SELECT target, action, actor, ts FROM audit
  WHERE ts > ? AND action IN ('review:confirm','review:confirm_gov','review:reject')
  AND actor NOT IN ('box_review') ORDER BY ts\"\"\", ({since},)).fetchall()
a = c.execute("SELECT id, ts FROM sightings WHERE ts > ? AND (tier='public' OR id % 200 = 0)",
              ({since - 3 * 86400},)).fetchall()
print(json.dumps({{"v": v, "a": a}}))
"""
    out = ssh(args, py).strip().splitlines()
    d = json.loads(out[-1])
    latest = {}
    for target, action, actor, ts in d["v"]:          # ordered: later wins
        try:
            latest[int(target)] = (action, actor, ts)
        except ValueError:
            pass
    anchors = sorted((int(i), float(t)) for i, t in d["a"])
    return latest, anchors


def ts_of(sid: int, anchors: list) -> float | None:
    """Sighting ids rise with time, so interpolate between the nearest rows."""
    ids = [a[0] for a in anchors]
    j = bisect.bisect_left(ids, sid)
    if j == 0 or j >= len(ids):
        return None
    (i0, t0), (i1, t1) = anchors[j - 1], anchors[j]
    return t0 + (t1 - t0) * (sid - i0) / max(1, i1 - i0)


def parse_log(want: set) -> dict:
    """sid -> (cycle_end_hms, prev_end_hms, k, n_printed) for wanted sids."""
    found, cur, prev = {}, [], None
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CROP.match(line)
            if m:
                cur.append(int(m.group(1)))
                continue
            m = CYCLE.match(line)
            if m:
                hms = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
                for k, sid in enumerate(cur):
                    if sid in want:
                        found[sid] = (hms, prev, k, len(cur))
                prev, cur = hms, []
            elif line.startswith("loading CLIP") or line.startswith("marked-law"):
                cur, prev = [], None                 # a restart: no open cycle
    return found


_listing: dict = {}


def listing(day: str) -> list:
    """[(mtime, stem)] for one day folder, sorted. One scandir, no opens."""
    if day not in _listing:
        rows = []
        d = BANK / f"remote_{day}"
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".json"):
                        rows.append((e.stat().st_mtime, e.name[:-5]))
        except FileNotFoundError:
            pass
        rows.sort()
        _listing[day] = rows
        print(f"  listed remote_{day}: {len(rows):,} sidecars", flush=True)
    return _listing[day]


def locate(sid: int, info: tuple, ts: float) -> Path | None:
    hms, prev, k, n = info
    base = datetime.fromtimestamp(ts)
    for add in (0, 1, 2):                          # pulled on the day, or after
        day = (base + timedelta(days=add)).date()
        end = datetime(day.year, day.month, day.day, *hms).timestamp()
        if end < ts - 600:
            continue
        start = end - 7200
        if prev:
            p = datetime(day.year, day.month, day.day, *prev).timestamp()
            start = p if p <= end else p - 86400
        rows = listing(day.isoformat())
        lo = bisect.bisect_left(rows, (start - 2, ""))
        hi = bisect.bisect_right(rows, (end + 2, "~"))
        window = rows[lo:hi]
        if not window:
            continue
        # The k-th printed crop is roughly the k-th written; repeats from the
        # bank's de-dup shift it a little, so look outwards from k.
        guess = min(len(window) - 1, int(k * len(window) / max(1, n)))
        order = sorted(range(len(window)), key=lambda i: abs(i - guess))[:400]
        for i in order:
            p = BANK / f"remote_{day.isoformat()}" / f"{window[i][1]}.json"
            try:
                if json.loads(p.read_text(encoding="utf-8")).get("sighting_id") == sid:
                    return p
            except Exception:
                continue
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--box", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--remote", default="/opt/sparrowmap")
    ap.add_argument("--since", default="2026-09-02")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).timestamp()

    t0 = time.time()
    verdicts, anchors = fetch(args, since)
    print(f"{len(verdicts):,} sightings with a human verdict since {args.since}; "
          f"{len(anchors):,} time anchors", flush=True)
    found = parse_log(set(verdicts))
    print(f"{len(found):,} of them appear in the puller log "
          f"({time.time() - t0:.0f}s)", flush=True)

    from tools import bank_index
    db = None if args.dry_run else bank_index.connect()
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    print(f"{len(cache):,} crops already found by an earlier run", flush=True)

    def save_cache():
        if not args.dry_run:
            tmp = CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            os.replace(tmp, CACHE)

    applied = same = missing = conflict = 0
    for n, (sid, info) in enumerate(sorted(found.items())):
        if n % 250 == 0:
            print(f"  {n:,}/{len(found):,}  applied {applied:,}  already {same:,}  "
                  f"missing {missing:,}  ({time.time() - t0:.0f}s)", flush=True)
            save_cache()
            if db:
                db.commit()
        jf = BANK / cache[str(sid)] if str(sid) in cache else None
        if jf is None or not jf.exists():
            ts = ts_of(sid, anchors)
            jf = locate(sid, info, ts) if ts else None
            if jf:
                cache[str(sid)] = f"{jf.parent.name}/{jf.name}"
        if not jf:
            missing += 1
            continue
        action, actor, vts = verdicts[sid]
        want = VERDICT_LABEL[action]
        d = json.loads(jf.read_text(encoding="utf-8"))
        have = d.get("label")
        if have and have != want and d.get("labelled_from") != "rv_review":
            conflict += 1                 # a human labelled it otherwise: keep
            continue
        if have == want and d.get("sampling") == "pen":
            same += 1
            continue
        if not args.dry_run:
            d.update(label=want, labelled_at=vts, labelled_from="rv_review",
                     labelled_by=actor or "reviewer", sampling="pen",
                     label_vocab=labelbank.LABEL_VOCAB)
            jf.write_text(json.dumps(d, indent=1), encoding="utf-8")
            bank_index.update_one(db, jf.parent.name, jf.stem, commit=False)
        applied += 1
    save_cache()
    if db:
        db.commit()
    print(f"\napplied {applied:,}, already done {same:,}, crop not found "
          f"{missing:,}, kept a different human label {conflict:,}"
          f"{' (dry run)' if args.dry_run else ''}  in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
