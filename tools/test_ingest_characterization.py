"""HTTP characterization of Handler._ingest() before Stage 3 extraction.

Uses a scratch SQLite database and scratch image directories.  The assertions
intentionally describe inherited behavior, including policy-sensitive behavior
that should be reviewed separately rather than silently corrected.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
CHECKS = 0
CLIENT_PORT = random.SystemRandom().randrange(20000, 50000)
SCRATCH = Path(tempfile.mkdtemp(prefix="ravenmap_ingest_"))

import core  # noqa: E402
import db  # noqa: E402
import mirror  # noqa: E402
import snapshot  # noqa: E402

REAL_DB = Path(db.DB_PATH)
for subdir in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / subdir).mkdir(parents=True, exist_ok=True)
for module, name, value in (
    (core, "DATA", SCRATCH),
    (core, "SNAPS", SCRATCH / "snaps"),
    (core, "EVIDENCE", SCRATCH / "evidence"),
    (core, "HELD", SCRATCH / "held"),
    (core, "DB_PATH", SCRATCH / "sparrow.db"),
    (db, "DB_PATH", SCRATCH / "sparrow.db"),
    (mirror, "DATA", SCRATCH),
    (mirror, "REVIEW", SCRATCH / "review"),
    (mirror, "INBOX", SCRATCH / "inbox"),
    (snapshot, "SNAPS", SCRATCH / "snaps"),
):
    setattr(module, name, value)

import hub  # noqa: E402
import nodes  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [ok] {name}")
    else:
        FAILURES.append(name)
        print(f"  [FAIL] {name}: {detail}")


def jpeg_b64(width: int = 320, height: int = 180) -> str:
    image = Image.new("RGB", (width, height), (70, 100, 140))
    buf = io.BytesIO()
    image.save(buf, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def add_node(node_id: str, *, token: str | None = "token", status: str = "active",
             pubkey: str | None = None, lat: float = 41.111111,
             lon: float = -82.222222) -> None:
    db.upsert_node({
        "id": node_id, "name": node_id, "token": token, "status": status,
        "pubkey": pubkey, "lat": lat, "lon": lon,
        "pub_lat": lat + 0.01, "pub_lon": lon - 0.01,
        "kind": "mobile",
    })


def event(node_id: str, **overrides: object) -> dict:
    value = {
        "node_id": node_id, "ts": time.time(), "lat": 42.5, "lon": -83.7,
        "source": "camera", "body": "car", "det_conf": 0.9,
        "plate_text": "", "plate_conf": 0.0, "evidence": {},
    }
    value.update(overrides)
    return value


def post(port: int, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    global CLIENT_PORT
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body).encode()
    source_port = CLIENT_PORT
    CLIENT_PORT += 1
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=10, source_address=("127.0.0.1", source_port))
    try:
        connection.request("POST", path, payload, headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, {"raw": raw.decode(errors="replace")}
    finally:
        connection.close()


def sighting(sighting_id: int) -> dict:
    row = db.sighting(sighting_id)
    assert row is not None
    return row


def high_evidence() -> dict:
    return {"light_bar": True, "agency_decal": True}


def main() -> int:
    if Path(db.DB_PATH) == REAL_DB:
        print("REFUSING TO RUN: scratch database was not configured.")
        return 2
    db.init()
    # This suite exercises the candidate/confirmation path deliberately gated
    # off by the repository's default safety configuration.
    core.CONFIG["publish_public_tier"] = True
    hub.RATE["/api/sightings"] = (1000, 3600)
    hub.RATE["_all_sightings"] = (10000, 3600)
    # Stay outside Windows' ephemeral client-port range.  Binding a listener to
    # a just-released ephemeral port can make a later localhost client attempt
    # to reuse that port and fail with WinError 10048.
    port = 18081
    server = hub.ThreadingHTTPServer(("127.0.0.1", port), hub.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        print("\n== authentication and trust ==")
        status, _ = post(port, "/api/sightings", event("missing"), "nope")
        check("unknown node is 404", status == 404, str(status))

        add_node("inactive", status="paused")
        status, _ = post(port, "/api/sightings", event("inactive"), "wrong")
        check("inactive node precedes bad-token rejection", status == 403, str(status))

        add_node("token-node")
        status, _ = post(port, "/api/sightings", event("token-node"), "wrong")
        check("active node with invalid bearer is 401", status == 401, str(status))

        private, public = nodes.new_keypair()
        add_node("signed-node", token="signed-token", pubkey=public)
        signed = event("signed-node", plate_text="GOV123", plate_conf=0.9)
        signed["sig"] = nodes.sign_event(signed, private)
        status, body = post(port, "/api/sightings", signed, "signed-token")
        check("valid signed key-node event succeeds", status == 200, str(body))
        check("valid signed key-node event persists sig_ok",
              status == 200 and sighting(body["id"])["sig_ok"] == 1, str(body))

        invalid = event("signed-node")
        invalid["sig"] = "not-a-valid-signature"
        status, _ = post(port, "/api/sightings", invalid, "signed-token")
        check("invalid signature precedes successful ingest", status == 401, str(status))

        add_node("tokenless", token=None)
        status, body = post(port, "/api/sightings", event("tokenless"))
        check("active tokenless node currently accepts no bearer",
              status == 200, str(body))

        add_node("privileged-flags")
        status, body = post(port, "/api/sightings", event(
            "privileged-flags", plate_text="GOV999", plate_conf=0.99,
            evidence={"human_confirmed": True, "visual_police": True}), "token")
        row = sighting(body["id"])
        check("submitted human_confirmed cannot create public row",
              status == 200 and row["tier"] == "private", str(row))
        check("submitted visual_police cannot create public row",
              row["plate_text"] is None and row["reviewed"] is None, str(row))

        print("\n== plate and candidate policy ==")
        add_node("plate-node")
        status, body = post(port, "/api/sightings", event(
            "plate-node", plate_text="WEAK123", plate_conf=0.54,
            evidence=high_evidence()), "token")
        row = sighting(body["id"])
        check("weak plate does not reject sighting", status == 200, str(body))
        check("weak plate is removed before persistence",
              row["plate_hash"] is None and row["plate_text"] is None and row["plate_conf"] == 0,
              str(row))
        check("ordinary classifier candidate is held private",
              row["tier"] == "private" and "held for human review" in body["why"], str(row))

        add_node("threshold-plate-node")
        status, body = post(port, "/api/sightings", event(
            "threshold-plate-node", plate_text="STRONG123", plate_conf=0.55,
            evidence=high_evidence()), "token")
        row = sighting(body["id"])
        check("threshold plate is retained as hash while held",
              row["plate_hash"] is not None and row["plate_text"] is None, str(row))
        check("sightable/non-tierable does not retain plate text",
              row["plate_text"] is None, str(row))

        print("\n== source and image behavior ==")
        add_node("source-node")
        status, body = post(port, "/api/sightings", event(
            "source-node", source="phone", evidence=high_evidence(),
            plate_text="PHONE123", plate_conf=0.99), "token")
        phone = sighting(body["id"])
        check("phone claim is held private regardless of candidate signals",
              phone["tier"] == "private" and "human-submitted" in body["why"], str(phone))

        add_node("camera-no-box")
        status, body = post(port, "/api/sightings", event(
            "camera-no-box", source="camera", snap_b64=jpeg_b64(),
            evidence=high_evidence()), "token")
        camera_without_box = sighting(body["id"])
        check("camera frame without vehicle_box is not stored",
              camera_without_box["snap"] is None and bool(body.get("image_dropped")), str(body))

        add_node("phone-node")
        status, body = post(port, "/api/sightings", event(
            "phone-node", source="phone_node", snap_b64=jpeg_b64(180, 100)), "token")
        phone_node = sighting(body["id"])
        check("phone-node subresolution image is stored",
              phone_node["snap"] is not None, str(phone_node))

        add_node("supplied-snap")
        status, body = post(port, "/api/sightings", event(
            "supplied-snap", source="camera", snap="caller-claimed.jpg",
            snap_b64=jpeg_b64(), vehicle_box=[20, 20, 260, 160]), "token")
        supplied_snap = sighting(body["id"])
        check("SECURITY/POLICY FINDING — inherited behavior: caller snap bypasses storage",
              supplied_snap["snap"] == "caller-claimed.jpg", str(supplied_snap))

        print("\n== confirmation, merge, time, and position ==")
        add_node("confirm-node")
        confirm_event = event(
            "confirm-node", source="phone", plate_text="CONF123", plate_conf=0.99,
            evidence=high_evidence())
        status, body = post(port, "/api/node/confirm", confirm_event, "token")
        confirmed = sighting(body["id"])
        check("confirm route overrides submitted node/source and uses trusted confirmation",
              status == 200 and confirmed["node_id"] == "confirm-node"
              and confirmed["tier"] == "public", str(confirmed))
        check("confirmed record stamps human review provenance",
              confirmed["reviewed"] == "confirmed" and confirmed["decided_by"] == "human",
              str(confirmed))
        check("confirmed public record retains tierable plate text",
              confirmed["plate_text"] == "CONF123", str(confirmed))

        add_node("merge-node")
        first = event("merge-node", evidence=high_evidence(), ts=time.time())
        status, first_body = post(port, "/api/sightings", first, "token")
        status, merged_body = post(port, "/api/sightings", event(
            "merge-node", evidence=high_evidence(), ts=time.time() + 1), "token")
        check("candidate duplicate merges into prior sighting",
              status == 200 and merged_body.get("merged_into") == first_body["id"], str(merged_body))
        check("merge increments detection count",
              sighting(first_body["id"])["detections"] == 2, str(sighting(first_body["id"])))

        noncandidate = event("merge-node", ts=time.time(), source="phone_node")
        status, nc1 = post(port, "/api/sightings", noncandidate, "token")
        status, nc2 = post(port, "/api/sightings", event(
            "merge-node", ts=time.time() + 1, source="phone_node"), "token")
        check("noncandidate traffic does not use candidate merge path",
              "merged_into" not in nc2 and nc1["id"] != nc2["id"], str(nc2))

        add_node("clock-node", lat=40.123456, lon=-81.654321)
        claimed = time.time() - 500
        status, body = post(port, "/api/sightings", event(
            "clock-node", ts=claimed, lat=42.5, lon=-83.7), "token")
        row = sighting(body["id"])
        check("out-of-window timestamp is replaced by server time",
              status == 200 and abs(row["ts"] - claimed) > 100, str(row))
        check("clock skew is disclosed in response",
              "clock_skew_s" in body and "server time was used instead" in body.get("note", ""),
              str(body))
        check("stored sighting does not use true configured node coordinates",
              (row["lat"], row["lon"]) != (40.123456, -81.654321), str(row))

        print("\n== inherited caller-controlled record fields ==")
        add_node("record-fields")
        status, body = post(port, "/api/sightings", event(
            "record-fields", evidence=high_evidence(), _reviewed="confirmed",
            _decided_by="client", bank_ref="client-bank-ref"), "token")
        row = sighting(body["id"])
        check("SECURITY/POLICY FINDING — inherited behavior: caller reviewed fields persist",
              row["reviewed"] == "confirmed" and row["decided_by"] == "client", str(row))
        check("caller bank_ref persists", row["bank_ref"] == "client-bank-ref", str(row))

        print("\n== public-mirror restrictions ==")
        core.CONFIG["public_mirror"] = True
        core.CONFIG["relay_inbox"] = True
        add_node("mirror-node")
        status, body = post(port, "/api/sightings", event(
            "mirror-node", source="camera", snap_b64=jpeg_b64(),
            bank_ref="private-bank-ref", color="blue", make="example",
            vehicle_box=[20, 20, 260, 160]), "token")
        private_mirror = sighting(body["id"])
        check("mirror strips private identifiers and image before persistence",
              status == 200 and private_mirror["snap"] is None
              and private_mirror["bank_ref"] is None
              and private_mirror["color"] is None
              and private_mirror["make"] is None, str(private_mirror))

        add_node("mirror-phone")
        status, body = post(port, "/api/sightings", event(
            "mirror-phone", source="phone_node", snap_b64=jpeg_b64(180, 100)), "token")
        mirror_phone = sighting(body["id"])
        inbox_jpg = SCRATCH / "inbox" / f"{body['id']}.jpg"
        check("mirror phone-node row stores no image",
              mirror_phone["snap"] is None, str(mirror_phone))
        check("mirror writes valid subresolution phone crop to quarantine",
              status == 200 and inbox_jpg.exists(), str(body))
        oversized = event("mirror-phone", source="phone_node", snap_b64=jpeg_b64(320, 180))
        status, body = post(port, "/api/sightings", oversized, "token")
        check("oversized phone-node crop is not quarantined",
              status == 200 and not (SCRATCH / "inbox" / f"{body['id']}.jpg").exists(),
              str(body))

        print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
        return 1 if FAILURES else 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
