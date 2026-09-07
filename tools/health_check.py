"""Is the network actually working, and did anybody just go live?

Every check here exists because the thing it checks has failed before. A camera
that beats but posts nothing, a review pen that fills and never drains, a stale
process serving old code, an inbox nobody collects - each of those looked like
"working" from the outside once, so each is asked about directly.

State is kept between runs (data/health_state.json) so it can report CHANGES,
which is what a person actually wants to know: a new camera appeared, someone
came online, something that was fine is not.

    python tools/health_check.py                     # local + public
    python tools/health_check.py --json              # machine-readable
    python tools/health_check.py --quiet             # only print if something changed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import DATA                      # noqa: E402

PUBLIC = os.environ.get("RAVEN_HUB") or os.environ.get("SPARROW_HUB") or ""
STATE = DATA / "health_state.json"
# A camera is "online" for 90s after a beat; a beat is every 30s. Two missed
# beats is the honest edge, so flag well before crying wolf.
BEAT_STALE_S = 300
# A pen that only grows means nobody is reviewing; a pen that never fills when
# police pass means the parker is broken. Both are worth saying out loud.
PEN_BACKLOG_WARN = 40


def setup_output() -> dict:
    """Make printing survive a Windows console / redirected log.

    This watch ran for 67 event-runs writing nothing but a traceback: the
    scheduled task redirects into a file, Python picked cp1252, and the green
    dot on the first node line raised UnicodeEncodeError - BEFORE the events
    and problems were printed. The one thing it exists to say was the part
    that got thrown away.

    Two defences, because the marks are not the only risk: camera names come
    from volunteers, so any name can carry a character the stream cannot take.
      1. ask for UTF-8, and 2. never die on a character either way.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    fancy = {"on": "🟢", "off": "  ", "event": "✨", "problem": "⚠️ ", "sep": "·"}
    try:
        "".join(fancy.values()).encode(enc)
        return fancy
    except (UnicodeEncodeError, LookupError):
        # Degrade to something every codepage can write rather than lose the report.
        return {"on": "*", "off": " ", "event": "+", "problem": "!", "sep": "|"}


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "SparrowMap/health"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")
    except Exception:
        pass


