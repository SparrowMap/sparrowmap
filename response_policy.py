"""response_policy.py - Stage 1B step 4: cache/CORS/CSRF/security-header policy.

Pure-function decisions extracted from hub.py's Handler._cache_control,
Handler._send's header-writing block, and Handler._do_POST_inner's CSRF
guard. This module decides WHAT the policy is; hub.py's Handler still owns
WHEN to call it and the actual wire write (send_header/end_headers), which
stays with _send per the Stage 1B pre-analysis (§11.1: _send must eventually
split into "decide" vs "write", and the write side is not moved this stage).

Every branch here was characterized against the pre-extraction implementation
in tools/test_cache_control_characterization.py before this module existed;
that test is re-run unchanged against this module's cache_control() to prove
byte-identical output.

Nothing here imports hub.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Public read paths served IDENTICALLY to every anonymous viewer - see
# microcache.CACHEABLE_API, which this module reads by reference so the two
# stay in lockstep (a route being cache-key-tracked and a route being
# Cache-Control-cacheable must never silently disagree).
import microcache

CACHEABLE_API = microcache.CACHEABLE_API


def cache_control(path: str, status: int) -> str:
    """Per-path (and per-status) caching policy.

    THIS IS WHAT LETS THE MAP SURVIVE A CROWD. The origin (a threaded Python
    server) caps near 55 req/s on the map data - measured. But that data is
    PUBLIC and identical for everyone, so it belongs on the edge: with a
    short shared cache, thousands of viewers collapse to about one origin
    fetch per window and the ceiling stops mattering.

    Default stays no-store. It is opened up ONLY for things that are public
    and the same for all viewers. The privacy reason no-store existed - not
    keeping a record of who looked at which plate - lives on the SEARCH and
    OPERATOR and per-user paths, which stay no-store below.
    """
    p = urlparse(path).path
    # NEVER PUT A LONG TTL ON A FAILURE.
    # This decided purely from the PATH, so a tile that 404d - an upstream
    # blip, or the tile-fetch bound shedding under load - was stamped
    # "public, max-age=604800" exactly like a real tile. That burns a blank
    # square into that visitor's browser for a WEEK, for a transient error
    # that would have resolved on the next request. The status is part of
    # what is cacheable, not just the path.
    if status >= 400:
        return "no-store"
    if p.startswith(("/vendor/", "/api/tile/")):
        # PINNED content only: the vendored detector runtime (a 10 MB model
        # + wasm + Leaflet) and basemap tiles. These do not change without a
        # deliberate library swap, so a long cache saves re-downloading 10 MB
        # on every visit. NOT `immutable` - if a library is ever replaced a
        # 7-day revalidation is cheap insurance against serving a stale one.
        return "public, max-age=604800"
    if p.startswith("/static/"):
        # THE APP'S OWN CODE (app.js, sitenav.js, transparency.js). It MUST
        # be able to change - marking it immutable froze every JS fix for a
        # week on returning visitors. Short cache: still absorbs a launch
        # spike (thousands of requests in a minute -> one origin hit) but a
        # code change propagates within the minute. (A content-hashed
        # filename would let this be immutable too - a later build step.)
        return "public, max-age=60"
    if p in CACHEABLE_API:
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
        # /api/nodes IS NOT A LIVE COUNTER AND MUST NOT BE PRICED LIKE ONE.
        # It sat on max-age=3 because it was grouped with the counters at the
        # top of the map - but those are /api/stats, which is 252 BYTES.
        # This is the camera list: 13,637 rows and 4 MB, the single most
        # expensive answer this server produces.
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


# The public map is meant to be embeddable and mirrorable by anyone. Operator
# JSON is not, and a wildcard on it is needless surface even with a
# SameSite=Strict cookie in front.
_CORS_EXCLUDED_PREFIXES = ("/api/review", "/api/operator", "/api/purge",
                           "/api/rv")


def cors_allowed(path: str) -> bool:
    """Whether Access-Control-Allow-Origin: * should be sent for this path."""
    return not path.startswith(_CORS_EXCLUDED_PREFIXES)


def security_headers(nonce: str) -> list[tuple[str, str]]:
    """The fixed set of headers that protect the VISITOR, in send order.

    REFERRER POLICY IS AN ANONYMITY CONTROL HERE, NOT A FORMALITY. Without
    it, every outbound click - the OpenStreetMap attribution link at the
    bottom of the map, for one - tells the destination that the visitor came
    from sparrowmap.com, and carries the full URL including any plate they
    searched for.
    """
    return [
        ("Referrer-Policy", "no-referrer"),
        # Stops a browser from second-guessing a declared content type,
        # which is how a stored file gets executed as script.
        ("X-Content-Type-Options", "nosniff"),
        # Nobody frames this map. Clickjacking a "retract" button would be a
        # quiet way to vandalise the record.
        ("X-Frame-Options", "DENY"),
        # Everything this site loads, it ships. Leaflet is vendored, there
        # is no CDN and no analytics, so the policy can be strict enough to
        # actually contain an injection rather than decorate the response.
        ("Content-Security-Policy",
         "default-src 'self'; img-src 'self' data: blob:; style-src 'self' "
         # 'wasm-unsafe-eval' IS REQUIRED OR THE DETECTOR CANNOT LOAD.
         # Compiling a WebAssembly module counts as script generation, so a
         # script-src without it refuses the ONNX runtime outright - which
         # broke the camera on every device, with the map and every other
         # page still working perfectly.
         #
         # It is far narrower than 'unsafe-eval': it permits WebAssembly
         # compilation and nothing else - no eval, no new Function, no
         # inline string execution. The policy stays meaningful.
         f"'unsafe-inline'; script-src 'self' 'wasm-unsafe-eval' "
         f"'nonce-{nonce}'; "
         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
         "form-action 'self'"),
    ]


# State-changing routes authenticated by the operator COOKIE. A browser
# attaches that cookie to any cross-site request automatically, so these
# need CSRF defence beyond the cookie itself.
CSRF_SENSITIVE = {"/api/review", "/api/review/bulk", "/api/review/edit",
                  "/api/purge", "/api/key/rotate", "/api/operator/login",
                  "/api/operator/logout", "/api/report",
                  "/api/rv/login", "/api/rv/logout", "/api/rv/verdict",
                  "/api/rv/tokens/new", "/api/rv/tokens/revoke",
                  "/api/rv/my-token", "/api/drive/report", "/api/drive/vote",
                  "/api/rv/retracted/delete", "/api/rv/held/fix",
                  "/api/node/span", "/api/node/key"}


def csrf_ok(path: str, content_type: str | None) -> bool:
    """True if a state-changing POST to path may proceed given content_type.

    CSRF: require a real application/json Content-Type on cookie-authed
    routes. Body parsing accepts JSON regardless of declared type, so
    without this a cross-site <form> POSTing text/plain that HAPPENS to be
    valid JSON would be accepted, and the browser would attach the operator
    cookie - letting any page the operator visits retract a sighting or
    purge data. application/json is NOT a form-reachable "simple" content
    type: a cross-origin fetch sending it triggers a CORS preflight, which
    this server never approves for these paths. SameSite=Strict already
    blocks the cookie cross-site; this is the second lock, and the one that
    still holds when auth is off and operator power comes from a LAN/
    loopback source IP instead.
    """
    if path not in CSRF_SENSITIVE:
        return True
    ctype = (content_type or "").split(";")[0].strip().lower()
    return ctype == "application/json"
