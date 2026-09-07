"""Vehicle-sighting submission decisions.

This module deliberately covers only the decision boundary for vehicle
sightings. It is not a generic intake framework and does not handle other
reporting domains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import classify


@dataclass
class VehicleSightingDecision:
    """The classification and publication state for one vehicle submission."""

    plate: str
    plate_confidence: float
    evidence: dict[str, Any]
    classification: dict[str, Any]
    tierable: bool
    sightable: bool
    tier: str
    candidate: bool
    reviewed: str | None
    decided_by: str | None


def normalize_plate_claim(
    plate_text: Any,
    plate_confidence: Any,
    min_plate_confidence: Any,
) -> tuple[str, float]:
    """Drop a weak plate claim without dropping the vehicle sighting."""
    # ⚠️ THE PLATE CONFIDENCE GATE ONLY APPLIES TO PLATES.
    #
    # It used to reject every submission scoring under the threshold,
    # including ones carrying no plate at all - which silently discarded
    # exactly the sightings the visual identifier exists to produce. A
    # camera that cannot read plates would have been gated out by a plate
    # rule. Drop a WEAK plate; never drop the whole sighting for not
    # having one.
    confidence = float(plate_confidence or 0)
    plate = plate_text or ""
    if plate and confidence < float(min_plate_confidence):
        plate, confidence = "", 0.0
    return plate, confidence


def sanitize_submission_evidence(submitted_evidence: Any) -> dict[str, Any]:
    """Remove confirmation signals a vehicle-sighting submitter cannot assert."""
    # A submitter cannot hand themselves signals that are supposed to be
    # EARNED, not asserted:
    #   human_confirmed - the reviewer's judgement; only the operator tool
    #     sets it, or anybody could tag a neighbour's car and publish it.
    #   visual_police   - the trained head's verdict. It carries weight 0.0,
    #     so asserting it as a boolean adds no confidence but fabricates a
    #     second "visual" marker for the two-marker police rule, storing a
    #     stranger's plate on one real cue. It may only arise inside the
    #     gated head block from visual_police_conf/margin, which the node
    #     computes and this server re-derives.
    evidence = dict(submitted_evidence or {})
    evidence.pop("human_confirmed", None)
    evidence.pop("visual_police", None)
    return evidence


def apply_confirmed_human_evidence(
    evidence: dict[str, Any],
    operator_confirmed: bool,
) -> None:
    """Restore the human signal only from the trusted internal capability."""
    # 🚨 THE OPERATOR'S CONFIRMATION, RESTORED AFTER THE STRIP AND NEVER
    # BEFORE IT. The strip above is right: `human_confirmed` is the single
    # strongest signal classify.py has (weight 4.0, and it WAIVES the
    # two-marker police rule), so a submitter who could assert it could
    # publish a neighbour's car. It is set here instead, from a flag this
    # server derived by checking a node token against the node that owns
    # the crop - never from anything the caller typed.
    #
    # 📌 WHY THIS PATH HAS TO EXIST AT ALL. The posting gate runs at the
    # moment a vehicle leaves frame, and it drops passes that clear no two
    # markers. That is correct and it is also why a confirmed patrol car
    # could never reach the map: a real one was scored by the trained head
    # at 0.987, failed the gate because the plate read disagreed (0.34 <
    # 0.55) and no second marker fired, and was discarded. The operator then
    # pressed "Yes - government" on the crop and nothing happened, because
    # there was no sighting to promote. The human arrived AFTER the gate.
    # classify.py has always known how to weigh a human; it simply never got
    # told, because the row it would have gone on was never created.
    if operator_confirmed:
        evidence["human_confirmed"] = True


def classify_vehicle_submission(evidence: dict[str, Any]) -> dict[str, Any]:
    """Classify a sanitized vehicle-sighting evidence claim."""
    return classify.classify(evidence)


def derive_vehicle_publication_state(
    classification: dict[str, Any],
    source: str,
    node_id: Any,
    operator_confirmed: bool,
) -> tuple[bool, bool, str, bool, str | None, str | None]:
    """Derive the vehicle's hold/publication state from classifier output."""
    # A public SIGHTING and a published PLATE are different decisions.
    # `sightable` puts "a marked patrol unit was here" on the public map -
    # no identifier, so nothing to protect. `tierable` is what allows the
    # plate TEXT through, and it keeps the strict bar. Gating both on
    # `tierable` meant a plate-blind camera could never contribute
    # anything, which is most cameras. See classify.py rule 4.
    # 🚨 A HUMAN SUBMISSION IS A CLAIM, NOT A RECORD (his call: nothing
    # auto-publishes without the trained head first).
    #
    # This used to reach the public tier directly on the argument that a
    # person looking at a marked patrol car beats any classifier. True of an
    # honest person, and that is the whole problem: the submitter chooses
    # the markers, so "two distinct visual markers" is two taps, and the map
    # would publish whatever a stranger asserted about a vehicle they picked.
    # Every other route to the public tier is gated by a model that cannot
    # be argued with. This one was gated by the submitter's own honesty.
    #
    # So it is recorded PRIVATE and routed to the pen, where the trained head
    # scores its crop and a human confirms it. Nothing is thrown away and
    # nothing is distrusted - the claim simply has to survive the same gate
    # everything else survives before it names a vehicle in public.
    #
    # ⚠️ THIS MUST HAPPEN BEFORE `tier` IS COMPUTED. Clearing the flags after
    # the tier line reads as a fix, changes the reason string, and publishes
    # exactly as before - the failure this codebase keeps producing: a check
    # that runs and is not applied to the thing it governs.
    if source == "phone":
        classification["why"] = (
            f"human-submitted by {node_id}, awaiting review; "
            + classification["why"]
        )
        classification["tierable"] = False
        classification["sightable"] = False

    tierable = classification["tierable"]
    sightable = classification["sightable"]
    tier = "public" if (tierable or sightable) else "private"

    # 🚨 THE PUBLIC TIER IS ENTERED BY A PERSON, NEVER BY INGEST.
    # 33 of the 34 sightings ever auto-published came through here: the
    # classifier judged a submission sightable and the row went public with
    # nobody having looked at it. That is the claim the project is now
    # making publicly - that a human decides what appears on the map - and a
    # claim has to be true in the code, not merely usual in practice.
    #
    # The classification is NOT discarded. `classification` still carries
    # vclass and the reason, the crop is still parked in the review pen by the
    # caller, and a reviewer promotes it with one press. All that changes is
    # that the default is private and the publish step needs a person.
    #
    # ⚠️ This is deliberately AFTER `tier` is computed, so the classifier's
    # own opinion is still what routes the crop to the pen. Clearing the
    # flags earlier would have made every candidate invisible instead of
    # merely unpublished - the difference between "waiting for review" and
    # "silently dropped".
    # ⚠️ REMEMBER THAT THIS WAS A CANDIDATE. Downstream code needs to know
    # "the classifier would have published this" AFTER the tier has been
    # rewritten to private, and the tier can no longer answer that. Two
    # separate behaviours were silently switched off by reading `tier`
    # here: fragment merging, and the pen write itself.
    candidate = tier == "public"
    reviewed = None
    decided_by = None
    if candidate and not operator_confirmed:
        classification["why"] = (
            (classification.get("why") or "") + " - held for human review"
        )
        tier = "private"
    elif candidate:
        classification["why"] = (
            (classification.get("why") or "")
            + " - confirmed by the camera operator"
        )
        # 🚨 RECORD THE DECISION. A public row with reviewed IS NULL is
        # indistinguishable from one that reached the map unreviewed, which
        # is the single claim this project makes about itself - "nothing is
        # published without a person". The audit checks for exactly this
        # ("public tier with no human decision"), and it would have started
        # counting these.
        reviewed = "confirmed"
        decided_by = "human"

    return tierable, sightable, tier, candidate, reviewed, decided_by


def decide_vehicle_sighting(
    plate_text: Any,
    plate_confidence: Any,
    submitted_evidence: Any,
    source: str,
    node_id: Any,
    operator_confirmed: bool,
    min_plate_confidence: Any,
) -> VehicleSightingDecision:
    """Apply all vehicle-sighting-only classification and publication decisions."""
    plate, confidence = normalize_plate_claim(
        plate_text, plate_confidence, min_plate_confidence
    )
    evidence = sanitize_submission_evidence(submitted_evidence)
    apply_confirmed_human_evidence(evidence, operator_confirmed)
    classification = classify_vehicle_submission(evidence)
    tierable, sightable, tier, candidate, reviewed, decided_by = (
        derive_vehicle_publication_state(
            classification, source, node_id, operator_confirmed
        )
    )
    return VehicleSightingDecision(
        plate=plate,
        plate_confidence=confidence,
        evidence=evidence,
        classification=classification,
        tierable=tierable,
        sightable=sightable,
        tier=tier,
        candidate=candidate,
        reviewed=reviewed,
        decided_by=decided_by,
    )
