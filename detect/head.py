"""The trained head, at inference time.

🚨 SINCE 2026-09-02 THIS HEAD ANSWERS "IS IT A POLICE UNIT", NOT "IS IT
GOVERNMENT". The function names below and `VehicleIdentifier.gov_call` still say
gov because renaming them across six callers is churn with a real chance of
missing one - but the QUESTION changed, and reading them as government-vs-not is
how a fire truck gets back into the review queue. See fit_local.POSITIVE for
why: buses, ambulances, fire trucks and municipal works trucks are all genuinely
government, so a government head fired on them correctly and they filled the pen
while the map published none of them (2,161 public sightings, all `police`,
zero `gov`).


This is the thing the operator's labelling has been building toward. Until now his
1,400+ judgements changed individual sightings and filled a training set, but
they did not change how the next vehicle was READ - `classify.py` ran CLIP
zero-shot and his corrections never reached it.

Measured before wiring (train/fit_local.py, 10 seeds, group-aware folds):

    CLIP zero-shot      AP 0.809   recall@95% precision 67%
    trained head        AP 0.828   recall@95% precision 76%     9/10 splits

## The feature vector, and why the order is not negotiable

    [ 512 CLIP image embedding, L2-normalised ]  ++
    [ police, emergency, gov_dot, fleet, civilian ]  ++  [ conf, margin ]

then standardised with the mean and scale saved alongside the weights.

Giving the head CLIP's own scores is what made it work: without them it was
being asked to rediscover from ~1,400 local crops what CLIP knows from 400
million images, and it lost. With them, "pass the police score straight
through" is inside its hypothesis space, so it can only add corrections.

🚨 EVERY PART OF THAT HAS TO MATCH TRAINING EXACTLY. A feature in the wrong
position, an unnormalised embedding, or a missing scaler does not raise - it
produces plausible numbers that mean nothing, which is the worst failure mode
this project has. So the width is checked, the class order is checked against
what the trainer saved, and anything unexpected disables the head and falls
back to zero-shot rather than guessing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import numpy as np

from core import DATA

MODEL = DATA / "models" / "vehicle_head.npz"

_STATE: dict = {"loaded": False, "ok": False, "why": ""}


#: How often to ask the filesystem whether the weights changed. The check is one
#: stat() and the caller is already about to run CLIP, so this is free in
#: context; it exists only so a hot loop does not stat on every single crop.
_RECHECK_S = 30.0


def _stale() -> bool:
    """Has the head on disk been replaced since we loaded it?

    🚨 THIS EXISTS BECAUSE A RETRAINED HEAD DID NOTHING UNTIL SOMEBODY
    REMEMBERED TO RESTART box_puller.

    `_STATE` was filled once and never revisited, so the weights a process used
    were whichever ones existed when it started. On 2026-09-02 two box_puller
    copies had been running since 25 August, both holding a head from the 18th -
    so retraining wrote a better classifier to disk and the queue went on being
    gated by a two-week-old one. Nothing was wrong with the model, the file, or
    the code that reads it. The fix simply was not RUNNING, which is the failure
    this project keeps meeting from a new direction each time.

    Restarting is the obvious answer and it is the fragile one: it depends on a
    human doing it at the right moment, every time, forever. Noticing is
    cheaper. The training run is the thing that knows something changed, and it
    announces it by writing the file - so read that.
    """
    try:
        st = MODEL.stat()
    except OSError:
        # The file has gone. Keep serving what is already loaded rather than
        # silently disabling the head mid-run; `available()` still reports.
        return False
    return (st.st_mtime_ns, st.st_size) != _STATE.get("stamp")


def _fresh() -> None:
    """Reload if the weights on disk have been replaced. Cheap and throttled."""
    now = time.monotonic()
    if now - _STATE.get("checked_at", -1e9) < _RECHECK_S:
        return
    _STATE["checked_at"] = now
    if _STATE.get("loaded") and _stale():
        was = _STATE.get("threshold")
        _load()
        print(f"[head] weights changed on disk - reloaded "
              f"(threshold {was} -> {_STATE.get('threshold')}, "
              f"ok={_STATE.get('ok')})")


def _load() -> None:
    _STATE["loaded"] = True
    _STATE["checked_at"] = time.monotonic()
    try:
        st = MODEL.stat()
        _STATE["stamp"] = (st.st_mtime_ns, st.st_size)
    except OSError:
        _STATE["stamp"] = None
    if not MODEL.exists():
        _STATE["why"] = f"no head at {MODEL}"
        return
    try:
        z = np.load(MODEL, allow_pickle=True)
        w = np.asarray(z["w"]).reshape(-1)
        b = float(np.asarray(z["b"]).reshape(-1)[0])
        if "mean" not in z or "scale" not in z:
            # A head saved before the scaler existed was FITTED on unscaled
            # features. Scoring it either way would be wrong, so refuse it.
            _STATE["why"] = "head predates the scaler; retrain before using it"
            return
        mean = np.asarray(z["mean"]).reshape(-1)
        scale = np.asarray(z["scale"]).reshape(-1)
        classes = [str(c) for c in np.asarray(z["clip_classes"]).reshape(-1)]
        if not (len(w) == len(mean) == len(scale)):
            _STATE["why"] = "weights and scaler disagree on width"
            return
        _STATE.update(w=w, b=b, mean=mean, scale=scale, classes=classes,
                      n_extra=len(classes) + 2,
                      threshold=float(z["threshold"]),
                      n_cam_pos=int(z["n_cam_pos"]) if "n_cam_pos" in z else 0,
                      ok=True, why="")
    except Exception as exc:
        _STATE["why"] = f"unreadable: {exc}"


def available() -> bool:
    if not _STATE["loaded"]:
        _load()
    else:
        _fresh()
    return bool(_STATE["ok"])


def status() -> dict:
    if not _STATE["loaded"]:
        _load()
    return {"ok": _STATE["ok"], "why": _STATE["why"],
            "threshold": _STATE.get("threshold"),
            "trained_on_camera_positives": _STATE.get("n_cam_pos")}


def score(embedding: np.ndarray, scores: dict,
          conf: float, margin: float) -> Optional[float]:
    """Probability this crop is a government vehicle, or None if unavailable.

    `embedding` must be the L2-normalised CLIP image feature - the same vector
    train/embed.py cached, produced by the same model. `scores` is the
    per-class softmax dict from VehicleIdentifier.classify.
    """
    if not available():
        return None
    try:
        emb = np.asarray(embedding, dtype=np.float64).reshape(-1)
        extra = np.array([float(scores.get(c, 0.0)) for c in _STATE["classes"]]
                         + [float(conf), float(margin)], dtype=np.float64)
        x = np.concatenate([emb, extra])
        if x.shape[0] != _STATE["w"].shape[0]:
            # Width mismatch means the model on disk was trained against a
            # different feature layout - a different CLIP backbone, or a head
            # from before the scores were added. Disable rather than score
            # garbage confidently.
            _STATE["ok"] = False
            _STATE["why"] = (f"feature width {x.shape[0]} != model "
                             f"{_STATE['w'].shape[0]}; head disabled")
            return None
        z = (x - _STATE["mean"]) / np.where(_STATE["scale"] == 0, 1.0,
                                            _STATE["scale"])
        return float(1.0 / (1.0 + np.exp(-(float(z @ _STATE["w"]) + _STATE["b"]))))
    except Exception:
        return None


def threshold() -> float:
    """The operating point measured by the trainer, not a chosen number."""
    if not available():
        return 1.0          # unreachable: a missing head must publish nothing
    return float(_STATE.get("threshold") or 1.0)
