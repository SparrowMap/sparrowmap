"""SparrowMap hub - HTTP API, live feed and the public map.

Standard library only, on purpose. A hub that a neighbourhood can run should
not need a package index to survive, and every dependency is another thing a
volunteer has to trust. Same reasoning as the sonar build: threading HTTP
server plus Server-Sent Events, no websocket stack, no ASGI server.

Run it:      python hub.py
Then open:   http://localhost:8150/
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import socket
import sys
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import bugs
import classify
import db
import mirror
import node_label
import operator_auth
import qr
import nodes as node_mod
import privacy
import review_api
import review_auth
import send_relay
import send_push
import air_relay
import snapshot
from core import CONFIG, DATA, PUBLIC, SNAPS, is_operator_addr, now

# --------------------------------------------------------------------------
# Basemap tiles
#
# 🚨 THE MAP HAD NO BASEMAP AT ALL AND NOTHING SAID SO.
# The tile layer pointed straight at basemaps.cartocdn.com while the CSP said
# `img-src 'self' data: blob:`. Every tile was refused, so the map rendered as
# a black field with sightings floating on it - which reads as "GPS isn't
# loading", not as a security header doing its job. The header was right. The
# tile URL was the thing that disagreed with the design, and the comment above
# the CSP had already stated that design: everything this site loads, it ships.
#
# So the tiles are PROXIED rather than the CSP widened: a viewer looking at this
# map should not have
# their IP and their pan-and-zoom trail delivered to a third-party CDN. On a
# public map that is one request per tile per viewer - a far better record of
# who is interested in which street than anything SparrowMap itself stores. A
# project that jitters its own volunteers' positions should not hand that over.
#
# The cache means upstream sees each tile once, not once per viewer.
TILES = DATA / "tiles"

# US police-station points [lat, lon, name], fetched from OpenStreetMap on the
# desktop and scp'd here (the box cannot reach Overpass, same as road snapping).
# Loaded once and served bounded by viewport, so the nationwide ~15k points
# never reach a phone at once. Static data; refresh with tools/police_fetch.py.
_POLICE_CACHE = None
def _police_stations():
    global _POLICE_CACHE
    if _POLICE_CACHE is None:
        try:
            _POLICE_CACHE = json.loads(
                (DATA / "police_stations.json").read_text(encoding="utf-8"))
        except Exception:
            _POLICE_CACHE = []
    return _POLICE_CACHE

# Flock/ALPR surveillance cameras from OpenStreetMap (the DeFlock dataset),
# fetched on the desktop and scp'd here. Each row is [osm_id, lat, lon, dir].
# Served bounded by viewport at /api/cameras, minus the ones confirmed removed.
_CAMERAS_CACHE = None
def _surveillance_cameras():
    global _CAMERAS_CACHE
    if _CAMERAS_CACHE is None:
        try:
            _CAMERAS_CACHE = json.loads(
                (DATA / "surveillance_cameras.json").read_text(encoding="utf-8"))
        except Exception:
            _CAMERAS_CACHE = []
    return _CAMERAS_CACHE

# osm_ids of cameras a review has confirmed REMOVED (a city took the camera
# down), so the map stops asserting a camera that is not there any more. Curated
# from the public /api/camera/report queue - a report never hides a camera by
# itself. Re-read each call (it is tiny) so a new verdict shows without a
# restart; the camera list itself is big and static, so that stays cached.
def _cameras_removed():
    try:
        return set(json.loads(
            (DATA / "cameras_removed.json").read_text(encoding="utf-8")))
    except Exception:
        return set()

# osm_ids an RF-beta detection has confirmed still present (a Flock camera heard
# on WiFi next to a mapped one). Written by tools/camera_rf_confirm.py; re-read
# each call because it is small and changes as the network reports in.
def _cameras_confirmed():
    try:
        return set(json.loads(
            (DATA / "cameras_confirmed.json").read_text(encoding="utf-8")))
    except Exception:
        return set()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Used to prove a camera report was filed
    while standing at the camera."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# RADAR / speed-trap layer (beta). A police speed-radar emission is physical
# evidence, unlike the tap-a-coordinate "patrol here" that was withdrawn on
# 2026-08-15 (see /api/drive/report) - so a hit MUST come from real detector
# hardware, authenticated with a node token, never an anonymous tap. Hits are
# ephemeral live signals: kept in memory, decayed in minutes, clustered on read
# into "a radar source is active near here" dots whose confidence follows the
# BAND. Ka (33-35 GHz) is essentially police-only; K (24 GHz) now fires on cars'
# blind-spot radar and X on automatic doors, so those need corroboration.
_radar_hits = []                       # [{ts,lat,lon,heading,band,strength,node}]
_radar_lock = threading.Lock()
RADAR_TTL_S = 300.0                    # a trap the network stops seeing fades in 5 min
RADAR_CLUSTER_M = 250.0               # hits this close, same band = one source
# Base trust per band. A lone Ka hit is already meaningful; K/X are not, and only
# climb when independent reporters agree.
RADAR_BAND_BASE = {"ka": 0.70, "laser": 0.85, "k": 0.30, "x": 0.18}


def _radar_add(lat, lon, band, heading, strength, node):
    now = time.time()
    with _radar_lock:
        _radar_hits.append({"ts": now, "lat": lat, "lon": lon, "band": band,
                            "heading": heading, "strength": strength, "node": node})
        # Prune here so the list never grows unbounded between reads.
        cut = now - RADAR_TTL_S
        _radar_hits[:] = [h for h in _radar_hits if h["ts"] >= cut]


def _radar_dots(box=None):
    """Cluster live hits into active radar sources. Each dot is the reporter-
    weighted centre of a cluster (more independent reporters -> the circles
    overlap and the centre tightens onto the real emitter), with a confidence
    from the band, the number of DISTINCT reporters, and how fresh it is."""
    now = time.time()
    cut = now - RADAR_TTL_S
    with _radar_lock:
        hits = [dict(h) for h in _radar_hits if h["ts"] >= cut]
    if box:
        try:
            s, w, n, e = box
            hits = [h for h in hits if s <= h["lat"] <= n and w <= h["lon"] <= e]
        except (TypeError, ValueError):
            pass
    dots = []
    for h in hits:
        placed = False
        for d in dots:
            if (h["band"] == d["band"]
                    and _haversine_m(h["lat"], h["lon"], d["lat"], d["lon"])
                    <= RADAR_CLUSTER_M):
                d["_hits"].append(h)
                # Running reporter-weighted centroid.
                k = len(d["_hits"])
                d["lat"] += (h["lat"] - d["lat"]) / k
                d["lon"] += (h["lon"] - d["lon"]) / k
                placed = True
                break
        if not placed:
            dots.append({"lat": h["lat"], "lon": h["lon"], "band": h["band"],
                         "_hits": [h]})
    out = []
    for d in dots:
        hs = d["_hits"]
        reporters = len({h["node"] for h in hs})
        newest = max(h["ts"] for h in hs)
        age = int(now - newest)
        base = RADAR_BAND_BASE.get(d["band"], 0.2)
        conf = min(0.98, base + 0.15 * (reporters - 1))
        conf *= max(0.25, 1.0 - age / RADAR_TTL_S)     # fades as it goes stale
        out.append({"lat": round(d["lat"], 5), "lon": round(d["lon"], 5),
                    "band": d["band"], "conf": round(conf, 2),
                    "reporters": reporters, "age": age})
    out.sort(key=lambda x: -x["conf"])
    return out


# ---------------------------------------------------------------------------
# Generic live-sensor layer for the OTHER hardware feeds - drones (Remote ID)
# and police-radio activity. Same shape as radar: ephemeral, node-token-written,
# publicly read, decayed on read. Kept separate from radar so that working code
# is untouched. Each event carries its kind; TTL and dedup differ per kind:
#   drone  a moving aircraft - short life, deduped by its Remote ID so a trail
#          collapses to one current position.
#   radio  activity heard AT a receiving node - lingers, deduped by node so one
#          scanner is one pulse, not a stream of them.
_live_events = []               # {ts,kind,lat,lon,label,key,node}
_live_lock = threading.Lock()
LIVE_TTL = {"drone": 180.0, "radio": 300.0}
LIVE_KINDS = set(LIVE_TTL)


def _live_add(kind, lat, lon, label, key, node):
    now = time.time()
    with _live_lock:
        # Replace any prior event with the same (kind,key) so a moving drone or a
        # chatty scanner is one point, not hundreds.
        _live_events[:] = [e for e in _live_events
                           if not (e["kind"] == kind and e["key"] == key)]
        _live_events.append({"ts": now, "kind": kind, "lat": lat, "lon": lon,
                             "label": label, "key": key, "node": node})
        # Global prune by each kind's TTL.
        _live_events[:] = [e for e in _live_events
                           if now - e["ts"] < LIVE_TTL.get(e["kind"], 300.0)]


def _live_points(kind, box=None):
    now = time.time()
    ttl = LIVE_TTL.get(kind, 300.0)
    with _live_lock:
        evs = [dict(e) for e in _live_events
               if e["kind"] == kind and now - e["ts"] < ttl]
    if box:
        try:
            s, w, n, e = box
            evs = [x for x in evs if s <= x["lat"] <= n and w <= x["lon"] <= e]
        except (TypeError, ValueError):
            pass
    out = []
    for x in evs:
        out.append({"lat": round(x["lat"], 5), "lon": round(x["lon"], 5),
                    "label": x["label"] or "", "age": int(now - x["ts"])})
    out.sort(key=lambda z: z["age"])
    return out


TILE_UPSTREAM = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
TILE_SUBDOMAINS = "abcd"
# When Carto rate-limits our single proxy IP it returns an "API KEY REQUIRED"
# placeholder PNG with HTTP 200. It is one fixed image, so we recognise it by
# hash and NEVER cache it - caching pinned "API KEY REQUIRED" across the map for
# the 7-day max-age. Add new hashes here if Carto changes the placeholder.
TILE_PLACEHOLDER_MD5 = {"0434b598b6a4b3bf56bbc8c295046381"}
TILE_MAX_ZOOM = 20
# Tiles are ~5-15 KB. 20k of them is a few hundred MB and covers a town at
# every zoom a viewer will use.
TILE_CACHE_MAX = 20000
_tile_count = None


_tile_prune_lock = threading.Lock()


def _tile_prune() -> None:
    """Keep the tile cache bounded, without stat-ing the tree on every hit.

    The count is held in memory and only recounted when it is unknown (first
    write after start) or when the cap is reached. Walking the cache on every
    tile would turn a 2 ms disk read into a directory crawl at exactly the
    moment a viewer is dragging the map.

    🚨 THAT IS EXACTLY WHAT IT DID, AND IT MELTED THE BOX.
    Two faults, and they compounded. The prune walked and STAT-ED the whole
    tree - sorted(rglob(...), key=st_mtime) over 20,000 files - and it ran
    under no lock, so every tile thread that arrived while the cache sat at
    capacity started its own full walk. Then it set the count back to None, so
    the next write recounted the tree as well.

    Measured mid-incident: the hub at 147% CPU with ONE request in flight, the
    CPU 60% SYSTEM time (that is the stat() storm, not computation), load above
    90, and the map timing out. Purging the CDN cache is what set it off: a cold
    edge turns every tile into a MISS, every MISS into a write, and every write
    into two directory crawls.

    ⚠️ A CACHE JANITOR MUST NEVER COST MORE THAN THE CACHE SAVES. One thread
    prunes and the rest carry on, the count is corrected in place instead of
    being thrown away, and the pruning is amortised - it drops to a low-water
    mark so the next few thousand writes cost nothing at all.
    """
    global _tile_count
    with _tile_prune_lock:
        if _tile_count is None:
            _tile_count = sum(1 for _ in TILES.rglob("*.png"))
        else:
            _tile_count += 1
        if _tile_count <= TILE_CACHE_MAX:
            return

    # Only ONE pruner. Everyone else returns immediately and keeps serving:
    # being slightly over the cap for a few seconds costs nothing, while a
    # dozen concurrent tree walks costs the whole machine.
    if not _tile_prune_lock.acquire(blocking=False):
        return
    try:
        # Oldest first. Tiles are interchangeable and cheap to refetch, so there
        # is no cleverness to buy here - unlike the crop bank, which prunes by
        # whole DAYS because dropping the oldest crops first would bias the
        # training set toward one time of day.
        files = sorted(TILES.rglob("*.png"), key=lambda f: f.stat().st_mtime)
        # Down to a LOW-WATER MARK, not to the cap. Trimming to exactly the cap
        # leaves the next write over it again, which is how one expensive walk
        # became an expensive walk per tile.
        target = max(0, len(files) - int(TILE_CACHE_MAX * 0.8))
        removed = 0
        for f in files[:target]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        # Corrected in place. Setting it to None forced a full recount on the
        # very next write - a second crawl for every prune.
        _tile_count = len(files) - removed
    finally:
        _tile_prune_lock.release()

VERSION = "0.1.0"

# High-water marks for /api/health, and when this process started. Kept in
# memory on purpose: they describe THIS process, they cost nothing, and a file
# would need permissions the hub should not want. They reset on restart, which
# is why uptime_s is published beside them - peaks without a window are a
# number pretending to be a measurement.
_STARTED = time.time()
_PEAK = {"fd_pct": 0.0, "threads": 0}

# How many requests may be BEING SERVED at once. See Handler._INFLIGHT for why
# this counts requests rather than connections - the two earlier attempts
# counted connections and locked real visitors out of an idle box twice.
#
# 🚨 RAISED FROM 32 AFTER IT REFUSED REAL VISITORS A THIRD TIME, AND THE NUMBER
# IS NOT THE LESSON. Three times now this gate has been set against a quantity I
# had not measured: connections instead of requests, then 250 connections when
# the steady state was already 32-64, then 32 requests while tile proxying held
# permits for fifteen seconds each. Every time, the symptom was a 503 on an idle
# box and every time I reasoned about the number instead of instrumenting it.
#
# So this is deliberately loose enough that it cannot be the thing that breaks
# the site, and /api/health now PUBLISHES how many permits are in use and which
# paths hold them (see _INFLIGHT_PATHS). Tune it from that, never from argument.
# The window `since` timestamps are rounded to for caching. Must match
# CACHE_BUCKET_S in public/app.js: the frontend rounds so its polls share a URL,
# and the server rounds so a client that DOESN'T round cannot mint a new cache
# key per request. Protection that depends on the client cooperating is not
# protection.
CACHE_BUCKET_S = 4

MAX_REQUESTS = 200

# 🚨 A SECOND CAP, BECAUSE MAX_REQUESTS COUNTS THE WRONG NOUN AND THE KERNEL
# KILLED US FOR IT.
#
# MAX_REQUESTS bounds how many requests may run at once. It says nothing about
# how BIG they are, and that was fine while the largest answer on this server
# was ~90 kB. Then the traffic cameras landed and /api/nodes became 3.4 MB of
# JSON, built from a list of dicts that costs several times that again while it
# is being serialised. 200 permits therefore authorised something like 2-3 GB
# of simultaneous allocation on a 3.8 GB box, and on 2026-08-16 the OOM killer
# took the hub FOUR TIMES while every health check still answered 200, because
# systemd restarted it within seconds each time.
#
# So the heavy routes - the ones that materialise the whole map - get their own
# much smaller permit pool. Eight is not a memory limit dressed up as a number:
# there are two cores and a GIL, so more than a handful of simultaneous builds
# buys no throughput whatsoever, it only buys peak memory.
#
# ⚠️ ADD A ROUTE HERE THE DAY ITS ANSWER GETS BIG, not the day it falls over.
# The test is the size of the body, not how often it is called.
HEAVY_ROUTES = frozenset({"/api/nodes", "/api/sightings"})
# ⚠️ RAISED 8 -> 12 ON MEASUREMENT. At 8, a cache-missing /api/nodes measured
# 12.7s during a poll burst - it was queueing, not computing (heavy_free was 0).
# 12 concurrent builds is ~180 MB of peak, affordable against the 620 MB the hub
# now sits at, and a reader waiting 12s for the map is the failure this whole
# exercise was meant to prevent.
#
# ⚠️ RAISED 12 -> 48 ON 2026-08-18, WHEN THE MACHINE UNDERNEATH CHANGED.
# Both halves of the reasoning above were properties of the OLD box, and the
# map moved: 2 cores and 3.8 GB became 8 cores and 31 GB. "There are two cores
# and a GIL" is simply no longer true, and 180 MB of peak was frightening
# against 3.8 GB in a way it is not against 31 GB with 27 GB free.
#
# The measurement that forced it: with 12 permits the pool sat at heavy_free=0
# and readers got 503s - the map showed "reconnecting" and only aircraft. It
# was not computing, it was queueing again, exactly as at 8.
#
# 🚨 THE PERMIT IS HELD ACROSS THE SINGLE-FLIGHT WAIT, NOT JUST THE BUILD.
# That is why a bigger pool is the fix rather than a faster build. When a
# cache-missing /api/nodes takes 15.6s cold, the leader builds and every
# follower BLOCKS holding a heavy permit of its own, so one slow build pins
# the entire pool even though only one build is running. The pool therefore
# has to be sized for concurrent READERS of a cold key, not for concurrent
# builds. 48 is ~720 MB of worst-case peak against 27 GB free.
#
# The better fix is to take the permit around the build alone and let
# followers wait outside the pool. That is a change to the dispatch path of a
# live site and it is not a thing to do at speed; this is the safe half.
MAX_HEAVY = 48

# How long a heavy request waits for a permit before giving up. Long enough to
# outlast the leader it is almost certainly queued behind (a build is well
# under a second), short enough that a wedged leader cannot pile up a queue
# that outlives the reader's patience.
HEAVY_WAIT_S = 12.0

# 🚨 INGEST MUST NEVER BE ABLE TO SPEND EVERY PERMIT, AND ON 2026-08-16 IT DID.
#
# The two camera boxes poll with 96 and 80 workers, so they can open ~176
# simultaneous POSTs into a server holding 200 permits. Add readers and the
# pool is gone: the origin answered "busy - too many requests in flight" to
# EVERYBODY, and the only reason the map stayed up is that Cloudflare fell back
# to serving stale copies. Stopping the two pollers took the origin from
# refusing everything to inflight 1/200 within seconds, which is the measurement
# that identified this rather than the reader traffic I first blamed.
#
# Readers are the point of the site and cameras are replaceable - a pass missed
# now is re-read on the next cycle. So ingest gets a minority of the pool and
# readers keep the rest, permanently.
# 🚨 ENROLMENT IS NOT INGEST, AND PUTTING IT HERE REFUSED A REAL PERSON.
# /api/enroll was in this pool, so somebody registering a camera queued behind
# 176 poller workers and got "busy - too many requests in flight". Measured at
# the time: ingest 0/40 saturated while the general pool sat at 55/200 - there
# was plenty of capacity, just not in the bucket a human had been put in.
#
# A person signing up is the single most valuable request this server handles
# and it happens a few times an hour. It belongs in the general pool, where the
# only thing that can refuse it is the box genuinely being full.
INGEST_ROUTES = frozenset({"POST /api/sightings", "POST /api/heartbeat/bulk"})
# ⚠️ LOWERED 60 -> 40 for the same measurement. Ingest held all 60 slots through
# the burst while a reader waited. Ingest is not latency-sensitive - it queues,
# and a missed pass is re-read on the next sweep - so it is the side that should
# give way. This is the priority stated above, applied to a real number.
#
# ⚠️ RAISED 40 -> 96 ON 2026-08-19, AND THE PRIORITY ABOVE IS UNCHANGED.
# 40 was chosen on the 2-core box, where ingest and readers genuinely competed
# for the same scarce pool. On the machine the map runs on now they do not.
#
# Measured over 40 minutes across several publish bursts: the camera fleet had
# 48.6% of its posts REFUSED - bursts of 700-917 in a single minute - while
# `ingest_free` sat at 0 of 40 and `inflight` peaked at 48 of 200 with
# `heavy_free` at 42-48 of 48. Ingest was starving while 150 general permits sat
# idle and no reader was waiting for anything.
#
# A refused post is not a delayed post: there is no node outbox, so the camera
# drops that sighting on the floor. Half the fleet's work was being thrown away
# to protect readers from a contention that was not happening.
#
# 🚨 AND REVERTED, WITHIN THE HOUR, BECAUSE IT HURT READERS BADLY.
# At 96 the refusals did fall, and inflight rose from a 48 peak to 105-114 -
# and READER LATENCY COLLAPSED: 5 of 12 samples of /api/sightings took 19 to 34
# SECONDS, against 0.4s when ingest was idle in the same run.
#
# The mistake was reading "150 permits are free" as "there is spare capacity".
# Permits were never the scarce thing. CPU behind them is. Letting 96 posts
# decode, classify and write at once starves the readers sharing that CPU, so
# the free permits were free precisely BECAUSE ingest was capped, not evidence
# that the cap was unnecessary.
#
# The 40 above is therefore correct on this box too, and the ~48% refusal rate
# it causes is the intended trade rather than a bug: a refused pass is re-read
# next cycle, a reader waiting 34 seconds is the failure this whole file exists
# to prevent. Do not raise this again without measuring READER latency during a
# publish burst - the ingest and inflight numbers alone will mislead you exactly
# as they misled me.
MAX_INGEST = 40

# ⚠️ INGEST QUEUES RATHER THAN BEING REFUSED, because there is still no node
# outbox: a node whose POST is refused DROPS that sighting on the floor. Waiting
# a few seconds costs a camera nothing and saves the reading.
INGEST_WAIT_S = 8.0

# The most cameras one /api/heartbeat/bulk request may beat. Sized against
# MAX_BODY rather than taste: an entry is a node id and a token, ~80 bytes of
# JSON, so a thousand is ~80 KB and comfortably inside the body cap. The
# 4,400-camera fleet therefore arrives as five requests instead of 4,400, and
# a caller cannot make one request cost unbounded work.
BULK_BEAT_MAX = 1000

# How coarsely a viewport box is snapped before it is used as a cache key or a
# filter.
#
# 🚨 THIS IS A CACHE-KEY CARDINALITY KNOB, NOT AN ACCURACY KNOB, AND 0.1 COST
# US THE BOX. At ~11 km, two people looking at the same city from slightly
# different scroll positions produced DIFFERENT keys, so the single-flight
# below collapsed almost nothing and the edge missed almost every time. During
# the 2026-08-16 spike that meant a crowd of readers each triggered their own
# 3.4 MB build. At ~55 km a whole city is one key, so a thousand readers of the
# same place become one build and 999 cache hits - which is the entire point of
# having a key at all.
#
# The cost is that the superset returned is larger, so a phone viewport carries
# some points just off its edges. That is invisible on a map and cheap; the
# alternative was measured and it was an out-of-memory kill.
BOX_SNAP = 0.5


def _snap_box(raw: str) -> str:
    """`S,W,N,E` snapped OUTWARD to the BOX_SNAP grid, or "" if unparseable.

    🚨 OUTWARD, ALWAYS. This string is both the cache key and the filter, so
    the snapped box must CONTAIN the caller's box - never merely approximate
    it. Round-to-nearest would shrink some of them, and the symptom is rows
    quietly missing at the edge of a viewport, only for whoever did not happen
    to be the request that populated the cache.
    """
    import math
    s, w, n, e = (float(x) for x in raw.split(","))
    return "%g,%g,%g,%g" % (
        math.floor(s / BOX_SNAP) * BOX_SNAP, math.floor(w / BOX_SNAP) * BOX_SNAP,
        math.ceil(n / BOX_SNAP) * BOX_SNAP, math.ceil(e / BOX_SNAP) * BOX_SNAP)


# ---------------------------------------------------------------------------
# Live feed
# ---------------------------------------------------------------------------

class Feed:
    """Fan a sighting out to every connected browser."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, rec: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(rec)
            except queue.Full:
                pass        # a stalled browser must never block the network


FEED = Feed()

# ---------------------------------------------------------------------------
# Rate limiting
#
# The two routes a stranger can write to - enrolment and sighting submission -
# had no limit at all. On a private tailnet that is fine; on sparrowmap.com it
# is an invitation to fill the database overnight. Deliberately crude: a fixed
# window per address, in memory, no dependency. It will not stop a distributed
# flood, and it is not trying to - it stops the trivial script, which is the
# actual threat to a small project on day one.
# ---------------------------------------------------------------------------

_HITS: dict = {}
_HIT_LOCK = threading.Lock()
# Per-IP request budgets: (count, window_seconds).
# /api/tile is here because each hit triggers one UPSTREAM fetch that holds a
# worker thread up to 15s - an anonymous amplification lever. A human panning
# the map pulls tens of tiles a minute; 600/5min is generous for that and still
# caps a scraper walking the whole tile pyramid.
# 🚨 EVERY LIMIT HERE IS GLOBAL, NOT PER-VISITOR, AND THE NUMBERS MUST REFLECT
# THAT. Caddy proxies this hub with `header_up -X-Forwarded-For` - the client
# IP is stripped on purpose, and correctly, because a hub that never learns
# visitor addresses cannot leak them. The consequence is that client_ip is
# 127.0.0.1 for everyone on earth, so these buckets are shared by the whole
# network.
#
# /api/enroll sat at 5/hour, written as a per-person guard. It behaved as a cap
# of five new cameras per hour ACROSS THE ENTIRE PROJECT. During the wave that
# followed the video, one person registering five cameras in a single minute
# locked out every other volunteer for the rest of the hour - and the message
# blamed their address, so nobody could tell. He hit it himself trying to place
# the only camera in an area.
#
# Sized as what it actually is: a flood guard against a script, not a guard
# against a person. A runaway loop still gets stopped; a viral hour does not.
# ⚠️ `/api/sightings` IS NOW PER CAMERA (see rate_ok's `who`), so this number
# changed meaning: it is what ONE camera may post in an hour, not what the
# whole project may. A busy street node runs a few hundred passes an hour, so
# 900 is generous for a real camera and still stops a runaway loop.
# `_all_sightings` is the network-wide ceiling that used to be the only bucket.
# Sized from measurement rather than habit: the busiest hour on the live box
# carried 1,267 sightings, so a global cap of 600 was already refusing real
# work. 20,000/hour is roughly 5.5 a second, which is about what 2 vCPUs will
# ingest before the tile path starts to suffer.
# 🚨 RAISED 2026-08-15 DURING A SECOND VIRAL WAVE, BEFORE THEY BIT.
#
# Both of these are GLOBAL buckets - client_ip is 127.0.0.1 for everyone, see
# the note above - so they are not "per person" in any sense. Measured while a
# post from a large account was spreading: 15 new cameras in the busiest hour
# against a cap of 120, and 168 in a day.
#
# 120/hour is 2 a minute for the entire project. That is fine on an ordinary
# day and it is the exact thing that failed last time: "one person registering
# five cameras in a single minute locked out every other volunteer for the rest
# of the hour". A wave eight times the current rate is not a stretch when a
# verified account posts, and the cost of being wrong is asymmetric - a
# volunteer who cannot register during the one hour they were motivated does
# not come back, whereas a few junk rows are swept.
#
# 600/hour still stops a runaway script (a loop managing ten a minute sustained
# is refused) and no longer refuses a crowd.
RATE = {"/api/enroll": (600, 3600), "/api/sightings": (900, 3600),
        "_all_sightings": (20000, 3600),
        # RF scans post a whole batch of nearby surveillance devices at once,
        # not one row at a time, so the per-node ceiling is lower than sightings.
        "/api/rf": (300, 3600),
        "/api/drive/report": (40, 3600), "/api/drive/vote": (120, 3600),
        # 🚨 TILES: 600/300s WAS 2 A SECOND FOR THE WHOLE WORLD.
        # One map load pulls roughly twenty tiles, so that bucket allowed about
        # six people to open the map per minute before the rest got 429s and a
        # grey rectangle. Cloudflare hides this most of the time - tiles are
        # cached for seven days and measured HIT today - but a viral wave is
        # precisely the case it does NOT hide: everybody arrives looking at a
        # DIFFERENT street, and a first look at a new area is a cache miss by
        # definition.
        # The real protection for the upstream tile CDN is _TILE_FETCH, the
        # semaphore that bounds concurrent origin fetches to 12. This bucket
        # only needs to stop somebody scraping the basemap, and 10/s does that
        # while leaving a crowd alone.
        "/api/tile": (3000, 300), "/api/report": (20, 3600),
        # Public camera-spot reports from the no-install /spot page. Each carries
        # a full JPEG and parks for review, so the ceiling is low: a flood is a
        # flood of pictures a human must wade through, not a map defacement.
        "/api/spot": (12, 3600),
        # Reports that an OSM camera is gone / still there. Cheap (no photo, no
        # map change without review), so a looser bucket than /spot.
        "/api/camera/report": (60, 3600),
        # Radar-detector hits from paired hardware. A detector on a highway
        # fires often, and several drivers feed at once, so the bucket is
        # generous - but every hit is node-token authenticated, so this is a
        # flood guard, not the anti-abuse control (the token is).
        "/api/radar/hit": (1200, 3600),
        # Drone / police-radio events from paired hardware. Also node-token
        # authenticated; generous bucket, same reasoning as radar.
        "/api/sensor/hit": (1200, 3600),
        # Sparrow Send (E2EE messages). All network-wide (Caddy strips IPs), so
        # these are flood guards, not per-user caps. Sending is generous (a chat
        # is bursty); fetching and challenges cheaper. The relay's own per-mailbox
        # and global queue caps are the real backstop.
        "/api/send/put": (3000, 3600),
        "/api/send/get": (3000, 3600),
        "/api/send/challenge": (3000, 3600),
        "/api/send/claim": (120, 3600),
        "/api/send/release": (120, 3600),
        "/api/send/handle": (3000, 3600),
        "/api/send/subscribe": (300, 3600),
        # The tag-routed air relay (air_relay): the internet twin of the LoRa
        # gateway, so any user of the encrypted messenger can reach any other by
        # a rotating routing tag without the hub ever naming a recipient. Puts
        # carry proof-of-work like a mailbox send; gets poll a cursor, so both
        # sides poll steadily - roomy like the mailbox routes.
        "/api/air/put": (3000, 3600),
        "/api/air/get": (6000, 3600),
        # Local ADS-B receivers pushing aircraft they see. A busy sky is a lot of
        # aircraft per push but one push every few seconds, so this is roomy.
        "/api/aircraft/ingest": (1800, 3600),
        # Network-wide (client_ip is 127.0.0.1 for everyone), so this is
        # a flood guard on the whole site rather than a per-person cap.
        # bugs.py enforces its own per-hour ceiling as well.
        "/api/bug": (120, 3600),
        # Reading back your own placement. Not brute-forceable (the token is
        # 24 random bytes), but a wrong-token loop should still cost something.
        "/api/node/me": (120, 3600),
        # Network-wide (see the note above RATE), and it protects a free
        # third-party service as much as this one.
        "/api/geocode": (300, 3600)}


