"""Vehicle-sighting persistence and post-insert orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import db
import mirror


@dataclass
class VehiclePersistenceResult:
    """The inserted or merged vehicle-sighting persistence outcome."""

    record: dict[str, Any] | None
    merged_into: int | None


def find_vehicle_fragment(
    node_id: str, classification: dict[str, Any], timestamp: float, candidate: bool,
    store: Any = None,
) -> dict[str, Any] | None:
    """Find a mergeable fragment only for a government candidate."""
    # A tracker that loses a vehicle behind a window pillar and re-acquires it
    # produces several completed tracks for one pass. Candidate, rather than
    # final tier, preserves pre-hold merge and review behavior; merging ordinary
    # traffic by class and time window would be far too blunt.
    store = db if store is None else store
    return (
        store.merge_window_row(node_id, classification["vclass"], timestamp)
        if candidate else None
    )


def merge_vehicle_fragment(
    prior: dict[str, Any], node_id: str, classification: dict[str, Any],
    timestamp: float, store: Any = None,
) -> VehiclePersistenceResult:
    """Apply the existing merge-side detection and liveness updates."""
    store = db if store is None else store
    store.bump_detections(prior["id"], timestamp, classification["conf"])
    # Liveness is a server observation. Passing the node's own timestamp made
    # fast and slow clocks disagree with /api/heartbeat about the same node.
    store.heartbeat(node_id)
    return VehiclePersistenceResult(None, prior["id"])


def insert_vehicle_sighting(record: dict[str, Any], store: Any = None) -> dict[str, Any]:
    """Reduce mirror data before inserting the vehicle sighting."""
    # Reduction must precede persistence: read-time redaction leaves private
    # data on the disk, and the disk is what gets copied.
    store = db if store is None else store
    reduced = mirror.strip_sighting(record)
    reduced["id"] = store.insert_sighting(reduced)
    return reduced


def run_vehicle_post_insert_actions(
    record: dict[str, Any], event: dict[str, Any], node: dict[str, Any],
    node_id: str, classification: dict[str, Any], timestamp: float,
    latitude: float, longitude: float, banked_stem: str | None,
    relay_crop: bytes | None, review_crop: bytes | None,
    evidence_crop: bytes | None, feed: Any, store: Any = None,
) -> None:
    """Perform existing, intentionally non-transactional post-insert effects."""
    store = db if store is None else store
    # The bank link is intentionally best-effort: a failed association must not
    # turn a successful sighting insert into an HTTP failure.
    if banked_stem:
        try:
            from detect import bank as bank
            bank.link_sighting(banked_stem, record["id"])
        except Exception:
            pass
    if relay_crop is not None:
        # This remains after strip_sighting and insertion, so the quarantine
        # artifact can be keyed by the persisted sighting without altering the
        # mirror's stored record.
        mirror.quarantine_write(record["id"], relay_crop, {
            "ts": timestamp, "pub_lat": latitude, "pub_lon": longitude,
            "node_name": node.get("name") or "",
            "det_conf": event.get("det_conf"), "body": event.get("body")})
    if review_crop is not None:
        # The full-resolution evidence follows its review crop. evidence_write
        # itself rejects mirror writes; both remain intentionally post-insert.
        mirror.review_write(record["id"], review_crop, {
            "ts": timestamp, "node_id": node_id, "node_name": node.get("name") or "",
            "score": classification.get("conf"), "vclass": classification["vclass"],
            "det_conf": event.get("det_conf"), "body": event.get("body")})
        if evidence_crop is not None:
            mirror.evidence_write(record["id"], evidence_crop)
    # Feed publication occurs only after an inserted row and never on merge.
    store.heartbeat(node_id)
    feed.publish(record)


def persist_vehicle_sighting(
    record: dict[str, Any], event: dict[str, Any], node: dict[str, Any],
    node_id: str, classification: dict[str, Any], timestamp: float,
    candidate: bool, latitude: float, longitude: float,
    banked_stem: str | None, relay_crop: bytes | None,
    review_crop: bytes | None, evidence_crop: bytes | None, feed: Any,
    store: Any = None,
) -> VehiclePersistenceResult:
    """Merge a vehicle fragment or insert it and run post-insert actions."""
    store = db if store is None else store
    prior = find_vehicle_fragment(node_id, classification, timestamp, candidate, store)
    if prior:
        return merge_vehicle_fragment(prior, node_id, classification, timestamp, store)
    persisted = insert_vehicle_sighting(record, store)
    run_vehicle_post_insert_actions(
        persisted, event, node, node_id, classification, timestamp, latitude,
        longitude, banked_stem, relay_crop, review_crop, evidence_crop, feed,
        store,
    )
    return VehiclePersistenceResult(persisted, None)
