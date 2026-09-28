"""Retire public cameras that measurement says can never produce a sighting.

    python tools/retire_dead_cams.py --source mi
    python tools/retire_dead_cams.py --source mi --apply

🚨 WHY THIS IS NEEDED, AND IT IS A CORRECTION TO MY OWN WORK.

Cameras were enrolled on a width test alone. Several networks answer a DEAD
camera with a full-HD "STREAM NOT AVAILABLE" card, which passes a width test
perfectly - so those got registered as cameras. Measured on Michigan: 77 of the
95 enrolled were that card. They beat, they count as online, they sit on the
map, and they will never produce a sighting for as long as the project runs.

That is worse than missing them. A camera that reports "online" and produces
nothing is indistinguishable from a real camera watching a quiet road, which is
exactly the signal the health watch exists to notice.

⚠️ THIS PAUSES, IT DOES NOT DELETE. The node is a real record of a real
registration and its sightings stay attached to it. `status='paused'` is the
same state a node gets before approval: it stops counting as online, stops
being polled, and can be un-paused if a network comes back. Deleting would
destroy history to fix a count.

⚠️ AND IT ONLY EVER ACTS ON MEASURED EVIDENCE. A camera missing from the probe
is UNMEASURED, not dead, and is left alone - the same rule probe_filter
follows. Missing data is not negative data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db                       # noqa: E402
import public_cams as pc        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--orphans", action="store_true",
                    help="also pause nodes whose name the source no longer "
                         "produces at all")
    a = ap.parse_args()

    probe = pc.load_probe()
    if not probe:
        print("no measurements on this machine - copy data/probe/ here first")
        return 1

    rows = [n for n in db.nodes(active_only=False)
            if (n.get("kind") or "") == "public_cam"
            and pc.ref_of(n.get("name") or "").startswith(a.source + ":")]
    creds = pc.creds_by_name(rows)

    # 🚨 FIRST: WHICH NODES IS THE POLLER USING RIGHT NOW? Those are never
    # touched, whatever else is true of them. Answered by the poller's own
    # list and the poller's own join (name, then position) - see
    # pc.join_source for the Utah/Illinois near-miss that made this a rule.
    # If the live list cannot be read, stop: acting without it is guessing.
    try:
        polled, _dead = pc.polled_index(a.source, probe)
    except Exception as exc:
        print(f"cannot read what the poller uses for {a.source} "
              f"({type(exc).__name__}: {str(exc)[:80]}) - refusing to act")
        return 1
    live, _, _ = pc.join_source(a.source, polled, creds)
    live_ids = {c["node_id"] for c in live}

    # Then everything the source offers, unfiltered, joined the same way, so
    # each remaining node is judged by the measurement of its OWN image.
    raw = pc.dedupe_index(pc.raw_index(a.source))
    hi = pc.SRC_MAX_WIDTH.get(a.source, 10 ** 9)
    matched, _, _ = pc.join_source(a.source, raw, creds)
    by_node = {c["node_id"]: c for c in matched}

    print(f"{len(rows)} registered {a.source} node(s); "
          f"{len(raw)} offered by the source now; "
          f"{len(live_ids)} in use by the poller")

    doomed, unmeasured, keep, already = [], 0, 0, 0
    for n in rows:
        if n["id"] in live_ids:
            keep += 1
            continue
        if (n.get("status") or "") == "paused":
            already += 1
            continue
        c = by_node.get(n["id"])
        if not c:
            # 🚨 AN ORPHAN CANNOT EVER BE POLLED AGAIN, AND THAT IS DIFFERENT
            # FROM UNMEASURED. The poller finds a node by rebuilding its NAME
            # from the live index; a name the source no longer produces will
            # never be rebuilt, so the node sits registered and unreachable for
            # ever. It happened to 580 Minnesota cameras when MnDOT rotated the
            # internal ids their names had been built on.
            #
            # Still opt-in, because "the source did not offer it today" can also
            # mean a feed hiccup, and pausing a live camera for a transient is
            # its own kind of wrong.
            if a.orphans:
                doomed.append((n, -1))
            else:
                unmeasured += 1
            continue
        w = probe.get(pc.probe_key(c))
        if w is None:
            unmeasured += 1              # missing data is not negative data
        elif w == 0 or w < pc.MIN_HD_WIDTH or w > hi:
            doomed.append((n, w))
        else:
            keep += 1

    # 🚨 AN ORPHAN WITH A SUCCESSOR IS NOT A DEAD CAMERA, IT IS A RENAMED ONE.
    # Most orphans exist because a source renumbered and a second node was
    # registered for the same camera (Iowa 09-16, Ontario 09-27). The camera is
    # alive and beating - under its NEW node. Pausing the old row would file a
    # working camera as dead; the true record is `superseded_by`, the mark
    # nodes.enroll already writes when a camera moves: "this row is where the
    # camera used to be, and it is now <node>". Its history stays attached, it
    # leaves the map and the online count, and the lineage is kept.
    #
    # Same evidence as the poller's own re-join: same source, within REJOIN_M,
    # assigned nearest-first one-to-one so two old rows cannot both claim one
    # live camera. An orphan with no live node near it really is gone, and is
    # paused.
    succ: dict = {}
    orphans = [n for n, w in doomed if w == -1]
    if orphans and live:
        import math
        pairs = []
        for i, n in enumerate(orphans):
            if n.get("lat") is None or n.get("lon") is None:
                continue
            kx = 111320.0 * math.cos(math.radians(n["lat"]))
            for j, c in enumerate(live):
                d = math.hypot((n["lon"] - c["lon"]) * kx,
                               (n["lat"] - c["lat"]) * 111320.0)
                if d <= pc.REJOIN_M:
                    pairs.append((d, i, j))
        pairs.sort()
        ti, tj = set(), set()
        for d, i, j in pairs:
            if i in ti or j in tj:
                continue
            ti.add(i)
            tj.add(j)
            succ[orphans[i]["id"]] = live[j]["node_id"]

    print(f"  {keep} still qualify (in use by the poller, or measured HD)")
    if already:
        print(f"  {already} already paused")
    if orphans:
        print(f"  {len(orphans)} orphan(s): {len(succ)} renamed (a live node "
              f"within {pc.REJOIN_M:.0f} m -> superseded_by), "
              f"{len(orphans) - len(succ)} gone (-> paused)")
    print(f"  {unmeasured} unmeasured or no longer offered - LEFT ALONE")
    print(f"  {len(doomed)} measured as unusable (placeholder, too small, "
          f"or a mosaic)")
    for n, w in doomed[:6]:
        print(f"    {n['id']}  width={w:<5} {n['name'][:52]}")
    if len(doomed) > 6:
        print(f"    ... and {len(doomed) - 6} more")

    if not a.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply.")
        return 0

    conn = db.connect()
    paused = 0
    for n, _w in doomed:
        if n["id"] in succ:
            continue
        conn.execute("UPDATE nodes SET status = 'paused' WHERE id = ?",
                     (n["id"],))
        paused += 1
    conn.commit()
    for old, new in succ.items():
        db.set_superseded(old, new)
    print(f"\npaused {paused} node(s), marked {len(succ)} superseded by their "
          f"live successor; every sighting and all history stay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