# How many tile MISSES may be fetching from the upstream CDN at once.
#
# Sized against what the box is: 2 vCPUs whose tile work is almost entirely
# WAITING, so this can be well above the CPU count - but it must exist. With no
# bound at all a purged Cloudflare cache turned every tile into an origin fetch
# and hundreds ran together, which is how a map went 502 while the machine sat
# idle waiting on somebody else's network.
_TILE_FETCH = threading.Semaphore(12)


def rate_ok(path: str, ip: str, who: str = "") -> bool:
    """Is this caller allowed another request on this route?

    🚨 `who` IS THE FIX FOR A CAP THAT WAS NEVER PER-PERSON.
    Caddy strips X-Forwarded-For on purpose, so `ip` is 127.0.0.1 for every
    caller on earth and every bucket keyed on it is really ONE bucket shared by
    the whole network. That is tolerable for a cheap public route and wrong for
    the route cameras post through: measured on the live box, the busiest hour
    carried 1,267 sightings against a 600/hour "per caller" cap, and 40 requests
    were refused in a day - volunteers losing real passes, on a project with no
    node outbox to retry them.

    Pass an authenticated identity - a node id - and the bucket becomes that
    node's own. It is a BETTER key than an address as well as a working one: a
    camera keeps its identity across a reconnect, a new address, and a phone
    moving between wifi and cellular mid-drive.

    ⚠️ IT MUST BE AUTHENTICATED FIRST. Keying on an id taken straight from an
    unauthenticated body would let anyone empty a chosen camera's bucket by
    naming it - a targeted denial of service against one volunteer, which is
    worse than the shared cap it replaces.
    """
    limit = RATE.get(path)
    if not limit:
        return True
    n, window = limit
    bucket = int(now() // window)
    key = (path, who or ip, bucket)
    with _HIT_LOCK:
        # 🚨 EVICT BY AGE, NEVER `clear()`. Wiping the whole table on overflow
        # reset every counter in it, so the limiter quietly stopped limiting
        # exactly when it was busiest - a pressure valve that opened under
        # pressure. With per-node keys the table is also much larger, which
        # would have made that far easier to trip.
        if len(_HITS) > 20000:
            for k in [k for k in _HITS if k[2] < bucket]:
                del _HITS[k]
        _HITS[key] = _HITS.get(key, 0) + 1
        return _HITS[key] <= n

# Anonymous viewers get a rolling daily alias instead of the real plate hash so
# a track is followable on screen but not archivable across days. We keep the
# reverse map in memory only, and it dies with the process.
# Geocode results, keyed by the lowercased query. Nominatim's usage policy is
# roughly one request a second and it is a free service run by volunteers, so a
# viral hour must not be passed straight through to them. A day is plenty: a
# town does not move.
# FIPS state codes. Broadcastify's /listen/stid/<n> IS the FIPS code - checked
# against Louisiana (22) and Michigan (26) rather than assumed, because the
# county ids on the same site are opaque internal numbers and it would have
# been easy to believe both were.
US_STATE_FIPS = {
    "AL": 1,  "AK": 2,  "AZ": 4,  "AR": 5,  "CA": 6,  "CO": 8,  "CT": 9,
    "DE": 10, "DC": 11, "FL": 12, "GA": 13, "HI": 15, "ID": 16, "IL": 17,
    "IN": 18, "IA": 19, "KS": 20, "KY": 21, "LA": 22, "ME": 23, "MD": 24,
    "MA": 25, "MI": 26, "MN": 27, "MS": 28, "MO": 29, "MT": 30, "NE": 31,
    "NV": 32, "NH": 33, "NJ": 34, "NM": 35, "NY": 36, "NC": 37, "ND": 38,
    "OH": 39, "OK": 40, "OR": 41, "PA": 42, "RI": 44, "SC": 45, "SD": 46,
    "TN": 47, "TX": 48, "UT": 49, "VT": 50, "VA": 51, "WA": 53, "WV": 54,
    "WI": 55, "WY": 56, "PR": 72,
}

_GEO_CACHE: dict = {}

_ALIAS: dict[str, str] = {}
_ALIAS_DAY = [0]


def _alias_map(rows: list[dict]) -> None:
    day = int(now() // 86400)
    if day != _ALIAS_DAY[0]:
        _ALIAS.clear()
        _ALIAS_DAY[0] = day
    for r in rows:
        red = privacy.redact(r, "anon")
        # `or ""` matters: plate_hash is NULL for a pass with no readable
        # plate, and dict.get's default does not fire on a present-but-None key.
        if (red.get("plate_hash") or "").startswith("a:") and r.get("plate_hash"):
            _ALIAS[red["plate_hash"]] = r["plate_hash"]


def _resolve_hash(h: str) -> str:
    """Turn a client-supplied alias back into the real hash, if we minted it."""
    return _ALIAS.get(h, h)



# The packaged desktop app. Hosted on GitHub releases rather than here - see the
# /download route for why - and CHECKED rather than assumed: the button on
# /IPCamera appears only when the asset really exists, so the page never offers
# a download that 404s. Cached, because this is a third-party round trip on a
# path a crowd may hit.
DOWNLOAD_URL = CONFIG.get(
    "download_url",
    "https://github.com/SparrowMap/sparrowmap/releases/latest/download/SparrowMap4Biz.exe")
_DL_CACHE = {"at": 0.0, "ok": False}
_DL_TTL_S = 600.0


def _download_url():
    """The URL if a build is actually published, else None."""
    if not DOWNLOAD_URL:
        return None
    if now() - _DL_CACHE["at"] < _DL_TTL_S:
        return DOWNLOAD_URL if _DL_CACHE["ok"] else None
    ok = False
    try:
        # Imported here, not at module scope: this is the only outbound HTTP
        # call the hub makes on a page path, and keeping it local makes that
        # obvious to anyone auditing what this server talks to.
        import urllib.request
        req = urllib.request.Request(
            DOWNLOAD_URL, method="HEAD",
            # 🚨 A User-Agent is REQUIRED. GitHub refuses requests without one,
            # and this project has already lost a whole ingest path to exactly
            # that mistake behind Cloudflare.
            headers={"User-Agent": "SparrowMap"})
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    _DL_CACHE.update(at=now(), ok=ok)
    return DOWNLOAD_URL if ok else None


def _cam_display_name(nd: dict) -> str:
    """'Public traffic camera - 7200 BLK ELROY RD [atx:1452]' -> '7200 BLK ELROY RD'.

    The enroller's prefix and its [network:id] tag are for operators; a reader
    wants the street the department printed on the camera.
    """
    name = str(nd.get("name") or nd.get("id") or "").strip()
    for pre in ("Public traffic camera - ", "Public traffic camera -", "Public traffic camera"):
        if name.startswith(pre):
            name = name[len(pre):].strip()
            break
    if name.endswith("]") and "[" in name:
        name = name[:name.rfind("[")].strip()
    return " ".join(name.split()) or str(nd.get("id") or "")


def _public_rows(rows: list[dict]) -> list[dict]:
    # 🚨 DROP THE NULLS. A sighting row has 31 columns and only ~12 are ever set,
    # so 19 of them ship as `"plate_text":null` on every row of every response.
    # Measured on the live all-time public feed: 1,302,448 bytes -> 578,551, a
    # 56% cut for no change in meaning (JS reads a missing key and a null key
    # identically for every check the clients make).
    #
    # This matters far more than it looks. The map re-fetches the WHOLE public
    # feed every CACHE_BUCKET_S (4s), so the payload is not paid once, it is
    # paid ~15 times a minute per open tab - it was ~2.6 Mbps of sustained
    # download per viewer, on top of everything else sharing the link, and it
    # grows with every sighting ever published. That is what "reconnecting"
    # was: the 12s client timeout losing to the transfer, while the box served
    # the query itself in 6ms.
    _alias_map(rows)
    return [{k: v for k, v in privacy.redact(r, "anon").items() if v is not None}
            for r in rows]


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # Do not advertise the software or the Python build. The default banner
    # read "SparrowMap/0.1.0 Python/3.12.10", which hands an attacker the exact
    # version to look up CVEs against before they try anything.
    server_version = "SparrowMap"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *args):        # quieter than the default
        pass

    # 🚨 THE ADMISSION GATE, AND IT COUNTS REQUESTS - NOT CONNECTIONS.
    #
    # The first two attempts gated CONNECTIONS in the server (dualstack), and
    # both refused real visitors while the box was idle: at a cap of 48 within
    # minutes, and again at 250 as soon as Caddy's pool reached 250. Measured
    # live the second time: 254 threads and /api/health answering "busy - too
    # many connections" on a box at load 5 doing nothing.
    #
    # 📌 A connection is not work. With HTTP/1.1 keep-alive a proxy holds a pool
    # of idle sockets open by design; each costs a thread and zero CPU. Counting
    # them and calling it load meant the limiter was measuring the one thing that
    # does not correlate with the resource it was protecting. Raising the number
    # never fixes that - it just moves where it starts lying.
    #
    # Here, the permit is taken AFTER the request line has been read and released
    # as soon as the response is written, so it measures exactly what it claims:
    # requests being served at this instant. An idle keep-alive connection holds
    # nothing and cannot lock anyone out.
    #
    # 32 is chosen against the machine, not the traffic: 2 vCPUs, shared. Beyond
    # a couple of dozen concurrent handlers the GIL and the sqlite writer make
    # every request slower without finishing any sooner - which is the collapse
    # this exists to prevent. Refusing in single-digit milliseconds lets the
    # proxy retry something that will work.
    # Named, module-level, so tools/test_overload.py can size its flood off the
    # real number instead of a copy that silently drifts out of step.
    _INFLIGHT = threading.Semaphore(MAX_REQUESTS)
    # The byte-shaped cap. See MAX_HEAVY.
    _HEAVY = threading.Semaphore(MAX_HEAVY)
    _HEAVY_ROUTES = HEAVY_ROUTES
    # The keep-the-readers-alive cap. See MAX_INGEST.
    _INGEST = threading.Semaphore(MAX_INGEST)
    _INGEST_ROUTES = INGEST_ROUTES
    # Who is holding a permit right now, and the worst hold time seen per path.
    # Both are published by /api/health so the cap can be tuned from evidence.
    _INFLIGHT_PATHS: dict = {}
    _INFLIGHT_LOCK = threading.Lock()
    _SLOW_HELD: dict = {}
    _SLOW_S = 2.0

    def handle_one_request(self):
        # One handler instance serves every request on a keep-alive connection,
        # so per-REQUEST state must be reset per request. Clearing the cache key
        # here means no path - including ones that bypass _send entirely - can
        # carry a key from one request into the next.
        self.__dict__.pop("_micro_key", None)
        self.__dict__.pop("_body_done", None)
        # The read that BLOCKS on an idle keep-alive connection happens inside
        # the parent, before any handler runs. Wrapping the whole call would put
        # us straight back to holding a permit for an idle socket, so the gate
        # goes around the DISPATCH instead - see _dispatch_guarded below, which
        # do_GET/do_POST route through.
        return super().handle_one_request()

    def _drain_body(self) -> None:
        """Read and discard a request body we are about to refuse.

        🚨 REFUSING WITHOUT READING DESYNCS A POOLED CONNECTION.
        Caddy keeps upstream connections alive and reuses them. If a 503 or 429
        is written while the request body is still unread, those bytes stay in
        the socket and are parsed as the START of the next request on that same
        connection - so the resulting 400 lands on some unrelated visitor's
        request, and log_message is suppressed here so nothing records it.

        The 429 paths matter more than the 503 one: a global 600/hour bucket is
        far easier to trip than 200 concurrent requests.
        """
        # ⚠️ IDEMPOTENT, OR IT HANGS THE REQUEST. Handlers that read the body
        # and THEN refuse are common (unknown node, bad token, bad json). A
        # second read of an exhausted stream blocks waiting for bytes that have
        # already been consumed - turning a tidy-up into a stall on the very
        # path that is shedding load.
        if getattr(self, "_body_done", False):
            return
        self._body_done = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            # Nothing to drain. A GET has no body, and closing the connection
            # here would make every refusal on a GET kill keep-alive - turning
            # a fix for a desync into a much larger performance bug. Caught by
            # tools/test_cache_key_leak.py, which reuses a connection after a
            # 503 and started aborting.
            return
        if n > self.MAX_BODY:
            # More than we are willing to read just to be polite. The bytes
            # cannot be left in the socket, so closing is the honest option.
            self.close_connection = True
            return
        try:
            self.rfile.read(n)
        except Exception:
            self.close_connection = True

    def _too_busy(self) -> None:
        self._drain_body()
        body = b'{"error": "busy - too many requests in flight"}'
        try:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "1")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    # Public read paths served IDENTICALLY to every anonymous viewer, so a short
    # shared cache collapses thousands of pollers into ~one origin fetch/window.
    _CACHEABLE_API = frozenset({"/api/sightings", "/api/stats", "/api/policy",
                                "/api/nodes", "/api/leaderboard", "/api/health",
                                # Identical for every viewer and it changes only
                                # when a camera is added, so it is the cheapest
                                # thing on the map to cache.
                                "/api/places",
                                "/api/heat"})

    def _cache_control(self) -> str:
        """Per-path caching policy.

        🚨 THIS IS WHAT LETS THE MAP SURVIVE A CROWD. The origin (a threaded
        Python server) caps near 55 req/s on the map data - measured. But that
        data is PUBLIC and identical for everyone, so it belongs on the edge:
        with a short shared cache, thousands of viewers collapse to about one
        origin fetch per window and the ceiling stops mattering.

        Default stays no-store. It is opened up ONLY for things that are public
        and the same for all viewers. The privacy reason no-store existed - not
        keeping a record of who looked at which plate - lives on the SEARCH and
        OPERATOR and per-user paths, which stay no-store below.
        """
        p = urlparse(self.path).path
        # 🚨 NEVER PUT A LONG TTL ON A FAILURE.
        # This decided purely from the PATH, so a tile that 404d - an upstream
        # blip, or the tile-fetch bound shedding under load - was stamped
        # "public, max-age=604800" exactly like a real tile. That burns a blank
        # square into that visitor's browser for a WEEK, for a transient error
        # that would have resolved on the next request. The status is part of
        # what is cacheable, not just the path.
        if getattr(self, "_status", 200) >= 400:
            return "no-store"
        if p.startswith(("/vendor/", "/api/tile/")):
            # PINNED content only: the vendored detector runtime (a 10 MB model
            # + wasm + Leaflet) and basemap tiles. These do not change without a
            # deliberate library swap, so a long cache saves re-downloading 10 MB
            # on every visit. NOT `immutable` - if a library is ever replaced a
            # 7-day revalidation is cheap insurance against serving a stale one.
            return "public, max-age=604800"
        if p.startswith("/static/"):
            # 🚨 THE APP'S OWN CODE (app.js, sitenav.js, transparency.js). It
            # MUST be able to change - marking it immutable froze every JS fix
            # for a week on returning visitors. Short cache: still absorbs a
            # launch spike (thousands of requests in a minute -> one origin hit)
            # but a code change propagates within the minute. (A content-hashed
            # filename would let this be immutable too - a later build step.)
            return "public, max-age=60"
        if p in self._CACHEABLE_API:
            # The public map data. The frontend buckets its `since` timestamps
            # so the URL is stable within the window and the cache actually hits.
            #
            # The live COUNTERS at the top of the map - cameras online, sightings
            # today - get a very short window so the page feels live, while still
            # collapsing a crowd into one origin hit every few seconds (stats and
            # the node list are small, cheap queries). The heavier per-row
            # sighting feed keeps a longer window; it is the expensive one.
            if p in ("/api/stats", "/api/health"):
                return "public, max-age=3"
            # 🚨 /api/nodes IS NOT A LIVE COUNTER AND MUST NOT BE PRICED LIKE
            # ONE. It sat on max-age=3 because it was grouped with the counters
            # at the top of the map - but those are /api/stats, which is 252
            # BYTES. This is the camera list: 13,637 rows and 4 MB, the single
            # most expensive answer this server produces.
            #
            # A 3s edge window means a crowd re-fetches it twenty times a
            # minute, and on 2026-08-18 that is what wedged the hub: 150
            # concurrent readers measured a median of 20s and half of them were
            # refused outright.
            #
            # What it actually contains changes when somebody ENROLS A CAMERA.
            # Thirty seconds of lag on that is invisible, and it turns thousands
            # of viewers into about one origin fetch per edge per window, which
            # is the only way this scales at all. The live feel of the page
            # comes from /api/sightings and /api/stats, both of which are cheap
            # and both of which keep their short windows.
            if p == "/api/nodes":
                return "public, max-age=30"
            # Town badges for the zoomed-out view. Derived from the same camera
            # list, changes on the same event, and is read far less often.
            if p == "/api/places":
                return "public, max-age=60"
            if p == "/api/sightings":
                return "public, max-age=4"    # live map: fresh within a few s
            return "public, max-age=15"
        if p == "/" or p.endswith(".html") or p in (
                "/about", "/transparency", "/status", "/IPCamera", "/app",
                "/node", "/key", "/checksums", "/support", "/donate"):
            return "public, max-age=60"        # page shells: reuse, revalidate
        # /api/plate search, /api/track, /api/sighting/<id>, operator routes,
        # /api/live, /api/audit - anything per-user or a lookup - is never cached.
        return "no-store"

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        # A fresh nonce per response. Four pages carry an inline <script>, and
        # the alternatives were both worse: allowing 'unsafe-inline' would make
        # the policy decorative, and moving the code out to four new files
        # would scatter page logic away from the page for a deployment detail.
        # Recorded before headers are built so _cache_control can see it - a
        # failure and a success must not get the same caching policy.
        self._status = code
        self._nonce = secrets.token_urlsafe(12)
        # ⚠️ SUBSTITUTE BEFORE Content-Length IS COMPUTED.
        # The placeholder and the nonce are different lengths, so filling it in
        # after the header was set truncated every page by seven bytes - a
        # silently broken site with a valid-looking response.
        if b"@@NONCE@@" in body:
            body = body.replace(b"@@NONCE@@", self._nonce.encode())

        # 🚨 FILL THE MICRO-CACHE HERE, AFTER THE NONCE SUBSTITUTION.
        # Caching a body that still held @@NONCE@@ would serve a placeholder to
        # everyone who got a hit; caching one WITH a nonce baked in would reuse
        # a single nonce across viewers. Neither matters for the JSON API paths
        # this is limited to (they carry no nonce), and the ordering is written
        # down so it stays true if that ever changes.
        # 🚨 TAKEN AND CLEARED IN ONE STEP, BEFORE ANY EARLY RETURN.
        # This used to clear the key only inside the success branch, so a
        # non-200 left it set on a handler instance that lives for the WHOLE
        # keep-alive connection - and the next 200 on that connection was then
        # stored under the previous request's public key. _too_busy() made it
        # reachable: it writes its 503 straight to wfile and never enters
        # _send, so it could not clear anything, and it fires exactly during
        # overload. Worst case the body cached under a public key came from
        # /api/node/me, the one endpoint that returns a camera's TRUE position.
        key = self.__dict__.pop("_micro_key", None)
        if key and code == 200:
            with Handler._MICRO_LOCK:
                Handler._MICRO[key] = (time.time(), body)
                # Bounded: the key includes the query string, and `since=` moves
                # every few seconds, so an unbounded dict is a slow leak.
                if len(Handler._MICRO) > 200:
                    for k in sorted(Handler._MICRO,
                                    key=lambda k: Handler._MICRO[k][0])[:80]:
                        Handler._MICRO.pop(k, None)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # A caller that set its own Cache-Control (the tile proxy) wins; this
        # also stops the two conflicting Cache-Control headers the tile response
        # used to carry. Otherwise apply the per-path policy.
        if not (extra and any(k.lower() == "cache-control" for k in extra)):
            self.send_header("Cache-Control", self._cache_control())

        # ---- headers that protect the VISITOR ---------------------------
        # 🚨 REFERRER POLICY IS AN ANONYMITY CONTROL HERE, NOT A FORMALITY.
        # Without it, every outbound click - the OpenStreetMap attribution
        # link at the bottom of the map, for one - tells the destination that
        # the visitor came from sparrowmap.com, and carries the full URL
        # including any plate they searched for.
        self.send_header("Referrer-Policy", "no-referrer")
        # Stops a browser from second-guessing a declared content type, which
        # is how a stored file gets executed as script.
        self.send_header("X-Content-Type-Options", "nosniff")
        # Nobody frames this map. Clickjacking a "retract" button would be a
        # quiet way to vandalise the record.
        self.send_header("X-Frame-Options", "DENY")
        # Everything this site loads, it ships. Leaflet is vendored, there is
        # no CDN and no analytics, so the policy can be strict enough to
        # actually contain an injection rather than decorate the response.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' "
            # 🚨 'wasm-unsafe-eval' IS REQUIRED OR THE DETECTOR CANNOT LOAD.
            # Compiling a WebAssembly module counts as script generation, so a
            # script-src without it refuses the ONNX runtime outright - which
            # broke the camera on every device, with the map and every other
            # page still working perfectly. I had verified the model loading
            # BEFORE this header existed and never re-tested after, so the
            # regression shipped invisibly.
            #
            # It is far narrower than 'unsafe-eval': it permits WebAssembly
            # compilation and nothing else - no eval, no new Function, no
            # inline string execution. The policy stays meaningful.
            f"'unsafe-inline'; script-src 'self' 'wasm-unsafe-eval' "
            f"'nonce-{self._nonce}'; "
            # 🚨 worker-src 'self' blob: OR MAPLIBRE CANNOT RENDER THE BASEMAP.
            # MapLibre GL runs its tile decoder in a Web Worker it creates from a
            # blob: URL. Without an explicit worker-src that falls back to
            # default-src 'self', which forbids blob:, so the worker is blocked -
            # the style loads on the main thread but no tile is ever processed and
            # the map is a silent blank. (The camera model's wasm is covered by
            # 'wasm-unsafe-eval' above; this is the map's equivalent.)
            "worker-src 'self' blob:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'")

        # The public map is meant to be embeddable and mirrorable by anyone.
        # Operator JSON is not, and a wildcard on it is needless surface even
        # with a SameSite=Strict cookie in front.
        if not self.path.startswith(("/api/review", "/api/operator",
                                     "/api/purge", "/api/rv")):
            self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        # 🚨 A HEAD RESPONSE CARRIES THE HEADERS AND NOT THE BODY.
        # Writing one anyway desynchronises a keep-alive connection: the client
        # reads our body as the start of its NEXT response. See do_HEAD.
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self) -> None:
        """HEAD, which this server answered with 501 for its whole life.

        🚨 IT LOOKS LIKE AN EDGE CASE AND IS NOT. BaseHTTPRequestHandler has no
        default do_HEAD, so every HEAD got "501 Unsupported method" - and HEAD is
        what link checkers, uptime monitors, CDNs and link-preview crawlers use
        BEFORE they fetch anything. A site that 501s them looks broken to
        exactly the tools that decide whether a shared link is worth showing,
        which matters most at the moment a link is spreading.

        Found by my own test doing `curl -I` and reading 501 as a broken route.

        Handled by running the ordinary GET path and dropping the body in
        _send, so a HEAD can never disagree with the GET it describes - the
        status, the headers and the Content-Length are all the real ones.
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _tile(self, path: str) -> None:
        """Serve one basemap tile, from disk if we already have it.

        ⚠️ THE UPSTREAM URL IS BUILT FROM INTEGERS, NEVER FROM THE REQUEST.
        A proxy that forwards a caller-supplied URL is an open proxy: it will
        happily fetch `http://169.254.169.254/` or anything else the box can
        reach, using the box's own network position. So z/x/y are parsed as
        ints, range-checked against the zoom, and formatted into a fixed
        template. There is no code path here that can be pointed somewhere
        else. A proxy that accepts a request-supplied URL and guards it with a
        prefix check may be acceptable on an operator-only LAN page; this one is
        reachable by every viewer of a public map, so it takes no URL at all.
        """
        parts = path[len("/api/tile/"):].split("/")
        if len(parts) != 3 or not parts[2].endswith(".png"):
            return self._send(404, b"", "text/plain")
        try:
            z = int(parts[0]); x = int(parts[1]); y = int(parts[2][:-4])
        except ValueError:
            return self._send(404, b"", "text/plain")
        # Reject anything outside the tile grid before it becomes a request.
        if not (0 <= z <= TILE_MAX_ZOOM) or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
            return self._send(404, b"", "text/plain")

        cached = TILES / str(z) / str(x) / f"{y}.png"
        if cached.exists():
            return self._send(200, cached.read_bytes(), "image/png",
                              {"Cache-Control": "public, max-age=604800"})

        # Rate-limit only the UPSTREAM path - a cache hit above is cheap, but a
        # miss makes this box fetch from the CDN and hold a thread up to 15s.
        # That is the amplification lever, so the budget guards exactly it.
        if not rate_ok("/api/tile", self.client_ip):
            return self._send(429, b"", "text/plain")

        # 🚨 BOUND THE CONCURRENT UPSTREAM FETCHES. THIS TOOK THE SITE DOWN.
        #
        # Tiles are exempt from the request gate, and correctly so: waiting on
        # somebody else's CDN is not work this box does, and gating it meant a
        # visitor panning the map consumed every permit for fifteen seconds a
        # time. But exempting them removed the ONLY bound, which is a different
        # mistake with the same outcome.
        #
        # It surfaced the moment the Cloudflare cache was purged: a cold edge
        # makes every tile a MISS, each MISS becomes an origin request, each one
        # opens an upstream fetch, and hundreds ran at once. Load 41 on 2 vCPUs,
        # the accept queue backed up, and the map returned 502 while the box sat
        # mostly idle waiting on the network.
        #
        # A cache HIT above never reaches here, so this bounds only the
        # amplifying path. The wait is short and deliberate: most fetches finish
        # well under a second, so a storm sheds rather than queues. A blank tile
        # for one pan is nothing; Leaflet fills it on the next move, and it beats
        # taking the whole site down to render one square of road.
        if not _TILE_FETCH.acquire(timeout=2.0):
            return self._send(404, b"", "text/plain")

        import urllib.request
        url = TILE_UPSTREAM.format(s=TILE_SUBDOMAINS[(x + y) % len(TILE_SUBDOMAINS)],
                                   z=z, x=x, y=y)
        try:
            raw = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "SparrowMap/0.1 (+https://sparrowmap.com)"}),
                timeout=15).read()
        except Exception:
            # A missing tile must not be an error page: Leaflet would draw the
            # HTML as a broken image across the map. Fail as a 404 and let it
            # leave that square blank.
            return self._send(404, b"", "text/plain")
        finally:
            # ⚠️ RELEASED HERE, NOT AFTER THE DISK WRITE. The permit exists to
            # bound UPSTREAM fetches; holding it through the local write would
            # count disk time against a network budget, and a permit leaked on
            # the 404 path would shrink the pool to nothing one failed tile at
            # a time - a slow strangle that looks like a network problem.
            _TILE_FETCH.release()

        # Never cache (or serve with a long TTL) Carto's "API KEY REQUIRED"
        # placeholder - drop it as a short-lived 404 so Leaflet leaves the square
        # blank and retries, and the real tile lands once Carto stops throttling.
        import hashlib
        if hashlib.md5(raw).hexdigest() in TILE_PLACEHOLDER_MD5:
            return self._send(404, b"", "text/plain", {"Cache-Control": "no-store"})

        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(raw)
            _tile_prune()
        except Exception:
            # Caching is an optimisation. If the disk says no, still serve.
            pass
        return self._send(200, raw, "image/png",
                          {"Cache-Control": "public, max-age=604800"})

    def _json(self, obj, code: int = 200) -> None:
        # Compact separators: json.dumps defaults to ", " and ": ", which on the
        # all-time public feed alone was ~124 KB of spaces per response, re-sent
        # every 4 seconds to every viewer.
        self._send(code, json.dumps(obj, default=str, separators=(",", ":")).encode(),
                   "application/json")

    def _err(self, code: int, msg: str) -> None:
        # 🚨 DRAIN BEFORE REFUSING. Several refusals - every 429, the unknown
        # node, the bad token - return BEFORE _body() has read the request, and
        # Caddy reuses upstream connections. Unread bytes are then parsed as the
        # start of the NEXT request on that connection, so the resulting 400
        # lands on an unrelated visitor and log_message is suppressed here so
        # nothing records it. Doing it in _err rather than at each call site
        # means a refusal path added later cannot forget.
        if code >= 400:
            self._drain_body()
        self._json({"error": msg}, code)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        # The browser refuses to instantiate a wasm module served as anything
        # else, and mimetypes on Windows does not know these two.
        ctype = {".wasm": "application/wasm",
                 ".onnx": "application/octet-stream"}.get(path.suffix, ctype)
        body = path.read_bytes()
        if path.suffix == ".html":
            # The nonce is generated inside _send, so mark the tags with a
            # placeholder and let _send fill it. Only bare <script> tags are
            # touched; ones with a src attribute are already covered by 'self'.
            body = body.replace(b"<script>", b"<script nonce=\"@@NONCE@@\">")
        self._send(200, body, ctype)

    # A sighting carries a base64 vehicle crop, which is the only large body this
    # server has any reason to accept. 8 MB covers a generous JPEG with base64's
    # 33% overhead; everything else is a few hundred bytes. Bigger than this is
    # not a real submission, it is memory pressure on a 2-vCPU/3-GB box.
    MAX_BODY = 8 * 1024 * 1024

    def _body(self) -> dict:
        # Marked before any read, so a refusal AFTER this point never tries to
        # drain an already-consumed stream. See _drain_body.
        self._body_done = True
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        # 🚨 CAP BEFORE READING. Trusting Content-Length and calling read(n) with
        # no ceiling lets one request ask the box to buffer gigabytes; a handful
        # of those, or a slow-loris trickle, exhausts a thread-per-connection
        # server. Never buffer more than MAX_BODY, and on an oversize claim close
        # the connection (unread bytes would otherwise corrupt the next
        # keep-alive request). The handler then sees {} and answers 400. No
        # response is sent from here, so the caller can never double-respond.
        if n > self.MAX_BODY:
            self.close_connection = True
            return {}
        # 🚨 A WALL-CLOCK DEADLINE, NOT JUST A PER-RECV TIMEOUT.
        # The socket timeout (dualstack.IDLE_TIMEOUT_S) applies to each recv
        # individually, so a client sending one byte every 19 seconds resets it
        # forever and holds an admission permit for as long as it likes. A few
        # hundred of those at ~10 B/s and every POST, every no-store route and
        # all map data past its micro-TTL returns 503 - while the page shell and
        # tiles keep serving, which makes it harder to diagnose rather than
        # milder. This is a slow-loris on the one resource the whole site
        # shares.
        #
        # Read in chunks against a total budget instead. A legitimate body here
        # is a few hundred KB of JPEG from a camera on a domestic uplink, so ten
        # seconds is generous; anything slower is not a camera.
        # ⚠️ THE DEADLINE ONLY WORKS IF EACH READ RETURNS. Checking the clock
        # between reads is not enough: rfile.read() blocks until it has the
        # bytes, and every trickled byte resets the socket timeout, so the very
        # first read never comes back and the deadline is never consulted. The
        # socket timeout must be shortened for the duration of the body read so
        # control returns to this loop regularly. (Found by
        # tools/test_slowloris.py, which failed identically before and after the
        # first version of this fix - a test earning its place.)
        deadline = time.time() + 10.0
        prev_timeout = None
        try:
            prev_timeout = self.connection.gettimeout()
            self.connection.settimeout(1.0)
        except Exception:
            pass
        chunks, got = [], 0
        try:
            while got < n:
                if time.time() > deadline:
                    # The bytes cannot be left in the socket for the next
                    # request on this connection.
                    self.close_connection = True
                    return {}
                try:
                    part = self.rfile.read(min(65536, n - got))
                except (TimeoutError, socket.timeout, OSError):
                    continue          # nothing yet; the deadline decides
                if not part:
                    self.close_connection = True
                    return {}
                chunks.append(part)
                got += len(part)
        finally:
            try:
                if prev_timeout is not None:
                    self.connection.settimeout(prev_timeout)
            except Exception:
                pass
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except Exception:
            return {}

    def _is_local(self) -> bool:
        """May this caller retract a published claim?

        Loopback, the LAN, and the Tailscale range - which on this deployment
        is exactly the operator and his phone, and nobody else. Strict localhost was
        the first cut and made the review page unusable from the phone he
        actually reviews things on; a control nobody can reach when they need
        it gets worked around rather than used.

        Checked against the SOCKET, never a header. X-Forwarded-For is
        client-controlled, and trusting it is how an "operator only" route
        becomes an anyone-who-asks route.

        🚨 THIS IS ONLY SAFE WHILE THE HUB IS PRIVATE. The moment it is exposed
        to the internet - or put behind a proxy, which makes every request
        appear to come from the proxy's address - this must become real
        authentication. `operator_requires_auth` in config.json is the reminder;
        the check refuses everything if it is ever set true without an auth
        mechanism existing, because failing closed is the correct direction.
        """
        # Auth exists now (operator_auth.py). When it is switched on the
        # SOCKET ADDRESS IS IGNORED, because behind a reverse proxy it is the
        # proxy's address and says nothing about who is calling - which is the
        # trap this check used to warn about and would have walked into the
        # first time sparrowmap.com pointed at a box.
        return operator_auth.check(self.headers, self.client_address[0])

    @property
    def client_ip(self) -> str:
        # Trust the socket, not a header. X-Forwarded-For is client-controlled
        # unless a proxy you own overwrote it, and treating it as truth is how
        # audit logs get poisoned.
        return self.client_address[0]

    # -- routing --------------------------------------------------------
    # Wrappers, not edits inside the handlers: both are hundreds of lines with
    # many returns and their own try/except at the bottom, and a permit that
    # leaks on one path takes the site down slowly. try/finally around a single
    # call covers every exit including the 500 handler.
    # 🚨 TILES AND STATIC FILES ARE NOT GATED, AND THAT IS THE WHOLE POINT.
    #
    # The cap exists to bound work that costs CPU and the sqlite writer. A tile
    # is the opposite: on a cache miss `_tile` fetches from the CDN and, as the
    # note above RATE already said, "holds a worker thread up to 15s". Gating it
    # meant a single visitor panning to a new area fired 20-40 tile misses that
    # each took a permit and sat on it for fifteen seconds waiting on somebody
    # else's network - so all 32 permits went to tiles and every real request
    # got "busy - too many requests in flight". Reported as the map being down
    # while /api/health, checked from elsewhere, answered 200 the whole time.
    #
    # 📌 The permit was measuring the wrong thing AGAIN - first connections
    # instead of requests, now waiting instead of working. What must be bounded
    # is work this box does, never time it spends waiting on someone else.
    #
    # Tiles are not unprotected: they carry their own budget (RATE
    # "/api/tile" = 600/5min) which limits exactly the upstream amplification,
    # and dualstack's thread ceiling plus the idle timeout still bound them.
    # Static files under /static and /vendor are disk reads served from cache
    # and a single page load pulls a dozen; gating those buys nothing either.
    _UNGATED = ("/api/tile/", "/static/", "/vendor/")

    def _gated(self, inner, label: str):
        """Run a handler holding one permit, and RECORD that it holds it.

        🚨 THE ACCOUNTING IS THE POINT, NOT THE CAP. Three separate times this
        limiter refused visitors on an idle box, and every time the diagnosis
        took a live investigation because nothing recorded what was actually
        holding permits. A limiter that cannot say who is using it can only be
        tuned by argument, and argument lost three times.
        """
        # ⚠️ A CLASS PERMIT FIRST, AND IT QUEUES RATHER THAN REFUSES. A request
        # that waits a moment nearly always finds the answer already built by
        # the leader it was queued behind; one that is refused sends a reader an
        # error for work that was about to be free, or makes a node drop a
        # sighting it cannot re-send.
        #
        # These pools are deliberately SMALLER than MAX_REQUESTS and they
        # overlap with it rather than replace it: MAX_REQUESTS still bounds the
        # total, while these bound the two classes that proved able to eat the
        # total on their own - big map answers, and camera ingest.
        extra, wait = None, 0.0
        if label in Handler._HEAVY_ROUTES:
            extra, wait = Handler._HEAVY, HEAVY_WAIT_S
        elif label in Handler._INGEST_ROUTES:
            extra, wait = Handler._INGEST, INGEST_WAIT_S
        if extra is not None and not extra.acquire(timeout=wait):
            return self._too_busy()
        try:
            if not Handler._INFLIGHT.acquire(blocking=False):
                return self._too_busy()
            started = time.time()
            with Handler._INFLIGHT_LOCK:
                Handler._INFLIGHT_PATHS[id(self)] = (label, started)
            try:
                return inner()
            finally:
                Handler._INFLIGHT.release()
                with Handler._INFLIGHT_LOCK:
                    Handler._INFLIGHT_PATHS.pop(id(self), None)
                    held = time.time() - started
                    if held > Handler._SLOW_S:
                        # A permit held this long is the shape of every outage
                        # so far: something waiting on a third party, not
                        # working.
                        #
                        # ⚠️ BOUNDED. The label is a route, but a route can be
                        # unbounded - /api/tile/{z}/{x}/{y} alone is millions of
                        # distinct strings - so an unbounded dict here is a slow
                        # memory leak fed by exactly the traffic that causes an
                        # outage. Keep the worst offenders; the tail is noise.
                        Handler._SLOW_HELD[label] = round(
                            max(held, Handler._SLOW_HELD.get(label, 0)), 1)
                        if len(Handler._SLOW_HELD) > 40:
                            for k, _ in sorted(Handler._SLOW_HELD.items(),
                                               key=lambda kv: kv[1])[:20]:
                                Handler._SLOW_HELD.pop(k, None)
        finally:
            if extra is not None:
                extra.release()

    # 🚨 THE EDGE CACHE THIS SERVER WAS DESIGNED AROUND IS NOT SWITCHED ON.
    #
    # _cache_control says it plainly: the origin caps near 55 req/s on map data,
    # and survives a crowd only because "thousands of viewers collapse to about
    # one origin fetch per window". That assumes something in front is doing the
    # collapsing. sparrowmap.com is proxied by Cloudflare; map.sparrowmap.com
    # resolves straight to this box, so every viewer, every poll and every tile
    # arrives here. Measured during the outage: 128 requests in flight, 254
    # threads, /api/nodes and /api/sightings each holding a permit for 10-21
    # SECONDS - plain reads, starved of CPU by the crowd doing them all at once.
    #
    # So the origin does the collapsing itself. These responses are PUBLIC and
    # byte-identical for every viewer - that is already why they carry a shared
    # Cache-Control - so one hundred simultaneous identical requests can be one
    # computation and ninety-nine memory reads.
    #
    # ⚠️ ONLY _CACHEABLE_API PATHS, and the key includes the query string. The
    # search, operator and per-visitor routes stay no-store and are never
    # entered here: caching a plate search would build the record of who looked
    # up what that no-store exists to prevent.
    _MICRO: dict = {}
    _MICRO_LOCK = threading.Lock()
    # key -> Event, held by whichever thread is currently computing it.
    _MICRO_FLIGHT: dict = {}

    # What each cacheable route ACTUALLY reads. Anything else is noise and must
    # not reach the cache key.
    _MICRO_PARAMS = {
        "/api/sightings":   ("since", "limit", "vclass", "bbox"),
        "/api/leaderboard": ("hours",),
        # 🚨 A PARAMETER THAT CHANGES THE ANSWER AND IS NOT LISTED HERE IS A
        # CACHE POISONING BUG, NOT AN OMISSION. /api/nodes now returns a
        # different set for public_cams=0, and without this line both variants
        # share one key: whichever request missed first decides what everybody
        # else gets for the life of the entry - the map either loses 4,800
        # cameras it asked for or gets a megabyte it deliberately declined.
        "/api/nodes":       ("public_cams", "box"),
    }

    def _micro_key_for(self, path: str) -> str:
        """Cache key from the path plus ONLY the parameters that change the answer.

        🚨 THE KEY WAS THE WHOLE QUERY STRING, WHICH HANDS ANYONE A CACHE-BUSTER.
        `?x=1`, `?x=2`, `?x=3` … are unlimited distinct keys on a route that
        ignores `x` entirely. Every one misses, becomes its own single-flight
        leader, takes an admission permit and does the full query - so the one
        defence the origin has against a crowd could be switched off from a
        browser address bar. The map is CORS-open and meant to be embedded, so
        an embedder that does not bucket its `since` values does this by
        accident rather than maliciously.

        ⚠️ `since` is BUCKETED here as well as in the frontend. Relying on the
        client to round it means the protection only exists for clients that
        cooperate, which is not a protection.
        """
        from urllib.parse import parse_qs
        names = self._MICRO_PARAMS.get(path)
        if not names:
            return path              # no parameters are read: one answer for all
        q = parse_qs(urlparse(self.path).query)
        bits = []
        for n in names:
            if n not in q:
                continue
            v = q[n][0]
            if n == "since":
                # Same bucket the cache TTL uses, so repeated polls land on one
                # key instead of one per second.
                try:
                    v = str(int(float(v) // CACHE_BUCKET_S * CACHE_BUCKET_S))
                except (TypeError, ValueError):
                    continue
            elif n in ("bbox", "box"):
                # 🚨 AN UNBUCKETED BOX IS THE CACHE-BUSTER THIS DOCSTRING
                # WARNS ABOUT, AND `bbox` HAS BEEN ONE ALL ALONG.
                #
                # A map sends a new box on every pan, to as many decimal places
                # as Leaflet feels like. Each distinct string is its own key,
                # its own miss, its own single-flight leader and its own
                # admission permit - so the busiest interaction on the site was
                # the one the micro-cache could never help with, and one person
                # dragging the map could mint keys as fast as they could move a
                # finger.
                #
                # ⚠️ SNAPPED OUTWARD, NEVER ROUNDED TO NEAREST, AND THAT IS A
                # CORRECTNESS RULE RATHER THAN A PREFERENCE. Rounding to nearest
                # can SHRINK the box, and then two viewports sharing a key get
                # an answer that covers one of them - rows missing at the edge
                # of somebody's screen, intermittently, depending on who missed
                # the cache first. Snapping outward makes every cached answer a
                # SUPERSET of any box in its bucket: extra rows off-screen are
                # harmless, absent ones are not.
                #
                # The route snaps identically (see _snap_box) so the key and the
                # query can never disagree about what was asked for.
                try:
                    v = _snap_box(v) or v
                except ValueError:
                    continue
            bits.append(f"{n}={v}")
        return path + ("?" + "&".join(bits) if bits else "")

    def _micro_ttl(self) -> float:
        cc = self._cache_control()
        if "no-store" in cc:
            return 0.0
        m = re.search(r"max-age=(\d+)", cc)
        return float(m.group(1)) if m else 0.0

    @staticmethod
    def _route_label(path: str) -> str:
        """A stable label for a path, so counters key on ROUTES not URLs.

        /api/sighting/45746 and /api/tile/12/1096/1521.png are one route each,
        not one label each. Keying on the raw path turns any per-id endpoint
        into an unbounded set of dictionary keys.
        """
        parts = path.split("/")
        out = []
        for seg in parts:
            if seg and (seg.isdigit() or seg.rstrip(".png").isdigit()):
                out.append("{n}")
            else:
                out.append(seg)
        return "/".join(out)[:48]

    def do_GET(self) -> None:
        if self.path.startswith(Handler._UNGATED):
            return self._do_GET_inner()

        p = urlparse(self.path).path
        ttl = self._micro_ttl() if p in self._CACHEABLE_API else 0.0
        if not ttl:
            return self._gated(self._do_GET_inner, self._route_label(p))

        key = self._micro_key_for(p)
        hit = Handler._MICRO.get(key)
        if hit and time.time() - hit[0] < ttl:
            # Served without taking a permit at all: a memory read is not the
            # work the gate exists to bound.
            return self._send(200, hit[1], "application/json")

        # 🚨 SINGLE-FLIGHT, NOT MERELY A TTL. A plain cache does nothing about a
        # STAMPEDE, and a stampede is precisely what was measured: 128 requests
        # in flight with /api/nodes and /api/sightings each held 10-21 seconds,
        # every one of them computing the same public answer at the same moment.
        # They all miss an empty cache together, so a TTL alone would let all 128
        # through and only help the 129th.
        #
        # So the FIRST caller computes and the rest wait for it. One hundred
        # simultaneous identical requests become one query and ninety-nine
        # waiters - which is exactly what the absent edge cache would have done.
        with Handler._MICRO_LOCK:
            leader = Handler._MICRO_FLIGHT.get(key)
            if leader is None:
                leader = threading.Event()
                Handler._MICRO_FLIGHT[key] = leader
                mine = True
            else:
                mine = False

        if not mine:
            # ⚠️ BOUNDED WAIT. If the leader dies or is unusually slow, a
            # follower must fall through and do the work itself rather than hang
            # - a cache that can wedge a request is worse than no cache.
            leader.wait(timeout=min(ttl + 5.0, 20.0))
            hit = Handler._MICRO.get(key)
            if hit and time.time() - hit[0] < ttl + 5.0:
                return self._send(200, hit[1], "application/json")
            return self._gated(self._do_GET_inner, self._route_label(p))

        try:
            # Captured in _send rather than by threading a key through
            # _do_GET_inner, which is hundreds of lines with dozens of exits.
            self._micro_key = key
            return self._gated(self._do_GET_inner, self._route_label(p))
        finally:
            with Handler._MICRO_LOCK:
                Handler._MICRO_FLIGHT.pop(key, None)
            leader.set()      # release the followers, success or failure

    def do_POST(self) -> None:
        return self._gated(self._do_POST_inner,
                           "POST " + self._route_label(self.path.split("?")[0]))

    def _do_GET_inner(self) -> None:
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)

            # A mirror has no operator surface at all. Reviewing happens at
            # home, on the hub that holds the evidence to judge with.
            if not mirror.route_allowed(p):
                return self._err(404, "not found")

            if p == "/":                 return self._file(PUBLIC / "index.html")
            if p == "/about":            return self._file(PUBLIC / "about.html")
            if p == "/contribute":       return self._file(PUBLIC / "contribute.html")
            if p == "/guides":           return self._file(PUBLIC / "guides.html")
            if p == "/tor":              return self._file(PUBLIC / "tor.html")
            if p == "/spot":             return self._file(PUBLIC / "spot.html")
            if p == "/guide":            return self._file(PUBLIC / "guide.html")
            if p == "/rfbeta":           return self._file(PUBLIC / "rfbeta.html")
            if p == "/radar":            return self._file(PUBLIC / "radar.html")
            if p == "/send":             return self._file(PUBLIC / "send.html")
            if p == "/send-sw.js":
                # Sparrow Send's service worker. Served from the ROOT path so it
                # can scope itself to /send (a script at /send-sw.js may control
                # any scope under /). Coexists with the map's own /sw.js - the
                # narrower /send scope wins for the messenger page.
                return self._file(PUBLIC / "send-sw.js")
            if p == "/api/send/qr":
                # A QR PNG of a Sparrow address (a public key - nothing secret),
                # so a contact can be added by scanning instead of pasting.
                _q = parse_qs(urlparse(self.path).query)
                d = (_q.get("d", [""])[0] or "")[:4096]
                if not d:
                    return self._err(400, "no data")
                try:
                    return self._send(200, qr.png(d, scale=8), "image/png")
                except Exception:
                    return self._err(400, "could not render")
            if p == "/api/send/vapid":
                # The public VAPID key the client needs to subscribe to push.
                return self._json({"key": send_push.public_key()})
            if p == "/api/send/handle":
                # Resolve a short handle -> address (an opt-in directory). The
                # address is a public key, so this leaks nothing a shared address
                # would not; only claimed handles resolve. No enumeration route.
                _q = parse_qs(urlparse(self.path).query)
                h = (_q.get("h", [""])[0] or "")[:32]
                addr = send_relay.resolve_handle(h)
                if not addr:
                    return self._err(404, "no such handle")
                return self._json({"handle": send_relay._norm_handle(h), "address": addr})
            if p == "/sensors":          return self._file(PUBLIC / "sensors.html")
            if p == "/setup":            return self._file(PUBLIC / "setup.html")
            if p == "/buy":              return self._file(PUBLIC / "buy.html")
            if p == "/radarlink":        return self._file(PUBLIC / "radarlink.html")
            # The phone beta scanner, served as plain text so a tester can
            # `curl` it straight onto their phone. Read-only, single known file
            # (no path from the request), so this cannot serve anything else.
            if p == "/rf/phone_scan.py":
                return self._file(PUBLIC.parent / "rf" / "phone_scan.py")
            if p == "/rf/rf_scan.py":
                return self._file(PUBLIC.parent / "rf" / "rf_scan.py")
            if p == "/rf/esp32_sparrow.ino":
                return self._file(PUBLIC.parent / "rf" / "esp32_sparrow.ino")
            if p == "/transparency":     return self._file(PUBLIC / "transparency.html")
            # 🚨 THE ANSWER TO "YOUR SITE IS BLOCKED, SO IT IS FAKE".
            # A filter's block page is served before the request ever leaves the
            # reader's device, so it is evidence about their filter and nothing
            # else. This page exists so the reply is a link rather than an
            # argument: if they can read it, they reached the server.
            if p == "/status":          return self._file(PUBLIC / "status.html")
            # SHA-256 for every published installer. Asked for on Hacker News,
            # and the honest answer at the time was that no checksum existed to
            # check against. Served from THIS host while the binaries live on
            # GitHub, so verifying means trusting two places rather than one.
            if p == "/checksums":       return self._file(PUBLIC / "checksums.html")
            # Growth, live capacity and what it costs, generated from the
            # database by tools/support_page.py. Asking for money without
            # showing the numbers - including the bad retention one - is the
            # kind of thing this project exists to be the opposite of.
            if p in ("/support", "/donate"):
                # ⚠️ GENERATED, SO IT CAN BE ABSENT. It is built on the server by
                # tools/support_page.py and gitignored, so a fresh checkout has
                # no copy. Say that rather than serving a 404, which would look
                # like the page was taken down - on a donations page that reads
                # as something worse than a missing file.
                f = PUBLIC / "support.html"
                if f.is_file():
                    return self._file(f)
                return self._send(
                    503, b"<!doctype html><meta charset=utf-8>"
                         b"<title>SparrowMap</title>"
                         b"<p style='font:15px system-ui;padding:24px'>"
                         b"This page is generated from the live database and has "
                         b"not been built on this server yet.<br>"
                         b"<a href='/'>Back to the map</a>", "text/html")
            # 🚨 THE ROUTE SOMEBODY WITH THEIR OWN CAMERA IS SENT TO. Everything
            # it needs is already public (enrol, aim, review); what did not exist
            # was one page that walks somebody who owns a shop - not a terminal -
            # from "I have a camera outside" to a running relay without them
            # having to know which of these pages to visit in which order.
            #
            # ⚠️ IT WAS CALLED /business AND THE NAME WAS THE PROBLEM. People
            # read "business" as "the paid tier" or "not for me", and asked. It
            # describes who we imagined using it rather than what it does, so it
            # is /IPCamera now - which is the thing they actually have.
            #
            # 🚨 /business STILL ANSWERS, FOREVER. It is printed in a viral reel's
            # comments, in DMs and in older builds of the desktop app, and none of
            # those can be edited. A rename that breaks them costs more than the
            # rename gains. Accept the lower-case spelling too: nobody types
            # capitals in the middle of a URL from memory.
            if p in ("/business", "/ipcamera"):
                # ⚠️ 301 SO SEARCH MOVES THE PAGE ACROSS, BUT WITH A SHORT
                # Cache-Control. A bare 301 is cached by browsers indefinitely
                # and this name has already changed twice; an hour is plenty for
                # search engines and leaves us able to change our minds without
                # having poisoned every visitor's browser.
                self._status = 301
                self.send_response(301)
                self.send_header("Location", "/IPCamera")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                return
            if p == "/IPCamera":        return self._file(PUBLIC / "ipcamera.html")
            # A Raspberry Pi in a car: the relay with --gps and --enroll, as a
            # guide somebody can follow from a parts list to a systemd unit.
            if p in ("/carpi", "/CarPi", "/dashcam-pi"):
                return self._file(PUBLIC / "carpi.html")
            # 🚨 THE RELAY, AS ONE FILE. It imports nothing from this project
            # and fetches its own model, so a business needs this file and three
            # pip packages - not a git checkout. Telling somebody to clone a
            # repository to run a background service is where most of them stop.
            if p == "/relay.py":
                return self._file(PUBLIC.parent / "detect" / "relay.py")
            # The packaged desktop app, when a build has been placed here. Kept
            # OUT of git (it is a 60 MB derived artefact full of absolute build
            # paths - preflight caught exactly that), so this 404s cleanly until
            # somebody uploads one, and /IPCamera hides its button accordingly.
            if p == "/download":
                # 🚨 REDIRECTED TO A GITHUB RELEASE, NOT SERVED FROM HERE.
                # The app is 76 MB. This box is 2 vCPUs on a 13 Mbps uplink and
                # also serves the map, so handing that file to a crowd would
                # take the site down at exactly the moment attention arrives -
                # which is the moment the download matters. GitHub carries it
                # for free and is built for it.
                #
                # `/releases/latest/download/` is a stable URL that always
                # points at the newest release's asset of that name, so cutting
                # a new version needs no change here.
                url = _download_url()
                if not url:
                    return self._err(404, "no desktop build is published yet")
                self._status = 302
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                return

            if p == "/api/download":
                # Same-origin, so the page can ask without CORS - a HEAD from
                # the browser straight to GitHub is opaque and would leave the
                # button hidden even when the file is there.
                url = _download_url()
                return self._json({"available": bool(url), "url": url})
            # What a node costs in compute, and how to measure your own board
            # rather than take this page's word for it.
            if p == "/hardware":         return self._file(PUBLIC / "hardware.html")
            # Building a long-lens node from salvaged CCTV optics: the range
            # geometry against this project's own two thresholds (120px of
            # vehicle, ~60px of plate), and the assembly order. Public because
            # the interesting half is the arithmetic, which applies to any lens
            # somebody already owns - not just the one this was written for.
            if p == "/build16":          return self._file(PUBLIC / "build16.html")
            # 🚨 COMMUNITY LABELLING. Public on purpose, and safe because of
            # what it cannot do rather than who it lets in: a vote lands in a
            # SEPARATE database file with no sightings table, it never becomes a
            # label here, and every crop in a task is from a PUBLIC traffic
            # camera carrying an opaque id with no day, node, time or place.
            # See help_api.py, which exists to hold those limits in one place.
            if p == "/help":             return self._file(PUBLIC / "help.html")
            if p == "/api/help/next":
                import help_api
                voter = (q.get("voter") or [""])[0]
                return self._json(help_api.next_for(voter))
            if p == "/api/help/stats":
                import help_api
                return self._json(help_api.stats())
            if p.startswith("/api/help/img/"):
                import help_api
                raw = help_api.image(p[len("/api/help/img/"):])
                if raw is None:
                    return self._err(404, "no such crop")
                return self._send(200, raw, "image/jpeg")
            # One program, three modes. /node and /key are kept as aliases
            # because keys, QR codes and bookmarks already point at them - a
            # link a volunteer printed must not stop working because the pages
            # were reorganised.
            #
            # /contribute is kept for the same reason, but it no longer has a
            # page of its own: log-by-hand was removed, so the alias lands on
            # the app rather than 404ing a printed link.
            if p in ("/app", "/node", "/key", "/contribute"):
                return self._file(PUBLIC / "app.html")
            # The way back in for a camera whose browser lost its key. See
            # /api/node/whoami for what was actually happening to these people.
            if p == "/admin/bugs":
                if not self._is_local():
                    return self._err(403, "operator only")
                return self._file(PUBLIC / "bugs.html")
            if p == "/api/bug/list":
                if not self._is_local():
                    return self._err(403, "operator only")
                q = parse_qs(urlparse(self.path).query)
                return self._json({"bugs": bugs.listing(
                    limit=200, include_closed=(q.get("all", ["0"])[0] == "1"))})
            if p.startswith("/api/bug/shot/"):
                # 🚨 OPERATOR ONLY, AND NOT IN SNAPS. A reporter's screenshot
                # can contain their own camera key or the QR that is their key.
                # It is served from core.BUGS, which no other route touches, so
                # a leaked filename reaches nothing.
                if not self._is_local():
                    return self._err(403, "operator only")
                raw = bugs.shot_bytes(p.rsplit("/", 1)[-1])
                if not raw:
                    return self._err(404, "no screenshot")
                return self._send(200, raw, "image/jpeg")

            if p in ("/signin", "/login/camera"):
                return self._file(PUBLIC / "signin.html")
            if p.startswith("/vendor/"):
                # 🚨 `.name` FLATTENS THE PATH, WHICH IS THE TRAVERSAL GUARD AND
                # ALSO WHY /vendor/images/* 404d. Leaflet asks for
                # /vendor/images/marker-icon.png; `.name` turned that into
                # vendor/marker-icon.png, which does not exist - so the marker
                # on /IPCamera rendered as a broken-image box, on the one
                # control the page asks a business to drag.
                #
                # The guard stays. One subdirectory is allowed and it is named
                # literally, so nothing here can walk anywhere else: any segment
                # that is not "images" falls through to the flat lookup, and the
                # filename is still reduced to its own `.name`.
                rest = [seg for seg in p[8:].split("/") if seg not in ("", ".", "..")]
                if len(rest) == 2 and rest[0] == "images":
                    return self._file(PUBLIC / "vendor" / "images"
                                      / Path(rest[1]).name)
                return self._file(PUBLIC / "vendor" / Path(p[8:]).name)
            if p.startswith("/static/"): return self._file(PUBLIC / Path(p[8:]).name)
            if p.startswith("/snap/"):   return self._file(SNAPS / Path(unquote(p[6:])).name)

            # --- reviewer app (token-gated; separate from operator /review) ---
            if p == "/drive":
                return self._file(PUBLIC / "drive.html")
            if p == "/api/drive/reports":
                # Live crowd reports for the driving radar. Public read, like the
                # map. Ephemeral and unverified by construction.
                return self._json({"reports": db.active_driver_reports()})
            if p == "/api/geocode":
                # 🚨 PROXIED, NEVER CALLED FROM THE BROWSER.
                # Tiles already go through /api/tile and road lookups happen in
                # road.py for the same reason: a visitor of this site never
                # talks to a third party. A client-side geocode would hand
                # someone else every visitor's IP together with the place they
                # searched for - on a site about being watched, that is the one
                # request you cannot afford to leak.
                # `q` is already the parsed query dict in this handler; the
                # search term needs its own name or it silently shadows it.
                term = (q.get("q") or [""])[0].strip()[:120]
                if len(term) < 3:
                    return self._json({"results": []})
                # 🚨 CACHE FIRST, BUDGET SECOND. This spent the rate-limit
                # token BEFORE looking in the cache, so repeated searches for
                # the same place burned quota they never needed - and because
                # Caddy strips XFF, client_ip is 127.0.0.1 for everyone and the
                # 300/hour bucket is NETWORK-WIDE. A handful of people searching
                # the same town could 429 the search box for the entire site
                # while the answer sat in memory. The budget exists to protect
                # NOMINATIM; a cache hit never touches Nominatim.
                hit = _GEO_CACHE.get(term.lower())
                if hit and now() - hit[0] < 86400:
                    return self._json({"results": hit[1]})
                if not rate_ok("/api/geocode", self.client_ip):
                    return self._err(429, "too many searches right now; "
                                          "try again in a moment")
                # hub.py imports urllib.parse only, so urllib.request has to
                # be imported here. The first version assumed it was module-
                # level and raised NameError - which the broad `except` below
                # then reported to the user as "search unavailable right now",
                # blaming a third party for a typo. Catch only what a network
                # call can actually do to us.
                import urllib.error
                import urllib.parse as _up
                import urllib.request as _ur
                try:
                    req = _ur.Request(
                        "https://nominatim.openstreetmap.org/search?"
                        + _up.urlencode({"format": "json", "limit": "6",
                                         "q": term}),
                        # Nominatim's policy requires an identifying agent. A
                        # generic one gets the whole project blocked, and the
                        # block would look like "search is broken".
                        headers={"User-Agent": "SparrowMap/1.0 "
                                               "(https://sparrowmap.com)"})
                    with _ur.urlopen(req, timeout=12) as r:
                        raw = json.loads(r.read())
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    print(f"[geocode] upstream failed: {exc}")
                    return self._json({"results": [], "error": "search "
                                       "unavailable right now"})
                out = [{"name": str(x.get("display_name") or "")[:140],
                        "lat": float(x["lat"]), "lon": float(x["lon"]),
                        "kind": str(x.get("type") or "")}
                       for x in raw if x.get("lat") and x.get("lon")]
                if len(_GEO_CACHE) > 500:
                    _GEO_CACHE.clear()
                _GEO_CACHE[term.lower()] = (now(), out)
                return self._json({"results": out})

            if p in ("/planes", "/api/aircraft"):
                # 🧪 STAGED. Off unless a deployment opts in, so this can live
                # in the repo - local, repo and box identical - without showing
                # on a public map before it has earned a place there.
                if not CONFIG.get("aircraft_preview"):
                    return self._err(404, "not found")
                if p == "/planes":
                    return self._file(PUBLIC / "planes.html")
                try:
                    import aircraft
                except Exception as exc:
                    return self._json({"error": f"aircraft module: {exc}"})
                try:
                    box = [float(x) for x in
                           (q.get("box") or ["42.3,-84.4,43.3,-83.2"])[0].split(",")]
                except ValueError:
                    return self._err(400, "box wants lamin,lomin,lamax,lomax")
                if not rate_ok("/api/geocode", self.client_ip):
                    return self._err(429, "too many lookups right now")
                return self._json(aircraft.live(box))

            if p == "/api/scanner":
                # Where to LISTEN to public-safety radio for a place.
                #
                # 🚨 A LINK, NEVER A STREAM. Broadcastify's terms allow a feed
                # OWNER to embed their OWN feed with a domain key, and forbid
                # becoming "a redistribution layer" - proxying their audio to
                # visitors is exactly the banned case. Receiving unencrypted
                # public-safety radio is legal; rebroadcasting someone else's
                # infrastructure is a contract question, and the answer is no.
                # So this resolves a place to their STATE page and stops.
                #
                # stid is the FIPS state code, verified against two states
                # (Louisiana 22, Michigan 26) rather than assumed - county ids
                # are opaque internal numbers, so this deliberately links one
                # level up and lets the reader pick their own county. A link
                # that is always right beats a deep link that is sometimes
                # wrong about which county is watching them.
                try:
                    lat = float((q.get("lat") or [""])[0])
                    lon = float((q.get("lon") or [""])[0])
                except (TypeError, ValueError):
                    return self._err(400, "lat and lon required")
                key = f"{lat:.2f},{lon:.2f}"      # ~1 km, plenty for a state
                hit = _GEO_CACHE.get("rev:" + key)
                if hit and now() - hit[0] < 86400:
                    return self._json(hit[1])
                if not rate_ok("/api/geocode", self.client_ip):
                    return self._err(429, "too many lookups right now")
                import urllib.error
                import urllib.parse as _up
                import urllib.request as _ur
                try:
                    req = _ur.Request(
                        "https://nominatim.openstreetmap.org/reverse?"
                        + _up.urlencode({"format": "json", "zoom": "8",
                                         "lat": lat, "lon": lon}),
                        headers={"User-Agent": "SparrowMap/1.0 "
                                               "(https://sparrowmap.com)"})
                    with _ur.urlopen(req, timeout=12) as r:
                        addr = (json.loads(r.read()) or {}).get("address") or {}
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    print(f"[scanner] upstream failed: {exc}")
                    return self._json({"ok": False})
                iso = str(addr.get("ISO3166-2-lvl4") or "")   # e.g. "US-MI"
                fips = US_STATE_FIPS.get(iso.split("-")[-1].upper())
                out = {"ok": bool(fips),
                       "state": addr.get("state"),
                       "county": addr.get("county"),
                       "url": (f"https://www.broadcastify.com/listen/stid/{fips}"
                               if fips else None)}
                _GEO_CACHE["rev:" + key] = (now(), out)
                return self._json(out)

            if p == "/api/places":
                # Towns with cameras, for the zoomed-out map.
                #
                # 🚨 WHY THIS EXISTS: AT LOW ZOOM THE SPANS BECOME MARKERS.
                # An 80 m watched span is 91 px at zoom 17 and under a pixel by
                # zoom 12, so a national view renders thirty sub-pixel green
                # smears - which read as "a thing is at this spot", the precise
                # impression the corridor shape was drawn to prevent. A town
                # badge says what is actually known at that zoom.
                #
                # It publishes LESS than the map already does: a watched span is
                # a named stretch of a named street, and the town containing it
                # is strictly coarser. No new exposure.
                #
                # ⚠️ The point is the mean of the JITTERED node positions, never
                # the true ones, and it is only ever rendered as a town-sized
                # badge - so it locates a town, which is what it claims.
                places: dict = {}
                for nd in db.nodes(active_only=True):
                    name = (nd.get("place") or "").strip()
                    if not name:
                        continue          # unresolved: absent beats invented
                    la, lo = nd.get("pub_lat"), nd.get("pub_lon")
                    if la is None or lo is None:
                        continue
                    e = places.setdefault(name, {"place": name, "cameras": 0,
                                                 "online": 0, "_la": 0.0,
                                                 "_lo": 0.0})
                    e["cameras"] += 1
                    e["_la"] += la
                    e["_lo"] += lo
                    if nd.get("last_beat") and now() - nd["last_beat"] \
                            < db.beat_window(nd.get("kind") or ""):
                        e["online"] += 1
                out = []
                for e in places.values():
                    c = e.pop("cameras")
                    out.append({"place": e["place"], "cameras": c,
                                "online": e["online"],
                                "lat": round(e.pop("_la") / c, 4),
                                "lon": round(e.pop("_lo") / c, 4)})
                out.sort(key=lambda x: -x["cameras"])
                return self._json({"places": out,
                                   "cameras_placed": sum(x["cameras"] for x in out)})

            if p == "/api/heat":
                # Cumulative "everywhere a patrol has ever been confirmed",
                # aggregated to a grid. The published record, gridded.
                cells = db.gov_heat()
                return self._json({"cells": cells,
                                   "total": sum(c["n"] for c in cells)})

            if p == "/api/node/me":
                # A camera's owner reading back their OWN placement, to change
                # it. Gated by the node's own token - the same secret that lets
                # that camera post sightings, so this grants nothing new.
                #
                # 🚨 THIS IS THE ONE ENDPOINT THAT RETURNS A TRUE lat/lon.
                # Everything else publishes the span and the jittered point,
                # because a camera position identifies a house. The owner
                # already knows where their own camera is, and cannot aim it
                # without seeing it on a map - but that makes the token check
                # the whole of the protection here, so it is checked before
                # anything is read, and a node id alone (they are printed on
                # the public map) gets nothing.
                nd = db.node((q.get("id") or [""])[0])
                if not nd:
                    return self._err(404, "unknown camera")
                if not nd.get("token") or not self._token_ok(nd):
                    return self._err(401, "this camera's token is required")
                return self._json({
                    "id": nd["id"], "name": nd.get("name") or nd["id"],
                    "kind": nd.get("kind") or "fixed",
                    "lat": nd["lat"], "lon": nd["lon"],
                    "heading": nd.get("heading") or 0,
                    "fov": nd.get("fov") or 60,
                    "reach_m": nd.get("reach_m") or 45,
                    "road_name": nd.get("road_name"),
                    "span_source": nd.get("span_source"),
                    "span": node_mod.span_of(nd),
                    # Whether the owner has agreed to publish that span. Sent so
                    # the aim page can show the switch in its true position -
                    # a consent control that renders "off" on every load is
                    # indistinguishable from one that never saved.
                    "publish_span": bool(nd.get("publish_span")),
                    "sightings": nd.get("sightings") or 0,
                    "last_seen": nd.get("last_seen"),
                })

            if p == "/aim":
                # Aiming a camera from the device that owns it. The capability
                # is in the fragment or in this device's localStorage, so the
                # page is served to anyone and shows nothing without one.
                return self._file(PUBLIC / "aim.html")

            # 🚨 ONE APP, THREE DOORS. `/rv` was the only way in and nothing
            # linked to it, so in practice there was no way in at all: the map
            # never mentioned it, and the single pointer that existed was a
            # banner on /app shown once, just after a token was minted. Close
            # that tab and the review queue is gone.
            #
            # The two scopes are now separate PAGES rather than a dropdown
            # inside one, because they are different jobs. "Is this my camera's
            # catch right?" and "help work through everyone's backlog" want
            # different framing, different empty states and different urgency,
            # and a control that silently changes which cameras you are ruling
            # on is a control someone will get wrong.
            #
            # Same file for all three: the page reads its own path and locks
            # its scope to it. Three copies of a 16 KB reviewer app would drift
            # apart by the second bug fix.
            if p in ("/rv", "/rv/mine", "/rv/pool"):
                return self._file(PUBLIC / "rv.html")
            if p == "/rv/admin":
                # Served open; the token endpoints it drives are operator-gated,
                # and the page shows an operator sign-in until you are.
                return self._file(PUBLIC / "rv-admin.html")
            if p == "/api/rv/me":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                return self._json({"ok": True, "label": r["label"],
                                   "scope": r["scope"],
                                   "nodes": sorted(r["nodes"]) if r["nodes"] else []})
            if p == "/api/rv/queue":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                scope = (q.get("scope") or ["pool"])[0]
                # ?rejected=1 asks for the pile the head threw out, so the
                # filter can be audited by the people it filters for.
                rejected = (q.get("rejected") or ["0"])[0] in ("1", "true", "yes")
                return self._json(review_api.queue(r, scope, rejected=rejected))
            if p == "/api/rv/contributed":
                # Counts only - never rows. It exists so an empty review queue
                # can say what the camera HAS done instead of reading like a
                # broken page to someone who just installed one.
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                return self._json(review_api.contributed(r))

            # --- retracted-photo shelf (pool-scope reviewer only) ------------
            # A retraction demotes the row and drops the plate, but never
            # touched the picture, and /snap/<name> serves any file by name -
            # so the photograph of a vehicle the map no longer claims stayed
            # fetchable by direct URL. This is where those are listed and
            # deleted. Gated on pool scope rather than an operator secret: a
            # mirror has no operator surface by design, and a pool reviewer can
            # already see and retract everything anyway.
            if p == "/rv/retracted":
                return self._file(PUBLIC / "retracted.html")
            if p == "/api/rv/retracted":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                if not review_api.is_trusted(r):
                    return self._err(403, "pool reviewers only")
                return self._json(review_api.retracted(r))
            if p.startswith("/api/rv/retracted/photo/"):
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                try:
                    sid = int(p.rsplit("/", 1)[-1])
                except ValueError:
                    return self._err(400, "bad id")
                b = review_api.retracted_photo_bytes(r, sid)
                if not b:
                    return self._err(404, "no photo")
                return self._send(200, b, "image/jpeg",
                                  {"Cache-Control": "no-store"})

            # --- held photographs: fix what is already on the map -------------
            # Where a privacy flag lands. The picture is off the map already
            # (moved into core.HELD, which no route serves); this is where a
            # human crops the person out and puts it back, restores it whole, or
            # deletes it. Any reviewer may work their own cameras' items, on the
            # rule in review_api.fix_photo: the two open actions can only ever
            # show LESS, and the one that re-publishes the flagged pixels is
            # gated on a trusted token.
            if p == "/rv/photos":
                return self._file(PUBLIC / "photos.html")
            if p == "/api/rv/held":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                return self._json(review_api.held_queue(r))
            if p.startswith("/api/rv/held/photo/"):
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                try:
                    sid = int(p.rsplit("/", 1)[-1])
                except ValueError:
                    return self._err(400, "bad id")
                b = review_api.held_photo_bytes(r, sid)
                if not b:
                    return self._err(404, "no photo")
                # no-store, like the retracted shelf: this is a picture that has
                # been taken off the public map, and a cache is a second copy of
                # it in a place nobody can revoke.
                return self._send(200, b, "image/jpeg",
                                  {"Cache-Control": "no-store"})

            if p.startswith("/api/rv/crop/"):
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                try:
                    sid = int(p.rsplit("/", 1)[-1])
                except ValueError:
                    return self._err(400, "bad id")
                # An 'own'-scoped token may only see its own cameras' crops -
                # checked here, not only in the queue listing.
                if not review_api.may_touch(r, sid):
                    return self._err(404, "no crop")
                b = review_api.crop_bytes(sid)
                if not b:
                    return self._err(404, "no crop")
                return self._send(200, b, "image/jpeg",
                                  {"Cache-Control": "no-store"})
            if p == "/api/rv/progress":
                # How close the label set is to training a detector small enough
                # to run on a phone. Computed on the machine that holds the crops
                # (tools/label_progress.py) and pushed here, because the mirror
                # deliberately banks nothing and cannot count them itself.
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                try:
                    return self._json(json.loads(
                        (DATA / "label_progress.json").read_text(encoding="utf-8")))
                except Exception:
                    return self._json({"unavailable": True})

            if p == "/api/rv/tokens":
                if not self._is_local():
                    return self._err(403, "operator only")
                return self._json(review_api.list_tokens())

            if p == "/api/health":
                # 🚨 THIS ROUTE SAID "ok": true THROUGH A TWO-HOUR OUTAGE.
                #
                # It reported that the HTTP server was answering, which was
                # true and useless: the process had exhausted its file
                # descriptors, every other route was returning "unable to open
                # database file", and the one endpoint a watchdog would poll
                # was the one endpoint that touched nothing. A health check
                # that cannot fail for the reason the service actually fails is
                # worse than none, because it is believed.
                #
                # So it now does the two things it was missing:
                #
                #  1. TOUCHES THE DATABASE. `ok` means "this process can serve
                #     a request", not "this process accepted a socket".
                #  2. REPORTS HEADROOM, NOT JUST STATE. Descriptor exhaustion
                #     is a RAMP - it climbed for roughly 45 minutes before the
                #     cliff - so a watcher that only sees up/down learns about
                #     it strictly too late. `fd_used_pct` is the leading
                #     indicator; the crash is the lagging one.
                #
                # Counts of descriptors and threads say nothing about any
                # vehicle or visitor, so this stays unauthenticated like the
                # rest of the operational surface.
                health = {"ok": True, "version": VERSION, "ts": now()}
                try:
                    db.connect().execute("SELECT 1").fetchone()
                    health["db"] = "ok"
                except Exception as exc:
                    health["ok"] = False
                    health["db"] = f"{type(exc).__name__}: {exc}"
                try:
                    import os as _os
                    import resource
                    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                    used = len(list(Path(f"/proc/{_os.getpid()}/fd").iterdir()))
                    health["fd_used"] = used
                    health["fd_limit"] = soft
                    health["fd_used_pct"] = round(100.0 * used / soft, 1)
                    health["threads"] = threading.active_count()
                    # 🚨 A POINT SAMPLE CANNOT SEE A RAMP, AND THE RAMP IS THE
                    # WHOLE STORY. An hourly checker polling only "how are you
                    # right now" would have watched this exact outage develop
                    # and reported ok, ok, ok, dead - because the climb from
                    # healthy to fatal fits between two visits. The high-water
                    # marks are what a visitor who was not here needs, so they
                    # are kept in-process (free, no file, no permissions) and
                    # published alongside the live numbers.
                    #
                    # ⚠️ `uptime_s` IS PART OF THE SIGNAL, NOT DECORATION. These
                    # peaks reset when the process does, so a small uptime is
                    # the caller's only warning that the peaks it is reading
                    # describe a short window - and that something restarted
                    # this service, which is itself the thing worth asking
                    # about.
                    _PEAK["fd_pct"] = max(_PEAK["fd_pct"], health["fd_used_pct"])
                    _PEAK["threads"] = max(_PEAK["threads"], health["threads"])
                    health["fd_peak_pct"] = _PEAK["fd_pct"]
                    health["threads_peak"] = _PEAK["threads"]
                    health["uptime_s"] = round(time.time() - _STARTED, 1)
                    # Degraded, not dead. Something is retaining descriptors
                    # and there is still time to look at it.
                    if used > soft * 0.8:
                        health["ok"] = False
                        health["warn"] = (f"{used}/{soft} file descriptors in "
                                          f"use - approaching the limit, past "
                                          f"which every route fails")
                except Exception:
                    pass          # not Linux, or no /proc: report what we have

                # 🚨 WHAT IS ACTUALLY HOLDING THE REQUEST PERMITS.
                # Published because this limiter has now refused visitors on an
                # idle box three times, and each diagnosis needed a live
                # investigation that this one field would have answered.
                # ⚠️ SNAPSHOT UNDER THE LOCK, SORT OUTSIDE IT.
                # This sorted the whole dict INSIDE _INFLIGHT_LOCK - the lock
                # every gated request takes twice - so an O(N log N) pass ran
                # while holding the one thing serialising all traffic. It grew
                # during overload and was read by the watchdog, which restarts
                # the service from this very URL: the diagnostic stalled the
                # server hardest exactly when it was being consulted about a
                # stall.
                with Handler._INFLIGHT_LOCK:
                    holding = list(Handler._INFLIGHT_PATHS.values())
                    slow = list(Handler._SLOW_HELD.items())
                health["slowest_ever"] = dict(
                    sorted(slow, key=lambda kv: -kv[1])[:6])
                nowt = time.time()
                health["inflight"] = len(holding)
                health["inflight_cap"] = MAX_REQUESTS
                # ⚠️ REPORTED, for the same reason _gated records who holds a
                # permit: this cap is the one that stopped the OOM kills, and a
                # cap nobody can watch is a cap that gets tuned by argument.
                # heavy_free near 0 under load means readers are queueing on
                # map data and MAX_HEAVY is the thing to look at first.
                health["heavy_cap"] = MAX_HEAVY
                health["heavy_free"] = Handler._HEAVY._value
                health["ingest_cap"] = MAX_INGEST
                health["ingest_free"] = Handler._INGEST._value
                # Longest-held first: the one at the top is the one to blame.
                # ⚠️ THE ONE RESOURCE THE LAST OUTAGE FIX INTRODUCED, AND THE
                # ONLY ONE HEALTH DID NOT REPORT. Saturate these 12 permits and
                # fds, threads and inflight all look perfectly normal while the
                # map goes blank - a failure mode invisible to every existing
                # number. Same for Overpass: a road lookup queue that is full
                # stalls enrolment and drive reports with nothing else moving.
                try:
                    health["tile_fetch_free"] = _TILE_FETCH._value
                    health["tile_fetch_cap"] = 12
                except Exception:
                    pass
                try:
                    import road as _road
                    health["road_lookup_free"] = _road._OVERPASS_SLOTS._value
                    health["road_cells_failing"] = len(_road._FAIL_UNTIL)
                except Exception:
                    pass
                health["inflight_now"] = [
                    {"path": lbl, "held_s": round(nowt - t0, 1)}
                    for lbl, t0 in sorted(holding, key=lambda x: x[1])[:8]]
                return self._json(health, 200 if health["ok"] else 503)

            if p == "/api/policy":
                # The privacy posture of this deployment, machine-readable.
                # Anyone mirroring or auditing the network can diff it.
                return self._json({
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
                    # Whether this deployment is currently willing to assert
                    # "police" about a vehicle. Published so an outside auditor
                    # can see that the tier is gated rather than having to
                    # infer it from an empty map. See classify.py.
                    "publishes_public_tier": CONFIG.get("publish_public_tier", False),
                    # 🚨 MEASURED, NOT ASSERTED.
                    # This used to publish `classifier_validated`, whose value
                    # was a verbatim copy of the line above - a config toggle
                    # wearing the name of a validation that had never happened.
                    # A transparency endpoint asserting its own validation with
                    # nothing behind it is the most attackable thing a project
                    # like this can ship, so it is replaced by counts anyone can
                    # check: how many public sightings a PERSON decided, out of
                    # how many there are. `decided_by` records that at the point
                    # of the decision; rows predating the column read 'unknown'
                    # and are reported separately rather than being quietly
                    # counted as human.
                    **db.public_decision_counts(),
                    # Where the map opens. This is CONFIG, not a camera: it
                    # belongs to the deployment, and it is served rather than
                    # hardcoded in app.js so that no real coordinate has to
                    # live in the published source. It is deliberately
                    # town-level and says nothing a viewer cannot see anyway -
                    # the watched spans are far more precise than this.
                    "map_center": CONFIG["map_center"],
                    "map_zoom": CONFIG["map_zoom"],
                })

            if p == "/api/whoami":
                # Lets the review page tell "you are not signed in" apart from
                # "the server is broken", without revealing anything.
                return self._json({"operator": self._is_local(),
                                   "auth_required": operator_auth.required()})
            if p == "/api/plate":
                # Search government plates. Only confirmed public-tier rows are
                # scanned at all - see db.search_plate for why filtering in the
                # redactor alone would still leak a yes/no answer about private
                # vehicles.
                # Searching public-tier data is NOT logged, on purpose. The
                # target of this system is government vehicles on public roads,
                # not the people curious enough to look them up. A search history
                # - even a truncated one - would be a chilling record of who
                # asked a question about a public record, which is exactly the
                # surveillance posture SparrowMap exists to refuse. Reading public
                # data is nobody's business but the reader's.
                query = (q.get("q") or [""])[0]
                rows = db.search_plate(query)
                return self._json({"query": query,
                                   "results": _public_rows(rows)})
            if p == "/api/stats":
                return self._json(db.stats())

            if p == "/sw.js":
                # Served at the ROOT so its scope covers /app and /node - a
                # service worker only controls pages under its own path. Its
                # Cache-Control is the default no-store, which is right: the
                # browser must re-check it to pick up an updated worker.
                return self._file(PUBLIC / "sw.js")

            if p == "/login":
                # The one operator page that must be reachable WITHOUT being the
                # operator - it is where you become one. Serving it never leaks
                # anything: it only offers a box to paste the token that already
                # lives in data/operator.token on this machine. When auth is off
                # it says so and points at /review.
                return self._file(PUBLIC / "login.html")

            if p == "/review":
                # OPERATOR ONLY. This page shows what the map has ASSERTED and
                # lets it be taken back, which is not a power a visitor gets.
                # A 403 here now points at /login instead of dead-ending, so
                # turning auth on does not read as "the review page is broken".
                if not self._is_local():
                    return self._err(403, "not signed in - open /login")
                return self._file(PUBLIC / "review.html")

            if p == "/api/review/queue":
                if not self._is_local():
                    return self._err(403, "local only")
                rows = db.connect().execute(
                    "SELECT * FROM sightings WHERE tier='public' "
                    "ORDER BY (reviewed IS NOT NULL), ts DESC LIMIT 200"
                ).fetchall()
                report_counts = db.open_report_counts()
                out = []
                for r in rows:
                    d = dict(r)
                    item = {k: d.get(k) for k in
                            ("id", "ts", "vclass", "vclass_conf", "vclass_why",
                             "plate_text", "snap", "node_id", "reviewed",
                             "reviewed_at", "source", "color", "body")}
                    # Attach any public flags so the operator sees WHAT a
                    # stranger disputed, not just that something is disputed.
                    item["reports"] = (db.reports_for(d["id"])
                                       if report_counts.get(d["id"]) else [])
                    out.append(item)
                # Flagged-but-unreviewed rows are the ones a human most needs to
                # look at, so lift them to the top without disturbing the rest.
                out.sort(key=lambda x: (x["reviewed"] is not None,
                                        0 if x["reports"] else 1))
                # 🚨 MISSES ARE ALSO ERRORS, and only one direction was
                # correctable. The first real patrol unit this network saw sat
                # in the private tier because a gate read a field nothing
                # populated - a bug, not a decision - and nothing surfaced it.
                # These are private sightings the model DID think were police,
                # offered for promotion. Bounded to the recent window: this is
                # a review queue, not a second map.
                missed = []
                try:
                    from detect import bank
                    day = now() - 86400 * 3
                    rows2 = db.connect().execute(
                        "SELECT * FROM sightings WHERE tier='private' AND ts > ? "
                        "AND reviewed IS NULL ORDER BY ts DESC LIMIT 400",
                        (day,)).fetchall()
                    from detect import head as _head
                    hthr = _head.threshold() if _head.available() else None
                    for r in rows2:
                        # One indexed column, one file read - no scanning.
                        if not r["bank_ref"]:
                            continue
                        j = bank.sidecar(r["bank_ref"])
                        if not j:
                            continue
                        meta = json.loads(j.read_text(encoding="utf-8"))
                        clip = meta.get("clip") or {}
                        # The trained head decides when it has looked at the
                        # crop; CLIP's raw argmax only gates the ones it never
                        # saw. This matters most for PHONE crops: a phone cannot
                        # run the head, so classify_worker scores them here later
                        # and records `head_conf`. CLIP's argmax alone calls ~13%
                        # of ordinary traffic police, so gating on it floods the
                        # queue with exactly the false positives the head exists
                        # to reject - honour the head whenever there is one.
                        hc = clip.get("head_conf")
                        if hc is not None and hthr is not None:
                            if hc < hthr:
                                continue
                        elif (clip.get("vclass") != "police"
                                or (clip.get("conf") or 0) < 0.50
                                or (clip.get("margin") or 0) < 0.20):
                            continue
                        d = dict(r)
                        missed.append({
                            **{k: d.get(k) for k in
                               ("id", "ts", "vclass", "snap", "node_id")},
                            # Sort and show the head's confidence when it decided,
                            # so the strongest head calls float to the top rather
                            # than being ordered by a CLIP score the head overrode.
                            "clip_conf": hc if (hc is not None and hthr is not None)
                            else clip.get("conf"),
                            "clip_margin": clip.get("margin"),
                            "by_head": hc is not None and hthr is not None,
                            "label": meta.get("label"),
                        })
                    missed.sort(key=lambda m: -(m.get("clip_conf") or 0))
                    missed = missed[:40]
                except Exception:
                    traceback.print_exc()
                # ⚠️ THE HEADER MUST COUNT EVERYTHING THAT NEEDS A DECISION.
                # `pending` counts only unreviewed PUBLISHED sightings, so the
                # page read "0 to review" while three possibly-missed cards sat
                # below it with live buttons. A counter that ignores half the
                # work is a counter that teaches you to ignore it.
                return self._json({"queue": out, "missed": missed,
                                   "missed_pending": len(missed),
                                   **db.review_stats()})

            if p == "/api/pending":
                # 🚨 "SOMETHING HERE WANTS A HUMAN", AND NOTHING MORE.
                #
                # A coarse cell and a count, for sightings the classifier called
                # police or government and nobody has reviewed. It exists so the
                # map can show that a possible patrol car is waiting on a person
                # WITHOUT asserting one is there - which is the same
                # propose/human-decide split the rest of the project runs on,
                # made visible instead of hidden in a queue.
                #
                # ⚠️ The coarsening happens in db.pending_areas, not here and
                # certainly not in the browser: an unreviewed claim's exact
                # position must never leave the database at all.
                return self._json({"cells": db.pending_areas(),
                                   "cell_deg": db.PENDING_CELL,
                                   "window_s": db.PENDING_WINDOW_S})

            if p.startswith("/api/tile/"):
                return self._tile(p)

            if p == "/api/cameras":
                # Flock/ALPR surveillance-camera overlay (OpenStreetMap /
                # DeFlock), bounded by viewport. The client sends its box and
                # gets only the cameras inside it - the nationwide set is tens of
                # thousands - and the map only asks above a zoom threshold.
                # Cameras a review has confirmed REMOVED are dropped, so the
                # layer shows what is still believed to be up.
                _q = parse_qs(urlparse(self.path).query)
                box = _q.get("box", [None])[0]
                cams = _surveillance_cameras()
                gone = _cameras_removed()
                ok_rf = _cameras_confirmed()
                out = []
                if box:
                    try:
                        s, w, n, e = (float(x) for x in box.split(","))
                        # Collect every camera in the viewport, THEN sample evenly
                        # if there are more than the cap. Taking the first 1500 in
                        # list order clumped them wherever the data happened to
                        # start - so zoomed all the way out you saw a blob in one
                        # region, or nothing where you were looking. An even
                        # stride spreads the sample across the whole view.
                        hits = [row for row in cams
                                if row[0] not in gone
                                and s <= row[1] <= n and w <= row[2] <= e]
                        cap = 1500
                        if len(hits) > cap:
                            step = len(hits) / float(cap)
                            hits = [hits[int(i * step)] for i in range(cap)]
                        out = [{"id": row[0], "lat": row[1], "lon": row[2],
                                "dir": row[3] if len(row) > 3 else "",
                                "confirmed": row[0] in ok_rf} for row in hits]
                    except (ValueError, AttributeError):
                        out = []
                return self._json({"cameras": out, "total": len(cams),
                                   "removed": len(gone), "confirmed": len(ok_rf)})

            if p == "/api/police":
                # Police-station overlay (OpenStreetMap), bounded by viewport.
                # The client sends its box and gets only the stations inside it,
                # so the nationwide ~15k points never reach a phone at once; the
                # map only asks for this above a zoom threshold. Capped as a
                # backstop. A distinct layer, deliberately NOT a sighting.
                _q = parse_qs(urlparse(self.path).query)
                box = _q.get("box", [None])[0]
                pts = _police_stations()
                out = []
                if box:
                    try:
                        s, w, n, e = (float(x) for x in box.split(","))
                        # Even sample when the viewport holds more than the cap,
                        # same reason as /api/cameras: a first-N slice clumps.
                        hits = [row for row in pts
                                if s <= row[0] <= n and w <= row[1] <= e]
                        cap = 1500
                        if len(hits) > cap:
                            step = len(hits) / float(cap)
                            hits = [hits[int(i * step)] for i in range(cap)]
                        out = [{"lat": row[0], "lon": row[1],
                                "name": row[2] if len(row) > 2 else ""}
                               for row in hits]
                    except (ValueError, AttributeError):
                        out = []
                return self._json({"stations": out, "total": len(pts)})

            if p == "/api/radar":
                # Live radar / speed-trap sources near a viewport. Reads are
                # public (there is nothing to leak - a dot is "a Ka source was
                # active here two minutes ago"); only WRITES are authenticated.
                # Clustered and decayed server-side, so the client just draws
                # what it gets. Empty until detector hardware is feeding it,
                # exactly like the RF-confirm layer.
                _q = parse_qs(urlparse(self.path).query)
                box = _q.get("box", [None])[0]
                bb = None
                if box:
                    try:
                        bb = tuple(float(x) for x in box.split(","))
                    except ValueError:
                        bb = None
                dots = _radar_dots(bb)
                return self._json({"dots": dots, "active": len(dots),
                                   "ttl_s": int(RADAR_TTL_S)})

            if p == "/api/sensor":
                # Live drone / police-radio events near a viewport. Public read,
                # like /api/radar. kind selects the feed; empty until hardware
                # feeds it.
                _q = parse_qs(urlparse(self.path).query)
                kind = (_q.get("kind", [""])[0] or "").strip().lower()
                if kind not in LIVE_KINDS:
                    return self._err(400, "kind must be one of "
                                          + ", ".join(sorted(LIVE_KINDS)))
                box = _q.get("box", [None])[0]
                bb = None
                if box:
                    try:
                        bb = tuple(float(x) for x in box.split(","))
                    except ValueError:
                        bb = None
                pts = _live_points(kind, bb)
                return self._json({"kind": kind, "points": pts,
                                   "active": len(pts),
                                   "ttl_s": int(LIVE_TTL.get(kind, 300))})

            if p == "/api/nodes":
                # 🚨 4,800 PUBLIC CAMERAS WERE DOWNLOADED ON EVERY MAP LOAD IN
                # ORDER TO BE HIDDEN.
                #
                # This response went from ~90 KB to 1.48 MB when the traffic
                # camera fleet landed, and the layer that draws them now
                # defaults OFF because 4,800 markers made the map lag on a
                # phone. So the default page was paying a megabyte and a second
                # of parsing for rows it then filtered out client-side - a
                # bound applied to the DRAWING and not to the DOWNLOAD, which
                # is the mistake this project keeps making in a new place.
                #
                # The client asks for them only when the layer is on. Absent
                # parameter = include, so every existing caller of this API
                # (and anyone reading it from outside) sees exactly what it
                # always returned.
                _q = parse_qs(urlparse(self.path).query)
                want_public = (_q.get("public_cams", ["1"])[0] != "0")
                # 🗺️ AND BOUNDED BY VIEWPORT WHEN THE LAYER IS ON.
                #
                # A phone in Michigan has no use for 2,160 Finnish cameras, and
                # switching the layer on used to mean downloading every one of
                # them. Snapped OUTWARD to the same grid the cache keys on, so
                # the answer is a superset of what was asked for and panning
                # keeps hitting one key instead of minting one per drag.
                #
                # ⚠️ ONLY public_cam is filtered. Volunteer nodes are ~350 rows
                # and their SPANS are the map's actual content; dropping those
                # off-screen would make the map look empty while it loaded, and
                # they are not what made this response big.
                cam_box = None
                if "box" in _q:
                    try:
                        cam_box = tuple(float(x) for x in
                                        _snap_box(_q["box"][0]).split(","))
                    except (ValueError, AttributeError):
                        cam_box = None
                out = []
                for n in db.nodes():
                    if n["kind"] == "public_cam":
                        if not want_public:
                            continue
                        if cam_box and n["lat"] is not None:
                            s, w, ne, e = cam_box
                            if not (s <= n["lat"] <= ne and w <= n["lon"] <= e):
                                continue
                    span = node_mod.span_of(n)
                    # 🚨 NO CAMERA POSITION IS PUBLISHED AT ALL. Not the true
                    # one, not the jittered one.
                    #
                    # This used to send `pub_lat/pub_lon` - the 60 m jittered
                    # point - and the map drew a dot on it. Jitter of that size
                    # does not hide a camera on a residential street: it puts
                    # the dot within a few houses of the right one, which is
                    # close enough for anyone standing on that street. The
                    # protection was always the ROAD SPAN being the published
                    # unit, and the point beside it was a second, weaker claim
                    # about the same camera that could only ever narrow the
                    # first.
                    #
                    # Removing it from the DRAWING would have been theatre -
                    # `curl /api/nodes` returns whatever this dict holds. The
                    # field is gone from the response, so there is nothing to
                    # hide client-side. Same rule this file keeps relearning: a
                    # control applied to one representation is bypassed by the
                    # other. See isolate.py.
                    #
                    # heading/fov/reach are gone with it. They were only ever
                    # sent so a span-less node had something to draw a cone
                    # from; with no origin point a cone cannot be drawn, and an
                    # aim vector is precisely what re-derives a camera from its
                    # span.
                    # 🚨 THE SPAN IS THE OWNER'S TO PUBLISH, AND THEY WERE NEVER
                    # ASKED.
                    #
                    # The line below used to read "Public, exact, and the ONLY
                    # geometry a viewer gets - people are entitled to know where
                    # they are recorded." The entitlement is real. The mistake
                    # was deciding it on the volunteer's behalf: an 80 m span on
                    # a NAMED road is coarse in a town and close to an address
                    # in the country, and on a rural stretch overlooked by one
                    # house, the span plus the road name IS that house.
                    #
                    # ⚠️ AND THE "watched roads" CHECKBOX WAS NEVER A CONTROL ON
                    # THIS. It only decided whether the map DREW them; every
                    # span was in this response for anyone who ran curl. That is
                    # the same lesson three comments up - a control applied to
                    # one representation is bypassed by the other - arriving
                    # again, on the field those comments were protecting.
                    #
                    # So consent is checked HERE, at the point of publication.
                    # road_name and span_source go with it: naming the street a
                    # camera watches re-states most of the span in words.
                    shares = bool(n.get("publish_span"))
                    # 🚨 A GOVERNMENT TRAFFIC CAMERA IS NOT SOMEBODY'S HOUSE.
                    #
                    # Every rule above exists because a volunteer's camera
                    # position describes where they live, and the project
                    # refuses to publish that. A public_cam is the opposite
                    # case: it is a state-owned camera on a pole whose exact
                    # coordinates the transport department publishes itself, in
                    # the same feed we read the pictures from. Withholding it
                    # protects nobody and hides the coverage from the people
                    # deciding whether this project is worth anything.
                    #
                    # So these get their true position and nothing else changes:
                    # the consent gate above still governs every volunteer node,
                    # which is the field it was written to protect.
                    is_public_cam = n["kind"] == "public_cam"
                    rec = {
                        "id": n["id"], "name": n["name"],
                        "kind": n["kind"], "sightings": n["sightings"],
                        "lat": n["lat"] if is_public_cam else None,
                        "lon": n["lon"] if is_public_cam else None,
                        "span": span if shares else None,
                        "road_name": n["road_name"] if shares else None,
                        "span_source": n["span_source"] if shares else None,
                        "last_seen": n["last_seen"], "last_beat": n["last_beat"],
                        # 'online' now means the node SAID SO. It used to mean
                        # 'a car drove past in the last 15 minutes', which
                        # marked every camera on a quiet street as switched off
                        # and read as a fault the user then went looking for.
                        "online": bool(n["last_beat"] and n["last_beat"]
                                       > now() - db.beat_window(n["kind"])),
                    }
                    out.append(rec)
                return self._json(out)

            if p == "/api/sightings":
                since = float(q.get("since", [now() - 3600])[0])
                limit = int(q.get("limit", [400])[0])
                vclass = q.get("vclass", ["all"])[0]
                bbox = None
                if "bbox" in q:
                    try:
                        bbox = tuple(float(x) for x in q["bbox"][0].split(","))
                    except ValueError:
                        bbox = None
                rows = db.recent_sightings(since, limit, vclass, bbox)
                return self._json(_public_rows(rows))

            if p.startswith("/api/sighting/"):
                r = db.sighting(int(p.rsplit("/", 1)[1]))
                if not r:
                    return self._err(404, "no such sighting")
                # 🚨 NO PICTURE, NO PUBLICATION - HERE TOO.
                # recent_sightings withholds a public row with no photograph
                # from the feed, and a rule applied to one representation is
                # bypassed by the other: ids are sequential and printed beside
                # every sighting, so without this the withheld claim is still
                # one URL away. Same lesson as the span consent gate and the
                # /snap file that outlived its row.
                if r.get("tier") == "public" and not r.get("snap"):
                    return self._err(404, "no such sighting")
                # 🚨 READING IS NOT AUDITED, DELIBERATELY.
                # This used to write a row saying that somebody looked at this
                # sighting. The whole project refuses to keep a record of which
                # vehicle went where; keeping a record of which reader looked
                # at which vehicle is the same dossier pointed at the public.
                # And it is written on the way IN, so hiding the endpoint would
                # not help - the disk is what gets copied or compelled.
                # The audit log's purpose is to show what the OPERATOR did to
                # the record, and operator actions are still audited below.
                out = _public_rows([r])[0]
                # WHERE THE PHOTO WAS TAKEN, in words a reader can use.
                # The panel used to print the node id and a heading. A public
                # traffic camera is named and positioned by the transport
                # department that runs it, so it gets its name; a volunteer
                # camera is never named or placed - it gets the road and the
                # town, which the map dot already discloses, and nothing more.
                nd = db.node(r.get("node_id") or "") or {}
                is_pub = nd.get("kind") == "public_cam"
                out["where"] = {
                    "kind": nd.get("kind") or "fixed",
                    "camera": _cam_display_name(nd) if is_pub else None,
                    "road": (nd.get("road_name") or "").strip() or None,
                    "place": (nd.get("place") or "").strip() or None,
                }
                return self._json(out)

            if p.startswith("/api/track/"):
                key = unquote(p.rsplit("/", 1)[1])
                if key.startswith("tag:"):
                    # A trail drawn from LINKS, not a plate. Every row in the
                    # group carries tag_why, so the reader can see the reason
                    # for each hop; the panel words it as "possibly the same
                    # vehicle" and nothing here upgrades that to a fact.
                    # tagged_group only returns public rows, and only
                    # police/gov rows can ever carry a tag (db.tag_sighting).
                    rows = db.tagged_group(key[4:])
                else:
                    rows = db.track_for(_resolve_hash(key))
                if not rows:
                    return self._json([])
                # 🚨 NOT AUDITED - and this one was the worst of the two.
                # Its target was the PLATE TEXT itself, so following a
                # government vehicle's trail wrote "this reader looked up this
                # plate" to disk. Someone checking on a patrol car that
                # followed them home should leave no trace here.
                out = _public_rows(rows)
                # ⚠️ COMPUTED ONCE. This sat inside the loop and takes the WHOLE
                # row set as its argument, so it was recomputed identically for
                # every row - O(n squared) for a value that cannot vary between
                # them. ~287ms at the 500-row cap, on a route that is no-store
                # and therefore never absorbed by the cache. Latent only because
                # the largest plate group is currently one row; public rows are
                # served with their real plate_hash, so no alias is needed to
                # drive the group size up.
                score = classify.patrol_score(rows)
                for r in out:
                    r["patrol_score"] = score
                return self._json(out)

            if p == "/api/leaderboard":
                hours = int(q.get("hours", [24])[0])
                return self._json(db.leaderboard(hours))

            if p == "/api/audit":
                rows = db.connect().execute(
                    "SELECT ts, action, target, ip FROM audit ORDER BY ts DESC LIMIT 200"
                ).fetchall()
                # The operator's decisions on the map - confirms, retractions,
                # public flags. Searches are never in here (they are not logged
                # at all). The IP is truncated so even this decision log cannot
                # become a second tracking database.
                return self._json([
                    {"ts": r["ts"], "action": r["action"], "target": r["target"],
                     "who": ".".join(str(r["ip"]).split(".")[:2]) + ".x.x"}
                    for r in rows])

            if p == "/api/live":
                # RETIRED. The map polls /api/sightings now (see public/app.js).
                # A persistent event-stream is one pinned thread per connection
                # on a threaded server - a scale ceiling and a trivial DoS lever
                # (open N connections, pin N threads) - so it no longer holds the
                # socket open. FEED stays for any internal consumer.
                return self._err(410, "live stream retired; the map polls now")

            return self._err(404, "no such route")
        except Exception:
            traceback.print_exc()
            self._err(500, "internal error")

    # State-changing routes authenticated by the operator COOKIE. A browser
    # attaches that cookie to any cross-site request automatically, so these need
    # CSRF defence beyond the cookie itself.
    _CSRF_SENSITIVE = {"/api/review", "/api/review/bulk", "/api/review/edit",
                       "/api/purge", "/api/key/rotate", "/api/operator/login",
                       "/api/operator/logout", "/api/report",
                       "/api/rv/login", "/api/rv/logout", "/api/rv/verdict",
                       "/api/rv/tokens/new", "/api/rv/tokens/revoke",
                       "/api/rv/my-token", "/api/drive/report", "/api/drive/vote",
                       "/api/rv/retracted/delete", "/api/rv/held/fix",
                       "/api/node/span", "/api/node/key"}

    def _do_POST_inner(self) -> None:
        try:
            p = urlparse(self.path).path

            # 🚨 CSRF: require a real application/json Content-Type on cookie-
            # authed routes. `_body` parses JSON regardless of type, so without
            # this a cross-site <form> POSTing text/plain that HAPPENS to be
            # valid JSON would be accepted, and the browser would attach the
            # operator cookie - letting any page the operator visits retract a
            # sighting or purge data. application/json is NOT a form-reachable
            # "simple" content type: a cross-origin fetch sending it triggers a
            # CORS preflight, which this server never approves for these paths.
            # SameSite=Strict already blocks the cookie cross-site; this is the
            # second lock, and the one that still holds when auth is off and
            # operator power comes from a LAN/loopback source IP instead.
            if p in self._CSRF_SENSITIVE:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._err(415, "state-changing requests must be "
                                          "application/json")

            if p == "/api/enroll":
                b = self._body()
                # 🚨 THE LIMIT IS ON CREATING CAMERAS, NOT ON CORRECTING ONE.
                # 5/hour is right for a stranger minting nodes and wrong for
                # the owner of an existing camera, who nudges a heading, looks
                # at which road it resolved to, and nudges again - the /aim
                # page is built on exactly that loop, and at 5/hour it would
                # lock them out mid-adjustment with their camera pointing at
                # the wrong street. The body is read first so the two cases can
                # be told apart; _body() is already capped at MAX_BODY.
                #
                # ⚠️ THE EXEMPTION IS FOR A PROVEN OWNER, NOT FOR ANY REQUEST
                # CARRYING A node_id. Exempting on the mere PRESENCE of a
                # node_id let anyone skip the limiter by inventing one: the
                # token is checked inside enroll(), which is the right place
                # for the AUTHORISATION, but by then the limiter had already
                # been stepped around. Node ids are printed on the public map,
                # so "has a node_id" is not a claim to anything. Ownership is
                # therefore proved HERE, before the exemption is granted - and
                # a wrong token now counts against the create limit, which is
                # exactly what guessing at somebody else's token should cost.
                _nid = str(b.get("node_id") or "").strip()
                _owner = False
                if _nid:
                    _prior = db.node(_nid)
                    _tok = (b.get("token")
                            or (self.headers.get("Authorization") or ""
                                ).replace("Bearer ", "").strip())
                    if _prior and _prior.get("token") and _tok:
                        import hmac as _hmac
                        _owner = _hmac.compare_digest(str(_tok),
                                                      str(_prior["token"]))
                if not _owner and not rate_ok(p, self.client_ip):
                    # Do NOT say "from this address". The limit is network-wide
                    # (see RATE), so blaming the visitor's address is both
                    # false and unactionable - they change networks, retry, and
                    # get the same refusal.
                    return self._err(429, "the network is registering a lot of "
                                          "cameras right now - please try "
                                          "again in a few minutes")
                for k in ("name", "lat", "lon"):
                    if k not in b:
                        return self._err(400, f"missing {k}")
                # ⚠️ node_id MUST be passed through. Without it every re-save
                # of a placement minted a NEW node, so nudging the heading
                # littered the map with duplicate cameras - the exact thing the
                # placement page claimed to avoid.
                try:
                    rec = node_mod.enroll(
                        name=str(b["name"])[:80], lat=float(b["lat"]),
                        lon=float(b["lon"]),
                        pubkey=b.get("pubkey"), heading=float(b.get("heading", 0)),
                        fov=float(b.get("fov", 60)),
                        reach_m=float(b.get("reach_m", 45)),
                        kind=b.get("kind", "fixed"), contact=b.get("contact", ""),
                        node_id=str(b.get("node_id") or "")[:32],
                        # Proving ownership of a node you claim to already be.
                        # Accepted from the body or the header, because the
                        # phone pages hold it in localStorage and the detector
                        # sends it as a bearer.
                        token=(b.get("token")
                               or (self.headers.get("Authorization") or ""
                                   ).replace("Bearer ", "").strip() or None))
                except node_mod.NodeAuthError as exc:
                    return self._err(403, str(exc))
                # The token is returned exactly once, at enrollment.
                out = {"id": rec["id"], "status": rec["status"],
                       "token": rec.get("token")}
                # A move past the threshold minted a NEW camera (see
                # nodes.enroll). The client MUST hear about it: it has to adopt
                # the new id and token, or it carries on posting as a camera
                # that is no longer where it says it is. Dropping this field
                # here would have made the split invisible and therefore worse
                # than not splitting at all.
                if rec.get("moved_from"):
                    out["moved_from"] = rec["moved_from"]
                # A brand-new camera (no node_id came in - a re-save carries one)
                # also gets its own reviewer token, scoped to just this camera,
                # so the volunteer can review their own camera's catches without
                # anyone having to hand them access. It is 'own' scope: it cannot
                # see the shared pool or anyone else's cameras, every verdict is
                # audited under it, and the operator can revoke it. Minted once,
                # at creation, so nudging the camera later does not spawn more.
                if not str(b.get("node_id") or "").strip():
                    try:
                        rtok = review_auth.ensure_own_token(
                            rec["id"], str(b["name"])[:60], created_by="enroll")
                        if rtok:
                            out["review_token"] = rtok
                            out["review_url"] = "/rv"
                        # 🚨 AND THE POOL TOKEN, AT ENROLMENT. HIS CALL.
                        # ensure_pool_token was already self-service through
                        # /api/rv/my-token, so this grants nothing that was not
                        # already one request away - it just stops the shared
                        # queue being a thing you have to know exists. A
                        # crowd-labelling queue nobody is told about is not a
                        # crowd, which is the same argument that made it
                        # self-service in the first place.
                        #
                        # 📌 STILL TWO SEPARATE TOKENS, deliberately. Widening
                        # one token would mean revoking a pool abuser also
                        # takes away the camera they run. These revoke apart:
                        # the pool token goes, they keep their own camera.
                        # And created_by stays "self"/"enroll", never
                        # "operator", so neither satisfies is_trusted and the
                        # retracted-photo shelf remains operator-only.
                        ptok = review_auth.ensure_pool_token(
                            rec["id"], str(b["name"])[:60])
                        if ptok:
                            out["pool_token"] = ptok
                            out["pool_url"] = "/rv/pool"
                    except Exception:
                        pass
                return self._json(out)

            if p == "/api/sightings":
                # 🚨 AUTHENTICATE BEFORE SPENDING THE BUDGET, AND SPEND THE
                # RIGHT NODE'S. This charged a single network-wide bucket
                # before it knew who was calling, so one busy camera could
                # refuse every other camera on the project for the rest of the
                # hour - and _ingest, which does the real authentication, only
                # ran afterwards. Same shape as the geocode cache-before-budget
                # note above: the check has to know what it is protecting.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if nd["status"] != "active":
                    return self._err(403, f"node is {nd['status']}")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                if not rate_ok(p, self.client_ip, who=nd["id"]):
                    return self._err(429, "this camera is posting too fast")
                # A second, much higher ceiling that protects the BOX rather
                # than any one camera. Without it a fleet of nodes each inside
                # its own limit can still add up to more than 2 vCPUs can take.
                if not rate_ok("_all_sightings", self.client_ip):
                    return self._err(503, "the map is at capacity right now - "
                                          "your camera will retry")
                return self._ingest(b)

            if p == "/api/rf":
                # 🚨 RF SURVEILLANCE-DEVICE CANDIDATES. No image, no civilian
                # data - the client dropped every private device at the edge, so
                # a payload here is only ever known surveillance hardware (a
                # Flock/ALPR camera) heard at a position. Same auth as a camera
                # node (enroll gives the token), and it NEVER publishes: every
                # candidate parks in the RF pen for a human, exactly like the
                # government-vehicle review pen. A false RF guess costs a review
                # click, never a wrong dot on the public map.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if nd["status"] != "active":
                    return self._err(403, f"node is {nd['status']}")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                if not rate_ok(p, self.client_ip, who=nd["id"]):
                    return self._err(429, "this node is posting too fast")
                cands = b.get("candidates") or []
                if not isinstance(cands, list):
                    return self._err(400, "candidates must be a list")
                parked = 0
                for c in cands[:200]:   # a single scan cannot see more than this
                    if isinstance(c, dict) and mirror.rf_park(nd["id"], c):
                        parked += 1
                return self._json({"parked": parked, "reviewed": "pending"})

            # A stranger's judgement about one crop. It is written to
            # label_votes.db and nowhere else - not to sightings, not to the
            # bank. Consensus and the decision happen later, on his machine.
            if p == "/api/help/vote":
                import help_api
                b = self._body()
                return self._json(help_api.record(
                    str(b.get("item") or ""), str(b.get("label") or ""),
                    str(b.get("voter") or "")))

            if p == "/api/node/progress":
                # How far setup got, so "never started" stops being one bucket.
                # Same token as the heartbeat: this says nothing a heartbeat
                # does not already say about who is talking, and it is refused
                # outright without it.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                plat = str(b.get("platform") or "")[:12].lower()
                if plat not in ("ios", "android", "desktop", "other", ""):
                    plat = "other"
                db.set_setup_stage(nd["id"], str(b.get("stage") or "")[:24],
                                   plat or None)
                # Always 200: a camera must never treat a telemetry failure as
                # a setup failure, and there is nothing useful to tell it.
                return self._json({"ok": True})

            if p == "/api/node/label":
                # 🚨 THE CAMERA OPERATOR'S VERDICT, ARRIVING FROM THE CAMERA.
                #
                # labelbank runs on the machine that owns the camera and used to
                # apply this judgement to its OWN local sqlite file. Once the box
                # became the single source of truth that file stopped being the
                # map: the crops camctl labels carry sighting ids issued HERE, and
                # 0 of the last 30 of them existed locally. So every "Yes -
                # government" answered at the camera since the cutover wrote a
                # perfectly good training label and moved nothing, while the popup
                # said it had moved the sighting. An invisible failure - the popup
                # closes, the label is real, the map just never changes.
                #
                # Same auth as /api/node/progress, plus two rules this route needs
                # that a heartbeat does not:
                #   - a token is REQUIRED. _token_ok passes a tokenless node, which
                #     is tolerable for telemetry and not for publishing.
                #   - the sighting must BELONG to the calling node. Ids are
                #     sequential and printed next to sightings on the public map,
                #     so without this any camera could publish or retract any
                #     other camera's vehicles by naming an id.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if nd["status"] != "active":
                    return self._err(403, f"node is {nd['status']}")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                try:
                    sid = int(b.get("sighting_id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad sighting_id")
                row = db.sighting(sid)
                if not row:
                    return self._err(404, "no such sighting")
                if (row.get("node_id") or "") != nd["id"]:
                    return self._err(403, "not your camera's sighting")
                label = str(b.get("label") or "")
                undo = str(b.get("undo") or "")
                out = node_label.apply(sid, row, label, undo=undo)
                if out.get("error"):
                    return self._err(400, out["error"])

                # 🚨 THE PICTURE THE HUMAN WAS LOOKING AT, AT FULL RESOLUTION.
                #
                # Every patrol car published through THIS route was a 160-200px
                # smudge - measured live: 51182 was 160x61, 50945 192x95, and
                # the newest camera-labelled ones 200px to the edge. The reason
                # is that nothing here ever attached a photograph, so the row
                # kept the sub-resolution PEN copy ingest had stored, and 200px
                # is not a thumbnail size chosen for the map: it is the size
                # that DESTROYS A PLATE (snapshot.SUBRES_MAX_EDGE). It is
                # exactly right for an unreviewed candidate and exactly wrong
                # for a published government vehicle, which is the one case the
                # plate and the livery are meant to survive.
                #
                # The full-resolution original is not on this machine and must
                # not be: core.EVIDENCE is home-only and mirror.evidence_write
                # refuses here, because a public box holding un-degraded
                # pictures of whatever drove past is the thing that rule exists
                # to prevent. So the CLIENT keeps it - a drive phone holds its
                # own crop in memory, a camera has it banked - and sends it
                # with the verdict. It arrives only AFTER a person has said
                # "yes, police", at which point it is a public-tier photograph
                # of a police vehicle: precisely what a mirror is allowed to
                # store, and nothing is held here pending anything.
                #
                # ⚠️ ONLY WHEN THE VERDICT PUBLISHES. `published` is the state
                # machine's own answer, not the label that was asked for - a
                # council pickup answered "gov" is recorded and NOT published
                # (node_label._publishes), and attaching an un-degraded picture
                # to a private-tier row would put a legible plate on a row the
                # deployment just decided to keep private.
                if out.get("published") and b.get("snap_b64"):
                    try:
                        # decode_upload's cap and EXIF strip run in here, before
                        # anything touches the disk.
                        full = snapshot.decode_bytes(str(b["snap_b64"]))
                        review_api.attach_confirmed_photo(
                            sid, row, full,
                            ts=row.get("ts"),
                            node_name=nd.get("name") or "a camera",
                            vclass=("police" if label == "police" else "gov"))
                    except Exception as exc:
                        # The vehicle is on the map either way. A rejected
                        # picture must not un-publish it, and the operator has
                        # already been told their answer landed.
                        print(f"[label] full-res photo not attached to {sid}: {exc}")

                # 🚨 AND THE PEN ENTRY GOES, BECAUSE THE DECISION IS MADE.
                # review_api.verdict deletes it on every outcome; this route
                # deleted it on none, so a vehicle the camera operator had
                # already judged sat in /rv/pool waiting to be judged again. Two
                # front ends onto one decision, and only one of them closed it.
                # It also silently undid the photograph above: a pool reviewer
                # confirming the leftover item republishes it from the 200px pen
                # crop, putting the smudge back over the full-resolution picture
                # the person who saw the vehicle had just supplied.
                #
                # Keyed on ANSWERED, not on `did`. A label that changed nothing
                # because the map already agreed is still a person having
                # looked, and leaving that one in the queue is the same wasted
                # second look. `unsure` is deliberately not an answer, and an
                # undo must put nothing back - it is a reversal, not a verdict.
                if undo:
                    pass
                elif label in node_label.VALID and label != "unsure":
                    try:
                        review_api._delete_pen(sid)
                    except Exception:
                        pass
                if out.get("did"):
                    db.audit(f"node_label:{out['did']}", str(sid),
                             actor=f"camera {nd['id']}",
                             ip=privacy.audit_ip(self.client_ip))
                return self._json(out)

            if p == "/api/bug":
                # 🚨 UNAUTHENTICATED ON PURPOSE, AND BOUNDED BECAUSE OF IT.
                # The people most likely to report a bug are the ones who
                # cannot get in - a volunteer whose key is gone, somebody whose
                # browser will not install the app. Requiring a login to report
                # "I cannot log in" is how a report never arrives.
                #
                # The cost of that is an open upload on a 3 GB box, so: a rate
                # bucket, a size cap checked before anything is decoded, a
                # re-encode that strips EXIF, a ceiling on how many reports can
                # exist per hour, and a TTL sweep. See bugs.py.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "too many reports right now - "
                                          "please try again in a few minutes")
                b = self._body()
                out = bugs.save(str(b.get("desc") or ""),
                                str(b.get("shot") or ""),
                                page=str(b.get("page") or ""),
                                ua=(self.headers.get("User-Agent") or ""))
                if out.get("error"):
                    return self._err(400, out["error"])
                # Tell the operator it exists. The alert carries an ID and
                # NOTHING ELSE - the default alert repo is the public one.
                try:
                    import subprocess
                    subprocess.Popen(
                        [sys.executable, str(DATA.parent / "tools" / "bug_alert.py"),
                         out["id"]],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass          # a failed alert must never lose the report
                return self._json({"ok": True, "id": out["id"]})

            if p == "/api/bug/close":
                if not self._is_local():
                    return self._err(403, "operator only")
                b = self._body()
                return self._json({"ok": bugs.close(str(b.get("id") or ""))})

            if p == "/api/bug/delete":
                if not self._is_local():
                    return self._err(403, "operator only")
                b = self._body()
                return self._json({"ok": bugs.delete(str(b.get("id") or ""))})

            if p == "/api/node/whoami":
                # 🚨 "MY CAMERA GOT DELETED." NOTHING WAS EVER DELETED.
                #
                # The camera app keeps its key in localStorage, and the boot
                # line was `go(node && node.token ? 'watch' : 'setup')` - so a
                # browser that lost that entry dropped the owner straight into
                # SETUP, which enrols a BRAND NEW camera. Their real node still
                # existed, still held its history and its watched road, and was
                # now unreachable. Measured on the live box: 160 of 262 nodes
                # have zero sightings AND zero heartbeats, and one street in
                # Somerville is enrolled six times.
                #
                # Storage is lost routinely and through no fault of theirs:
                # Safari evicts script-writable storage after 7 days for a site
                # that is not installed to the home screen, in-app browsers keep
                # their own throwaway copy, and clearing site data does it too.
                #
                # So there has to be a way back IN, and this is what a pasted
                # key is checked against before the app adopts it. Telling
                # somebody "that key is not valid" is only possible if something
                # can say so; without it a mistyped key fails silently later,
                # somewhere confusing.
                #
                # ⚠️ NOT AN ORACLE. It answers only to the node's OWN token, so
                # it discloses nothing that the holder of the key does not
                # already have, and it returns the node's own public-facing
                # details rather than anything new.
                #
                # ⚠️ THE TOKEN ARRIVES IN `Authorization`, NOT THE BODY, because
                # that is the only place _token_ok looks. Worth stating: this
                # route takes an id in the body and a credential in a header,
                # and the first version of /signin put both in the body - so it
                # rejected every valid key while looking entirely correct.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "no camera with that id")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "that key does not match this camera")
                return self._json({
                    "id": nd["id"], "name": nd.get("name") or nd["id"],
                    "status": nd.get("status"),
                    "kind": nd.get("kind"),
                    "sightings": nd.get("sightings") or 0,
                    "last_seen": nd.get("last_seen"),
                })

            if p == "/api/node/parked":
                # 🚨 THE DRIVE CLIENT CANNOT LEARN THIS ANY OTHER WAY.
                #
                # A camera node scores its own crop, so _ingest can answer
                # "parked: true" in the POST response and the owner is asked
                # immediately. A PHONE cannot: there is no head in a browser, so
                # its crop goes to the inbox, box_puller pulls it home, the head
                # scores it there, and box_publish moves the head-positives into
                # the pen back here - minutes later, long after the response the
                # phone was reading.
                #
                # So the drive popup built on `parked` could never fire for the
                # one client that needed it most. This is the missing half: the
                # phone names the sightings it posted and asks which of them
                # have since become government candidates.
                #
                # Deliberately CLIENT-NAMED ids rather than "everything recent
                # for this node". The phone already knows what it sent and is
                # holding the crop to show; a route that volunteers a node's
                # recent history would be a second, chattier read path over the
                # same rows for no benefit.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                try:
                    ids = [int(x) for x in (b.get("ids") or [])][:40]
                except (TypeError, ValueError):
                    return self._err(400, "bad ids")
                out = []
                for sid in ids:
                    row = db.sighting(sid)
                    # 🚨 IT MUST BE THEIR OWN. Ids are sequential and printed
                    # beside every sighting on the public map, so without this
                    # any camera could enumerate whether ANY id is a government
                    # candidate - which is a government-vehicle oracle over the
                    # whole database, from one enrolled phone. Same rule
                    # /api/node/label already enforces, for the same reason.
                    if not row or (row.get("node_id") or "") != nd["id"]:
                        continue
                    meta = review_api._pen_meta(sid)
                    if not meta:
                        continue
                    out.append({"id": sid,
                                "vclass": meta.get("vclass") or row.get("vclass"),
                                "score": meta.get("score")})
                return self._json({"parked": out})

            if p == "/api/node/key":
                # 🚨 A CAMERA REGISTERS ITS OWN SIGNING KEY, WITHOUT RE-ENROLLING.
                #
                # Enrolment only happens when somebody SAVES a placement, so
                # every camera already running would have gone on unsigned for
                # ever - the fix would have shipped and changed nothing, which
                # is how pubkey came to be NULL on 158 nodes in the first place.
                # The detector calls this once at startup instead.
                #
                # Deliberately NOT /api/enroll: that call carries a position,
                # and a position is what makes nodes.enroll split a node past
                # MOVED_THRESHOLD_M. Registering a key must never be able to
                # mint a camera. Nothing here touches placement.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                pub = str(b.get("pubkey") or "").strip()
                if not pub:
                    return self._err(400, "missing pubkey")
                # Verify it is a usable ed25519 key BEFORE storing it. Storing
                # an unusable one is the worst outcome available here: _ingest
                # would then demand a signature it can never verify and 401
                # every sighting from a camera that is working perfectly.
                try:
                    import base64 as _b64
                    from cryptography.hazmat.primitives.asymmetric.ed25519 \
                        import Ed25519PublicKey
                    Ed25519PublicKey.from_public_bytes(_b64.b64decode(pub))
                except Exception:
                    return self._err(400, "that is not an ed25519 public key")
                conn = db.connect()
                conn.execute("UPDATE nodes SET pubkey=? WHERE id=?",
                             (pub, nd["id"]))
                conn.commit()
                db.audit("node_key", nd["id"], actor=f"camera {nd['id']}",
                         ip=privacy.audit_ip(self.client_ip))
                return self._json({"ok": True, "id": nd["id"]})

            if p == "/api/node/span":
                # The camera owner deciding whether the stretch of road their
                # camera watches appears on the public map. Same node-token auth
                # as /api/node/label, and for the same reason: this publishes
                # something, so a tokenless node is not good enough.
                #
                # Only ever about THEIR node. There is no id to guess at here -
                # the node is the one the token belongs to.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                on = bool(b.get("publish"))
                db.set_publish_span(nd["id"], on)
                db.audit("node_span:" + ("publish" if on else "hide"),
                         nd["id"], actor=f"camera {nd['id']}",
                         ip=privacy.audit_ip(self.client_ip))
                # Report whether there is actually a span to publish, so the
                # camera page can say "on, but this camera has no road yet"
                # instead of showing a switch that appears to do nothing.
                return self._json({"ok": True, "publish": on,
                                   "has_span": node_mod.span_of(db.node(nd["id"]))
                                   is not None,
                                   "road_name": (db.node(nd["id"]) or {}).get("road_name")})

            if p == "/api/node/confirm":
                # 🚨 THE OPERATOR SAYING "YES, THAT WAS A PATROL CAR" ABOUT A
                # PASS THE GATE THREW AWAY.
                #
                # /api/node/label moves a sighting that already exists. This one
                # exists because most confirmations have nothing to move: the
                # posting gate runs when the vehicle leaves frame, and a marked
                # patrol car with an unreadable plate clears only one marker, so
                # the pass is dropped and no sighting is ever created. The human
                # then confirms the crop and the map cannot change, because
                # there is no row. Every such confirmation was silently lost.
                #
                # This is NOT a way to publish anything a camera fancies. It is
                # the same claim /api/sightings accepts, from the same node
                # token, with one addition the SERVER makes: human_confirmed,
                # which classify.py weighs at 4.0 and which waives the
                # two-marker rule. The submitter cannot assert it (it is
                # stripped in _ingest and re-set only from this flag), and the
                # sighting still goes through classify() exactly like any other
                # - so `public_tiers` still decides what becomes public, and a
                # confirmed council truck still stays private.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if nd["status"] != "active":
                    return self._err(403, f"node is {nd['status']}")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                if not rate_ok("/api/sightings", self.client_ip):
                    return self._err(429, "posting too fast")
                # The node may only confirm its OWN camera's crop, and the
                # source is fixed here rather than taken from the body.
                b["node_id"] = nd["id"]
                b["source"] = "camera"

                # 🚨 A BANKED CROP IS ALREADY THE VEHICLE, SO THERE IS NOTHING
                # TO CROP TO - AND _ingest CORRECTLY REFUSES TO GUESS.
                # It requires a `vehicle_box` because a camera normally posts a
                # whole FRAME, and storing that uncropped would publish the
                # street and the neighbours' houses. Right rule, wrong shape of
                # input: what arrives here is the already-tight crop the
                # labeller looked at. Without this the row is created with
                # `snap: None` - a public claim with no photograph behind it,
                # which is both less useful and less checkable.
                #
                # store_subresolution is the same call review_api._publish uses
                # to attach a pen crop, for the same reason: the image is
                # already small and already cropped, so it needs storing, not
                # cropping. Sub-resolution is enforced inside it, so an
                # oversized image is refused rather than quietly published.
                if b.get("snap_b64"):
                    try:
                        # ⚠️ DOWNSCALE FIRST. store_subresolution ENFORCES a
                        # 200px longest edge and raises otherwise - correctly,
                        # it is the guard that keeps a published crop
                        # unidentifiable. Banked training crops are 512px wide,
                        # so every operator confirmation hit that ValueError,
                        # got caught by the except below, and the sighting was
                        # created with snap=None: on the map, confirmed, with no
                        # photograph behind it. The measurement, not the guess:
                        # "sub-resolution submission is 512x229".
                        import base64 as _b64
                        _small = snapshot.downscale_to_subres(b["snap_b64"])
                        b["snap"] = snapshot.store_subresolution(
                            "data:image/jpeg;base64," + _b64.b64encode(_small).decode(),
                            {"ts": b.get("ts") or now(), "node_id": nd["id"],
                             "node_name": nd.get("name") or "a camera",
                             "tier": "public", "vclass": "police",
                             "watermark": "CONFIRMED"})
                    except Exception as exc:
                        # The sighting is worth more than the picture. Losing
                        # the image must not lose the report.
                        print(f"[confirm] could not store crop: {exc}")
                    b.pop("snap_b64", None)
                db.audit("node_confirm", str(b.get("bank_ref") or "?"),
                         actor=f"camera {nd['id']}",
                         ip=privacy.audit_ip(self.client_ip))
                return self._ingest(b, operator_confirmed=True)

            if p == "/api/heartbeat":
                # "I am awake and watching." Deliberately separate from a
                # sighting: a camera pointed at an empty street at 4am is
                # working perfectly and has nothing to report, and inferring
                # liveness from traffic reported exactly that camera as down.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                # ⚠️ A PAUSED OR REVOKED NODE MUST BE TOLD, NOT COUNTED.
                # It used to beat happily forever: counted as online, inflating
                # heartbeats_total and the public "hours watched", while
                # _ingest 403d every sighting it sent. The camera had no way to
                # learn it had been switched off - the only endpoint it could
                # reach kept answering {"ok": true}.
                if nd["status"] != "active":
                    return self._json({"ok": True, "posting": False,
                                       "status": nd["status"],
                                       "note": "this camera is not active, so "
                                               "its sightings are refused"})
                db.heartbeat(nd["id"])
                # 🚨 ASK FOR THE GOOD PICTURE HERE. Everything a camera uploads
                # is capped at 200px so a private plate cannot survive the trip,
                # and that must never change. But once the head decides a
                # vehicle IS a government one it belongs in the public tier,
                # where the plate is legible on purpose - and the only copy the
                # server has by then is the deliberately ruined one.
                #
                # The camera that took it is the only device that ever had the
                # original, so it has to be asked, and this beat is already
                # going back to exactly that device every few seconds.
                want = []
                try:
                    want = db.wants_fullres(nd["id"])
                except Exception as exc:
                    print(f"[beat] wants_fullres failed for {nd['id']}: {exc}")
                return self._json({"ok": True, "posting": True, "ts": now(),
                                   "want_full": want})

            if p == "/api/signals":
                # 🚨 AN OUTSIDE SENSOR NETWORK REPORTING WHAT A VEHICLE
                # BROADCASTS. It reports what it HEARD - a time, a place, a
                # band, an opaque signature. It never says what the vehicle
                # was, and it cannot write to `sightings`. What an observation
                # MEANS is decided here, later, against published rows only.
                #
                # ⚠️ TOKEN-GATED PER PARTNER, revocable on its own. One
                # partner, one token: if somebody downstream of them abuses it,
                # that token dies and nobody else is affected. That is the
                # whole point - it means a partner is never blamed for a third
                # party using their system.
                auth = self.headers.get("Authorization", "")
                tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                # ⚠️ review_auth._hash, not a second sha256 here. One
                # definition of how a token becomes a stored hash; two
                # would drift and the drift would look like a bad token.
                partner = (db.signal_token(review_auth._hash(tok))
                           if tok else None)
                if not partner:
                    return self._err(401, "a signal partner token is required")

                b = self._body()
                rows = b.get("observations")
                if not isinstance(rows, list) or not rows:
                    return self._err(400, "send {\"observations\": [ ... ]}")
                if len(rows) > db.SIGNAL_MAX_BATCH:
                    return self._err(
                        400, f"at most {db.SIGNAL_MAX_BATCH} observations per request")
                kept = db.add_signal_obs(int(partner["id"]), rows)
                # ⚠️ Report BOTH numbers. Silently dropping malformed rows and
                # answering "ok" is how a partner spends a week sending
                # something we never stored.
                return self._json({"ok": True, "received": len(rows),
                                   "stored": kept,
                                   "dropped": len(rows) - kept})

            if p == "/api/sighting/fullres":
                # 🚨 THE ANSWER TO want_full. The camera hands back the picture
                # it still holds for a sighting the hub has already PUBLISHED.
                #
                # ⚠️ EVERY CONDITION HERE IS A GUARD, NOT A FORMALITY:
                #   - the node's own token, so nobody else can attach a picture;
                #   - the sighting must BELONG to that node, so a camera cannot
                #     overwrite another camera's evidence;
                #   - the row must already be tier='public', so this can never
                #     put a legible plate on a private-tier vehicle - which is
                #     the entire promise the 200px cap exists to keep.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                try:
                    sid = int(b.get("id") or 0)
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                row = db.sighting(sid)
                if not row or row.get("node_id") != nd["id"]:
                    return self._err(404, "not this camera's sighting")
                if row.get("tier") != "public":
                    return self._err(403, "not published; the small crop stands")
                if row.get("snap_full"):
                    return self._json({"ok": True, "already": True})
                if not b.get("snap_b64"):
                    return self._err(400, "no image")
                try:
                    full = snapshot.decode_bytes(str(b["snap_b64"]))
                    name = review_api.attach_confirmed_photo(
                        sid, row, full, ts=row.get("ts"),
                        node_name=nd.get("name") or "a camera",
                        vclass=("police" if row.get("vclass") == "police"
                                else "gov"))
                    if name:
                        db.mark_fullres(sid, name)
                    return self._json({"ok": bool(name), "snap": name})
                except Exception as exc:
                    # ⚠️ The vehicle is on the map either way. A rejected
                    # picture must never un-publish it.
                    print(f"[fullres] {sid}: {exc}")
                    return self._err(400, "image rejected")

            if p == "/api/heartbeat/bulk":
                # 🚨 ONE PROCESS, THOUSANDS OF CAMERAS, ONE REQUEST.
                #
                # The public-camera poller holds ~4,400 credentials. Beating
                # them individually is 4,400 requests and 4,400 commits every
                # five minutes at a box that is two cores and has already been
                # taken down once by concurrency it was not sized for. This is
                # the same operation, batched, and it is the ONLY reason it
                # exists - it grants no authority the single endpoint does not.
                #
                # ⚠️ EVERY ENTRY IS AUTHENTICATED SEPARATELY. There is no
                # "trusted caller" here: a batch of a thousand is a thousand
                # token checks, and one bad token fails that entry alone.
                #
                # ⚠️ AND THE TOKEN IS IN THE BODY, WHICH IS A DEPARTURE.
                # _token_ok deliberately reads the Authorization HEADER only,
                # because a body-supplied token was once a way round it. That
                # rule is intact: this route does not call _token_ok, it
                # compares each entry's token against that node's own record
                # with compare_digest, and a batch can only ever beat cameras
                # whose tokens the caller already holds. One header cannot
                # carry a thousand different credentials, so a body field is
                # the only shape available - and it authorises nothing beyond
                # "this camera is awake".
                import hmac as _hmac
                b = self._body()
                items = b.get("nodes")
                if not isinstance(items, list):
                    return self._err(400, "nodes must be a list")
                if len(items) > BULK_BEAT_MAX:
                    return self._err(413, f"at most {BULK_BEAT_MAX} per request")
                ok, bad, inactive = [], 0, 0
                for it in items:
                    if not isinstance(it, dict):
                        bad += 1
                        continue
                    nd = db.node(str(it.get("node_id") or ""))
                    if not nd:
                        bad += 1
                        continue
                    tok = str(nd.get("token") or "")
                    if tok and not _hmac.compare_digest(str(it.get("token") or ""),
                                                        tok):
                        bad += 1
                        continue
                    # Same rule as the single endpoint: a paused or revoked
                    # camera must not beat, or it counts as online while every
                    # sighting it sends is refused.
                    if nd["status"] != "active":
                        inactive += 1
                        continue
                    ok.append(nd["id"])
                db.heartbeat_many(ok)
                return self._json({"ok": True, "beat": len(ok),
                                   "rejected": bad, "inactive": inactive,
                                   "ts": now()})

            if p == "/api/review/edit":
                # Operator fixes a cosmetic description (e.g. the detector called
                # an SUV a 'motorcycle'). Descriptive fields only - class, plate
                # and position have their own review paths and are not touched.
                if not self._is_local():
                    return self._err(403, "local only")
                b = self._body()
                try:
                    sid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                if not db.sighting(sid):
                    return self._err(404, "no such sighting")
                db.set_sighting_desc(sid, body=b.get("body"), color=b.get("color"))
                db.audit("edit_desc", str(sid), actor="operator",
                         ip=privacy.audit_ip(self.client_ip))
                return self._json({"ok": True})

            if p == "/api/camera/report":
                # A public "this ALPR camera is GONE / is still HERE" report
                # about a known OSM camera on the Flock layer. It parks for
                # review and NEVER hides or confirms a camera by itself - the
                # served layer only drops osm_ids a human curated into
                # cameras_removed.json. This is how the layer stays honest as
                # cities add and remove cameras, and where an RF-beta "still
                # here" confirmation lands too (source=rf).
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of camera reports right now - "
                                          "please try again shortly")
                b = self._body()
                # 🚨 PROXIMITY GATE. A report only counts if the reporter's live
                # GPS is next to the camera, so nobody can log on from anywhere
                # and mass-report cameras gone (or present). The camera position
                # is exact (OSM); GPS wobbles, so the reporter's own accuracy is
                # allowed as slack up to a cap. RF confirmations (source=rf) come
                # from a trusted local job and skip this.
                src = str(b.get("source") or "public")
                if src != "rf":
                    try:
                        cam_lat = float(b.get("lat")); cam_lon = float(b.get("lon"))
                        at_lat = float(b.get("at_lat")); at_lon = float(b.get("at_lon"))
                    except (TypeError, ValueError):
                        return self._err(400, "your location is required to "
                                              "report a camera - stand next to it")
                    acc = 0.0
                    try:
                        acc = min(float(b.get("acc") or 0), 60.0)
                    except (TypeError, ValueError):
                        acc = 0.0
                    dist = _haversine_m(at_lat, at_lon, cam_lat, cam_lon)
                    if dist > 75.0 + acc:
                        return self._err(403, "you are about %d m away - stand "
                                              "at the camera to report it"
                                              % int(dist))
                stem = mirror.camera_status_report({
                    "id": b.get("id"), "kind": b.get("kind"),
                    "lat": b.get("lat"), "lon": b.get("lon"),
                    "at_lat": b.get("at_lat"), "at_lon": b.get("at_lon"),
                    "note": b.get("note"), "source": src,
                    "ip": privacy.audit_ip(self.client_ip),
                })
                if not stem:
                    return self._err(400, "need a camera id and kind "
                                          "'removed' or 'present'")
                return self._json({"ok": True})

            if p == "/api/radar/hit":
                # A police-radar emission detected by PAIRED HARDWARE. This is
                # deliberately NOT the withdrawn tap-a-patrol route: that placed
                # a marker from bare numbers anyone could type, this places one
                # only when a real detector on a real node saw a real emission.
                # So it carries the node's token and is refused without it -
                # every dot is attributable and the node revocable, the same
                # guarantee the rest of the map runs on. Band decides trust
                # downstream (Ka is police-only; K/X need corroboration); this
                # endpoint just authenticates, validates, and records.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of radar hits right now - "
                                          "the detector can retry shortly")
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                band = str(b.get("band") or "").strip().lower()
                if band not in RADAR_BAND_BASE:
                    return self._err(400, "band must be one of ka, k, x, laser")
                try:
                    lat = float(b.get("lat")); lon = float(b.get("lon"))
                except (TypeError, ValueError):
                    return self._err(400, "a location is required")
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    return self._err(400, "location out of range")
                try:
                    heading = float(b.get("heading")) if b.get("heading") is not None else None
                except (TypeError, ValueError):
                    heading = None
                try:
                    strength = max(0.0, min(1.0, float(b.get("strength"))))
                except (TypeError, ValueError):
                    strength = None
                _radar_add(lat, lon, band, heading, strength, nd["id"])
                return self._json({"ok": True})

            if p == "/api/sensor/hit":
                # Drone (Remote ID) or police-radio activity from paired hardware.
                # Node-token authenticated, same as radar: a live point on the map
                # must be attributable to a node, never an anonymous tap.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of sensor events right now - "
                                          "retry shortly")
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                kind = str(b.get("kind") or "").strip().lower()
                if kind not in LIVE_KINDS:
                    return self._err(400, "kind must be one of "
                                          + ", ".join(sorted(LIVE_KINDS)))
                try:
                    lat = float(b.get("lat")); lon = float(b.get("lon"))
                except (TypeError, ValueError):
                    return self._err(400, "a location is required")
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    return self._err(400, "location out of range")
                label = str(b.get("label") or "")[:80]
                # Dedup key: a drone by its own id (so a trail is one point),
                # radio by the receiving node (one scanner is one pulse).
                if kind == "drone":
                    key = str(b.get("id") or ("%.5f,%.5f" % (lat, lon)))
                else:
                    key = nd["id"]
                _live_add(kind, lat, lon, label, key, nd["id"])
                return self._json({"ok": True})

            if p == "/api/send/challenge":
                # A short-lived challenge the client signs to prove it owns a
                # mailbox before fetching. Stateless; no message content involved.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                ch = send_relay.issue_challenge(str(b.get("mb") or ""))
                if not ch:
                    return self._err(400, "bad mailbox")
                return self._json({"challenge": ch})

            if p == "/api/send/put":
                # Drop one sealed ciphertext envelope into a recipient's mailbox.
                # No auth to SEND (anyone with your address can write to you, the
                # same as email), but the envelope is opaque and hard-capped.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of messages right now - retry shortly")
                b = self._body()
                to, env = str(b.get("to") or ""), str(b.get("env") or "")
                # Proof-of-work gate. Anyone may send (no account), but each send
                # carries a small hashcash stamp so flooding costs real CPU. The
                # required difficulty RISES with how full the target mailbox
                # already is (progressive), so packing a mailbox to eviction is
                # expensive even natively. The client sends at the base and
                # re-stamps when we answer with need_bits. Verified in ONE hash.
                need = send_relay.required_bits(send_relay.queue_depth(to))
                if not send_relay.pow_ok(to, env, b.get("pt"), b.get("pn"), bits=need):
                    return self._json({"error": "proof-of-work too low - resend",
                                       "need_bits": need}, 400)
                ok = send_relay.put(to, env, str(b.get("mid") or ""))
                if not ok:
                    return self._err(400, "could not accept that message "
                                          "(bad mailbox, too large, or relay full)")
                # Content-free push nudge so the recipient's phone rings even with
                # the app closed. Fire-and-forget; never blocks the send.
                send_push.notify_async(to)
                return self._json({"ok": True})

            if p == "/api/send/claim":
                # Claim a short handle for an address. Signed by the identity's
                # key + PoW (see send_relay.claim_handle). Anti-squat: rate-
                # limited, one handle per identity, reserved names blocked.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                ok, res = send_relay.claim_handle(
                    str(b.get("handle") or ""), str(b.get("address") or ""),
                    str(b.get("sig") or ""), b.get("pt"), b.get("pn"))
                if not ok:
                    return self._json({"error": res}, 400)
                return self._json({"ok": True, "handle": res})

            if p == "/api/send/release":
                # Give up a handle you own (signed; no PoW - only the owner can).
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                ok, res = send_relay.release_handle(
                    str(b.get("handle") or ""), str(b.get("address") or ""),
                    str(b.get("sig") or ""))
                if not ok:
                    return self._json({"error": res}, 400)
                return self._json({"ok": True, "handle": res})

            if p == "/api/send/subscribe":
                # Store (or clear) a device's Web Push subscription for a mailbox
                # so the hub can send content-free "new message" nudges. Opt-in.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                mb = str(b.get("mb") or "")
                if b.get("unsubscribe"):
                    send_push.unsubscribe(mb)
                    return self._json({"ok": True})
                if not send_push.subscribe(mb, b.get("sub")):
                    return self._err(400, "bad subscription")
                return self._json({"ok": True})

            if p == "/api/send/get":
                # Prove ownership, then hand back and DELETE this mailbox's
                # envelopes. The hub cannot read any of them - they are ciphertext.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                msgs = send_relay.fetch_and_clear(
                    str(b.get("mb") or ""), str(b.get("pk") or ""),
                    str(b.get("ch") or ""), str(b.get("sig") or ""))
                return self._json({"msgs": msgs})

            if p == "/api/air/put":
                # Drop one sealed frame onto the air under a ROUTING TAG. The hub
                # never learns the recipient - the tag is an HMAC over a pairwise
                # secret only the two parties share, and it rotates each epoch.
                # No account (like a mailbox send), so a hashcash stamp gates the
                # flood; the required difficulty rises with how full the tag
                # already is. See air_relay for the trust model.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of messages right now - retry shortly")
                b = self._body()
                tag, frame = str(b.get("tag") or ""), str(b.get("frame") or "")
                if not air_relay.valid_tag(tag):
                    return self._err(400, "bad routing tag")
                need = air_relay.required_bits(tag)
                if not send_relay.pow_ok(tag, frame, b.get("pt"), b.get("pn"), bits=need):
                    return self._json({"error": "proof-of-work too low - resend",
                                       "need_bits": need}, 400)
                s = air_relay.put(tag, frame)
                if s is None:
                    return self._err(400, "could not accept that frame "
                                          "(malformed, too large, or air full)")
                return self._json({"ok": True, "seq": s})

            if p == "/api/air/get":
                # Poll the air for frames routed to a tag. A POST (not a GET query)
                # so the tag never lands in an access or proxy log - the whole
                # point is to leave no lasting record of who is reachable. No
                # ownership proof: only the two parties can compute the tag, so it
                # IS the capability. Non-destructive cursor read (since -> head).
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "slow down")
                b = self._body()
                # Batch form: a client polls ALL its contacts' recv-tags in ONE
                # request. Per-contact polling would multiply requests by the
                # address book and blow the rate limit; one batched poll per
                # cycle keeps a client at roughly the same request rate as the
                # mailbox path no matter how many contacts it has. Capped so the
                # batch itself cannot be turned into an amplification lever.
                tags = b.get("tags")
                if isinstance(tags, list):
                    out = {}
                    for entry in tags[:64]:
                        try:
                            t = str(entry.get("tag") or "")
                            since = entry.get("since") or 0
                        except AttributeError:
                            t = str(entry or "")
                            since = 0
                        if not air_relay.valid_tag(t):
                            continue
                        head, frames = air_relay.get(t, since)
                        if frames or head:
                            out[t] = {"seq": head, "m": frames}
                    return self._json({"results": out})
                tag = str(b.get("tag") or "")
                if not air_relay.valid_tag(tag):
                    return self._err(400, "bad routing tag")
                head, frames = air_relay.get(tag, b.get("since") or 0)
                return self._json({"seq": head, "m": frames})

            if p == "/api/aircraft/ingest":
                # A LOCAL ADS-B receiver (dump1090 on a Pi) pushing the aircraft it
                # sees, to augment the OpenSky-fed layer with local coverage. Only
                # reached when the aircraft preview is on; node-token authenticated
                # like every other write that puts something on the map.
                if not CONFIG.get("aircraft_preview"):
                    return self._err(404, "aircraft layer is not enabled here")
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "a lot of aircraft pushes right now - "
                                          "retry shortly")
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not nd.get("token"):
                    return self._err(401, "this node has no token; re-enroll it")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                craft = b.get("aircraft")
                if not isinstance(craft, list):
                    return self._err(400, "aircraft must be a list")
                try:
                    import aircraft as _air
                    n = _air.ingest_local(craft, nd["id"])
                except Exception as exc:
                    return self._err(400, "could not ingest: %s" % exc)
                return self._json({"ok": True, "accepted": n})

            if p == "/api/spot":
                # A public "I see a fixed surveillance camera here" report from
                # the no-install /spot page. It is the camera equivalent of a
                # flag: a CLAIM, backed by a photograph, that parks for review
                # and NEVER draws on the map by itself.
                #
                # 🚨 THE PHOTO IS MANDATORY, AND THIS IS WHY THE FEATURE IS SAFE.
                # /api/drive/report was withdrawn because it asserted a police
                # presence from a pair of numbers - no picture, no review. A
                # camera spot without a photo would be the same mistake wearing
                # a different hat, so a report with no decodable image is a 400,
                # not a maybe. A person looks at every picture before anything
                # is ever published.
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "the network is sending a lot of "
                                          "camera reports right now - please "
                                          "try again in a few minutes")
                b = self._body()
                raw = str(b.get("photo") or "")
                if "," in raw and raw[:5].lower() == "data:":
                    raw = raw.split(",", 1)[1]
                try:
                    import base64 as _b64
                    photo = _b64.b64decode(raw, validate=False) if raw else b""
                except Exception:
                    photo = b""
                if not photo:
                    return self._err(400, "a photo of the camera is required")
                stem = mirror.camera_park({
                    "lat": b.get("lat"), "lon": b.get("lon"),
                    "note": b.get("note"), "kind": b.get("kind"),
                    "accuracy_m": b.get("accuracy_m"),
                    "ip": privacy.audit_ip(self.client_ip),
                }, photo)
                if not stem:
                    # camera_park rejects a missing/oversize photo or bad coords.
                    return self._err(400, "need a photo (under 4 MB) and a "
                                          "valid location")
                db.audit("camera_spot", stem, actor="public",
                         ip=privacy.audit_ip(self.client_ip))
                return self._json({"ok": True})

            if p == "/api/report":
                # A public flag on a published sighting. Anyone may send one: it
                # is a SUGGESTION that surfaces the sighting in the operator's
                # review queue, never an edit to the map. Rate-limited (RATE) and
                # JSON-only (_CSRF_SENSITIVE) so it cannot be driven from a
                # cross-site form or hammered. Only public-tier rows can be
                # flagged - a private pass has no claim to dispute.
                b = self._body()
                reason = b.get("reason")
                if reason not in db.REPORT_REASONS:
                    return self._err(400, "unknown reason")
                try:
                    sid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                row = db.sighting(sid)
                if not row or row.get("tier") != "public":
                    return self._err(404, "no such public sighting")
                db.add_report(sid, reason, b.get("note") or "",
                              privacy.audit_ip(self.client_ip))
                db.audit("report", str(sid), actor="public",
                         ip=privacy.audit_ip(self.client_ip))
                # 🚨 AND PUT IT IN FRONT OF A HUMAN.
                # This used to end at the table. The only reader of `reports`
                # is the OPERATOR queue, which is local-only and does not exist
                # on a public mirror - so on the live site every public flag
                # landed where nothing running there could show it. Parking it
                # in the review pen routes it to the reviewer app, and the
                # existing "not a cop" verdict already retracts the row and
                # resolves the flag from there.
                #
                # Best-effort on purpose: the flag is recorded either way, and
                # a sighting with no stored photo simply cannot be re-judged
                # visually. Never let the queueing failure lose the report.
                try:
                    review_api.park_reported(sid, row, reason)
                except Exception:
                    traceback.print_exc()
                # 🚨 A PRIVACY FLAG ACTS FIRST AND ASKS AFTERWARDS.
                # Every other reason above is a claim about a VEHICLE and can
                # safely wait in the queue. This one is a claim about a PERSON
                # who never asked to be photographed - a passenger's arm, a
                # face, a watch, caught alongside the patrol car - and the harm
                # accrues for as long as the queue takes. The photograph is
                # moved out of the served directory now; the sighting itself
                # (dot, class, time) does not move, and a reviewer decides
                # whether the picture comes back cropped, whole, or not at all.
                # See review_api.hold_photo for why this is recoverable and the
                # opposite failure is not.
                held = False
                if reason == db.PRIVACY_REPORT:
                    try:
                        held = review_api.hold_photo(sid, row, reason)
                    except Exception:
                        traceback.print_exc()
                return self._json({"ok": True, "held": held})

            if p == "/api/review":
                # 🚨 A PUBLIC SIGHTING IS AN ASSERTION, SO IT HAS TO BE
                # RETRACTABLE - and the retraction is the most valuable label
                # this project can get: a human judgement on the camera's own
                # view of something the system actually published. It is
                # written straight back into the training set.
                if not self._is_local():
                    return self._err(403, "local only")
                b = self._body()
                sid, verdict = b.get("id"), b.get("verdict")
                if verdict not in ("confirmed", "retracted", "promoted"):
                    return self._err(400, "verdict must be confirmed, retracted "
                                          "or promoted")
                row = db.sighting(int(sid)) if sid else None
                if not row:
                    return self._err(404, "no such sighting")

                if verdict == "promoted":
                    # A miss being corrected. The plate stays gone - see
                    # db.promote_sighting.
                    conf = None
                    try:
                        from detect import bank
                        j = bank.find_by_sighting(int(sid))
                        if j:
                            conf = ((json.loads(j.read_text(encoding="utf-8"))
                                     .get("clip") or {}).get("conf"))
                    except Exception:
                        pass
                    db.promote_sighting(int(sid), "police", conf,
                                        "confirmed by the camera operator; the "
                                        "classifier identified it but a gating "
                                        "bug held it private")
                else:
                    db.review_sighting(int(sid), verdict)
                # Feed it back as ground truth. Best effort: a node that ran
                # with --no-bank has no crop to label, and that must not make
                # the retraction fail - taking the claim down matters more.
                labelled = False
                try:
                    sys.path.insert(0, str(Path(__file__).parent))
                    from detect import bank
                    labelled = bank.label_by_sighting(
                        int(sid),
                        "civilian" if verdict == "retracted" else "police")
                except Exception:
                    pass
                db.audit(f"review_{verdict}", str(sid), actor="operator",
                         ip=privacy.audit_ip(self.client_ip))
                return self._json({"ok": True, "labelled": labelled,
                                   **db.review_stats()})

            if p == "/api/key/qr":
                # 🚨 THE TOKEN ARRIVES IN A POST BODY, NEVER A QUERY STRING.
                # A key in a URL is written to every proxy log between here and
                # the browser, kept in history, and leaked in the Referer of
                # any outbound link. The same reason operator_auth refuses to
                # read one from the query.
                b = self._body()
                nid, tok = str(b.get("node_id") or ""), str(b.get("token") or "")
                nd = db.node(nid) if nid else None
                if not nd or not nd.get("token") or not tok:
                    return self._err(404, "unknown camera")
                import secrets as _s
                if not _s.compare_digest(str(nd["token"]), tok):
                    return self._err(403, "wrong key")
                origin = b.get("origin") or ""
                if not origin.startswith(("http://", "https://")):
                    return self._err(400, "bad origin")
                # The key lives in the FRAGMENT. Browsers never transmit it, so
                # scanning this code sends nothing to any server - the
                # capability travels in the picture and stops at the device.
                url = f"{origin.rstrip('/')}/node#k={nid}.{tok}"
                try:
                    return self._send(200, qr.png(url), "image/png")
                except ValueError as exc:
                    return self._err(400, str(exc))

            if p == "/api/key/rotate":
                # Losing a key must be recoverable. Rotating mints a new token
                # and every copy of the old QR stops working at once.
                b = self._body()
                nid, tok = str(b.get("node_id") or ""), str(b.get("token") or "")
                nd = db.node(nid) if nid else None
                if not nd or not nd.get("token"):
                    return self._err(404, "unknown camera")
                import secrets as _s
                if not (_s.compare_digest(str(nd["token"]), tok) or self._is_local()):
                    return self._err(403, "wrong key")
                new = _s.token_urlsafe(24)
                c = db.connect()
                c.execute("UPDATE nodes SET token=? WHERE id=?", (new, nid))
                c.commit()
                return self._json({"ok": True, "node_id": nid, "token": new})

            if p == "/api/operator/login":
                if not operator_auth.required():
                    return self._json({"ok": True, "note": "auth is off"})
                val = operator_auth.login(self._body())
                if not val:
                    return self._err(401, "wrong token")
                self._send(200, json.dumps({"ok": True}).encode(),
                           "application/json",
                           {"Set-Cookie": operator_auth.cookie_header(val)})
                return
            if p == "/api/operator/logout":
                self._send(200, json.dumps({"ok": True}).encode(),
                           "application/json",
                           {"Set-Cookie": operator_auth.cookie_header("", clear=True)})
                return

            # --- reviewer session + verdicts (token-gated) --------------------
            if p == "/api/rv/login":
                val = review_auth.login_value((self._body() or {}).get("token", ""))
                if not val:
                    # 🚨 NO SLEEP INSIDE AN ADMISSION PERMIT.
                    # This slept 0.3s while holding one of the concurrency
                    # permits, on an unauthenticated route with no rate budget -
                    # so a few hundred empty POSTs could occupy the gate and
                    # 503 the site. _body() returns {} on Content-Length 0, so
                    # an EMPTY post reached the sleep.
                    #
                    # The delay is also unnecessary here. It exists to slow
                    # blind guessing, and a reviewer token is 192 bits of
                    # randomness: an attacker guessing at any rate a network
                    # allows will not finish before the heat death of the sun.
                    # Rate-limiting the ROUTE would be worse than useless -
                    # Caddy strips XFF so the bucket is network-wide, and one
                    # attacker would lock every reviewer out.
                    return self._err(401, "invalid token")
                self._send(200, json.dumps({"ok": True}).encode(),
                           "application/json",
                           {"Set-Cookie": review_auth.cookie_header(val)})
                return
            if p == "/api/rv/logout":
                self._send(200, json.dumps({"ok": True}).encode(),
                           "application/json",
                           {"Set-Cookie": review_auth.cookie_header("", clear=True)})
                return
            if p == "/api/rv/retracted/delete":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                b = self._body()
                try:
                    sid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                return self._json(review_api.delete_retracted_photo(
                    r, sid, privacy.audit_ip(self.client_ip)))

            if p == "/api/rv/held/fix":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                b = self._body()
                try:
                    sid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                crop = b.get("crop")
                return self._json(review_api.fix_photo(
                    r, sid, b.get("action") or "",
                    crop if isinstance(crop, dict) else None,
                    privacy.audit_ip(self.client_ip)))

            if p == "/api/rv/verdict":
                r = review_auth.identify(self.headers)
                if not r:
                    return self._err(401, "not signed in")
                b = self._body()
                try:
                    sid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                # The tighten-only rectangle, if the reviewer drew one. Passed
                # through as-is: review_api.tighten clamps every value into
                # [0,1] and refuses anything degenerate, so validating the
                # shape twice in two places would just be two places to get it
                # wrong. A non-dict is dropped here rather than reaching it.
                box = b.get("crop")
                return self._json(review_api.verdict(
                    r, sid, b.get("verdict"), privacy.audit_ip(self.client_ip),
                    crop_box=box if isinstance(box, dict) else None))

            if p == "/api/drive/report":
                # 🚨 CLOSED 2026-08-15. His call, and the right one.
                #
                # It let anybody POST "there is a patrol at this coordinate" -
                # no camera, no photograph, no review, no identity. Every other
                # route onto this map puts a human in front of a picture before
                # a police marker appears; this one asserted a vehicle from a
                # pair of numbers, and the numbers were supplied by the caller.
                # Sending someone a false "police ahead", or clearing a street
                # by covering it in fake ones, cost one request.
                #
                # ⚠️ THE BUTTON WAS NOT THE VULNERABILITY. drive.html was this
                # route's only caller, so deleting the button and leaving the
                # route open would have removed the feature from honest users
                # and left it working for everybody else - which is worse than
                # doing nothing, because it looks fixed.
                #
                # Reads and votes stay open so reports already on the layer age
                # out normally. Re-enabling means restoring the body below; the
                # db helper (add_driver_report) is untouched.
                return self._err(410, "reporting a patrol by tapping has been "
                                      "withdrawn - sightings come from cameras")
            if p == "/api/drive/vote":
                if not rate_ok(p, self.client_ip):
                    return self._err(429, "voting too fast")
                b = self._body()
                try:
                    rid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                ok = db.vote_driver_report(rid, bool(b.get("still_there")))
                return self._json({"ok": ok})

            if p == "/api/rv/my-token":
                # A camera fetches its OWN reviewer token, proving ownership with
                # its node token. Lets a volunteer who enrolled before tokens
                # existed get theirs automatically the next time their node runs,
                # with no operator involvement. Minted once (ensure_own_token), so
                # calling it repeatedly does not spawn tokens.
                b = self._body()
                nd = db.node(str(b.get("node_id") or ""))
                if not nd:
                    return self._err(404, "unknown node")
                if not self._token_ok(nd):
                    return self._err(401, "bad node token")
                # 🚨 TWO TOKENS, TWO PAGES, ON PURPOSE.
                # `own` sees only this camera and is what the owner wants most
                # of the time. `pool` is the shared queue of everyone's pending
                # government calls - self-service, because a crowd-labelling
                # queue only the operator can reach is not a crowd. They are
                # separate tokens rather than one widened token so that either
                # can be revoked without taking the other with it: if a camera
                # owner starts publishing rubbish into the pool, the pool token
                # goes and they keep their own camera.
                want = str(b.get("scope") or "own")
                url = "/rv/pool" if want == "pool" else "/rv/mine"
                # 🚨 A WAY OUT OF "ALREADY ISSUED". The token is stored as a
                # hash and cannot be shown twice, so an owner who lost theirs
                # was told "this camera already has that token" for ever. The
                # node token in this very request is the same secret that lets
                # the camera publish sightings, so replacing its reviewer token
                # grants nothing new - and the old one is revoked, because a
                # token that was lost to somebody else must not stay live.
                if b.get("regenerate"):
                    tok = review_auth.reissue(
                        nd["id"], nd.get("name") or "a camera", want,
                        created_by="self")
                    return self._json({"ok": True, "new": True, "scope": want,
                                       "replaced": True,
                                       "review_token": tok, "review_url": url})
                if want == "pool":
                    tok = review_auth.ensure_pool_token(
                        nd["id"], nd.get("name") or "a camera")
                else:
                    tok = review_auth.ensure_own_token(
                        nd["id"], nd.get("name") or "a camera",
                        created_by="self")
                if tok:
                    return self._json({"ok": True, "new": True, "scope": want,
                                       "review_token": tok, "review_url": url})
                return self._json({"ok": True, "new": False, "scope": want,
                                   "review_url": url,
                                   "note": "this camera already has that token"})

            # --- token administration (operator only) -------------------------
            if p == "/api/rv/tokens/new":
                if not self._is_local():
                    return self._err(403, "operator only")
                b = self._body()
                nodes = b.get("nodes") if isinstance(b.get("nodes"), list) else []
                return self._json(review_api.issue_token(
                    str(b.get("label") or "reviewer")[:60],
                    "own" if b.get("scope") == "own" else "pool",
                    [str(n)[:32] for n in nodes]))
            if p == "/api/rv/tokens/revoke":
                if not self._is_local():
                    return self._err(403, "operator only")
                b = self._body()
                try:
                    tid = int(b.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad id")
                return self._json(review_api.revoke_token(tid))
            if p == "/api/review/bulk":
                # Clearing the possibly-missed queue in one action, after a
                # human has scrolled it and promoted the government vehicles.
                #
                # 🚨 IT CLEARS THE IDs THE PAGE SENDS, NEVER "everything the
                # query matches". Re-running the query here would also clear
                # anything that arrived between him looking and pressing the
                # button - sightings nobody has seen, marked reviewed by
                # someone who never saw them. The page knows what was on
                # screen; the server must not guess.
                #
                # Each one is still written back as a `civilian` label, so a
                # sweep of 17 non-police is 17 negatives for the classifier
                # rather than a delete.
                if not self._is_local():
                    return self._err(403, "local only")
                b = self._body()
                ids = b.get("ids") or []
                verdict = b.get("verdict")
                if verdict not in ("retracted", "confirmed"):
                    return self._err(400, "verdict must be retracted or confirmed")
                if not isinstance(ids, list) or not ids:
                    return self._err(400, "no ids")
                if len(ids) > 200:
                    return self._err(400, "too many at once; 200 is the limit")

                done, labelled, skipped = 0, 0, 0
                for raw in ids:
                    try:
                        sid = int(raw)
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    row = db.sighting(sid)
                    if not row or row["reviewed"] is not None:
                        # Already decided - by him a moment ago, or by another
                        # tab. Not an error, and not something to overwrite.
                        skipped += 1
                        continue
                    db.review_sighting(sid, verdict)
                    done += 1
                    try:
                        from detect import bank
                        if bank.label_by_sighting(
                                sid, "civilian" if verdict == "retracted"
                                else "police"):
                            labelled += 1
                    except Exception:
                        pass
                db.audit(f"review_bulk_{verdict}", ",".join(str(i) for i in ids[:50]),
                         actor="operator", ip=privacy.audit_ip(self.client_ip))
                return self._json({"ok": True, "cleared": done,
                                   "labelled": labelled, "skipped": skipped,
                                   **db.review_stats()})

            if p == "/api/purge":
                # Deleting rows is an operator action. It was reachable by
                # anyone: it only removes ALREADY-EXPIRED data, so the damage
                # was bounded, but an unauthenticated state change on a public
                # box is a thing an attacker builds on, not a thing to leave.
                if not self._is_local():
                    return self._err(403, "operator only")
                rep = privacy.purge_expired(db.connect())
                return self._json(rep)

            return self._err(404, "no such route")
        except Exception:
            traceback.print_exc()
            self._err(500, "internal error")

    # -- auth -----------------------------------------------------------
    def _token_ok(self, nd: dict) -> bool:
        """Constant-time bearer check. A node with no token accepts anyone."""
        if not nd.get("token"):
            return True
        import hmac as _hmac
        supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return _hmac.compare_digest(supplied, nd["token"])

    # -- ingest ---------------------------------------------------------
    def _ingest(self, ev: dict, operator_confirmed: bool = False) -> None:
        """Accept a detection from a node.

        `operator_confirmed` may ONLY be set by /api/node/confirm, which proves
        the caller owns the camera the crop came from. It is a parameter rather
        than a field in `ev` on purpose: anything inside `ev` is attacker-
        controlled, and this one grants the right to publish.

        The node has already done recognition locally. What arrives here is a
        few hundred bytes of structured claim plus a cropped still. No video
        ever crosses this boundary, which is the architectural reason SparrowMap
        is not a wiretap: there is no stream to intercept, subpoena or leak.
        """
        nid = ev.get("node_id")
        nd = db.node(nid) if nid else None
        if not nd:
            return self._err(404, "unknown node")
        if nd["status"] != "active":
            return self._err(403, f"node is {nd['status']}")

        # --- authenticate the node ---------------------------------------
        sig_ok = node_mod.verify_event(ev, ev.get("sig", ""), nd.get("pubkey") or "")
        if nd.get("pubkey") and not sig_ok:
            return self._err(401, "signature did not verify")
        if not self._token_ok(nd):
            return self._err(401, "bad node token")

        conf = float(ev.get("plate_conf") or 0)
        plate = ev.get("plate_text") or ""

        # ⚠️ THE PLATE CONFIDENCE GATE ONLY APPLIES TO PLATES.
        #
        # It used to reject every submission scoring under the threshold,
        # including ones carrying no plate at all - which silently discarded
        # exactly the sightings the visual identifier exists to produce. A
        # camera that cannot read plates would have been gated out by a plate
        # rule. Drop a WEAK plate; never drop the whole sighting for not
        # having one.
        if plate and conf < float(CONFIG.get("min_plate_confidence", 0.55)):
            plate, conf = "", 0.0

        evidence = dict(ev.get("evidence") or {})
        source = ev.get("source", "camera")

        # A submitter cannot hand themselves signals that are supposed to be
        # EARNED, not asserted:
        #   human_confirmed - the reviewer's judgement; only the operator tool
        #     sets it, or anybody could tag a neighbour's car and publish it.
        #   visual_police   - the trained head's verdict. It carries weight 0.0,
        #     so asserting it as a boolean adds no confidence but fabricates a
        #     second "visual" marker for the two-marker police rule, storing a
        #     stranger's plate on one real cue. It may only arise inside the
        #     gated head block from visual_police_conf/margin, which the node
        #     computes and this server re-derives.
        evidence.pop("human_confirmed", None)
        evidence.pop("visual_police", None)

        # 🚨 THE OPERATOR'S CONFIRMATION, RESTORED AFTER THE STRIP AND NEVER
        # BEFORE IT. The strip above is right: `human_confirmed` is the single
        # strongest signal classify.py has (weight 4.0, and it WAIVES the
        # two-marker police rule), so a submitter who could assert it could
        # publish a neighbour's car. It is set here instead, from a flag this
        # server derived by checking a node token against the node that owns
        # the crop - never from anything the caller typed.
        #
        # 📌 WHY THIS PATH HAS TO EXIST AT ALL. The posting gate runs at the
        # moment a vehicle leaves frame, and it drops passes that clear no two
        # markers. That is correct and it is also why a confirmed patrol car
        # could never reach the map: a real one was scored by the trained head
        # at 0.987, failed the gate because the plate read disagreed (0.34 <
        # 0.55) and no second marker fired, and was discarded. The operator then
        # pressed "Yes - government" on the crop and nothing happened, because
        # there was no sighting to promote. The human arrived AFTER the gate.
        # classify.py has always known how to weigh a human; it simply never got
        # told, because the row it would have gone on was never created.
        if operator_confirmed:
            evidence["human_confirmed"] = True

        # A public mirror cannot score a phone crop (no GPU, no trained head),
        # so it parks a plate-less copy for the home classifier to pull. Captured
        # HERE, before the mirror drops the image below, and written to the inbox
        # after the row exists so it can be keyed by the sighting id. The size is
        # re-verified (subresolution_bytes), so an oversized crop is refused, not
        # quarantined. See mirror.quarantine_write.
        relay_crop = None
        if (source == "phone_node" and ev.get("snap_b64")
                and mirror.relay_enabled()):
            try:
                relay_crop = snapshot.subresolution_bytes(ev["snap_b64"])
            except Exception:
                relay_crop = None

        c = classify.classify(evidence)

        # A public SIGHTING and a published PLATE are different decisions.
        # `sightable` puts "a marked patrol unit was here" on the public map -
        # no identifier, so nothing to protect. `tierable` is what allows the
        # plate TEXT through, and it keeps the strict bar. Gating both on
        # `tierable` meant a plate-blind camera could never contribute
        # anything, which is most cameras. See classify.py rule 4.
        # 🚨 A HUMAN SUBMISSION IS A CLAIM, NOT A RECORD (his call: nothing
        # auto-publishes without the trained head first).
        #
        # This used to reach the public tier directly on the argument that a
        # person looking at a marked patrol car beats any classifier. True of an
        # honest person, and that is the whole problem: the submitter chooses
        # the markers, so "two distinct visual markers" is two taps, and the map
        # would publish whatever a stranger asserted about a vehicle they picked.
        # Every other route to the public tier is gated by a model that cannot
        # be argued with. This one was gated by the submitter's own honesty.
        #
        # So it is recorded PRIVATE and routed to the pen, where the trained head
        # scores its crop and a human confirms it. Nothing is thrown away and
        # nothing is distrusted - the claim simply has to survive the same gate
        # everything else survives before it names a vehicle in public.
        #
        # ⚠️ THIS MUST HAPPEN BEFORE `tier` IS COMPUTED. Clearing the flags after
        # the tier line reads as a fix, changes the reason string, and publishes
        # exactly as before - the failure this codebase keeps producing: a check
        # that runs and is not applied to the thing it governs.
        if source == "phone":
            c["why"] = f"human-submitted by {nid}, awaiting review; " + c["why"]
            c["tierable"] = False
            c["sightable"] = False

        # A public SIGHTING and a published PLATE are different decisions.
        # `sightable` puts "a marked patrol unit was here" on the public map -
        # no identifier, so nothing to protect. `tierable` is what allows the
        # plate TEXT through, and it keeps the strict bar. Gating both on
        # `tierable` meant a plate-blind camera could never contribute
        # anything, which is most cameras. See classify.py rule 4.
        tier = "public" if (c["tierable"] or c["sightable"]) else "private"

        # 🚨 THE PUBLIC TIER IS ENTERED BY A PERSON, NEVER BY INGEST.
        # 33 of the 34 sightings ever auto-published came through here: the
        # classifier judged a submission sightable and the row went public with
        # nobody having looked at it. That is the claim the project is now
        # making publicly - that a human decides what appears on the map - and a
        # claim has to be true in the code, not merely usual in practice.
        #
        # The classification is NOT discarded. `c` still carries vclass and the
        # reason, the crop is still parked in the review pen below, and a
        # reviewer promotes it with one press. All that changes is that the
        # default is private and the publish step needs a person.
        #
        # ⚠️ This is deliberately AFTER `tier` is computed, so the classifier's
        # own opinion is still what routes the crop to the pen. Clearing the
        # flags earlier would have made every candidate invisible instead of
        # merely unpublished - the difference between "waiting for review" and
        # "silently dropped".
        # ⚠️ REMEMBER THAT THIS WAS A CANDIDATE. Downstream code needs to know
        # "the classifier would have published this" AFTER the tier has been
        # rewritten to private, and the tier can no longer answer that. Two
        # separate behaviours were silently switched off by reading `tier`
        # here: fragment merging, and the pen write itself.
        candidate = (tier == "public")
        # 🚨 AN OPERATOR CONFIRMATION IS THE HUMAN STEP. DO NOT HOLD IT FOR ONE.
        #
        # The hold above exists because nobody has looked yet. On this path
        # somebody has: /api/node/confirm is only reached by the owner of the
        # camera, authenticated with its token, answering the popup about a
        # vehicle they just watched go past. Holding it for review asked the
        # same person the same question twice.
        #
        # It also made the publish depend on a SECOND request succeeding
        # (labelbank then calls /api/node/label to promote it). If that call
        # failed the sighting sat private with no pen card - the confirmation
        # reaching neither the map nor the queue, which is the exact silent loss
        # /api/node/confirm was built to end.
        #
        # `public_tiers` still decides: a confirmed council truck is not public
        # here either, because `tier` came from privacy.tier_for(vclass) above.
        if candidate and not operator_confirmed:
            c["why"] = (c.get("why") or "") + " - held for human review"
            tier = "private"
        elif candidate:
            c["why"] = (c.get("why") or "") + " - confirmed by the camera operator"
            # 🚨 RECORD THE DECISION. A public row with reviewed IS NULL is
            # indistinguishable from one that reached the map unreviewed, which
            # is the single claim this project makes about itself - "nothing is
            # published without a person". The audit checks for exactly this
            # ("public tier with no human decision"), and it would have started
            # counting these.
            ev["_reviewed"] = "confirmed"
            ev["_decided_by"] = "human"

        # A camera node scores its own crop, so its GOVERNMENT candidates go
        # straight to the review pen for a human to confirm - captured here as a
        # sub-resolution, plate-less crop BEFORE the mirror strips the image
        # below. Phone-node crops take the inbox path instead (box_puller pulls
        # and scores them at home first), so they are excluded here.
        # `phone` (a human submission) is included here now that it no longer
        # reaches the public tier by itself: the pen is where its claim gets
        # looked at. `phone_node` is still excluded because it has its own route
        # - the inbox, which box_puller pulls and scores at home.
        review_crop = None
        evidence_crop = None
        if (source != "phone_node" and mirror.relay_enabled()
                and ev.get("snap_b64") and c["vclass"] in ("police", "gov_dot")):
            try:
                # 🚨 CROP TO THE VEHICLE FIRST. A camera node posts its whole
                # FRAME (store_submitted crops it server-side), so merely
                # downscaling it parked a 200px photograph of the street - and
                # the neighbours' houses with it - in front of every reviewer.
                # The published snapshot was already being cropped correctly;
                # only this second reader of the same field was not.
                _vb = ev.get("vehicle_box")
                if _vb:
                    review_crop = snapshot.crop_to_subres(ev["snap_b64"],
                                                          tuple(_vb))
                    # And the same crop WITHOUT the 200px shrink, for the
                    # reviewer and for whatever gets published if they say yes.
                    # Built here because this is the last point the original
                    # frame is still in hand - below, the mirror strips the
                    # image and the redaction path rewrites it. Failing to
                    # produce it must never cost the pen its card, so the pen
                    # crop above is computed first and this cannot unset it.
                    try:
                        evidence_crop = snapshot.crop_full(ev["snap_b64"],
                                                           tuple(_vb))
                    except Exception:
                        evidence_crop = None
                else:
                    # No box means nothing to crop to. Park no picture rather
                    # than a bystander's - the same call the snapshot path
                    # already makes a few lines below. The candidate still
                    # reaches the reviewer, without an image.
                    review_crop = None
            except Exception:
                review_crop = None

        dropped_image = None
        banked_stem = None
        if ev.get("snap_b64") and not mirror.may_store_image(tier):
            # Nothing to redact, nothing to leak, nothing to subpoena. A
            # mirror keeps photographs of published government vehicles only.
            ev.pop("snap_b64", None)
            dropped_image = "public mirror keeps no private-tier imagery"
        if ev.get("snap_b64") and not ev.get("snap"):
            pbox = ev.get("plate_box")
            pboxes = ev.get("plate_boxes") or ([pbox] if pbox else [])
            vbox = ev.get("vehicle_box")
            meta = {"ts": float(ev.get("ts") or now()), "node_id": nid,
                    "node_name": nd["name"], "tier": tier,
                    "plate_text": plate, "vclass": c["vclass"],
                    "watermark": "UNVERIFIED" if source == "phone" else ""}
            if source == "phone_node" and not pbox:
                # A phone node cannot locate a plate to redact, so it destroys
                # it instead: the crop arrives already below plate legibility.
                # store_subresolution MEASURES that rather than believing it.
                try:
                    ev["snap"] = snapshot.store_subresolution(ev["snap_b64"], meta)
                    # And keep a copy for labelling. This is the entire reason
                    # phone nodes are worth building: every window someone puts
                    # a camera in is real vehicles in real conditions, which is
                    # what the classifier has been starving for.
                    #
                    # 🚨 BUT A MIRROR MUST NEVER BANK. mirror.may_bank() existed
                    # for exactly this and was never called, so a public mirror
                    # was writing the ORIGINAL full-resolution crop to disk -
                    # the un-degraded image, the thing THREAT_MODEL promises a
                    # breach cannot yield. Labelling happens where the camera is;
                    # the mirror carries claims, not photographs of the street.
                    if mirror.may_bank():
                        from detect import bank as _bank
                        banked_stem = _bank.bank_remote(
                            snapshot.decode_bytes(ev["snap_b64"]), nid,
                            {"ts": float(ev.get("ts") or now()),
                             "cls_name": ev.get("body") or "car",
                             "det_conf": ev.get("det_conf")})
                except ValueError as exc:
                    dropped_image = str(exc)
                except Exception as exc:
                    return self._err(400, f"snapshot rejected: {exc}")
            elif tier != "public" and not pbox and not candidate:
                # We cannot redact a plate we cannot locate, and a photograph of
                # a car IS a photograph of its plate. So a private-tier image
                # with no plate box is discarded rather than stored. The
                # sighting itself still counts; only the picture is dropped.
                #
                # ⚠️ `not candidate` IS THE THIRD BEHAVIOUR THIS FILE LOST BY
                # READING `tier` AFTER IT WAS FORCED TO PRIVATE. The two named
                # above tier's rewrite are fragment merging and the pen write;
                # this is the same mistake with the worst outcome. A marked
                # patrol car whose plate the camera could not resolve - which is
                # MOST of them, at 22px against the 60 an OCR needs - hit this
                # branch and had its photograph thrown away for failing to
                # locate a plate that was never going to be legible. The
                # candidate's original is kept in core.EVIDENCE below instead,
                # where the reviewer can actually see the livery.
                dropped_image = "no plate box to redact on a private-tier image"
            elif source == "phone":
                # A human aimed the camera; their framing IS the crop.
                try:
                    ev["snap"] = snapshot.store_prepared(
                        ev["snap_b64"], meta,
                        plate_box=tuple(pbox) if pbox else None)
                except Exception as exc:
                    return self._err(400, f"snapshot rejected: {exc}")
            elif not vbox:
                # 🚨 A CAMERA NODE MUST SEND THE BOX IT DETECTED.
                # Without one there is nothing to crop to, and the previous
                # behaviour - fall through to the phone path - stored the whole
                # street: the neighbours' houses and other vehicles' plates,
                # none of them redacted. Drop the picture instead. The sighting
                # still counts; an un-croppable image is not worth a bystander.
                dropped_image = "camera submission carried no vehicle_box to crop to"
            else:
                try:
                    ev["snap"] = snapshot.store_submitted(
                        ev["snap_b64"], meta, tuple(vbox),
                        plate_box=tuple(pbox) if pbox else None,
                        plate_boxes=[tuple(b) for b in pboxes])
                except Exception as exc:
                    return self._err(400, f"snapshot rejected: {exc}")

        # 🚨 CLAMP THE NODE'S CLOCK. IT DECIDES RETENTION, ORDERING AND LIVENESS.
        #
        # This took the submitted value unchecked, and three separate systems
        # read it afterwards. A camera running one hour slow has its sighting
        # stored, penned, confirmed by a human and promoted to public - and then
        # never drawn, because /api/sightings defaults to since = now() - 3600.
        # Every counter reports success and the dot is simply not on the map.
        # A back-dated row is also what the retention sweep deletes first, so a
        # skewed clock can quietly feed evidence to the janitor.
        #
        # The node's claim is kept in the response rather than thrown away: the
        # camera is the only party that can fix its own clock, and it cannot fix
        # what it is never told. `clock_skew_s` is what a node should log loudly.
        claimed = float(ev.get("ts") or now())
        server_now = now()
        skew = claimed - server_now
        # A little slack for network delay and honest drift; beyond that the
        # SERVER's clock wins, because it is the one every reader compares
        # against.
        ts = claimed if abs(skew) <= 120 else server_now

        # ⚠️ NEVER nd["lat"] / nd["lon"] HERE. Those are the camera's TRUE
        # coordinates, and /api/sightings serves whatever is stored to anyone.
        # Storing them defeated the node-position jitter entirely - one
        # sighting gave up the exact camera location. See
        # nodes.sighting_position.
        #
        # The seed makes the position stable for this sighting and different
        # from the next one, so passes spread along the watched stretch instead
        # of stacking 31 dots on one pixel.
        s_lat, s_lon = node_mod.sighting_position(
            nd, ev.get("lat"), ev.get("lon"),
            seed=f"{nid}:{ts:.3f}:{ev.get('snap_sha256') or plate or ''}")

        # An empty string is not "no plate", it is a value - and it was being
        # counted as a distinct vehicle. Store the absence as an absence.
        phash = privacy.plate_hash(plate, ev.get("plate_state", "")) or None

        rec = {
            "node_id": nid, "ts": ts,
            "lat": s_lat, "lon": s_lon,
            "tier": tier,
            "plate_hash": phash,
            # Plate text rides on `tierable` alone, NOT on the tier. A public
            # SIGHTING is public because it carries no identifier; attaching an
            # unverified plate to it would smuggle the identifier back in
            # through the very row that was supposed to be identifier-free.
            # Stored for a PUBLIC-tier row, served only after a human confirms
            # it - see privacy.redact. The photograph on a public row already
            # shows the plate, so keeping the text alongside adds no exposure
            # that the image did not; what it adds is SEARCH, and search waits
            # for a person. A retraction purges both (db.review_sighting).
            # 🚨 PLATE TEXT RIDES ON `tierable` ALONE. The old `or tier=="public"`
            # attached a plate to any public row - including a `sightable`-only
            # dot, which is public precisely BECAUSE it carries no identifier.
            # That smuggled the identifier back into the row that was supposed to
            # be identifier-free, the exact thing the comment below warns of.
            "plate_text": plate if c["tierable"] else None,
            "plate_state": ev.get("plate_state") if c["tierable"] else None,
            "plate_conf": conf,
            "vclass": c["vclass"], "vclass_conf": c["conf"], "vclass_why": c["why"],
            "color": ev.get("color"), "body": ev.get("body"),
            "make": ev.get("make"), "model": ev.get("model"),
            "heading": ev.get("heading"), "speed_mph": ev.get("speed_mph"),
            "snap": ev.get("snap"), "source": ev.get("source", "camera"),
            "reviewed": ev.get("_reviewed"), "decided_by": ev.get("_decided_by"),
            "bank_ref": (ev.get("bank_ref") or None),
            "sig_ok": 1 if sig_ok else 0,
        }
        # 🚨 IS THIS A NEW VEHICLE, OR THE SAME ONE STILL CROSSING THE FRAME?
        # A tracker that loses a vehicle behind a window pillar and re-acquires
        # it produces several completed tracks for one pass, and each posted its
        # own sighting - three dots on the map for one patrol car. Fold them.
        # See db.merge_window_row for why the test is deliberately blunt.
        # 🚨 `candidate`, NOT `tier`. This read tier == "public" a few lines
        # after tier was forced to "private", so it was dead code and the
        # occluded-pass bug came straight back - now as THREE review cards for
        # one patrol car, which a human then confirms three times onto the map.
        # Only government candidates are folded: merging ordinary traffic by
        # class and time window would be far too blunt.
        prior = (db.merge_window_row(nid, c["vclass"], ts)
                 if candidate else None)
        if prior:
            db.bump_detections(prior["id"], ts, c["conf"])
            # Liveness is a SERVER observation: "this node spoke to me just
            # now". Passing the node's own timestamp let a fast clock pin a
            # dead camera online and a slow one flap a working camera offline,
            # while /api/heartbeat recorded the same event correctly - so the
            # two paths disagreed about the same node.
            db.heartbeat(nid)
            return self._json({"id": prior["id"], "tier": tier,
                               "vclass": c["vclass"], "merged_into": prior["id"],
                               "why": "same pass as a sighting seconds earlier"})

        # Reduce BEFORE the write, never on the way out: a read-time redaction
        # leaves the data on the disk, and the disk is what gets copied.
        rec = mirror.strip_sighting(rec)
        rec["id"] = db.insert_sighting(rec)
        # 🚨 LINK THE BANKED CROP TO THE SIGHTING IT CAME FROM.
        # Without this a remote crop is an orphan: labelling it "police" in the
        # UI would record the judgement and leave the map untouched, because
        # _sync_sighting has no id to promote. A volunteer's phone catching a
        # patrol car has to be able to reach the map, or their contribution is
        # training data and nothing else.
        if banked_stem:
            try:
                from detect import bank as _bank
                _bank.link_sighting(banked_stem, rec["id"])
            except Exception:
                pass
        # Park the plate-less crop for the home classifier, keyed by this row.
        # After strip_sighting, so nothing about the mirror's own record changed.
        if relay_crop is not None:
            mirror.quarantine_write(rec["id"], relay_crop, {
                "ts": ts, "pub_lat": s_lat, "pub_lon": s_lon,
                "node_name": nd.get("name") or "",
                "det_conf": ev.get("det_conf"), "body": ev.get("body")})
        # A camera's government candidate parks in the review pen for a human.
        if review_crop is not None:
            mirror.review_write(rec["id"], review_crop, {
                "ts": ts, "node_id": nid, "node_name": nd.get("name") or "",
                "score": c.get("conf"), "vclass": c["vclass"],
                "det_conf": ev.get("det_conf"), "body": ev.get("body")})
            # The full-resolution original beside it, at home only. This is
            # what the reviewer is shown and what a confirmation publishes, so
            # the livery survives the wait and a plate is not destroyed for a
            # vehicle whose plate is the entire point of the public tier.
            # mirror.evidence_write refuses on a mirror; core.EVIDENCE carries
            # the rails and the cost.
            if evidence_crop is not None:
                mirror.evidence_write(rec["id"], evidence_crop)

        # A node that is posting is self-evidently awake, so a submission is
        # also a heartbeat. Detectors that never learn to beat still show
        # online while they are actually working.
        db.heartbeat(nid)
        FEED.publish(rec)
        # 🚨 TELL THE CAMERA ITS CROP IS WAITING ON A HUMAN.
        # A phone detects VEHICLES; the government call happens here, after the
        # post. So the phone has never known it caught a patrol car - the
        # judgement was in this response all along and the client read only the
        # error field. A desktop node has had push_confirm since the start
        # ("ask the owner to confirm, NOW, while the vehicle is still in
        # sight"), and a phone volunteer got nothing until they opened /rv
        # hours later to a still image of a car they no longer remember.
        # `parked` is the honest signal: not "this is a cop", but "a person is
        # being asked about this one", which is exactly when it is worth asking
        # the person who is standing there.
        out = {"id": rec["id"], "tier": tier, "vclass": c["vclass"],
               "why": c["why"], "parked": review_crop is not None}
        if abs(skew) > 120:
            # Said plainly, because the node cannot see this any other way and
            # the consequence - its sightings landing outside every default
            # time window - is invisible from its side.
            out["clock_skew_s"] = round(skew, 1)
            out["note"] = (f"your clock is {abs(skew):.0f}s "
                           f"{'ahead of' if skew > 0 else 'behind'} the hub; "
                           f"the server time was used instead")
        if dropped_image:
            out["image_dropped"] = dropped_image
        return self._json(out)

    # -- server-sent events ---------------------------------------------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = FEED.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_beat = now()
            while True:
                try:
                    rec = q.get(timeout=5.0)
                    payload = json.dumps(_public_rows([rec])[0], default=str)
                    self.wfile.write(f"event: sighting\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    if now() - last_beat > 15:
                        self.wfile.write(b": beat\n\n")
                        self.wfile.flush()
                        last_beat = now()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            FEED.unsubscribe(q)


# ---------------------------------------------------------------------------
# Background chores
# ---------------------------------------------------------------------------

def _janitor() -> None:
    """Enforce retention on a timer.

    A retention promise that only runs when someone remembers to click a button
    is not a retention promise.
    """
    while True:
        try:
            rep = privacy.purge_expired(db.connect())
            if rep["private_deleted"] or rep["public_deleted"]:
                print(f"[janitor] purged {rep}")
        except Exception:
            traceback.print_exc()
        time.sleep(600)


def _simulator() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "sources"))
    import synthetic
    print("[sim] simulated town running (source=synthetic)")
    synthetic.run(ticks=0, dt=4.0, sleep=1.0, on_sighting=FEED.publish)


def _tls_listener(port: int) -> None:
    """Second listener over TLS.

    A browser will not hand a page the camera or a precise GPS fix unless the
    origin is secure, so the phone contributor page is unusable over plain http
    on the LAN. Self-signed is enough: the browser warns once, and accepting it
    makes the origin secure. (Same wall, same fix, as the acoustic sonar page.)
    """
    import ssl
    crt = Path(__file__).parent / "certs" / "sparrow.crt"
    key = Path(__file__).parent / "certs" / "sparrow.key"
    if not (crt.exists() and key.exists()):
        print("[tls] no certs in ./certs, https listener not started")
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    import os as _os
    from dualstack import serve
    srv = serve(Handler, port, _os.environ.get("SPARROW_BIND") or "::")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"  https  ->  https://localhost:{port}/app   (phone camera)")
    srv.serve_forever()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="SparrowMap hub")
    ap.add_argument("--port", type=int, default=CONFIG.get("http_port", 8150))
    ap.add_argument("--https-port", type=int, default=CONFIG.get("https_port", 8151))
    # ⚠️ THE SIMULATOR IS OFF BY DEFAULT AND MUST STAY THAT WAY.
    #
    # It was invaluable for building the whole system before any hardware
    # existed. It is a liability the moment anyone else can see the map,
    # because a synthetic sighting is indistinguishable from a real one once
    # it is a dot on a map with a photograph attached - and this project's
    # entire value is that what it shows is true.
    #
    # Opt in explicitly with --sim, and everything it writes is tagged
    # source='synthetic' so it can always be found and removed.
    ap.add_argument("--sim", action="store_true",
                    help="run the SIMULATED town (development only - writes fake sightings)")
    args = ap.parse_args()

    # 🚨 FAIL-CLOSED BACKSTOP - RUNS BEFORE ANYTHING BINDS A SOCKET.
    # When operator auth is not effectively required (operator_auth.required is
    # also forced on by behind_tls), operator power is granted purely by source
    # IP, which is only safe on loopback: `::`/`0.0.0.0` are wildcard binds that
    # expose the port to the whole network, where "the request came from an
    # operator address" stops meaning anything. The repo ships no config.json and
    # a first run auto-generates one from fail-open defaults, so this is the last
    # thing between a fresh `python hub.py` and an internet-reachable box that
    # trusts anyone. It runs here, before the HTTP and TLS listeners start, so no
    # socket is ever briefly bound on a public interface before the check.
    import os as _os
    import ipaddress as _ip
    host = _os.environ.get("SPARROW_BIND") or "::"

    def _loopback_only(h: str) -> bool:
        h = (h or "").strip()
        if h in ("localhost",):
            return True
        try:
            return _ip.ip_address(h).is_loopback
        except ValueError:
            return False        # "::", "0.0.0.0", or a hostname => not loopback

    if not _loopback_only(host) and not operator_auth.required():
        raise SystemExit(
            f"REFUSING TO START: binding {host}:{args.port} with operator "
            "authentication OFF lets anyone who can reach this port act as the "
            "operator (retract, promote, confirm - which publishes a plate). "
            "Fix ONE of:\n"
            "  * set operator_requires_auth: true in config.json (then log in "
            "at /login with the token in data/operator.token), or\n"
            "  * set behind_tls: true if a proxy terminates TLS in front, or\n"
            "  * bind loopback only: SPARROW_BIND=127.0.0.1 python hub.py")

    db.init()
    threading.Thread(target=_janitor, daemon=True).start()
    if args.sim:
        print("!! SIMULATOR ON - this map will contain FAKE sightings (source=synthetic)")
        threading.Thread(target=_simulator, daemon=True).start()
    threading.Thread(target=_tls_listener, args=(args.https_port,),
                     daemon=True).start()

    # Dual-stack (see dualstack). `host` and the fail-closed bind backstop were
    # computed right after arg-parse, before any listener bound a socket.
    from dualstack import serve
    srv = serve(Handler, args.port, host)
    print(f"SparrowMap hub {VERSION}  ->  http://localhost:{args.port}/  "
          f"(bound to {host})")
    print(f"  policy: civilian plates hashed, {CONFIG['civilian_retention_days']}d "
          f"retention, pepper rotates every {CONFIG['pepper_rotation_days']}d")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
