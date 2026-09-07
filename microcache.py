"""microcache.py - Stage 1B step 3: single-flight micro-cache for public GET routes.

Moved out of hub.py with the smallest possible semantic change. This module
owns the cache/lock/flight state and the pure key/TTL helpers; hub.py's
do_GET still owns the actual dispatch choreography (deciding whether to lead,
follow, or wait), because that choreography also calls back into
Handler._gated/_do_GET_inner/_send, which have not moved. See
docs/HUB_ARCHITECTURE.md Stage 1B pre-analysis (§11.3) for the extraction
seam this follows.

Nothing here imports hub - see admission.py/ratelimit.py for the same rule.
"""

from __future__ import annotations

import math
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

# The window `since` timestamps are rounded to for caching. Must match
# CACHE_BUCKET_S in public/app.js: the frontend rounds so its polls share a URL,
# and the server rounds so a client that DOESN'T round cannot mint a new cache
# key per request. Protection that depends on the client cooperating is not
# protection.
CACHE_BUCKET_S = 4

# How coarsely a viewport box is snapped before it is used as a cache key or a
# filter.
#
# THIS IS A CACHE-KEY CARDINALITY KNOB, NOT AN ACCURACY KNOB, AND 0.1 COST US
# THE BOX. At ~11 km, two people looking at the same city from slightly
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


def snap_box(raw: str) -> str:
    """`S,W,N,E` snapped OUTWARD to the BOX_SNAP grid, or "" if unparseable.

    THIS IS BOTH THE CACHE KEY AND THE FILTER. The snapped box must CONTAIN
    the caller's box - never merely approximate it. Round-to-nearest would
    shrink some of them, and the symptom is rows quietly missing at the edge
    of a viewport, only for whoever did not happen to be the request that
    populated the cache.
    """
    s, w, n, e = (float(x) for x in raw.split(","))
    return "%g,%g,%g,%g" % (
        math.floor(s / BOX_SNAP) * BOX_SNAP, math.floor(w / BOX_SNAP) * BOX_SNAP,
        math.ceil(n / BOX_SNAP) * BOX_SNAP, math.ceil(e / BOX_SNAP) * BOX_SNAP)


# Public read paths served IDENTICALLY to every anonymous viewer, so a short
# shared cache collapses thousands of pollers into ~one origin fetch/window.
# Kept here (rather than only in hub.py) because it decides whether a path
# is a micro-cache candidate at all; hub.py's Handler._CACHEABLE_API remains
# the name other code reads and is aliased to this frozenset unchanged.
CACHEABLE_API = frozenset({"/api/sightings", "/api/stats", "/api/policy",
                           "/api/nodes", "/api/leaderboard", "/api/health",
                           # Identical for every viewer and it changes only
                           # when a camera is added, so it is the cheapest
                           # thing on the map to cache.
                           "/api/places",
                           "/api/heat"})

# key -> (timestamp, body)
MICRO: dict = {}
MICRO_LOCK = threading.Lock()
# key -> Event, held by whichever thread is currently computing it.
MICRO_FLIGHT: dict = {}

# What each cacheable route ACTUALLY reads. Anything else is noise and must
# not reach the cache key.
MICRO_PARAMS = {
    "/api/sightings":   ("since", "limit", "vclass", "bbox"),
    "/api/leaderboard": ("hours",),
    # A PARAMETER THAT CHANGES THE ANSWER AND IS NOT LISTED HERE IS A CACHE
    # POISONING BUG, NOT AN OMISSION. /api/nodes now returns a different set
    # for public_cams=0, and without this line both variants share one key:
    # whichever request missed first decides what everybody else gets for
    # the life of the entry - the map either loses 4,800 cameras it asked
    # for or gets a megabyte it deliberately declined.
    "/api/nodes":       ("public_cams", "box"),
}


def key_for(path: str, query_string: str) -> str:
    """Cache key from the path plus ONLY the parameters that change the answer.

    THE KEY WAS ONCE THE WHOLE QUERY STRING, WHICH HANDS ANYONE A
    CACHE-BUSTER. `?x=1`, `?x=2`, `?x=3` ... are unlimited distinct keys on a
    route that ignores `x` entirely. Every one misses, becomes its own
    single-flight leader, takes an admission permit and does the full query -
    so the one defence the origin has against a crowd could be switched off
    from a browser address bar. The map is CORS-open and meant to be
    embedded, so an embedder that does not bucket its `since` values does
    this by accident rather than maliciously.

    `since` is BUCKETED here as well as in the frontend. Relying on the
    client to round it means the protection only exists for clients that
    cooperate, which is not a protection.
    """
    names = MICRO_PARAMS.get(path)
    if not names:
        return path              # no parameters are read: one answer for all
    q = parse_qs(query_string)
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
            # AN UNBUCKETED BOX IS THE CACHE-BUSTER THIS DOCSTRING WARNS
            # ABOUT, AND `bbox` HAS BEEN ONE ALL ALONG.
            #
            # A map sends a new box on every pan, to as many decimal places
            # as Leaflet feels like. Each distinct string is its own key,
            # its own miss, its own single-flight leader and its own
            # admission permit - so the busiest interaction on the site was
            # the one the micro-cache could never help with, and one person
            # dragging the map could mint keys as fast as they could move a
            # finger.
            #
            # SNAPPED OUTWARD, NEVER ROUNDED TO NEAREST, AND THAT IS A
            # CORRECTNESS RULE RATHER THAN A PREFERENCE. Rounding to nearest
            # can SHRINK the box, and then two viewports sharing a key get
            # an answer that covers one of them - rows missing at the edge
            # of somebody's screen, intermittently, depending on who missed
            # the cache first. Snapping outward makes every cached answer a
            # SUPERSET of any box in its bucket: extra rows off-screen are
            # harmless, absent ones are not.
            #
            # The route snaps identically (see snap_box) so the key and the
            # query can never disagree about what was asked for.
            try:
                v = snap_box(v) or v
            except ValueError:
                continue
        bits.append(f"{n}={v}")
    return path + ("?" + "&".join(bits) if bits else "")


def ttl_for(cache_control: str) -> float:
    """TTL in seconds implied by an already-decided Cache-Control string."""
    if "no-store" in cache_control:
        return 0.0
    m = re.search(r"max-age=(\d+)", cache_control)
    return float(m.group(1)) if m else 0.0


def get_hit(key: str):
    """Return (timestamp, body) if present, else None. No TTL check here -
    callers compare against their own ttl, exactly as hub.py's do_GET did."""
    return MICRO.get(key)


def store(key: str, body: bytes) -> None:
    """Record a freshly-built body under key. Bounded: the key includes the
    query string, and `since=` moves every few seconds, so an unbounded dict
    is a slow leak."""
    with MICRO_LOCK:
        MICRO[key] = (time.time(), body)
        if len(MICRO) > 200:
            for k in sorted(MICRO, key=lambda k: MICRO[k][0])[:80]:
                MICRO.pop(k, None)


def begin_or_join(key: str):
    """Register as the leader for key, or find the existing leader.

    Returns (is_leader, event). If is_leader is True, the caller MUST call
    finish(key, event) when done (success or failure) - normally from a
    try/finally exactly as hub.py's do_GET did.
    """
    with MICRO_LOCK:
        leader = MICRO_FLIGHT.get(key)
        if leader is None:
            leader = threading.Event()
            MICRO_FLIGHT[key] = leader
            return True, leader
        return False, leader


def finish(key: str, leader_event: threading.Event) -> None:
    """Release the followers waiting on key, success or failure."""
    with MICRO_LOCK:
        MICRO_FLIGHT.pop(key, None)
    leader_event.set()
