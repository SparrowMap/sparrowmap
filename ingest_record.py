"""Vehicle-sighting record construction.

This module deliberately constructs records only for vehicle sightings. It is
not a generic reporting or persistence framework.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import nodes as node_mod
import privacy
from core import now


@dataclass
class ResolvedIngestTimestamp:
    """The accepted record timestamp and the original node-clock difference."""

    timestamp: float
    skew: float


@dataclass
class VehicleSightingRecord:
    """A not-yet-persisted vehicle sighting and its safe public coordinates."""

    record: dict[str, Any]
    latitude: float
    longitude: float


def resolve_ingest_timestamp(claimed_timestamp: Any) -> ResolvedIngestTimestamp:
    """Accept a node time within 120 seconds; otherwise use server time."""
    # A stale claimed timestamp can leave a new sighting never drawn, because
    # /api/sightings defaults to since = now() - 3600. Every counter reports
    # success and the dot is simply not on the map.
    # A back-dated row is also what the retention sweep deletes first, so a
    # skewed clock can quietly feed evidence to the janitor.
    #
    # The node's claim is kept in the response rather than thrown away: the
    # camera is the only party that can fix its own clock, and it cannot fix
    # what it is never told. `clock_skew_s` is what a node should log loudly.
    claimed = float(claimed_timestamp or now())
    server_now = now()
    skew = claimed - server_now
    # A little slack for network delay and honest drift; beyond that the
    # SERVER's clock wins, because it is the one every reader compares
    # against.
    timestamp = claimed if abs(skew) <= 120 else server_now
    return ResolvedIngestTimestamp(timestamp, skew)


def build_vehicle_sighting_record(
    event: dict[str, Any],
    node: dict[str, Any],
    node_id: str,
    plate: str,
    plate_confidence: float,
    classification: dict[str, Any],
    tier: str,
    signature_verified: bool,
    timestamp: float,
) -> VehicleSightingRecord:
    """Build the privacy-safe, not-yet-persisted record for one vehicle."""
    # ⚠️ NEVER node["lat"] / node["lon"] HERE. Those are the camera's TRUE
    # coordinates, and /api/sightings serves whatever is stored to anyone.
    # Storing them defeated the node-position jitter entirely - one
    # sighting gave up the exact camera location. See
    # nodes.sighting_position.
    #
    # The seed makes the position stable for this sighting and different
    # from the next one, so passes spread along the watched stretch instead
    # of stacking 31 dots on one pixel.
    latitude, longitude = node_mod.sighting_position(
        node, event.get("lat"), event.get("lon"),
        seed=f"{node_id}:{timestamp:.3f}:{event.get('snap_sha256') or plate or ''}",
    )

    # An empty string is not "no plate", it is a value - and it was being
    # counted as a distinct vehicle. Store the absence as an absence.
    plate_hash = privacy.plate_hash(plate, event.get("plate_state", "")) or None

    record = {
        "node_id": node_id, "ts": timestamp,
        "lat": latitude, "lon": longitude,
        "tier": tier,
        "plate_hash": plate_hash,
        # Plate text rides on `tierable` alone, NOT on the tier. A public
        # SIGHTING is public because it carries no identifier; attaching an
        # unverified plate to it would smuggle the identifier back in
        # through the very row that was supposed to be identifier-free.
        # Stored for a PUBLIC-tier row, served only after a human confirms
        # it - see privacy.redact. The photograph on a public row already
        # shows the plate, so keeping the text alongside adds no exposure
        # that the image did not; what it adds is SEARCH, and search waits
        # for a person. A retraction purges both (db.review_sighting).
        # 🚨 PLATE TEXT RIDES ON `tierable` ALONE. The old `or tier=="public"`
        # attached a plate to any public row - including a `sightable`-only
        # dot, which is public precisely BECAUSE it carries no identifier.
        # That smuggled the identifier back into the row that was supposed to
        # be identifier-free, the exact thing the comment below warns of.
        "plate_text": plate if classification["tierable"] else None,
        "plate_state": event.get("plate_state") if classification["tierable"] else None,
        "plate_conf": plate_confidence,
        "vclass": classification["vclass"],
        "vclass_conf": classification["conf"],
        "vclass_why": classification["why"],
        "color": event.get("color"), "body": event.get("body"),
        "make": event.get("make"), "model": event.get("model"),
        "heading": event.get("heading"), "speed_mph": event.get("speed_mph"),
        "snap": event.get("snap"), "source": event.get("source", "camera"),
        "reviewed": event.get("_reviewed"),
        "decided_by": event.get("_decided_by"),
        "bank_ref": event.get("bank_ref") or None,
        "sig_ok": 1 if signature_verified else 0,
    }
    return VehicleSightingRecord(record, latitude, longitude)


def build_ingest_response_metadata(
    sighting_id: int,
    tier: str,
    classification: dict[str, Any],
    parked: bool,
    skew: float,
    dropped_image: str | None,
) -> dict[str, Any]:
    """Build the existing success response before HTTP serialization."""
    response = {
        "id": sighting_id,
        "tier": tier,
        "vclass": classification["vclass"],
        "why": classification["why"],
        "parked": parked,
    }
    if abs(skew) > 120:
        # Said plainly, because the node cannot see this any other way and
        # the consequence - its sightings landing outside every default
        # time window - is invisible from its side.
        response["clock_skew_s"] = round(skew, 1)
        response["note"] = (
            f"your clock is {abs(skew):.0f}s "
            f"{'ahead of' if skew > 0 else 'behind'} the hub; "
            f"the server time was used instead"
        )
    if dropped_image:
        response["image_dropped"] = dropped_image
    return response
