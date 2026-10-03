"""Turn review verdicts into training labels.

A reviewer answering "cop" or "not a cop" is the most valuable signal the project
produces: a human judgement on a real camera's view of a real vehicle, in the
conditions the model actually fails in. But the reviewer app runs on the public
mirror, and a mirror deliberately banks nothing - so the verdict lands in the
box's audit log while the crop it refers to sits unlabelled on the machine that
banked it. The loop was open.

This closes it. It reads the box's review verdicts over the existing SSH admin
channel, matches each one to the locally banked crop by the sighting id that
box_puller wrote into the sidecar, and writes the label:

    review:confirm  ->  label 'police'    (a human said this is a government vehicle)
    review:reject   ->  label 'civilian'  (a human said it is not)

Rejections matter more than confirmations here. They are the hard negatives -
the unmarked SUVs and dark sedans the classifier is most likely to be wrong
about - and there is no other way to collect them.

    python tools/sync_review_labels.py --box root@host --key ~/.ssh/key
    python tools/sync_review_labels.py --box ... --key ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import DATA                       # noqa: E402

BANK = DATA / "training"
# What a verdict means in the label vocabulary the trainer reads.
VERDICT_LABEL = {"review:confirm": "police", "review:reject": "civilian"}


def ssh(args, cmd: str, timeout: int = 60) -> str:
    return subprocess.run(
        ["ssh", "-i", args.key, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", args.box, cmd],
        capture_output=True, text=True, timeout=timeout).stdout


def fetch_verdicts(args) -> list[dict]:
    """Every review verdict on the box, newest first."""
    remote = (
        f"cd {args.remote} && ./.venv/bin/python -c \""
        "import db,json,time;"
        "c=db.connect();"
        f"rows=[dict(r) for r in c.execute("
        f"\\\"SELECT action,target,actor,ts FROM audit WHERE action IN "
        f"('review:confirm','review:reject') AND ts>? "
        # 🚨 NOT actor 'box_review' rejects: 13.8M of those are the HEAD's own
        # automatic discards, mislogged as human until 2026-09-29 (see
        # box_publish.discard_one). Labelling them 'civilian' would train the
        # head on its own misses. Human CLI rejects now log as box_review_cli.
        f"AND NOT (action='review:reject' AND actor='box_review') "
        f"ORDER BY ts DESC\\\","
        f"(time.time()-{int(args.days)}*86400,))];"
        "print(json.dumps(rows))\"")
    out = ssh(args, remote, timeout=90).strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else []
    except Exception:
        print(f"could not read verdicts: {out[:300]}", file=sys.stderr)
        return []


def newest_per_target(verdicts: list[dict]) -> tuple[list[dict], int]:
    """One verdict per sighting: the LATEST. Returns (kept, superseded_count).

    🚨 WITHOUT THIS THE TOOL TELLS YOU TO CORRUPT YOUR OWN DATA.
    People review the same vehicle twice, and 3 of the 7 repeats here changed
    answer - #30921 and #30922 went reject -> confirm, #32681 went confirm ->
    reject. The query returns newest first, so the loop applied the NEW verdict
    and then met the OLD one, found the label already changed, and reported a
    "conflict - use --relabel to overwrite". Following that advice would have
    applied the STALE verdict on top of the fresh one and left the OLDEST
    answer as the final label, on exactly the hard cases (unmarked SUVs, decal'd
    fleet trucks) where the second look is the one worth having.

    A person who rules on the same vehicle twice has changed their mind, not
    voted twice. Latest wins, and the superseded ones never reach the loop, so
    the only things left to report as conflicts are real disagreements between
    a human's verdict and a human's existing label.
    """
    seen: set = set()
    kept: list[dict] = []
    for v in verdicts:                      # query is ORDER BY ts DESC
        t = str(v.get("target"))
        if t in seen:
            continue
        seen.add(t)
        kept.append(v)
    return kept, len(verdicts) - len(kept)


def sidecars_by_sighting() -> dict:
    """{sighting_id: sidecar path} for every locally banked crop that carries one."""
    idx = {}
    for jf in BANK.rglob("*.json"):
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = d.get("sighting_id")
        if sid is not None:
            idx[str(sid)] = jf
    return idx


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--box", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--remote", default="/opt/sparrowmap")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--relabel", action="store_true",
                    help="overwrite a label that disagrees (default: leave it)")
    args = ap.parse_args()

    verdicts = fetch_verdicts(args)
    print(f"{len(verdicts)} review verdict(s) on the box in the last "
          f"{args.days:.0f} days")
    if not verdicts:
        return
    verdicts, superseded = newest_per_target(verdicts)
    if superseded:
        print(f"{superseded} superseded verdict(s) ignored - a vehicle reviewed "
              f"twice keeps the LATEST answer")
    idx = sidecars_by_sighting()
    print(f"{len(idx)} locally banked crop(s) carry a sighting id")

    applied = already = missing = conflict = 0
    for v in verdicts:
        want = VERDICT_LABEL.get(v["action"])
        jf = idx.get(str(v["target"]))
        if not want:
            continue
        if not jf:
            missing += 1
            continue
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            missing += 1
            continue
        have = d.get("label")
        if have == want:
            already += 1
            continue
        if have and not args.relabel:
            # A human already said something else about this crop. Two humans
            # disagreeing is a real signal; silently overwriting it is not.
            conflict += 1
            print(f"  #{v['target']}: labelled '{have}' but the review said "
                  f"'{want}' - left alone (use --relabel to overwrite)")
            continue
        if args.dry_run:
            applied += 1
            continue
        d["label"] = want
        d["labelled_at"] = time.time()
        d["labelled_from"] = "rv_review"
        d["labelled_by"] = v.get("actor") or "reviewer"
        d["sampling"] = "review"     # a judgement on what the system published
        jf.write_text(json.dumps(d, indent=1), encoding="utf-8")
        applied += 1

    print(f"\napplied {applied}, already correct {already}, "
          f"no local crop {missing}, conflicts {conflict}"
          f"{' (dry run)' if args.dry_run else ''}")
    if missing:
        print("  'no local crop' is normal for sightings from another person's "
              "camera that this machine never banked.")


if __name__ == "__main__":
    main()
