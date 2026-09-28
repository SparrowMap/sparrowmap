"""Register thousands of public traffic cameras, on the box, without abuse.

🚨 WHY THIS IS NOT `public_cams.py enrol`.

That command posts to /api/enroll one camera at a time with a 0.5s pause. It is
the right shape for eight cameras and the wrong shape for five thousand: 45
minutes of wall clock, and it would consume the enrol rate limit that exists to
stop a runaway client minting cameras - the exact limit that once locked the
operator out of registering a real one.

So this runs ON THE BOX and calls nodes.enroll directly. Same function, same
validation, same records; it simply does not travel over the network to reach
itself, and therefore does not pretend to be five thousand strangers signing up.

⚠️ SAFETY, because this writes thousands of rows:
  * dry run unless --apply, printing exactly what it would create;
  * --limit caps every run, so a mistake is small and bounded;
  * idempotent: cameras already registered under a given node name are
    skipped - COUNTED, not merely detected, because one name can legitimately
    cover several cameras (see the `have` comment). Re-running never
    duplicates and never leaves a shortfall; duplicate cameras are already the
    single biggest data-quality problem on this map;
  * snap_road=False. A public camera gets no span (see nodes.enroll), so there
    is no road lookup at all - which is what makes this finish at all rather
    than issuing one Overpass query per camera.

    python tools/bulk_enrol_cams.py --source fi --limit 50
    python tools/bulk_enrol_cams.py --source fi --limit 3000 --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db                       # noqa: E402
import nodes as node_mod        # noqa: E402
import public_cams as pc        # noqa: E402

PREFIX = "Public traffic camera - "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=sorted(pc.SOURCES))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    print(f"fetching the {a.source} index...")
    offered = pc.SOURCES[a.source]()
    # ⚠️ THE SOURCE ITSELF SERVES THE SAME PICTURE TWICE. Iowa publishes some
    # RWIS snapshots under both `/Public/` and `/public/`, and both were
    # registered, putting two nodes on one mast. Deduped before anything is
    # created, because a duplicate node is the hardest thing here to undo.
    cams = pc.dedupe_index(offered)
    dropped = len(offered) - len(cams)
    print(f"  {len(cams)} camera(s) offered"
          + (f" ({dropped} dropped as the same image under another URL)"
             if dropped else ""))

    # 🚨 "ALREADY REGISTERED" IS DECIDED BY THE POLLER'S OWN JOIN, NOT BY NAME.
    #
    # This once counted exact names, which is right for a NEW camera and wrong
    # for a RENAMED one: when a source renumbers its catalogue every name
    # changes, so every camera looked new and got a second node - while the
    # first sat orphaned holding the history. That is where Iowa's 1,431 dead
    # nodes (09-16) and Ontario's 401 (09-27) came from. pc.join_source pairs by
    # name and then by position, exactly as the poller does, so a camera the
    # poller would find is never registered twice.
    #
    # Counting still matters and join_source keeps it: one name can cover
    # several real cameras (Iowa's ENTRY/CENTER/EXIT share coordinates,
    # device_id and description - 101 names, 499 cameras), so it pairs a LIST
    # of nodes per name rather than asking whether any exists.
    #
    # ⚠️ EVERY STATUS, NOT JUST ACTIVE. A camera whose node is paused IS
    # registered; counting only active nodes re-registers it. If the camera is
    # offered again (it is in the measured list, so it measures usable now) the
    # right fix is to un-pause its node - done below - never a duplicate.
    rows = [n for n in db.nodes(active_only=False)
            if (n.get("kind") or "") == "public_cam"]
    creds = pc.creds_by_name(rows)
    status = {n["id"]: (n.get("status") or "") for n in rows}
    for i, c in enumerate(cams):
        c["_i"] = i
    matched, _, _ = pc.join_source(a.source, cams, creds)
    got = {m["_i"] for m in matched}
    wake = sorted({m["node_id"] for m in matched
                   if status.get(m["node_id"]) == "paused"})
    # pc.node_name_for is the ONE definition of the name - the poller has to
    # rebuild this identical string to find the credentials again.
    todo = [(pc.node_name_for(c), c) for c in cams if c["_i"] not in got]

    print(f"  {len(matched)} already registered, {len(todo)} new"
          + (f", {len(wake)} registered but PAUSED and offered again "
             f"(will be un-paused)" if wake else ""))
    batch = todo[:a.limit]
    print(f"  this run would create {len(batch)}")

    if not a.apply:
        for name, c in batch[:10]:
            print(f"    {name[:64]}  @{c['lat']:.4f},{c['lon']:.4f}")
        if len(batch) > 10:
            print(f"    ... and {len(batch) - 10} more")
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return 0

    if wake:
        conn = db.connect()
        conn.executemany("UPDATE nodes SET status = 'active' "
                         "WHERE id = ? AND status = 'paused'",
                         [(i,) for i in wake])
        conn.commit()
        print(f"  un-paused {len(wake)} node(s) whose camera is offered again")

    made = failed = 0
    for name, c in batch:
        try:
            node_mod.enroll(name=name, lat=c["lat"], lon=c["lon"],
                            kind="public_cam", reach_m=60, snap_road=False)
            made += 1
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"    FAILED {name[:50]}: {str(exc)[:80]}")
        if made and made % 250 == 0:
            print(f"    ...{made} registered")

    print(f"\nregistered {made}, failed {failed}")
    print(f"nodes now: {len(db.nodes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
