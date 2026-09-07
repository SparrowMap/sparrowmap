"""Stage 0 characterization checks for hub.py.

This is intentionally a source-level contract test: it has no network, database,
or model setup requirements and does not change runtime behavior.
"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "hub.py"
NODE_SELF = ROOT / "node_self.py"
DOC = ROOT / "docs" / "HUB_ARCHITECTURE.md"


GET_ROUTES = {
    "/", "/about", "/transparency", "/status", "/checksums", "/support",
    "/donate", "/business", "/ipcamera", "/IPCamera", "/relay.py", "/download",
    "/api/download", "/hardware", "/build16", "/help", "/app", "/node", "/key",
    "/contribute", "/admin/bugs", "/drive", "/planes", "/api/aircraft",
    "/api/geocode", "/api/scanner", "/api/places", "/api/heat", "/api/node/me",
    "/aim", "/rv", "/rv/mine", "/rv/pool", "/rv/admin", "/api/rv/me",
    "/api/rv/queue", "/api/rv/contributed", "/rv/retracted", "/api/rv/retracted",
    "/rv/photos", "/api/rv/held", "/api/rv/progress", "/api/rv/tokens",
    "/api/health", "/api/policy", "/api/whoami", "/api/plate", "/api/stats",
    "/sw.js", "/login", "/review", "/api/review/queue", "/api/pending",
    "/api/nodes", "/api/sightings", "/api/audit", "/api/live",
}

POST_ROUTES = {
    "/api/enroll", "/api/sightings", "/api/help/vote", "/api/node/progress",
    "/api/node/label", "/api/bug", "/api/bug/close", "/api/bug/delete",
    "/api/node/whoami", "/api/node/parked", "/api/node/key", "/api/node/span",
    "/api/node/confirm", "/api/heartbeat", "/api/signals",
    "/api/sighting/fullres", "/api/heartbeat/bulk", "/api/review/edit",
    "/api/report", "/api/review", "/api/key/qr", "/api/key/rotate",
    "/api/operator/login", "/api/operator/logout", "/api/rv/login",
    "/api/rv/logout", "/api/rv/retracted/delete", "/api/rv/held/fix",
    "/api/rv/verdict", "/api/drive/report", "/api/drive/vote",
    "/api/rv/my-token", "/api/rv/tokens/new", "/api/rv/tokens/revoke",
    "/api/review/bulk", "/api/purge",
}


def main() -> int:
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (HUB, NODE_SELF)
        if p.exists()
    )
    doc = DOC.read_text(encoding="utf-8")
    # response_policy.py (Stage 1B step 4) now owns the literal security-header
    # values that used to live directly in hub.py; the contract check widens to
    # either file since hub.py still calls into it for every response.
    RESPONSE_POLICY = ROOT / "response_policy.py"
    policy_text = RESPONSE_POLICY.read_text(encoding="utf-8") if RESPONSE_POLICY.is_file() else ""

    assert "class Handler(BaseHTTPRequestHandler)" in text
    assert "def do_HEAD" in text
    assert "def do_GET" in text and "def do_POST" in text
    assert "operator_auth.check" in text
    assert "nodes.verify_event" in text or "verify_event" in text
    assert "mirror.strip_sighting" in text
    assert "privacy.redact" in text
    assert "Content-Security-Policy" in text or "Content-Security-Policy" in policy_text
    assert "Content-Type" in text
    assert "public_mirror" in doc

    # Every explicitly listed contract route must still occur in the handler or
    # in a documented grouped/prefix branch.
    for route in sorted(GET_ROUTES | POST_ROUTES):
        assert route in text, f"route missing from source inventory: {route}"
        assert route in doc, f"route missing from inventory: {route}"

    # Prefix routes and dynamic route families are part of the inventory too.
    for prefix in ("/api/tile/", "/api/help/img/", "/api/bug/shot/",
                   "/api/rv/retracted/photo/", "/api/rv/held/photo/",
                   "/api/rv/crop/", "/api/sighting/",
                   "/api/track/", "/snap/", "/vendor/", "/static/"):
        assert prefix in text, f"dynamic route family missing: {prefix}"

    print("hub contract characterization passed")
    print(f"  documented GET contracts: {len(GET_ROUTES)}")
    print(f"  documented POST contracts: {len(POST_ROUTES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
