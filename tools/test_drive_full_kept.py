"""A drive phone's good photo must still reach a sighting confirmed hours later.

    python tools\\test_drive_full_kept.py

2026-10-11: sixteen drive-mode patrol cars were confirmed on /rv ~9.5 h after
the drive. The hub only asks for the full picture for an hour
(db.FULLRES_WINDOW_S), so all sixteen kept the plate-redacted preview. The
phone now keeps its crops on the device and names them (`held`) on
/api/node/parked; the hub answers with the published ones only. Runs a real
hub on a SCRATCH data dir.
"""

from __future__ import annotations

import base64
import io
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

SCRATCH = Path(tempfile.mkdtemp(prefix="sparrow_heldfull_")).resolve()
os.environ["SPARROW_DATA"] = str(SCRATCH)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import db   # noqa: E402
import hub  # noqa: E402

assert str(db.DB_PATH).startswith(str(SCRATCH)), db.DB_PATH
import review_api  # noqa: E402

# Storing the photo isolates the vehicle with a model, which is slow to load
# and covered by test_fullres_on_confirm. This tests who gets ASKED.
review_api.attach_confirmed_photo = lambda sid, row, crop, **k: "full_test.jpg"
PORT = 8794
BASE = f"http://127.0.0.1:{PORT}"
UA = "SparrowMap-test/1.0"
fails = 0


def jpeg(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (90, 120, 160)).save(b, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


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

    AT = (10.0, 20.0)                     # synthetic, never a real place
    nodes = {}
    for who in ("me", "other"):
        st, r = call("/api/enroll", {"name": f"t-{who}", "kind": "mobile",
                                     "lat": AT[0], "lon": AT[1]})
        nodes[who] = r
        check(f"enrol {who}", st == 200 and r.get("id"), f"{st} {r}")

    # Rows written straight in: posting through /api/sightings with a photo
    # loads the classifier, which is not what this tests.
    c = db.connect()
    ten_h = time.time() - 10 * 3600       # confirmed long after the drive

    def row(who, tier):
        cur = c.execute("INSERT INTO sightings(node_id, ts, tier, vclass, source, snap) "
                        "VALUES (?, ?, ?, 'police', 'phone_node', 'x.jpg')",
                        (nodes[who]["id"], ten_h, tier))
        c.commit()
        return cur.lastrowid

    pub, priv, theirs = row("me", "public"), row("me", "private"), row("other", "public")

    me = nodes["me"]
    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": []}, me["token"])
    check("without `held`, a 10 h old sighting is not asked for (old behaviour)",
          st == 200 and pub not in (r.get("want_full") or []), f"{st} {r}")

    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": [],
                                      "held": [pub, priv, theirs]}, me["token"])
    want = r.get("want_full") or []
    check("held + published + mine -> asked for", pub in want, f"{st} {r}")
    check("held but private -> never asked for", priv not in want, str(want))
    check("held but another camera's -> never asked for", theirs not in want, str(want))

    st, r = call("/api/sighting/fullres", {"node_id": me["id"], "id": pub,
                                           "snap_b64": jpeg(900, 540)}, me["token"])
    check("upload accepted", st == 200 and r.get("ok"), f"{st} {r}")
    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": [],
                                      "held": [pub]}, me["token"])
    check("and it stops being asked for", pub not in (r.get("want_full") or []), str(r))

    # 2026-10-13: `keep` - which held crops may still be published. A crop still
    # in the inbox (not yet scored) or the review pen (not yet answered) is kept;
    # a decided private one is not, and nor is anything of another camera's.
    import mirror
    assert str(mirror.INBOX).startswith(str(SCRATCH)), mirror.INBOX
    assert str(mirror.REVIEW).startswith(str(SCRATCH)), mirror.REVIEW
    mirror.INBOX.mkdir(parents=True, exist_ok=True)
    mirror.REVIEW.mkdir(parents=True, exist_ok=True)
    inbox, pen, done = row("me", "private"), row("me", "private"), row("me", "private")
    their_pen = row("other", "private")
    (mirror.INBOX / f"{inbox}.json").write_text("{}")
    (mirror.REVIEW / f"{pen}.json").write_text("{}")
    (mirror.REVIEW / f"{their_pen}.json").write_text("{}")
    pub2 = row("me", "public")

    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": [],
                                      "held": [inbox, pen, done, their_pen, pub2]},
                 me["token"])
    keep = r.get("keep")
    check("`keep` is answered when `held` is sent", isinstance(keep, list), f"{st} {r}")
    keep = keep or []
    check("still in the inbox -> keep", inbox in keep, str(keep))
    check("waiting in the review pen -> keep", pen in keep, str(keep))
    check("decided private -> not kept (phone deletes it)", done not in keep, str(keep))
    check("another camera's pen card -> not kept", their_pen not in keep, str(keep))
    check("published -> wanted, not kept", pub2 in (r.get("want_full") or [])
          and pub2 not in keep, str(r))

    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": []}, me["token"])
    check("no `held` -> no `keep` (a missing answer never means delete)",
          "keep" not in r, str(r))

    # A reviewer answers the pen card: it is published, so it becomes wanted.
    (mirror.REVIEW / f"{pen}.json").unlink()
    c.execute("UPDATE sightings SET tier='public' WHERE id=?", (pen,)); c.commit()
    st, r = call("/api/node/parked", {"node_id": me["id"], "ids": [],
                                      "held": [pen]}, me["token"])
    check("pen card confirmed days later -> asked for", pen in (r.get("want_full") or []),
          str(r))
    srv.shutdown()
finally:
    try:
        db.close_thread()
    except Exception:
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