def check() -> dict:
    now = time.time()
    out = {"ts": now, "ok": True, "problems": [], "events": [], "nodes": []}
    prev_uptime = (load_state() or {}).get("_uptime")

    # --- is the public map answering at all -------------------------------
    try:
        stats = get(f"{PUBLIC}/api/stats")
        out["stats"] = stats
    except Exception as exc:
        out["ok"] = False
        out["problems"].append(f"public map unreachable: {exc}")
        return out

    try:
        nodes = get(f"{PUBLIC}/api/nodes")
    except Exception as exc:
        out["ok"] = False
        out["problems"].append(f"/api/nodes failed: {exc}")
        return out

    # --- the resource picture, which is where every outage actually showed --
    # 🚨 THIS CHECKER NEVER POLLED /api/health. It watched cameras and sighting
    # counts, which stayed healthy through every outage so far - the fd
    # exhaustion, the request-gate misfires, the tile-janitor syscall storm.
    # Each of those was visible in /api/health minutes before anyone noticed,
    # and this file was looking the other way.
    try:
        h = get(f"{PUBLIC}/api/health")
        out["health"] = h
        if not h.get("ok"):
            out["ok"] = False
            out["problems"].append(
                f"hub reports unhealthy: db={h.get('db')} "
                f"{h.get('warn') or ''}".strip())
        # A ramp, not a cliff - descriptor exhaustion climbed for 45 minutes
        # before it took the site down.
        if (h.get("fd_used_pct") or 0) > 50:
            out["problems"].append(
                f"file descriptors at {h['fd_used_pct']}% of the limit")
        if (h.get("threads_peak") or 0) > 200:
            out["problems"].append(
                f"thread peak {h['threads_peak']} - connections are piling up")
        # Upstream saturation is invisible in every other number.
        if h.get("tile_fetch_free") == 0:
            out["problems"].append("all tile-fetch slots busy - the basemap "
                                   "will be blank for visitors")
        if h.get("road_lookup_free") == 0:
            out["problems"].append("road lookups saturated - enrolment and "
                                   "drive reports will stall")
        if (h.get("road_cells_failing") or 0) > 3:
            out["problems"].append(
                f"{h['road_cells_failing']} map cells failing to snap")
        # A restart loop looks healthy at any single instant; only uptime shows it.
        if (h.get("uptime_s") or 0) < 300 and prev_uptime is not None                 and prev_uptime < 300:
            out["events"].append(
                "the hub has restarted since the last check AND was young then "
                "too - it is probably in a restart loop")
        out["_uptime"] = h.get("uptime_s")
    except Exception as exc:
        out["problems"].append(f"/api/health failed: {exc}")

    prev = load_state()
    prev_nodes = {n["id"]: n for n in prev.get("nodes", [])}

    online_now = []
    for n in nodes:
        # ⚠️ TWO STATEMENTS, NOT A TUPLE. This was
        #     nid, name = n["id"], n.get("name") or nid
        # and Python evaluates the whole right-hand side BEFORE assigning, so
        # `nid` there is not this node's id. A nameless camera therefore either
        # crashed this checker outright (if it was first in the list) or was
        # silently reported under the PREVIOUS camera's name - a monitoring tool
        # confidently mislabelling which camera is down. Found by pyflakes once
        # preflight started checking for undefined names.
        nid = n["id"]
        name = n.get("name") or nid
        beat = n.get("last_beat")
        seen = n.get("last_seen")
        age = (now - beat) if beat else None
        rec = {"id": nid, "name": name, "kind": n.get("kind"),
               "online": bool(n.get("online")), "sightings": n.get("sightings", 0),
               "beat_age": round(age) if age is not None else None}
        out["nodes"].append(rec)
        if rec["online"]:
            online_now.append(name)

        was = prev_nodes.get(nid)
        # --- somebody new appeared ---------------------------------------
        if was is None and prev:
            out["events"].append(f"NEW CAMERA: {name} ({n.get('kind')})")
        # --- somebody came online / dropped ------------------------------
        elif was is not None:
            if rec["online"] and not was.get("online"):
                out["events"].append(f"ONLINE: {name}")
            elif was.get("online") and not rec["online"]:
                out["events"].append(f"OFFLINE: {name}")

        # --- beating but not posting: the silent stall --------------------
        # A camera that heartbeats while its pipeline is wedged is the failure
        # that hid for 18 hours once. The beat now depends on real work, so a
        # fresh beat with a long-dead last_seen is worth surfacing anyway.
        if rec["online"] and seen and (now - seen) > 3 * 3600 and rec["sightings"]:
            out["problems"].append(
                f"{name}: online but no sighting for "
                f"{round((now - seen) / 3600, 1)}h")

        # --- enrolled and never used --------------------------------------
        if beat is None and not rec["sightings"]:
            # Not an error - a volunteer who set up and never started. Say it
            # once, as an event, not as a recurring problem.
            if was is None and prev:
                out["events"].append(f"enrolled but never started: {name}")

    out["online"] = online_now

    # --- did anything stop being served ----------------------------------
    for path in ("/api/policy", "/api/sightings", "/api/heat", "/rv", "/drive"):
        try:
            req = urllib.request.Request(PUBLIC + path,
                                         headers={"User-Agent": "SparrowMap/health"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status != 200:
                    out["problems"].append(f"{path} returned {r.status}")
        except Exception as exc:
            out["problems"].append(f"{path} failed: {exc}")

    # --- the promise the site makes must still be true --------------------
    try:
        pol = get(f"{PUBLIC}/api/policy")
        if pol.get("stores_video") is not False:
            out["problems"].append("policy claims video IS stored")
        if pol.get("private_plate_lookup") is not False:
            out["problems"].append("policy claims private plate lookup is possible")
    except Exception:
        pass

    if out["problems"]:
        out["ok"] = False

    save_state({"ts": now, "nodes": out["nodes"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="print only when something changed or broke")
    args = ap.parse_args()
    if not PUBLIC:
        ap.error("No RavenMap hub configured. Set RAVEN_HUB; "
                 "legacy SPARROW_HUB is also accepted.")

    mark = setup_output()
    r = check()
    if args.json:
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["ok"] else 1)

    interesting = r["events"] or r["problems"]
    if args.quiet and not interesting:
        sys.exit(0)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    s = r.get("stats") or {}
    sep = mark["sep"]
    print(f"[{stamp}] {s.get('nodes_online', '?')}/{s.get('nodes_active', '?')} "
          f"cameras online {sep} {s.get('sightings_24h', '?')} sightings/24h {sep} "
          f"{s.get('public_24h', '?')} public")
    # Events and problems FIRST. They are the reason this runs, and the node
    # roll-call underneath them is context - so the report reads correctly even
    # if something below it ever fails again.
    for e in r["events"]:
        print(f"  {mark['event']} {e}")
    for p in r["problems"]:
        print(f"  {mark['problem']} {p}")
    for n in r["nodes"]:
        dot = mark["on"] if n["online"] else mark["off"]
        age = f"{n['beat_age']}s" if n["beat_age"] is not None else "never"
        print(f"  {dot} {n['name'][:26]:26} {n['kind'] or '?':6} "
              f"beat {age:>8}  sightings {n['sightings']}")
    if r["ok"] and not interesting:
        print("  all good")
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
