"""Vehicle-sighting media preparation and snapshot storage.

This module deliberately handles media only for vehicle sightings. It is not a
generic reporting-media framework.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mirror
import snapshot
from core import now


class SnapshotRejected(Exception):
    """A submitted snapshot must reject the vehicle-sighting request."""


@dataclass
class VehicleMediaResult:
    """Prepared media state for post-insert vehicle-sighting side effects."""

    relay_crop: bytes | None
    review_crop: bytes | None
    evidence_crop: bytes | None
    dropped_image: str | None
    banked_stem: str | None


def prepare_phone_node_relay(event: dict[str, Any], source: str) -> bytes | None:
    """Prepare the subresolution relay crop before a mirror strips the image."""
    # A public mirror cannot score a phone crop (no GPU, no trained head),
    # so it parks a plate-less copy for the home classifier to pull. Captured
    # HERE, before the mirror drops the image below, and written to the inbox
    # after the row exists so it can be keyed by the sighting id. The size is
    # re-verified (subresolution_bytes), so an oversized crop is refused, not
    # quarantined. See mirror.quarantine_write.
    if source == "phone_node" and event.get("snap_b64") and mirror.relay_enabled():
        try:
            return snapshot.subresolution_bytes(event["snap_b64"])
        except Exception:
            return None
    return None


def prepare_vehicle_review_media(
    event: dict[str, Any],
    source: str,
    classification: dict[str, Any],
) -> tuple[bytes | None, bytes | None]:
    """Prepare candidate review/evidence crops before mirror image policy runs."""
    # A camera node scores its own crop, so its GOVERNMENT candidates go
    # straight to the review pen for a human to confirm - captured here as a
    # sub-resolution, plate-less crop BEFORE the mirror strips the image
    # below. Phone-node crops take the inbox path instead (box_puller pulls
    # and scores them at home first), so they are excluded here.
    # `phone` (a human submission) is included here now that it no longer
    # reaches the public tier by itself: the pen is where its claim gets
    # looked at. `phone_node` is still excluded because it has its own route
    # - the inbox, which box_puller pulls and scores at home.
    if (source != "phone_node" and mirror.relay_enabled()
            and event.get("snap_b64")
            and classification["vclass"] in ("police", "gov_dot")):
        try:
            # 🚨 CROP TO THE VEHICLE FIRST. A camera node posts its whole
            # FRAME (store_submitted crops it server-side), so merely
            # downscaling it parked a 200px photograph of the street - and
            # the neighbours' houses with it - in front of every reviewer.
            vehicle_box = event.get("vehicle_box")
            if vehicle_box:
                review_crop = snapshot.crop_to_subres(
                    event["snap_b64"], tuple(vehicle_box)
                )
                # And the same crop WITHOUT the 200px shrink, for the
                # reviewer and for whatever gets published if they say yes.
                # Built here because this is the last point the original
                # frame is still in hand - below, the mirror strips the
                # image and the redaction path rewrites it. Failing to
                # produce it must never cost the pen its card, so the pen
                # crop above is computed first and this cannot unset it.
                try:
                    evidence_crop = snapshot.crop_full(
                        event["snap_b64"], tuple(vehicle_box)
                    )
                except Exception:
                    evidence_crop = None
                return review_crop, evidence_crop

            # No box means nothing to crop to. Park no picture rather than a
            # bystander's - the same call the snapshot path already makes
            # below. The candidate still reaches the reviewer, without an image.
            return None, None
        except Exception:
            return None, None
    return None, None


def store_vehicle_submission_snapshot(
    event: dict[str, Any],
    node: dict[str, Any],
    node_id: str,
    plate: str,
    source: str,
    classification: dict[str, Any],
    tier: str,
    candidate: bool,
) -> tuple[str | None, str | None]:
    """Apply mirror policy and preserve the existing source-specific storage."""
    dropped_image = None
    banked_stem = None
    if event.get("snap_b64") and not mirror.may_store_image(tier):
        # Nothing to redact, nothing to leak, nothing to subpoena. A
        # mirror keeps photographs of published government vehicles only.
        event.pop("snap_b64", None)
        dropped_image = "public mirror keeps no private-tier imagery"
    if event.get("snap_b64") and not event.get("snap"):
        plate_box = event.get("plate_box")
        plate_boxes = event.get("plate_boxes") or (
            [plate_box] if plate_box else []
        )
        vehicle_box = event.get("vehicle_box")
        metadata = {
            "ts": float(event.get("ts") or now()),
            "node_id": node_id,
            "node_name": node["name"],
            "tier": tier,
            "plate_text": plate,
            "vclass": classification["vclass"],
            "watermark": "UNVERIFIED" if source == "phone" else "",
        }
        if source == "phone_node" and not plate_box:
            # A phone node cannot locate a plate to redact, so it destroys
            # it instead: the crop arrives already below plate legibility.
            # store_subresolution MEASURES that rather than believing it.
            try:
                event["snap"] = snapshot.store_subresolution(
                    event["snap_b64"], metadata
                )
                # And keep a copy for labelling. This is the entire reason
                # phone nodes are worth building: every window someone puts
                # a camera in is real vehicles in real conditions, which is
                # what the classifier has been starving for.
                #
                # 🚨 BUT A MIRROR MUST NEVER BANK. mirror.may_bank() existed
                # for exactly this and was never called, so a public mirror
                # was writing the ORIGINAL full-resolution crop to disk - the
                # un-degraded image, the thing THREAT_MODEL promises a breach
                # cannot yield. Labelling happens where the camera is; the
                # mirror carries claims, not photographs of the street.
                if mirror.may_bank():
                    from detect import bank as bank
                    banked_stem = bank.bank_remote(
                        snapshot.decode_bytes(event["snap_b64"]), node_id,
                        {
                            "ts": float(event.get("ts") or now()),
                            "cls_name": event.get("body") or "car",
                            "det_conf": event.get("det_conf"),
                        },
                    )
            except ValueError as exc:
                dropped_image = str(exc)
            except Exception as exc:
                raise SnapshotRejected(str(exc)) from exc
        elif tier != "public" and not plate_box and not candidate:
            # We cannot redact a plate we cannot locate, and a photograph of
            # a car IS a photograph of its plate. So a private-tier image
            # with no plate box is discarded rather than stored. The
            # sighting itself still counts; only the picture is dropped.
            #
            # ⚠️ `not candidate` IS THE THIRD BEHAVIOUR THIS FILE LOST BY
            # READING `tier` AFTER IT WAS FORCED TO PRIVATE. The two named
            # above tier's rewrite are fragment merging and the pen write;
            # this is the same mistake with the worst outcome. A marked
            # patrol car whose plate the camera could not resolve - which is
            # MOST of them, at 22px against the 60 an OCR needs - hit this
            # branch and had its photograph thrown away for failing to
            # locate a plate that was never going to be legible. The
            # candidate's original is kept in core.EVIDENCE below instead,
            # where the reviewer can actually see the livery.
            dropped_image = "no plate box to redact on a private-tier image"
        elif source == "phone":
            # A human aimed the camera; their framing IS the crop.
            try:
                event["snap"] = snapshot.store_prepared(
                    event["snap_b64"], metadata,
                    plate_box=tuple(plate_box) if plate_box else None,
                )
            except Exception as exc:
                raise SnapshotRejected(str(exc)) from exc
        elif not vehicle_box:
            # 🚨 A CAMERA NODE MUST SEND THE BOX IT DETECTED.
            # Without one there is nothing to crop to, and the previous
            # behaviour - fall through to the phone path - stored the whole
            # street: the neighbours' houses and other vehicles' plates,
            # none of them redacted. Drop the picture instead. The sighting
            # still counts; an un-croppable image is not worth a bystander.
            dropped_image = "camera submission carried no vehicle_box to crop to"
        else:
            try:
                event["snap"] = snapshot.store_submitted(
                    event["snap_b64"], metadata, tuple(vehicle_box),
                    plate_box=tuple(plate_box) if plate_box else None,
                    plate_boxes=[tuple(box) for box in plate_boxes],
                )
            except Exception as exc:
                raise SnapshotRejected(str(exc)) from exc
    return dropped_image, banked_stem


def prepare_vehicle_media(
    event: dict[str, Any],
    node: dict[str, Any],
    node_id: str,
    plate: str,
    source: str,
    classification: dict[str, Any],
    tier: str,
    candidate: bool,
) -> VehicleMediaResult:
    """Prepare review/relay media, then apply snapshot storage policy."""
    relay_crop = prepare_phone_node_relay(event, source)
    review_crop, evidence_crop = prepare_vehicle_review_media(
        event, source, classification
    )
    dropped_image, banked_stem = store_vehicle_submission_snapshot(
        event, node, node_id, plate, source, classification, tier, candidate
    )
    return VehicleMediaResult(
        relay_crop, review_crop, evidence_crop, dropped_image, banked_stem
    )
