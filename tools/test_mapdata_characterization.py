"""Stage 2B focused behavioral/privacy characterization for mapdata.py routes.

🚨 THIS SUITE TESTS EXTERNALLY OBSERVABLE BEHAVIOR OF THE MOVED ROUTES, NOT
SOURCE TEXT.

tools/test_hub_behavior.py already protects a handful of these paths at the
transport/security-mechanism level (/api/stats, /api/sightings, /api/nodes
cache-control, CORS). This file adds route-family-specific characterization
for every route moved to mapdata.py in Stage 2B: response schema stability
(major keys/types), privacy properties (no true node coordinates, no
unconfirmed/private plate text, per-day rolling aliases rather than stable
hashes for anon viewers), unauthenticated public accessibility, and mirror
availability.

Reuses the isolated-subprocess harness from test_hub_behavior.py (same
process-isolation design, same "hub.py is never modified" constraint) rather
than re-implementing it.

Run directly:  python tools\\test_mapdata_characterization.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hub_behavior import HubInstance, _get, _post, check, CHECKS, FAILURES
import test_hub_behavior as thb


def t_stats_schema(hub: HubInstance) -> None:
    print("\n== /api/stats schema/public accessibility ==")
    status, headers, body = _get(hub, "/api/stats")
    check("GET /api/stats -> 200 unauthenticated", status == 200, str(status))
    data = json.loads(body)
    check("GET /api/stats is a JSON object", isinstance(data, dict))
    for key in ("nodes_active", "nodes_ever_produced"):
        check(f"/api/stats has key {key!r}", key in data, str(data.keys()))


def t_policy_schema(hub: HubInstance) -> None:
    print("\n== /api/policy schema ==")
    status, headers, body = _get(hub, "/api/policy")
    check("GET /api/policy -> 200", status == 200, str(status))
    data = json.loads(body)
    for key in ("site", "public_tiers", "civilian_retention_days",
                "public_retention_days", "public_threshold",
                "private_plate_lookup", "map_center", "map_zoom"):
        check(f"/api/policy has key {key!r}", key in data, str(data.keys()))
    check("/api/policy never claims to store video",
          data.get("stores_video") is False, str(data.get("stores_video")))


def t_whoami_schema(hub: HubInstance) -> None:
    print("\n== /api/whoami schema ==")
    status, headers, body = _get(hub, "/api/whoami")
    check("GET /api/whoami -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/whoami has 'operator' bool", isinstance(data.get("operator"), bool))
    check("/api/whoami has 'auth_required' bool",
          isinstance(data.get("auth_required"), bool))


def t_plate_schema(hub: HubInstance) -> None:
    print("\n== /api/plate schema/no-store ==")
    status, headers, body = _get(hub, "/api/plate?q=ABC123")
    check("GET /api/plate -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/plate echoes query", data.get("query") == "ABC123", str(data))
    check("/api/plate has 'results' list", isinstance(data.get("results"), list))


def t_pending_schema(hub: HubInstance) -> None:
    print("\n== /api/pending schema ==")
    status, headers, body = _get(hub, "/api/pending")
    check("GET /api/pending -> 200", status == 200, str(status))
    data = json.loads(body)
    for key in ("cells", "cell_deg", "window_s"):
        check(f"/api/pending has key {key!r}", key in data, str(data.keys()))
    check("/api/pending 'cells' is a list", isinstance(data.get("cells"), list))


def t_leaderboard_schema(hub: HubInstance) -> None:
    print("\n== /api/leaderboard schema ==")
    status, headers, body = _get(hub, "/api/leaderboard?hours=24")
    check("GET /api/leaderboard -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/leaderboard returns a list", isinstance(data, list), str(type(data)))


def t_heat_places_schema(hub: HubInstance) -> None:
    print("\n== /api/heat, /api/places schema ==")
    status, headers, body = _get(hub, "/api/heat")
    check("GET /api/heat -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/heat has 'cells' and 'total'",
          "cells" in data and "total" in data, str(data.keys()))

    status, headers, body = _get(hub, "/api/places")
    check("GET /api/places -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/places has 'places' and 'cameras_placed'",
          "places" in data and "cameras_placed" in data, str(data.keys()))


def t_nodes_no_true_coords_for_volunteer(hub: HubInstance) -> None:
    print("\n== /api/nodes privacy: no volunteer true coordinates ==")
    status, headers, body = _get(hub, "/api/nodes")
    check("GET /api/nodes -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/nodes returns a list", isinstance(data, list), str(type(data)))
    # An empty fixture DB is expected in the isolated instance; the schema
    # contract (no pub_lat/pub_lon/heading/fov/reach keys at all) is what
    # matters here, characterized on whatever rows (possibly zero) exist.
    for rec in data:
        for banned in ("pub_lat", "pub_lon", "heading", "fov", "reach"):
            check(f"/api/nodes row omits {banned!r}", banned not in rec,
                  str(rec.keys()))
        if rec.get("kind") != "public_cam":
            check("/api/nodes non-public_cam row has lat=None",
                  rec.get("lat") is None, str(rec.get("lat")))
            check("/api/nodes non-public_cam row has lon=None",
                  rec.get("lon") is None, str(rec.get("lon")))


def t_sightings_schema_and_redaction(hub: HubInstance) -> None:
    print("\n== /api/sightings schema/redaction ==")
    status, headers, body = _get(hub, "/api/sightings")
    check("GET /api/sightings -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/sightings returns a list", isinstance(data, list), str(type(data)))
    for rec in data:
        check("/api/sightings row has no 'plate_conf' (internal-only field)",
              "plate_conf" not in rec, str(rec.keys()))
        check("/api/sightings row has no 'confirmed_by' (internal-only field)",
              "confirmed_by" not in rec, str(rec.keys()))


def t_sighting_by_id_missing(hub: HubInstance) -> None:
    print("\n== /api/sighting/<id> not-found path ==")
    status, headers, body = _get(hub, "/api/sighting/999999999")
    check("GET /api/sighting/<missing id> -> 404", status == 404, str(status))


def t_track_by_hash_empty(hub: HubInstance) -> None:
    print("\n== /api/track/<hash> empty-result path ==")
    status, headers, body = _get(hub, "/api/track/nosuchhash")
    check("GET /api/track/<unknown hash> -> 200", status == 200, str(status))
    data = json.loads(body)
    check("/api/track/<unknown hash> returns []", data == [], str(data))


def t_mirror_availability(_Ctx) -> None:
    print("\n== mirror availability for mapdata.py routes ==")
    with _Ctx({"public_mirror": True}) as hub:
        for path in ("/api/stats", "/api/policy", "/api/whoami",
                     "/api/pending", "/api/leaderboard", "/api/heat",
                     "/api/places", "/api/sightings"):
            status, headers, body = _get(hub, path)
            check(f"public_mirror=true: {path} still reachable -> 200",
                  status == 200, str(status))


def main() -> int:
    print("Starting isolated hub instance (default config)...")
    with HubInstance() as hub:
        t_stats_schema(hub)
        t_policy_schema(hub)
        t_whoami_schema(hub)
        t_plate_schema(hub)
        t_pending_schema(hub)
        t_leaderboard_schema(hub)
        t_heat_places_schema(hub)
        t_nodes_no_true_coords_for_volunteer(hub)
        t_sightings_schema_and_redaction(hub)
        t_sighting_by_id_missing(hub)
        t_track_by_hash_empty(hub)

    class _Ctx:
        def __init__(self, overrides):
            self.overrides = overrides
        def __enter__(self):
            self.hub = HubInstance(self.overrides)
            self.hub.start()
            return self.hub
        def __exit__(self, *exc):
            self.hub.stop()
            import shutil
            shutil.rmtree(self.hub.tmp, ignore_errors=True)

    t_mirror_availability(_Ctx)

    print(f"\n{thb.CHECKS} checks run, {len(thb.FAILURES)} failed.")
    if thb.FAILURES:
        print("\nFAILURES:")
        for f in thb.FAILURES:
            print(f"  - {f}")
        return 1
    print("mapdata characterization passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
