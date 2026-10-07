"""A fixed camera's id must not carry sightings from somewhere else.

    python tools\\test_fixed_node_away.py

2026-10-07: drive mode reused the phone's stationary camera id, and 217 drive
sightings were drawn on his home road. The hub now refuses an event whose GPS
is more than FIXED_NODE_MAX_AWAY_M from a `fixed` node, and still accepts the
same far-away GPS from a `mobile` node. Runs a real hub on a SCRATCH data dir.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_away_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db   # noqa: E402
import hub  # noqa: E402

assert str(db.DB_PATH).startswith(str(SCRATCH)), db.DB_PATH
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-test/1.0"
fails = 0


def call(path, body, token=""):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, method="POST",
                                 data=json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def check(name, ok, detail=""):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok else ""))
    fails += 0 if ok else 1


try:
    db.init()
    hub.CONFIG["auto_approve_nodes"] = True       # the box's setting
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/health", headers={"User-Agent": UA}), timeout=2)
            break
        except Exception:
            time.sleep(0.2)

    HOME = (42.8155, -83.7822)
    FAR = (42.5600, -83.3000)                      # ~48 km away
    nodes = {}
    for kind in ("fixed", "mobile"):
        st, r = call("/api/enroll", {"name": f"t-{kind}", "kind": kind,
                                     "lat": HOME[0], "lon": HOME[1]})
        nodes[kind] = r
        check(f"enrol {kind}", st == 200 and r.get("id"), f"{st} {r}")

    def sight(kind, at):
        n = nodes[kind]
        return call("/api/sightings", {"node_id": n["id"], "ts": time.time(),
                                       "lat": at[0], "lon": at[1],
                                       "source": "phone_node"}, n["token"])

    st, r = sight("fixed", FAR)
    check("fixed camera, GPS 48 km away -> refused 409", st == 409, f"{st} {r}")
    st, r = sight("fixed", (HOME[0] + 0.001, HOME[1]))
    check("fixed camera, GPS at home -> accepted", st in (200, 201), f"{st} {r}")
    st, r = sight("mobile", FAR)
    check("mobile camera, GPS 48 km away -> accepted", st in (200, 201), f"{st} {r}")
    srv.shutdown()
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
