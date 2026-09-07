"""Rate limiting extracted from hub.py (Stage 1B, step 1).

The two routes a stranger can write to - enrolment and sighting submission -
had no limit at all. On a private tailnet that is fine; on sparrowmap.com it
is an invitation to fill the database overnight. Deliberately crude: a fixed
window per address, in memory, no dependency. It will not stop a distributed
flood, and it is not trying to - it stops the trivial script, which is the
actual threat to a small project on day one.

This module owns the shared `_HITS` table, its lock, and the `RATE` table of
per-route budgets, exactly as they lived in hub.py. `rate_ok` had zero
`Handler` coupling before this move (it only needed `core.now`), so this is a
near-verbatim relocation, not a redesign.
"""

from __future__ import annotations

import threading

from core import now

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
