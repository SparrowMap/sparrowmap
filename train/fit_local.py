"""Train the vehicle head on locally labelled crops, and measure it honestly.

This replaces threshold tuning, which is finished: on real camera data the
zero-shot confidence distributions OVERLAP - a confirmed patrol car at 0.901
sits below a false positive at 0.962 - so no constant can separate them. See
the ledger entry for 2026-08-08.

## The constraint that shapes everything here

    camera-view (review mode)   224 civilian,  6 police
    hunt mode                    26 civilian             (biased: hardest cases)
    handheld photographs          6 civilian, 15 police  (different distribution)

SIX camera-view positives. That is the honest size of the test set, because it
is the only data drawn from the distribution the system actually runs on, and
it is small enough that any single number computed from it carries enormous
error bars. So this script:

  * TESTS only on review-mode camera-view crops - never on handheld photos and
    never on hunt-mode crops, both of which are biased samples by construction;
  * uses STRATIFIED K-FOLD so all six positives take a turn being held out,
    instead of one arbitrary split deciding the verdict;
  * TRAINS on everything else available, including handheld and hunt, because
    training on a biased sample is fine - it is only measuring on one that lies;
  * compares against CLIP ZERO-SHOT on exactly the same held-out rows, because
    "is the new thing better than what it replaces" is the only question worth
    answering, and it has to be asked of identical data;
  * prints PRECISION and RECALL with the raw counts beside them, never accuracy.
    At a 1-in-40 base rate "always say civilian" is 97% accurate and useless.

## Why a head on frozen CLIP rather than fine-tuning

277 labelled crops cannot fine-tune a vision transformer. They can fit a
logistic boundary in a 512-dimensional space that CLIP has already organised,
which is exactly the part that is wrong - CLIP's features distinguish these
vehicles perfectly well, its zero-shot PROMPT does not. Strong L2 is essential
at 512 features and 277 samples, so C is chosen by inner cross-validation
rather than picked.

    python train\\fit_local.py
    python train\\fit_local.py --min-precision 0.95
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import labelbank   # noqa: E402  - for LABEL_VOCAB; see the POSITIVE note below
from core import DATA   # noqa: E402
from sklearn.metrics import average_precision_score   # noqa: E402

BANK = DATA / "training"
MODEL_OUT = DATA / "models" / "vehicle_head.npz"

# ── MEASURED, not chosen. Average precision over 8 fold seeds, 590 held-out
# camera rows with 10 government vehicles:
#
#   CLIP zero-shot                    AP 0.806
#   head C=0.05  with handheld        AP 0.740   ← my first guess, WORSE than
#   head C=0.05  camera-only          AP 0.765     the thing it replaces
#   head C=0.5   with handheld        AP 0.844
#   head C=0.5   camera-only          AP 0.896 ± 0.005   ← beats zero-shot 8/8 seeds
#
# Two things I had wrong. C=0.05 was over-regularised to the point of being
# worse than doing nothing. And the HANDHELD PHOTOGRAPHS HURT: 15 extra
# positives from the police-station car park cost 0.05 AP, because close sharp
# side-on shots teach a boundary that does not hold at a window at 45 degrees
# in motion blur. train/README.md predicted that domain gap; it turns out to
# outweigh the value of the extra positives rather than merely dilute it.
# 🚨 RE-MEASURED 2026-09-02 AND MOVED 0.5 -> 0.001, A FACTOR OF 500.
#
# The numbers above were taken on 590 held-out rows with 10 positives. The set
# is now 12,848 rows and the held-out slice is 10,028 with 23 positives, because
# 9,048 `review_public` labels arrived - and 0.5 does not survive that. A
# regularisation strength is a statement about HOW MUCH DATA THERE IS, not a
# constant of the problem, so a value baked in at one dataset size silently
# answers "is the head any good" at the wrong setting forever after. It nearly
# cost the retrained head: at C=0.5 it measured WORSE than the zero-shot it
# replaces (AP 0.501 vs 0.568) and read as "the retarget failed".
#
# Out-of-fold, 10 seeds, recall at >=95% precision / average precision:
#
#   C=2.0     30%      AP 0.468        ← 0/3 splits
#   C=0.5     33% ± 9% AP 0.501        ← what was deployed, WORSE than zero-shot
#   CLIP zero-shot 35% AP 0.568          (the thing it has to beat)
#   C=0.1     43%      AP 0.533        ← better recall, ranking REGRESSED
#   C=0.02    41% ± 2% AP 0.569
#   C=0.005   43% ± 0% AP 0.582
#   C=0.002   43% ± 2% AP 0.592 ± 0.005  10/10 splits
#   C=0.001   43% ± 1% AP 0.592 ± 0.004  10/10 splits   ← HERE
#   C=0.0005  43% ± 0% AP 0.585
#   C=0.0002  42% ± 2% AP 0.582
#
# 📌 IT IS A PLATEAU, NOT A PEAK, AND THAT IS WHY IT IS TRUSTWORTHY. Every value
# from 0.0002 to 0.01 - a 50x range - beats zero-shot on both metrics on every
# split. 0.001 and 0.002 are indistinguishable (0.592 both); 0.001 is taken for
# the tighter spread and the lower operating point (0.979 vs 0.984), which
# queues marginally more at the same measured precision. Do not read anything
# into 42% vs 43%: that is ONE positive out of 23 and the set cannot resolve it.
#
# ⚠️ RE-MEASURE THIS WHENEVER THE LABEL COUNT CHANGES BY MUCH. That is the whole
# lesson - `--C` is a flag now so it costs one command, not an edit.
C_BEST = 0.001
# ⚠️ "CAMERA ONLY" MEANS EXCLUDE HANDHELD, NOT "REVIEW MODE ONLY".
#
# The first version trained on review-mode rows alone, which quietly discarded
# every positive gathered in `likely` or `hunt` mode - five government vehicles
# he had just spent an evening finding, thrown away by the training set while
# the test set correctly ignored them too.
#
# Those two exclusions answer different questions and were wrongly fused:
#   TEST  must be review-mode only, because a biased sample cannot MEASURE.
#   TRAIN wants every camera-view row available, because a biased sample
#         trains perfectly well - it is only measurement that it corrupts.
# Handheld stays out of both, because it was MEASURED to hurt (AP 0.890 -> 0.844).
EXCLUDE_FROM_TRAINING = {"handheld", "scraped"}

# 🚨 AN ALLOWLIST, NOT A DENYLIST. HIS RULE, AND IT IS THE RIGHT ONE:
# "training data should be gated and approved by me. all of it."
#
# The line above is a denylist, and a denylist fails silently the moment a new
# sampling tag appears. That is not hypothetical - it already happened. 186
# machine-made labels were written on 2026-08-18, were not in the denylist, and
# therefore trained the head without anybody approving them. Recall fell twelve
# points and the labels were never the decision they looked like.
#
# So membership is now positive: a tag trains only if it is HERE, and every tag
# here is one he clicked himself. `machine` and `community` are deliberately
# absent. They become trainable by being APPROVED on /proof, which retags them
# `confirmed` - so the gate is a human action, not a config line somebody
# forgets to update.
#
# What this protects is the thing he actually asked for: that a private car can
# never be labelled police in the training data, and that a cruiser is only ever
# found with a police label. No machine and no stranger can put either claim
# into the training set without him seeing the picture first.
#
# ⚠️ `handheld` is absent for a DIFFERENT reason and it is not about approval -
# he labelled those himself. It was MEASURED to hurt (AP 0.890 -> 0.844),
# because press-style photographs teach photography rather than policing.
APPROVED_FOR_TRAINING = {
    "review",      # random draw, his
    "review_public",  # random draw restricted to public/other-node cameras
    "likely",      # highest-government-confidence queue, his
    "hunt",        # CLIP's hardest cases, his
    "split",       # police-vs-gov re-ask, his
    "marked",      # crops he called government, re-shown for undo
    "recheck",     # civilians the model disagreed with, his
    "gap",         # CLIP says government, head refused - his clicks
    "patrol",      # the same queue with heavy plant filtered out - his clicks
    "remote",      # other people's nodes, oldest first - his clicks
    "confirmed",   # a machine or community label he has approved on /proof
}

# ⚠️ THE LIST ABOVE IS EVERY MODE THE LABELLING PAGE OFFERS, AND THAT IS THE
# POINT: if a tag is missing, HIS OWN work silently stops training. Writing the
# allowlist from memory got `gap` and `remote` wrong on the first attempt - 58
# and 29 of his own labels would have been dropped. Check it against the mode
# buttons in camctl/label.html when a queue is added, not against recollection.

# 🚦 WHAT MAY MEASURE. His call, 2026-08-18: crowd consensus is allowed to
# measure, because the project is open and the statistic is checkable by anyone.
# But only a RANDOM sample can measure anything, so this is his random draw plus
# the community's random stratified slice - never the patrol queue, whose whole
# purpose is to be biased toward the model's mistakes.
MEASURABLE = {"review", "review_public", "community_random"}


def _trainable(src):
    """Boolean mask: rows he has approved for training."""
    import numpy as np
    return np.isin(src, list(APPROVED_FOR_TRAINING))

# Label -> is this a POLICE UNIT we would publish?
#
# 🚨 THIS HEAD ANSWERS "IS IT A COP", NOT "IS IT GOVERNMENT" (his call,
# 2026-09-02). It used to be POSITIVE = {"police", "gov"}, and that was correct
# for what it was built to do - but it is not what the review queue asks.
#
# A city bus, a fire truck, an ambulance and a municipal works pickup are all
# genuinely government vehicles, so a government head fires on them CORRECTLY
# and they filled the review pen. Nothing publishes them - the map carries
# patrol units - so every one of those cards cost a human judgement and
# produced nothing. His words: "we aren't putting the gov vehicles that aren't
# cops on the map anyways so its kind of pointless for those to ever show up in
# review".
#
# 🚨 AND `gov` MOVES TO THE NEGATIVE SIDE, WHICH IS THE POINT. 36 passes is not
# many, but they are the exact confusions this head keeps making - the bus, the
# ambulance, the amber-beacon works truck - so they are worth more per row than
# any ordinary civilian car. As `split` mode produces more, this gets stronger
# on its own.
#
# ⚠️ A vocab-1 `police` LABEL IS NOT A POLICE LABEL. See labelbank.LABEL_VOCAB:
# until 2026-08-10 the label set had one government key and the button read
# "Government", so a municipal pickup and a marked patrol car were both clicked
# `police`. Narrowing the target WITHOUT excluding those would quietly teach
# this head that a bin lorry is a patrol car - the precise failure it is being
# retargeted to remove. They are dropped in `load()`, not reinterpreted:
# `split` mode exists to have a human resolve them, and nothing is inferred.
POSITIVE = {"police"}
NEGATIVE = {"civilian", "fleet", "gov"}   # gov = government, but not a cop


# A vehicle crossing the frame breaks into several tracker tracks, so ONE car
# becomes several crops seconds apart. Crops closer than this from the same
# node are treated as one pass and must never be split across train and test.
SAME_PASS_S = 10.0


_CUTOFF = float(os.environ.get("SPARROW_LABEL_CUTOFF") or 0)

# The order matters and must never change: it is baked into any saved head.
CLIP_CLASSES = ("police", "emergency", "gov_dot", "fleet", "civilian")
_WITH_CLIP = os.environ.get("SPARROW_NO_CLIP_FEATURES") != "1"


def _labelled_paths() -> list:
    """Image paths of every crop that carries a usable label.

    🚨 EMBED ONLY THESE, NOT THE WHOLE BANK. embed_dir used to walk all 655k
    crops and embed every unlabelled one - hours on the GPU, and it deadlocked
    box_puller's CLIP. The measurement only ever uses labelled crops, so ask the
    index which those are (fast) and hand embed_dir exactly that list.
    """
    from tools import bank_index
    db = bank_index.read()
    try:
        rows = db.execute(
            "SELECT day, stem FROM crops WHERE label IN "
            "('police','gov','civilian','fleet')").fetchall()
    finally:
        db.close()
    return [BANK / r["day"] / f"{r['stem']}.jpg" for r in rows]


def load() -> tuple[np.ndarray, np.ndarray, np.ndarray, list]:
    """Embeddings, labels, and which distribution each row came from."""
    from train.embed import embed_dir
    e = embed_dir(BANK, only=_labelled_paths())
    by_rel = {rel: vec for vec, rel in zip(e["vecs"], e["paths"])}

    X, y, src, meta = [], [], [], []
    for rel, vec in by_rel.items():
        side = (BANK / rel).with_suffix(".json")
        if not side.exists():
            continue
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        clip = d.get("clip") or {}
        lab = d.get("label")
        # SPARROW_LABEL_CUTOFF lets a run reproduce an EARLIER state of the
        # dataset, so "the number moved" can be attributed to the labels that
        # arrived rather than guessed at. Unset in normal use.
        if _CUTOFF and float(d.get("labelled_at") or 0) > _CUTOFF:
            continue
        # 🚨 DROP THE AMBIGUOUS POSITIVES. A `police` label written under
        # vocab 1 means "government vehicle" - the button said Government and
        # there was no way to say "government, not police" (labelbank.VALID).
        # Reading it as a police label is exactly how a fire truck becomes a
        # training positive for a police head. The NEGATIVE keys are unaffected:
        # `civilian` and `fleet` meant the same thing under both vocabularies,
        # so their 1,440 vocab-1 rows still train.
        if lab in POSITIVE and int(d.get("label_vocab") or 1) < labelbank.LABEL_VOCAB:
            continue
        if lab in POSITIVE:
            y.append(1)
        elif lab in NEGATIVE:
            y.append(0)
        else:
            continue                     # 'unsure' is not a training signal
        # 🚨 GIVE THE HEAD CLIP'S OWN ANSWER, NOT JUST THE PICTURE.
        #
        # the operator asked why his labels do not change how the model reads a
        # vehicle. They should - but the head was only ever shown the raw
        # embedding, so it was being asked to rediscover from ~1,400 local
        # examples what CLIP already knows from 400 million. That is why it
        # kept LOSING to the thing it was meant to replace.
        #
        # Appending the zero-shot scores turns the job into the right one:
        # learn where CLIP is WRONG, on this street, rather than start from
        # nothing. A head with these features can in principle never do worse
        # than zero-shot, because passing the police score straight through is
        # inside its hypothesis space - it only has to learn the corrections
        # his labels encode ("that shape at 55% is a pickup, not a patrol car").
        #
        # Set SPARROW_NO_CLIP_FEATURES=1 to reproduce the old behaviour.
        if _WITH_CLIP:
            sc = clip.get("scores") or {}
            vec = np.concatenate([vec, np.array([
                float(sc.get(k, 0.0)) for k in CLIP_CLASSES] + [
                float(clip.get("conf") or 0.0),
                float(clip.get("margin") or 0.0)], dtype=vec.dtype)])
        X.append(vec)
        # Provenance, NOT the UI's mode. Labelling a scraped crop through
        # :8160/label overwrites `sampling` with whatever mode the page was in
        # ("hunt"), which silently destroys the one field that says this row is
        # a press photograph rather than a frame from his window. `source` is
        # written by the ingest and never touched again.
        src.append("scraped" if d.get("source") == "scraped"
                   else (d.get("sampling") or "review"))
        meta.append({"rel": rel, "clip_conf": float(clip.get("conf") or 0),
                     "clip_class": clip.get("vclass"),
                     "clip_margin": float(clip.get("margin") or 0),
                     "ts": float(d.get("ts") or 0.0),
                     "node": d.get("node_id") or ""})
    return np.array(X), np.array(y), np.array(src), meta


def pass_groups(meta: list) -> np.ndarray:
    """One id per VEHICLE PASS, not per crop.

    🚨 WITHOUT THIS THE EVALUATION LIES, AND IT LIED BY 17 POINTS.
    The tracker fragments a single car into several tracks, so one patrol car
    passing produced up to three near-identical crops seconds apart. Some
    landed in review mode and their twins in likely mode - so the head trained
    on a car and was then "tested" on the same car, from the same second, at
    the same angle. Three of ten test positives had a twin in training, and
    recall came out at 87% against a true 70%.

    Random k-fold assumes rows are independent. These are not, and nothing in
    the data says so - the duplication is invisible unless you go looking for
    it by timestamp. Grouping by pass is what makes the split honest.
    """
    order = sorted(range(len(meta)), key=lambda i: (meta[i]["node"], meta[i]["ts"]))
    groups = np.zeros(len(meta), dtype=int)
    gid, prev = -1, None
    for i in order:
        m = meta[i]
        if (prev is None or m["node"] != prev["node"]
                or not m["ts"] or abs(m["ts"] - prev["ts"]) > SAME_PASS_S):
            gid += 1
        groups[i] = gid
        prev = m
    return groups


def pr(scores, y, t):
    pred = scores >= t
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    return p, r, tp, fp, fn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-precision", type=float, default=0.95,
                    help="operating point: a false positive is a public accusation")
    ap.add_argument("--folds", type=int, default=8)
    # 🚨 MULTIPLE SEEDS, ALWAYS. A single fold split reported 9/10 recall for
    # this exact model; forty runs put it at 69% ± 7%. The 9/10 was a lucky
    # partition and I came within one command of quoting it as a result. An
    # instrument that CAN hand you a flattering number eventually will, so this
    # one is not able to.
    ap.add_argument("--seeds", type=int, default=10)
    # 🚨 ABLATION, BECAUSE "MORE LABELS" IS A HYPOTHESIS NOT A FACT.
    # 186 machine-made positives were added on 2026-08-18 and recall against
    # zero-shot fell from 60% to 48%. Almost all of them came from PUBLIC
    # TRAFFIC CAMERAS while all 43 test positives come from his own camera, so
    # the obvious suspect is distribution rather than label quality - and the
    # only way to tell is to train without them and look. This flag makes that
    # a one-line experiment instead of an edit.
    # 🚨 AN ABLATION IS A MEASUREMENT, NOT A DEPLOYMENT.
    #
    # This script fits a final model and writes it over the LIVE head as its
    # last act, which is right when you are deploying and wrong every other
    # time. It has now cost a working classifier twice: on 2026-08-15 the live
    # threshold silently became 0.98885, and on 2026-08-18 an ablation run
    # installed a 0.9948 head that box_puller loaded and scored against for the
    # minutes between the run finishing and a human reading the output.
    #
    # Nothing warned, because a saved file looks the same as a good one. So a
    # run that exists to ANSWER A QUESTION can now decline to touch the live
    # head at all. Default behaviour is unchanged, so deploying still works the
    # way it always did.
    # 🚨 C=0.5 WAS TUNED ON 1,548 ROWS AND THE SET IS NOW 12,848.
    # A regularisation strength is not a constant of nature, it is a statement
    # about how much data there is. Leaving it baked in meant every later run
    # silently answered "is the head better" at a setting chosen for a training
    # set eight times smaller. Sweep it instead of trusting it.
    ap.add_argument("--C", type=float, default=C_BEST,
                    help=f"inverse regularisation strength (default {C_BEST})")
    ap.add_argument("--promote-anyway", action="store_true",
                    help="save even if the head did NOT beat zero-shot. For a "
                         "deliberate retarget whose old numbers are not "
                         "comparable - never to get past a bad measurement.")
    ap.add_argument("--no-save", action="store_true",
                    help="evaluate and report, but do NOT write the head. Use "
                         "this for every ablation and comparison run.")
    ap.add_argument("--out", default="",
                    help="write the fitted head here instead of the live path")
    ap.add_argument("--exclude", default="",
                    help="comma-separated sampling tags to drop from TRAINING "
                         "as well (e.g. machine,community)")
    args = ap.parse_args()
    if args.exclude:
        for t in args.exclude.split(","):
            APPROVED_FOR_TRAINING.discard(t.strip())
        print("training on: %s" % sorted(APPROVED_FOR_TRAINING))

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedGroupKFold
    from sklearn.preprocessing import StandardScaler

    X, y, src, meta = load()
    groups = pass_groups(meta)
    cam = np.isin(src, list(MEASURABLE))
    print(f"\n{len(y)} labelled crops: {int(y.sum())} government, "
          f"{int((y == 0).sum())} not")
    print(f"  camera-view (review) : {int(cam.sum())}  "
          f"[{int(y[cam].sum())} government]")
    print(f"  other (handheld/hunt): {int((~cam).sum())}  "
          f"[{int(y[~cam].sum())} government]")

    n_pos_cam = int(y[cam].sum())
    if n_pos_cam < 2:
        raise SystemExit("need at least 2 camera-view positives to cross-validate")
    folds = min(args.folds, n_pos_cam)

    # Every held-out prediction, gathered across folds, so one honest curve can
    # be drawn over all six positives instead of six curves over one each.
    oof_head = np.full(len(y), np.nan)
    oof_clip = np.full(len(y), np.nan)
    cam_idx = np.where(cam)[0]

    seed_scores = []
    for seed in range(args.seeds):
      oof_head[:] = np.nan
      skf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
      for tr_i, te_i in skf.split(X[cam_idx], y[cam_idx], groups[cam_idx]):
        test_rows = cam_idx[te_i]
        # Train on the other camera folds PLUS every non-camera row. Biased
        # training data is fine; biased TEST data is not.
        # This fold's review rows, PLUS every other camera-view row (likely,
        # hunt) that is not being tested on. Never handheld.
        # ⚠️ AND THE EXTRA ROWS MUST DROP ANY PASS BEING TESTED ON.
        # Group-aware folds keep a pass together WITHIN the review set, but the
        # likely/hunt rows are added afterwards - and that is exactly where the
        # twins were. Excluding by group closes the door they came through.
        test_groups = set(groups[cam_idx[te_i]].tolist())
        extra = np.where(~cam
                         & _trainable(src)
                         & ~np.isin(groups, list(test_groups)))[0]
        train_rows = np.concatenate([cam_idx[tr_i], extra])

        # 🚨 STANDARDISE. Without it the seven CLIP-score features sit on a
        # completely different scale from the 512 embedding dimensions, and one
        # L2 penalty applied across both shrinks the informative few toward
        # nothing. Adding the CLIP scores alone moved AP 0.730 -> 0.792 and
        # still lost; adding the scaler as well took it to 0.830, past
        # zero-shot for the first time. The features were always there to be
        # used - the regulariser was throwing them away.
        sc = StandardScaler().fit(X[train_rows])
        clf = LogisticRegression(max_iter=5000, C=args.C,
                                 class_weight="balanced")
        clf.fit(sc.transform(X[train_rows]), y[train_rows])
        oof_head[test_rows] = clf.predict_proba(sc.transform(X[test_rows]))[:, 1]
        for r in test_rows:
            m = meta[r]
            # CLIP's own answer for the same row: its police confidence when it
            # said police, and 0 when it said anything else.
            oof_clip[r] = (m["clip_conf"]
                           if m["clip_class"] in ("police", "gov_dot", "emergency")
                           else 0.0)
      seed_scores.append(oof_head[cam].copy())

    yt = y[cam]
    n_cam = int(cam.sum())
    print(f"\nHELD-OUT CAMERA-VIEW ROWS: {n_cam} ({int(yt.sum())} government)")
    print(f"Every row scored by a model that never saw it, averaged over "
          f"{args.seeds} fold splits.\n")

    def recall_at(s):
        """Best recall at a threshold precision NEVER drops below, going up.

        🚨 THIS USED TO SCAN UPWARDS AND RETURN THE FIRST CROSSING, AND THAT IS
        HOW THE LIVE HEAD ENDED UP OPERATING AT 0.45475.

        First-crossing is a MINIMUM STATISTIC. On a held-out set with 36
        positives it only takes one fold where the few negatives above some low
        score happen to be scarce for precision to touch the target there, and
        that fluke - not the model - sets the operating point for everything
        downstream. The median across seeds does not save it: every seed is
        reporting its own minimum, so the median is a median of flukes.

        Measured 2026-09-02 on 9,358 randomly-drawn labelled crops, which is
        the distribution the pen actually draws from:

            head score      crops   actually government
            0.455 - 0.60      10          0
            0.60  - 0.80      16          0
            0.80  - 0.90       7          2
            >= 0.95           23         23

        So the deployed threshold opened a band containing ZERO true positives
        and 26 false ones - better than half of everything a human was asked to
        judge. The band is not noise the model is unsure about; it is a region
        it is confidently wrong in, and a rule that stops at its lower edge
        cannot see that because it never looks any higher.

        Scanning DOWN from the strictest threshold and stopping at the first
        break gives the lowest threshold above which precision holds all the
        way up - a property of the whole tail rather than of one point in it.
        A single unlucky point can now only make the answer STRICTER, which is
        the safe direction for a decision that ends in a public accusation.
        """
        best = None
        for t_ in sorted(set(np.round(s, 4)), reverse=True):
            p_, r_, tp_, *_ = pr(s, yt, t_)
            if np.isnan(p_):
                continue           # nothing predicted at all yet; keep going
            if p_ < args.min_precision:
                break              # precision has broken - everything below is
                                   # unreachable, whatever it scores in isolation
            best = (r_, t_)
        return best or (0.0, None)

    clip_s = oof_clip[cam]
    clip_r, clip_t = recall_at(clip_s)
    clip_ap = average_precision_score(yt, clip_s)
    print(f"  CLIP zero-shot (what it replaces)")
    print(f"    recall @>={args.min_precision:.0%} precision : {clip_r:.0%}"
          f"  (threshold {clip_t:.3f})" if clip_t else "    unreachable")
    print(f"    average precision          : {clip_ap:.3f}\n")

    recs = np.array([recall_at(s)[0] for s in seed_scores])
    aps = np.array([average_precision_score(yt, s) for s in seed_scores])
    print(f"  trained head  (C={args.C}, trained on every camera-view row, "
          f"handheld excluded)")
    print(f"    recall @>={args.min_precision:.0%} precision : "
          f"{recs.mean():.0%} ± {recs.std():.0%}   "
          f"(range {recs.min():.0%}-{recs.max():.0%})")
    print(f"    average precision          : {aps.mean():.3f} ± {aps.std():.3f}")
    print(f"    beats zero-shot on recall  : {int((recs > clip_r).sum())}"
          f"/{len(recs)} splits")
    print(f"    beats zero-shot on AP      : {int((aps > clip_ap).sum())}"
          f"/{len(aps)} splits\n")

    # 🚨 A MEAN THAT WINS BY LESS THAN ITS OWN SPREAD HAS NOT WON.
    #
    # `better_ap` was `aps.mean() > clip_ap`, and on 2026-09-02 that turned
    # AP 0.569 ± 0.013 against zero-shot's 0.568 into "✅ Better on both. Wire
    # it in." - a one-thousandth difference, thirteen times smaller than the
    # seed-to-seed spread, while the per-split count printed directly above it
    # said the head won 1 split out of 3. The verdict and the evidence for the
    # verdict were on adjacent lines and disagreed.
    #
    # This file already knows the lesson - "a single fold split reported 9/10
    # for a model that averages 69%" - and applied it to the SEEDS while
    # leaving the COMPARISON on a bare mean. So:
    #
    #   ranking       must not REGRESS beyond one standard deviation. Equal is
    #                 fine: a head that ranks like zero-shot but converts it
    #                 into a better yes/no is exactly what a head is for.
    #   the operating point   must improve on a MAJORITY OF SPLITS, not just on
    #                 average. This is the number the review pen actually uses,
    #                 so it is the one that has to be robust rather than lucky.
    ap_regressed = aps.mean() < clip_ap - aps.std()
    op_splits = float((recs > clip_r).mean())
    better_ap = not ap_regressed
    better_op = recs.mean() > clip_r and op_splits > 0.5
    if better_ap and better_op:
        print(f"  ✅ Better where it counts: the operating point improves on "
              f"{op_splits:.0%} of splits and ranking has not regressed.")
    elif better_ap and recs.mean() > clip_r:
        print(f"  ⚖️  Better ON AVERAGE at the operating point but only on "
              f"{op_splits:.0%} of splits - that is a lucky partition, not a "
              f"better model. More camera-view positives should settle it.")
    elif better_ap:
        print("  ⚖️  Better at RANKING, not at the publish decision.")
        print("     It has learned something real - it orders vehicles better -")
        print("     but there are not enough positives to turn that into a better")
        print("     yes/no at the threshold. More camera-view positives should")
        print("     convert it. Do NOT promote on the flattering metric alone.")
    else:
        print("  ❌ Not better. Leave zero-shot in place.")

    # A single split can and did report 9/10 for a model that averages 69%.
    oof_head[cam] = seed_scores[0]

    # 🚨 "LEAVE ZERO-SHOT IN PLACE" USED TO BE ADVICE THIS SCRIPT THEN IGNORED.
    #
    # It printed the verdict and saved the head anyway, so the ONLY thing
    # standing between a measured-worse model and the live classifier was a
    # human reading the output and having remembered to pass --no-save. On
    # 2026-09-02 a police-only refit measured AP 0.501 against zero-shot's
    # 0.568 - beaten on 10 splits out of 10 - and would have been installed on
    # the strength of having been the most recent run.
    #
    # The failure this file already documents twice (an ablation overwriting
    # production, a threshold silently becoming 0.98885) is the same one: a
    # saved file looks exactly like a good one, and nothing downstream re-asks
    # the question. So the verdict now GATES the write instead of narrating it.
    #
    # --promote-anyway exists because "worse on this measurement" is not always
    # "worse" - the honest case is a deliberate retarget whose old numbers are
    # not comparable - but it has to be typed, by someone who has read why.
    if not (better_ap and better_op) and not args.promote_anyway:
        print()
        print("🛑 NOT SAVED. This head did not beat what it replaces, and a")
        print("   worse classifier installed by default is how the last two")
        print("   regressions shipped. Re-run with --promote-anyway if you have")
        print("   a reason this measurement does not apply.")
        return

    # 🚨 AN ABLATION MUST NEVER OVERWRITE THE DEPLOYED MODEL.
    # SPARROW_NO_CLIP_FEATURES exists to reproduce the old behaviour for
    # comparison. Running it wrote a 512-feature head over the 519-feature one
    # that had just been promoted, so production was silently serving the
    # WORSE model from a diagnostic. It was only caught because head.py checks
    # the feature width before scoring; without that guard the detector would
    # have run on a mismatched model producing confident nonsense.
    #
    # A run that is not the real configuration does not get to publish.
    if not _WITH_CLIP:
        print()
        print("⚠️  ablation run (SPARROW_NO_CLIP_FEATURES=1) - model NOT saved")
        return

    # Final model on everything, for deployment.
    fit_rows = np.where(_trainable(src))[0]
    # 🚨 FIT ON THE SCALED FEATURES, AND SAVE THE SCALER WITH THE WEIGHTS.
    # The folds above are evaluated with standardisation; a deployed head fitted
    # WITHOUT it is a different model from the one that was measured, and it
    # would fail silently - plausible scores, no error, numbers that no longer
    # mean what the report said. Whatever transform the evaluation used has to
    # travel with the weights.
    scaler = StandardScaler().fit(X[fit_rows])
    clf = LogisticRegression(max_iter=5000, C=args.C, class_weight="balanced")
    clf.fit(scaler.transform(X[fit_rows]), y[fit_rows])
    thrs = [recall_at(s)[1] for s in seed_scores]
    thrs = [x for x in thrs if x is not None]
    thr = float(np.median(thrs)) if thrs else None

    if args.no_save:
        print()
        print(f"--no-save: the live head was NOT touched "
              f"(would have been threshold {thr if thr else 'UNREACHABLE'}).")
        return

    out_path = Path(args.out) if args.out else MODEL_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out_path, w=clf.coef_, b=clf.intercept_,
             mean=scaler.mean_, scale=scaler.scale_,
             # Named so a consumer can check it is feeding the right features
             # in the right order rather than discovering a mismatch as bad
             # predictions.
             clip_classes=np.array(CLIP_CLASSES),
             with_clip_features=_WITH_CLIP,
             threshold=thr if thr is not None else 0.99,
             n_train=len(y), n_pos=int(y.sum()),
             n_cam_pos=n_pos_cam)
    print(f"saved {out_path}  (threshold {thr if thr else 'UNREACHABLE'})")
    print(f"\n⚠️  {n_pos_cam} camera-view positives, so recall moves in "
          f"{100 / n_pos_cam:.0f}-point steps.")
    print("    Read AP for DIRECTION and recall for MAGNITUDE - a single fold")
    print("    split reported 9/10 for this exact model, and forty runs put it")
    print("    at 69%. That is why this script always averages over seeds.")
    print("\n🚦 NOT WIRED INTO THE DETECTOR. classify.py still uses CLIP")
    print("    zero-shot. Promote only when the recall line above wins too.")


if __name__ == "__main__":
    main()
