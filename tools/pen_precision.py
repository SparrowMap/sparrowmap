"""What the review pen would actually be handed, at a given head threshold.

🚨 THIS EXISTS BECAUSE THE TRAINER'S OWN PRECISION NUMBER DID NOT SURVIVE
CONTACT WITH THE QUEUE. fit_local reports precision on held-out CAMERA-VIEW
rows, where positives are a large fraction of the set. The pen is fed from
ordinary traffic, where they are a fraction of a percent. A threshold chosen at
95% precision on the first distribution delivered 45% on the second - measured
2026-09-02, when better than half of every card put in front of a human was not
a police vehicle at all.

So this asks the question the trainer cannot: over the RANDOM DRAW - his own
`review` / `review_public` sampling, which is a random sample of what the
cameras actually see - what does each score band contain?

    python tools\\pen_precision.py                 # the live head's threshold
    python tools\\pen_precision.py --threshold 0.9

⚠️ Rows the live head was FITTED on are scored in-sample here, so the top band
flatters itself. The band that matters is the one BELOW the threshold you are
considering: a false positive there is a real card a real human really had to
judge, and no amount of in-sample optimism invents those.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The random draw. Anything else is a queue built to be biased toward the
# model's mistakes (`likely`, `hunt`, `gap`) and cannot measure precision.
RANDOM_DRAW = ("review", "review_public", "community_random")

BANDS = [0.0, 0.2, 0.4, 0.6, 0.8, 0.9, 0.95, 1.01]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=None,
                    help="the operating point to score (default: the live head's)")
    ap.add_argument("--positive", default="police",
                    help="comma-separated labels that count as a true positive")
    args = ap.parse_args()

    from detect import head as _head
    from tools import bank_index

    pos = {s.strip() for s in args.positive.split(",") if s.strip()}
    neg = {"police", "gov", "civilian", "fleet"} - pos
    thr = args.threshold if args.threshold is not None else _head.threshold()
    print(f"head: {_head.status()}")
    print(f"positive = {sorted(pos)}   negative = {sorted(neg)}")
    print(f"threshold under test: {thr:.5f}\n")

    db = bank_index.read()
    try:
        marks = ",".join("?" * len(RANDOM_DRAW))
        rows = db.execute(
            f"SELECT head_conf, label FROM crops "
            f"WHERE label IN ('police','gov','civilian','fleet') "
            f"AND head_conf IS NOT NULL AND sampling IN ({marks})",
            RANDOM_DRAW).fetchall()
    finally:
        db.close()

    s = [float(r["head_conf"]) for r in rows]
    y = [1 if r["label"] in pos else 0 for r in rows]
    if not s:
        raise SystemExit("no scored rows in the random draw; nothing to measure")

    print(f"{len(s)} randomly-drawn labelled crops, {sum(y)} of them positive\n")
    print(f"{'score band':>16}{'crops':>8}{'true':>7}{'precision':>11}")
    print("-" * 42)
    for lo, hi in zip(BANDS, BANDS[1:]):
        idx = [i for i, v in enumerate(s) if lo <= v < hi]
        if not idx:
            continue
        t = sum(y[i] for i in idx)
        print(f"{lo:>7.2f} - {hi:<6.2f}{len(idx):>8}{t:>7}{t / len(idx):>10.0%}")

    print()
    print(f"{'threshold':>10}{'queued':>8}{'true':>7}{'precision':>11}{'recall':>9}")
    print("-" * 45)
    total_pos = sum(y)
    for t_ in sorted({round(thr, 5), 0.5, 0.7, 0.8, 0.9, 0.95, 0.98}):
        idx = [i for i, v in enumerate(s) if v >= t_]
        tp = sum(y[i] for i in idx)
        p = tp / len(idx) if idx else float("nan")
        r = tp / total_pos if total_pos else float("nan")
        mark = "  <- live" if abs(t_ - thr) < 1e-9 else ""
        print(f"{t_:>10.4f}{len(idx):>8}{tp:>7}{p:>10.0%}{r:>9.0%}{mark}")


if __name__ == "__main__":
    main()
