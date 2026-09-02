"""Identify police / government / emergency vehicles BY SIGHT, no plate needed.

WHY THIS IS THE RIGHT TARGET FOR THE PUBLIC TIER
The project treated the licence plate as the thing a node must resolve. For
civilians that is correct - the plate is the identifier, and it is hashed. But
for the PUBLIC tier the valuable output is "a marked patrol unit was at this
corner at 14:32", and that sentence contains no plate.

That matters because the two tasks have wildly different pixel budgets. At
the reference camera (143 px per metre) a plate is 22 px against the 60 needed for
OCR, while a roof light bar is 186 px against the 20 needed to recognise a
shape. Identifying a patrol vehicle visually has roughly an order of magnitude
more headroom than reading its plate, because the features are 4-6x larger AND
the task needs 3x fewer pixels per millimetre.

Consequence for the network: a volunteer with an ordinary porch camera can
contribute police sightings on day one, even though their camera will never
read a plate. That is the difference between a project that needs everyone to
buy telephoto lenses and one that runs on what people already own.

WHAT THIS COSTS
Without a plate you cannot follow a SPECIFIC cruiser across cameras - you get
"a patrol unit was here", not "unit 4521 patrolled these streets". Weaker, but
it is the accountability layer that actually works at scale, and it is strictly
MORE privacy-preserving: no identifier is stored for anyone.

ZERO-SHOT FIRST, ON PURPOSE
CLIP classifies against text prompts with no training data, so the capability
exists today and, more usefully, it can LABEL the crops the nodes are already
collecting. Those labels then train a small CNN that runs on a Raspberry Pi.
Zero-shot is the bootstrap, not the destination.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Prompt sets. Several phrasings per class, averaged: CLIP is sensitive to
# wording, and a single prompt makes the classifier a hostage to one sentence.
CLASSES = {
    "police": [
        "a police car with a light bar on the roof",
        "a marked police patrol vehicle",
        "a sheriff patrol SUV with police markings",
        "a state trooper car with emergency lights",
        "a police cruiser with door decals",
    ],
    "emergency": [
        "a fire truck",
        "an ambulance",
        "an emergency response vehicle with flashing lights",
    ],
    "gov_dot": [
        "a department of transportation truck with an amber beacon",
        "a municipal works truck with warning lights",
        "a government utility vehicle with high visibility markings",
        "a snow plow or road maintenance truck",
    ],
    "fleet": [
        "a delivery van with company livery",
        "a commercial truck with a company logo on the side",
        "a utility company service van",
    ],
    "civilian": [
        "an ordinary private car",
        "a family SUV",
        "a pickup truck with no markings",
        "a parked passenger vehicle",
        "an ordinary car with no lights or markings on the roof",
    ],
}

# Only these ever justify the public tier. 'civilian' is the safe default.
PUBLIC_CLASSES = {"police", "emergency", "gov_dot", "fleet"}


class VehicleIdentifier:
    """CLIP zero-shot vehicle classifier. Loads lazily; the model is ~600 MB."""

    def __init__(self, model_name: str = "openai/clip-vit-base-patch32",
                 device: Optional[str] = None):
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = CLIPModel.from_pretrained(model_name).to(self.device).eval()
        self.proc = CLIPProcessor.from_pretrained(model_name)

        self.labels, self.prompts = [], []
        for cls, ps in CLASSES.items():
            self.labels += [cls] * len(ps)
            self.prompts += ps

        with torch.no_grad():
            tok = self.proc(text=self.prompts, return_tensors="pt",
                            padding=True).to(self.device)
            t = self.model.get_text_features(**tok)
            self.text = t / t.norm(dim=-1, keepdim=True)

    def classify(self, bgr: np.ndarray) -> dict:
        """Return {vclass, conf, margin, scores}. `margin` is the gap to the
        runner-up, which is a better gate than the top score on its own: CLIP
        is confidently wrong far more often than it is uncertain."""
        import cv2
        from PIL import Image

        img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        with self.torch.no_grad():
            inp = self.proc(images=img, return_tensors="pt").to(self.device)
            f = self.model.get_image_features(**inp)
            f = f / f.norm(dim=-1, keepdim=True)
            sims = (f @ self.text.T).squeeze(0).float().cpu().numpy()

        # Average the prompts belonging to each class, then softmax over classes.
        per_class = {}
        for cls in CLASSES:
            idx = [i for i, l in enumerate(self.labels) if l == cls]
            per_class[cls] = float(np.mean(sims[idx]))
        keys = list(per_class)
        vals = np.array([per_class[k] for k in keys])
        e = np.exp((vals - vals.max()) / 0.01)      # CLIP temperature
        probs = e / e.sum()

        order = np.argsort(-probs)
        top, second = keys[order[0]], keys[order[1]]
        return {
            "vclass": top,
            "conf": float(probs[order[0]]),
            "margin": float(probs[order[0]] - probs[order[1]]),
            "runner_up": second,
            "scores": {k: round(float(p), 4) for k, p in zip(keys, probs)},
            # The embedding the head needs, kept rather than discarded. It was
            # already computed here and thrown away, so the trained head had no
            # way to see the picture at all.
            #
            # ⚠️ THE HEAD MUST BE FED THE *ROUNDED* `scores` ABOVE, NOT FULL
            # PRECISION. The trainer reads its features out of the banked
            # sidecars, and those store scores at 4dp - so 4dp is what the head
            # was fitted on. Handing it exact values at inference would be a
            # train/serve mismatch: tiny here, invisible always, and exactly
            # the class of bug that produces plausible numbers meaning nothing.
            # Parity with training beats precision.
            "_embedding": f.squeeze(0).float().cpu().numpy(),
        }

    # ✅ MEASURED 2026-08-08 ON REAL GROUND TRUTH, not chosen to look safe.
    #
    # the operator photographed the police station and the crops were labelled by
    # hand, giving the project its first confirmed positives. Against them:
    #
    #   16 police / 9 civilian, hand-labelled
    #     true police confidences   0.77 0.89 0.92 0.97 0.98 0.99 x4 1.00 x5
    #     civilians called police   0.60  0.83  0.85     <- all below 0.90
    #
    #   335 crops of ordinary traffic from his own camera
    #     CLIP's raw argmax        13.1% called police   (its prior talking)
    #     at conf>=0.90 margin>=0.70  0.30%  - ONE crop, and that one is a
    #     genuinely marked municipal patrol unit at 0.997/0.995.
    #
    # So 0.90 sits in a real gap: above every false positive in the labelled
    # set (max 0.85) and below every true positive that matters, and on live
    # traffic it admits one vehicle in 335 - which is the right order for how
    # often a patrol car actually passes a residential street.
    #
    # MARGIN IS STILL THE IMPORTANT ONE. CLIP is confidently wrong far more
    # often than it is uncertain, so "how far ahead is the winner" separates
    # better than "how sure is the winner". Both must pass.
    #
    # 🔁 RAISED AGAIN 2026-08-08 EVENING, and this time from the RIGHT data.
    #
    # 0.90 came from handheld photographs, where the worst false positive
    # scored 0.85. It did not survive contact with the street: a black Pontiac
    # Solstice half-overlapping a grey Jeep published at 0.938, and the operator
    # retracted it. His verdicts on published claims are camera-view labels -
    # the actual distribution this has to work in - and they separate cleanly:
    #
    #   confirmed patrol units   0.997  0.996  0.994  0.975
    #   retracted (the Solstice) 0.938
    #
    # 0.96 sits in that gap. ⚠️ It is FIVE data points. The gap is real and it
    # is narrow, and the previous threshold looked just as safe right up until
    # it was exceeded - so treat this as the last adjustment of this kind that
    # is defensible, not as a solved problem. The margin between the worst
    # false positive and the worst true positive is under 0.04, which is not a
    # comfortable operating point for a zero-shot model.
    #
    # ➡️ The real answer is the trained head (train/README.md), not another
    # constant. Every retraction now adds a camera-view label, so the dataset
    # that replaces this number is being built by using the thing.
    MIN_CONF, MIN_MARGIN = 0.96, 0.90

    @staticmethod
    def gov_call(r: dict) -> dict:
        """Does this crop read as a POLICE UNIT, and how strongly.

        ⚠️ THE NAME IS HISTORICAL. It answered "is it government" until
        2026-09-02, when the head was refitted police-only (see
        train/fit_local.POSITIVE and detect/head.py). The `gov` key in the
        returned dict means "publishable police unit" now. The zero-shot
        FALLBACK below still admits gov_dot and emergency, and that is
        deliberate: it only runs when there is no head at all, where being
        broader is the safer failure - but it is not what decides anything on a
        machine that has the weights.


        🚨 ONE IMPLEMENTATION, THREE CONSUMERS. The published tier, the live
        overlay on the camera page, and the confirm popup each used to work
        this out for themselves against CLIP's 0.96 gate. The moment the
        trained head started deciding what gets published, those two others
        were answering a different question with a different model - the
        overlay would light up on vehicles the map would not publish, and the
        popup would ask about the wrong cars.

        Same lesson as `core.is_operator_addr`: a rule copied into three files
        gets fixed in one of them.

        Returns {gov, conf, source}. `conf` is on the scale of whichever
        decided, so compare it only against `threshold` from the same dict.
        """
        from detect import head as _head
        hp = _head.score(r.get("_embedding"), r.get("scores") or {},
                         r.get("conf") or 0.0, r.get("margin") or 0.0)
        if hp is not None:
            thr = _head.threshold()
            return {"gov": hp >= thr, "conf": hp, "threshold": thr,
                    "source": "head"}
        return {"gov": (r.get("vclass") in ("police", "gov_dot", "emergency")
                        and (r.get("conf") or 0) >= VehicleIdentifier.MIN_CONF
                        and (r.get("margin") or 0) >= VehicleIdentifier.MIN_MARGIN),
                "conf": r.get("conf") or 0.0,
                "threshold": VehicleIdentifier.MIN_CONF, "source": "zero-shot"}

    def evidence(self, bgr: np.ndarray, min_conf: float = MIN_CONF,
                 min_margin: float = MIN_MARGIN) -> dict:
        """Map a classification onto classify.py's evidence keys.

        Deliberately conservative and deliberately NOT sufficient on its own:
        it emits `police_body`, a weak signal, rather than anything decisive.
        classify.py still requires corroboration and a high combined score
        before a plate is published, and that rule is what keeps a confidently
        wrong CLIP call from making a stranger public.
        """
        r = self.classify(bgr)
        ev: dict = {"_visual": r}

        # ---- the trained head, when there is one --------------------------
        # This is the point of the whole labelling exercise: his corrections
        # decide how the next vehicle is read, instead of only moving the one
        # he was looking at. Measured against zero-shot on group-aware folds:
        # AP 0.828 vs 0.809, recall 76% vs 67%, winning 9 of 10 splits.
        #
        # Fed the ROUNDED scores on purpose - see classify() - so the features
        # match what the trainer fitted on. Falls back to zero-shot silently
        # only when the head is absent; a head that is present but MISMATCHED
        # disables itself loudly inside head.score rather than guessing.
        call = self.gov_call(r)
        if call["source"] == "head":
            hp = call["conf"]
            r["head_conf"] = hp
            ev["_visual"] = r
            if call["gov"]:
                # Graded, like the zero-shot path: classify.py scales its
                # weight by the value rather than treating it as a boolean.
                ev["visual_police_conf"] = hp
                ev["visual_police_margin"] = hp
                ev["visual_source"] = "head"
                # classify.py needs the operating point to scale the weight;
                # hard-coding it there would let the two drift apart the first
                # time the head is retrained.
                ev["visual_head_threshold"] = call["threshold"]
            return ev

        if r["conf"] < min_conf or r["margin"] < min_margin:
            return ev
        if r["vclass"] == "police":
            # GRADED, not boolean. Passing a 0.99 identification as a plain
            # True and letting classify.py apply a fixed weight discards the
            # classifier's certainty - which it did, mapping it onto the
            # weakest signal in the table and publishing nothing.
            ev["visual_police_conf"] = r["conf"]
            # ⚠️ THE MARGIN HAS TO TRAVEL WITH IT.
            # classify.py gates on both, and this only ever emitted the
            # confidence - so the margin read as 0.0 there and the gate could
            # never open. Recall measured 0/16 on a set of obvious patrol cars,
            # which is how it was caught. A gate whose input is never supplied
            # is a gate that is always shut, silently.
            ev["visual_police_margin"] = r["margin"]
        elif r["vclass"] in ("gov_dot", "emergency"):
            ev["agency_decal"] = True
        elif r["vclass"] == "fleet":
            ev["fleet_decal"] = True
        return ev


_SINGLETON: Optional[VehicleIdentifier] = None


def get() -> VehicleIdentifier:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = VehicleIdentifier()
    return _SINGLETON
