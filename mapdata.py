"""mapdata.py - Stage 2B route adapters: public, read-only map/data APIs.

This is a ROUTE ADAPTER module, not an application-service layer, exactly
like pages.py (Stage 2A) and tiles.py (Stage 1B). It owns path recognition,
query parsing, and HTTP-level response mapping for the ordinary public,
read-only RavenMap/SparrowMap APIs that already had a single narrow
application-level seam to call (a `db.py` read function plus, where needed,
`privacy.public_rows`/`classify`). It does NOT contain new domain logic and
does NOT duplicate privacy/redaction rules - see privacy.py for those.

Moved verbatim (smallest possible semantic change) from hub.py's
`_do_GET_inner`:
    /api/stats, /api/policy, /api/whoami, /api/plate, /api/pending,
    /api/leaderboard, /api/heat, /api/places, /api/sightings,
    /api/sighting/<id>, /api/track/<hash>

Deliberately NOT moved this stage (left in hub.py; see Stage 2B report):
    /api/nodes   - contains non-trivial consent-gating/redaction domain
                   logic (public_cam vs. volunteer position disclosure,
                   publish_span gate) that Stage 2B's rules say must not be
                   relocated merely to make a route module look cleaner.
    /api/health  - transport/admission diagnostics (inflight/heavy/ingest
                   semaphore internals, /proc fd introspection), not a
                   map/data API.
    /api/audit   - see Stage 2B findings: the route has no explicit
                   authentication check in hub.py despite being documented
                   as operator-gated; left in place unmodified and flagged
                   rather than silently extracted or "fixed".

Each function takes the same duck-typed handler object every other route
module takes (only handler._json / handler._err are used here), so hub.py's
dispatch chain can call straight into this module at the exact position
each route currently occupies, preserving first-match-wins ordering across
the hub.py/mapdata.py boundary. The handler's own microcache/admission/
cache-control wrapping in do_GET is unaffected - it runs entirely outside
_do_GET_inner and keys on the path, not on which module serves it.

Does not import hub. Stdlib plus db/classify/privacy/core only.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import classify
import db
import operator_auth
import privacy
from core import CONFIG, now


def stats(handler) -> None:
    """GET /api/stats - moved verbatim from hub.py."""
    return handler._json(db.stats())


def policy(handler) -> None:
    """GET /api/policy - the privacy posture of this deployment, machine-readable.

    Moved verbatim from hub.py; see the original comment there (preserved
    below) for why each field is published.
    """
    # Anyone mirroring or auditing the network can diff it.
    return handler._json({
        "site": CONFIG["site_name"],
        "public_tiers": CONFIG["public_tiers"],
        "civilian_retention_days": CONFIG["civilian_retention_days"],
        "public_retention_days": CONFIG["public_retention_days"],
        "pepper_rotation_days": CONFIG["pepper_rotation_days"],
        "node_position_jitter_m": CONFIG["node_position_jitter_m"],
        "min_plate_confidence": CONFIG["min_plate_confidence"],
        "public_threshold": classify.PUBLIC_THRESHOLD,
        "private_plate_lookup": False,
        "stores_video": False,
        "stores_full_frames": not CONFIG.get("crop_only", True),
        # Whether this deployment is currently willing to assert "police"
        # about a vehicle. Published so an outside auditor can see that the
        # tier is gated rather than having to infer it from an empty map.
        # See classify.py.
        "publishes_public_tier": CONFIG.get("publish_public_tier", False),
        # MEASURED, NOT ASSERTED - see db.public_decision_counts for the
        # reasoning this replaced a self-asserted "classifier_validated"
        # flag with.
        **db.public_decision_counts(),
        # Where the map opens. This is CONFIG, not a camera: it belongs to
        # the deployment, and it is served rather than hardcoded in app.js
        # so that no real coordinate has to live in the published source.
        "map_center": CONFIG["map_center"],
        "map_zoom": CONFIG["map_zoom"],
    })


def whoami(handler) -> None:
    """GET /api/whoami - moved verbatim from hub.py.

    Lets the review page tell "you are not signed in" apart from "the
    server is broken", without revealing anything. `_is_local` stays a
    Handler method - it is an existing seam, not domain logic - so this
    calls back into it exactly as hub.py did.
    """
    return handler._json({"operator": handler._is_local(),
                          "auth_required": operator_auth.required()})


def plate(handler, query: dict) -> None:
    """GET /api/plate - moved verbatim from hub.py.

    Search government plates. Only confirmed public-tier rows are scanned
    at all - see db.search_plate for why filtering in the redactor alone
    would still leak a yes/no answer about private vehicles. Searching
    public-tier data is NOT logged, on purpose (see the original comment
    preserved in hub.py's Stage 2B history).
    """
    q = (query.get("q") or [""])[0]
    rows = db.search_plate(q)
    return handler._json({"query": q, "results": privacy.public_rows(rows)})


def pending(handler) -> None:
    """GET /api/pending - moved verbatim from hub.py.

    "Something here wants a human", and nothing more - see db.pending_areas
    for why the coarsening happens there and not here.
    """
    return handler._json({"cells": db.pending_areas(),
                          "cell_deg": db.PENDING_CELL,
                          "window_s": db.PENDING_WINDOW_S})


def leaderboard(handler, query: dict) -> None:
    """GET /api/leaderboard - moved verbatim from hub.py."""
    hours = int(query.get("hours", [24])[0])
    return handler._json(db.leaderboard(hours))


def heat(handler) -> None:
    """GET /api/heat - moved verbatim from hub.py.

    Cumulative "everywhere a patrol has ever been confirmed", aggregated to
    a grid. The published record, gridded.
    """
    cells = db.gov_heat()
    return handler._json({"cells": cells, "total": sum(c["n"] for c in cells)})


def places(handler) -> None:
    """GET /api/places - moved verbatim from hub.py.

    Towns with cameras, for the zoomed-out map. Publishes LESS than the map
    already does; see the original comment preserved in hub.py's Stage 2B
    history for why a town badge at this zoom is not new exposure.
    """
    out_places: dict = {}
    for nd in db.nodes(active_only=True):
        name = (nd.get("place") or "").strip()
        if not name:
            continue          # unresolved: absent beats invented
        la, lo = nd.get("pub_lat"), nd.get("pub_lon")
        if la is None or lo is None:
            continue
        e = out_places.setdefault(name, {"place": name, "cameras": 0,
                                         "online": 0, "_la": 0.0, "_lo": 0.0})
        e["cameras"] += 1
        e["_la"] += la
        e["_lo"] += lo
        if nd.get("last_beat") and now() - nd["last_beat"] \
                < db.beat_window(nd.get("kind") or ""):
            e["online"] += 1
    out = []
    for e in out_places.values():
        c = e.pop("cameras")
        out.append({"place": e["place"], "cameras": c,
                    "online": e["online"],
                    "lat": round(e.pop("_la") / c, 4),
                    "lon": round(e.pop("_lo") / c, 4)})
    out.sort(key=lambda x: -x["cameras"])
    return handler._json({"places": out,
                          "cameras_placed": sum(x["cameras"] for x in out)})


def sightings(handler, query: dict) -> None:
    """GET /api/sightings - moved verbatim from hub.py."""
    since = float(query.get("since", [now() - 3600])[0])
    limit = int(query.get("limit", [400])[0])
    vclass = query.get("vclass", ["all"])[0]
    bbox = None
    if "bbox" in query:
        try:
            bbox = tuple(float(x) for x in query["bbox"][0].split(","))
        except ValueError:
            bbox = None
    rows = db.recent_sightings(since, limit, vclass, bbox)
    return handler._json(privacy.public_rows(rows))


def sighting(handler, path: str) -> None:
    """GET /api/sighting/<id> - moved verbatim from hub.py."""
    r = db.sighting(int(path.rsplit("/", 1)[1]))
    if not r:
        return handler._err(404, "no such sighting")
    # NO PICTURE, NO PUBLICATION - HERE TOO. recent_sightings withholds a
    # public row with no photograph from the feed, and a rule applied to one
    # representation is bypassed by the other: ids are sequential and
    # printed beside every sighting, so without this the withheld claim is
    # still one URL away.
    if r.get("tier") == "public" and not r.get("snap"):
        return handler._err(404, "no such sighting")
    # READING IS NOT AUDITED, DELIBERATELY - see the original comment
    # preserved in hub.py's Stage 2B history.
    return handler._json(privacy.public_rows([r])[0])


def track(handler, path: str) -> None:
    """GET /api/track/<hash> - moved verbatim from hub.py."""
    h = privacy.resolve_hash(unquote(path.rsplit("/", 1)[1]))
    rows = db.track_for(h)
    if not rows:
        return handler._json([])
    # NOT AUDITED - see the original comment preserved in hub.py's Stage 2B
    # history for why this one especially must leave no trace.
    out = privacy.public_rows(rows)
    # COMPUTED ONCE (documented latent O(n^2) bug preserved unchanged - see
    # hub.py's Stage 2B history / docs/HUB_ARCHITECTURE.md findings).
    score = classify.patrol_score(rows)
    for r in out:
        r["patrol_score"] = score
    return handler._json(out)
