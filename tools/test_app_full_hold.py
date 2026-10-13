"""The /app phone must still hand over its clear photo when review is days late.

    python tools\\test_app_full_hold.py

2026-10-13: every phone-camera police sighting published that week kept the
200px preview with the plate band painted out (7 of 7 fixed cameras). The phone
held its clear crop in memory for 1 hour and 48 entries; a busy road pushed it
out in ~15 minutes and review comes hours to days later.

This runs the REAL /app page in headless Chrome (tools/app_full_hold_driver.mjs)
against a hub on a SCRATCH data dir, and checks the whole loop: the phone names
what it holds, uploads what was published, keeps what is still in the inbox or
review pen, deletes decided traffic after the grace period, keeps a fresh crop,
and still has the kept ones after a reload.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_appfull_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import db   # noqa: E402
import hub  # noqa: E402
import mirror  # noqa: E402

assert str(db.DB_PATH).startswith(str(SCRATCH)), db.DB_PATH
assert str(mirror.INBOX).startswith(str(SCRATCH)), mirror.INBOX
assert str(mirror.REVIEW).startswith(str(SCRATCH)), mirror.REVIEW
import review_api  # noqa: E402

# Storing the photo isolates the vehicle with a model (test_fullres_on_confirm
# covers that). This tests that the phone hands it over at all.
uploads = []
review_api.attach_confirmed_photo = (
    lambda sid, row, crop, **k: uploads.append((sid, len(crop))) or "full_test.jpg")
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-test/1.0"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok else ""))
    fails += 0 if ok else 1


def post(path, body):
    req = urllib.request.Request(BASE + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


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

    me = post("/api/enroll", {"name": "t-app", "kind": "mobile", "lat": 10.0, "lon": 20.0})
    check("enrol", bool(me.get("id")), str(me))

    c = db.connect()
    day = time.time() - 2 * 86400            # reviewed two days after the pass

    def row(tier, ts=day):
        cur = c.execute("INSERT INTO sightings(node_id, ts, tier, vclass, source, snap) "
                        "VALUES (?, ?, ?, 'police', 'phone_node', 'x.jpg')",
                        (me["id"], ts, tier))
        c.commit()
        return cur.lastrowid

    ids = {"published": row("public"), "pen": row("private"), "inbox": row("private"),
           "decided": row("private"), "fresh": row("private", time.time())}
    mirror.INBOX.mkdir(parents=True, exist_ok=True)
    mirror.REVIEW.mkdir(parents=True, exist_ok=True)
    (mirror.REVIEW / f"{ids['pen']}.json").write_text("{}")
    (mirror.INBOX / f"{ids['inbox']}.json").write_text("{}")

    b = io.BytesIO()
    Image.new("RGB", (900, 540), (90, 120, 160)).save(b, "JPEG")
    jfile = SCRATCH / "crop.txt"
    jfile.write_text("data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode())
    ifile = SCRATCH / "ids.json"
    ifile.write_text(json.dumps(ids))

    p = subprocess.run(["node", str(ROOT / "tools" / "app_full_hold_driver.mjs"), BASE,
                        me["id"], me["token"], str(ifile), str(jfile)],
                       capture_output=True, text=True, timeout=180)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    out = json.loads(lines[-1]) if lines else {"error": p.stdout[-800:] + p.stderr[-800:]}
    check("driver ran", "error" not in out, out.get("error", ""))
    print("   ", json.dumps(out))

    check("the page adopted the camera key", out.get("node") == me["id"], str(out.get("node")))
    check("all five crops held on the device",
          out.get("held_before") == sorted(ids.values()), str(out.get("held_before")))
    after = {i: k for i, k in (out.get("after_sync") or [])}
    check("published two days later -> uploaded",
          any(s == ids["published"] for s, _ in uploads), str(uploads))
    check("the uploaded one leaves the phone", ids["published"] not in after, str(after))
    check("hub records the clear photo",
          (db.sighting(ids["published"]) or {}).get("snap_full") == "full_test.jpg")
    check("waiting in the review pen -> kept, marked keep", after.get(ids["pen"]) is True,
          str(after))
    check("still in the inbox -> kept", after.get(ids["inbox"]) is True, str(after))
    check("decided traffic past the grace period -> deleted", ids["decided"] not in after,
          str(after))
    check("a fresh crop inside the grace period -> still held", ids["fresh"] in after,
          str(after))
    again = {i: k for i, k in (out.get("after_reload") or [])}
    check("kept crops survive a page reload", again.get(ids["pen"]) is True, str(again))
    srv.shutdown()
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
