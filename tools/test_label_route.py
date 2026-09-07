"""Drive /api/node/label over real HTTP and measure the photograph it publishes.

🚨 THIS EXISTS BECAUSE "THE FUNCTION WORKS" IS NOT THE CLAIM ANYBODY CARES
ABOUT. The drive-mode popup once shipped with a trigger that could never fire
while every piece of it passed its own test, so this one starts the actual
server, enrols a node the way a phone does, posts a sighting the way the
detector does, answers "yes, police" the way the popup does, and then opens the
published file and looks at how many pixels are in it.

    python tools\\test_label_route.py

Runs against a SCRATCH data directory - a throwaway sqlite file and an empty
snaps folder in the temp dir - so it cannot touch the live map, the box, or the
local bank. The redirection has to happen before hub is imported, because every
module in this project binds its paths at import time.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_route_"))

import core        # noqa: E402
import db          # noqa: E402
import mirror      # noqa: E402
import snapshot    # noqa: E402
import review_api  # noqa: E402
from PIL import Image  # noqa: E402

REAL_SNAPS = Path(core.SNAPS)
REAL_DB = Path(db.DB_PATH)
for _p in ("snaps", "evidence", "held", "review", "inbox", "tiles"):
    (SCRATCH / _p).mkdir(parents=True, exist_ok=True)
for _mod, _name, _val in [
        (core, "DATA", SCRATCH), (core, "SNAPS", SCRATCH / "snaps"),
        (core, "EVIDENCE", SCRATCH / "evidence"), (core, "HELD", SCRATCH / "held"),
        (core, "DB_PATH", SCRATCH / "sparrow.db"),
        (db, "DB_PATH", SCRATCH / "sparrow.db"),
        (snapshot, "SNAPS", SCRATCH / "snaps"),
        (review_api, "SNAPS", SCRATCH / "snaps"),
        (review_api, "EVIDENCE", SCRATCH / "evidence"),
        (review_api, "HELD", SCRATCH / "held"),
        (mirror, "DATA", SCRATCH), (mirror, "REVIEW", SCRATCH / "review"),
        (mirror, "INBOX", SCRATCH / "inbox")]:
    setattr(_mod, _name, _val)

import hub  # noqa: E402

for _mod, _name, _val in [(hub, "DATA", SCRATCH), (hub, "SNAPS", SCRATCH / "snaps")]:
    setattr(_mod, _name, _val)

# 🚨 REFUSE RATHER THAN RISK IT. A redirect that missed one binding is a test
# that writes into the live snaps directory and deletes real photographs.
if Path(db.DB_PATH) == REAL_DB or Path(snapshot.SNAPS) == REAL_SNAPS:
    print("REFUSING TO RUN: a path still points at the live data directory.")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(2)

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-test/1.0"
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(name)


def jpeg(w: int, h: int) -> str:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def post(path: str, body: dict, token: str = ""):
    hdrs = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(body).encode(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"raw": raw[:200].decode("utf8", "replace")}


def main() -> int:
    print(f"scratch: {SCRATCH}\nserving on {BASE}\n")
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(60):
        try:
            urllib.request.urlopen(
                urllib.request.Request(BASE + "/api/health",
                                       headers={"User-Agent": UA}), timeout=2).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        print("server never came up")
        return 2

    # --- enrol a drive node, exactly as drive.html does -----------------
    st, nd = post("/api/enroll", {"name": "route test drive", "lat": 42.5,
                                  "lon": -83.7, "kind": "phone"})
    check("enrol returned a node with a token",
          st == 200 and bool(nd.get("id")) and bool(nd.get("token")),
          f"{st} {list(nd)[:4]}")
    if not nd.get("token"):
        return 1
    nid, tok = nd["id"], nd["token"]
    # This deployment's config has auto_approve_nodes off, so a freshly enrolled
    # node is 'paused' and every route 403s it. That is the operator's approval
    # policy, not the thing under test - a real drive node is approved before it
    # ever posts - so approve it here rather than testing the approval queue.
    _c = db.connect()
    _c.execute("UPDATE nodes SET status='active' WHERE id=?", (nid,))
    _c.commit()

    # --- post a sighting, exactly as the drive loop does ----------------
    st, sg = post("/api/sightings", {
        "node_id": nid, "ts": time.time(), "source": "phone_node",
        "body": "car", "det_conf": 0.9, "plate_text": "", "plate_conf": 0,
        "evidence": {}, "snap_b64": jpeg(200, 95), "lat": 42.5, "lon": -83.7},
        token=tok)
    sid = sg.get("id")
    check("the 200px drive crop was accepted", st == 200 and bool(sid),
          f"{st} {sg.get('error') or sid}")
    if not sid:
        return 1

    before = (db.sighting(sid) or {}).get("snap")
    if before:
        size = Image.open(Path(core.SNAPS) / before).size
        check("what ingest stored is pen resolution (this IS the bug)",
              max(size) <= snapshot.SUBRES_MAX_EDGE, str(size))

    # --- another node's credentials must not label THIS sighting --------
    st, nd2 = post("/api/enroll", {"name": "route test drive other", "lat": 42.5,
                                   "lon": -83.7, "kind": "phone"})
    nid2, tok2 = nd2["id"], nd2["token"]
    db.connect().execute("UPDATE nodes SET status='active' WHERE id=?", (nid2,))
    db.connect().commit()
    st, out = post("/api/node/label", {
        "node_id": nid2, "sighting_id": sid, "label": "police"}, token=tok2)
    check("labeling another node's sighting -> 403 not your camera's sighting",
          st == 403 and out.get("error") == "not your camera's sighting",
          f"{st} {out}")

    # --- answer "yes, police" WITH the phone's full-res copy ------------
    st, out = post("/api/node/label", {
        "node_id": nid, "sighting_id": sid, "label": "police",
        "snap_b64": jpeg(1600, 760)}, token=tok)
    check("the label was accepted", st == 200 and not out.get("error"),
          f"{st} {out}")

    row = db.sighting(sid) or {}
    check("the sighting is on the public map", row.get("tier") == "public",
          str(row.get("tier")))
    snap = row.get("snap")
    check("it has a photograph", bool(snap), str(snap))
    if snap:
        p = Path(core.SNAPS) / snap
        check("the photograph FILE exists", p.exists(), str(p.name))
        if p.exists():
            size = Image.open(p).size
            check("🎯 the PUBLISHED photograph is full resolution, not 200px",
                  max(size) > snapshot.SUBRES_MAX_EDGE, str(size))
            check("and is served by the map", bool(snap), f"/snap/{snap}")
    check("the 200px original it replaced is gone",
          not before or not (Path(core.SNAPS) / before).exists(), str(before))

    # --- the pen entry must not survive a decision ----------------------
    check("no pen entry is left for a second reviewer to re-judge",
          not (Path(mirror.REVIEW) / f"{sid}.json").exists(), str(sid))

    # --- and a NO must upload nothing ----------------------------------
    st, sg2 = post("/api/sightings", {
        "node_id": nid, "ts": time.time(), "source": "phone_node",
        "body": "car", "det_conf": 0.9, "plate_text": "", "plate_conf": 0,
        "evidence": {}, "snap_b64": jpeg(200, 95), "lat": 42.5, "lon": -83.7},
        token=tok)
    sid2 = sg2.get("id")
    if sid2:
        # 🚨 THE ONE THAT MUST NEVER PUBLISH A PLATE. If a client ever sent the
        # un-degraded crop with an ordinary-car answer, the route must ignore
        # it: `published` is false, so nothing is stored.
        post("/api/node/label", {"node_id": nid, "sighting_id": sid2,
                                 "label": "civilian", "snap_b64": jpeg(1600, 760)},
             token=tok)
        row2 = db.sighting(sid2) or {}
        s2 = row2.get("snap")
        big = bool(s2) and (Path(core.SNAPS) / s2).exists() and \
            max(Image.open(Path(core.SNAPS) / s2).size) > snapshot.SUBRES_MAX_EDGE
        check("a 'civilian' answer never stores the full-resolution crop",
              row2.get("tier") != "public" and not big,
              f"tier={row2.get('tier')} big={big}")

    srv.shutdown()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED: " + ", ".join(FAIL))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
