"""A phone camera left watching must pick up a new /app by itself, and keep watching.

    python tools\\test_app_selfupdate.py

2026-10-13: the clear-photo fix shipped while both online phone cameras kept
the old code, because a volunteer's phone runs /app for days and only a reload
loads new code. The beat now carries the hub's app version (hub._app_version,
read from app.html's ?v=); a phone that is behind reloads and resumes watching.

Runs the REAL /app in headless Chrome with a fake camera against a hub on a
SCRATCH data dir, twice: the hub one version ahead (must reload exactly once
and come back watching), and the hub on the same version (must not reload).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_selfupd_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db   # noqa: E402
import hub  # noqa: E402

assert str(db.DB_PATH).startswith(str(SCRATCH)), db.DB_PATH
PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-test/1.0"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok else ""))
    fails += 0 if ok else 1


def post(path, body, token=""):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, method="POST", data=json.dumps(body).encode(),
                                 headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def drive(me):
    p = subprocess.run(["node", str(ROOT / "tools" / "app_selfupdate_driver.mjs"), BASE,
                        me["id"], me["token"]], capture_output=True, text=True, timeout=400)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    out = json.loads(lines[-1]) if lines else {"error": p.stdout[-800:] + p.stderr[-800:]}
    print("   ", json.dumps(out))
    return out


try:
    db.init()
    hub.CONFIG["auto_approve_nodes"] = True
    srv = hub.ThreadingHTTPServer(("127.0.0.1", PORT), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/health", headers={"User-Agent": UA}), timeout=2)
            break
        except Exception:
            time.sleep(0.2)

    real = hub._app_version()
    check("hub reads the app version from app.html", isinstance(real, int) and real > 0,
          str(real))
    me = post("/api/enroll", {"name": "t-upd", "kind": "mobile", "lat": 10.0, "lon": 20.0})
    post("/api/heartbeat", {"node_id": me["id"]}, me["token"])   # warm
    r = post("/api/heartbeat", {"node_id": me["id"]}, me["token"])
    check("the beat carries app_v", r.get("app_v") == real, str(r))

    # 1. The hub is one version ahead of the page.
    hub._app_version = lambda: real + 1
    out = drive(me)
    check("driver ran (newer)", "error" not in out, out.get("error", ""))
    check("the page loaded the current version", out.get("app_v_loaded") == real,
          str(out.get("app_v_loaded")))
    check("watching before the update", out.get("watching_before") is True)
    check("a newer version -> the page reloads itself", out.get("reloaded") is True)
    check("and is watching again with no tap", out.get("watching_after") is True)
    check("exactly once, no loop past the next beat", out.get("loads") == 2,
          str(out.get("loads")))
    check("still watching at the end", out.get("watching_end") is True)

    # 2. The hub is on the same version: nothing happens.
    hub._app_version = lambda: real
    out = drive(me)
    check("driver ran (same)", "error" not in out, out.get("error", ""))
    check("same version -> never reloads", out.get("loads") == 1, str(out.get("loads")))
    check("and keeps watching", out.get("watching_end") is True)
    srv.shutdown()
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
