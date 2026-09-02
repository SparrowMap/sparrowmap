"""Re-derive the live head's OPERATING POINT without refitting the weights.

🚨 WHY THIS IS A SEPARATE THING FROM TRAINING, AND WHY IT HAD TO EXIST.

A head has two parts and they are learned from different data. The WEIGHTS come
from fit_local's training rows. The THRESHOLD is a claim about a distribution -
"above this score, 95 out of 100 are really police" - and fit_local can only
measure it on its held-out CAMERA-VIEW rows, which for a long time was a few
hundred rows carrying 36 positives. That is not enough to locate a threshold,
and the one it produced (0.45475) delivered 45% precision on the queue it
actually gates. Measured 2026-09-02 on 9,358 randomly-drawn labelled crops: 58
cards above the live threshold, 26 of them really police.

So this asks the same question of a much better sample - his whole random draw,
`review` / `review_public` / `community_random`, which is a random sample of
what the cameras see rather than a fold of what happened to be labelled early.
It uses the SAME RULE fit_local now uses (train/fit_local.recall_at): scan DOWN
from the strictest threshold and stop at the first break, so the answer is the
lowest score above which precision holds all the way up. A single unlucky point
can only make it stricter.

    python tools\\set_head_threshold.py                 # measure, change nothing
    python tools\\set_head_threshold.py --apply

⚠️ WEIGHTS ARE NEVER TOUCHED. Every other field in the npz is copied through
byte-for-byte, so this cannot quietly become a retrain. If the weights are what
is wrong, refit - do not move the threshold until the number looks nice.

⚠️ THE SCORES ARE PARTLY IN-SAMPLE. Rows the live head was fitted on are scored
by a model that has seen them, so the precision ABOVE the chosen threshold reads
optimistically. The half that matters is not inflated: the false positives it
removes are cards a real human really was handed, and they do not become fewer
because the model has met them before. Treat the recall figure as the soft one.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RANDOM_DRAW = ("review", "review_public", "community_random")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-precision", type=float, default=0.95,
                    help="a false positive here is a wasted human judgement; a "
                         "false positive downstream is a public accusation")
    ap.add_argument("--positive", default="police",
                    help="labels that count as a true positive")
    ap.add_argument("--apply", action="store_true",
                    help="write the new threshold into the head")
    args = ap.parse_args()

    from detect import head as _head
    from tools import bank_index

    if not _head.available():
        raise SystemExit(f"no usable head: {_head.status()}")

    pos = {s.strip() for s in args.positive.split(",") if s.strip()}
    db = bank_index.read()
    try:
        marks = ",".join("?" * len(RANDOM_DRAW))
        rows = db.execute(
            f"SELECT head_conf, label FROM crops WHERE label IN "
            f"('police','gov','civilian','fleet') AND head_conf IS NOT NULL "
            f"AND sampling IN ({marks})", RANDOM_DRAW).fetchall()
    finally:
        db.close()

    s = np.array([float(r["head_conf"]) for r in rows])
    y = np.array([1 if r["label"] in pos else 0 for r in rows])
    if y.sum() < 10:
        raise SystemExit(
            f"only {int(y.sum())} positives in the random draw - not enough to "
            f"locate a threshold. Label more before moving this number.")

    def at(t):
        m = s >= t
        tp, fp = int((m & (y == 1)).sum()), int((m & (y == 0)).sum())
        p = tp / (tp + fp) if tp + fp else float("nan")
        return p, tp / y.sum(), tp, fp

    # The descending scan. See the module docstring and fit_local.recall_at.
    best = None
    for t in sorted(set(np.round(s, 4)), reverse=True):
        p, r, tp, fp = at(t)
        if np.isnan(p):
            continue
        if p < args.min_precision:
            break
        best = (float(t), p, r, tp, fp)

    old = _head.threshold()
    op, orr, otp, ofp = at(old)
    print(f"{len(s)} randomly-drawn labelled crops, {int(y.sum())} really "
          f"{'/'.join(sorted(pos))}\n")
    print(f"  live threshold  {old:.4f}   precision {op:.0%}   recall {orr:.0%}"
          f"   ({otp} true, {ofp} FALSE)")
    if not best:
        raise SystemExit(
            f"\nno threshold reaches {args.min_precision:.0%} precision on this "
            f"sample. The weights are the problem, not the operating point - "
            f"refit rather than moving this number.")
    new, p, r, tp, fp = best
    print(f"  measured        {new:.4f}   precision {p:.0%}   recall {r:.0%}"
          f"   ({tp} true, {fp} FALSE)")
    print(f"\n  -> {ofp} false cards become {fp}; "
          f"{otp - tp} real sighting(s) lost.")

    if not args.apply:
        print("\n[--apply to write it into the head]")
        return

    z = dict(np.load(_head.MODEL, allow_pickle=True))
    z["threshold"] = np.array(new)
    np.savez(_head.MODEL, **z)
    print(f"\nwrote threshold {new:.4f} into {_head.MODEL}")
    print("⚠️  RESTART box_puller - it loads the head once and holds it. A head "
          "on disk that nothing has reloaded is not a head that is running.")


if __name__ == "__main__":
    main()
