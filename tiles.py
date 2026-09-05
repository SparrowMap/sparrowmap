"""tiles.py - Stage 1B tile substage: basemap tile proxy/cache adapter.

This is an external-integration adapter: the boundary this module owns is
"talk to the upstream tile CDN and keep a bounded on-disk cache of what it
returned", not a generic HTTP-client abstraction and not a second copy of
rate-limiting or admission-gating logic.

Moved verbatim (smallest possible semantic change) from hub.py:
  * TILES, TILE_UPSTREAM, TILE_SUBDOMAINS, TILE_MAX_ZOOM, TILE_CACHE_MAX
  * _tile_count / _tile_prune_lock / _tile_prune() - the amortised,
    single-pruner-at-a-time cache-bound janitor (see prune()'s docstring
    for the outage this was written to prevent; that reasoning is not
    re-litigated here, only carried over)
  * _TILE_FETCH - the semaphore bounding concurrent upstream fetches
  * Handler._tile - now serve(), parameterized over a duck-typed handler
    exactly like the other Stage 1B extractions (needs only
    handler.client_ip and handler._send)

Reused, not duplicated:
  * ratelimit.rate_ok / ratelimit.RATE - the existing "/api/tile" budget is
    read through the same interface every other route uses; this module
    does not keep its own hit table.
  * admission.py is NOT involved. Tiles are deliberately UNGATED (see
    hub.py's _UNGATED comment) - the concurrency bound they need is the
    fetch-only _TILE_FETCH semaphore kept here, not the INFLIGHT/HEAVY/
    INGEST admission pools, which would bound the wrong resource (see
    _tile_prune's docstring and the semaphore's own comment for why).

Does not import hub. Stdlib plus ratelimit only.
"""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path
from core import CONFIG

from ratelimit import rate_ok

# --------------------------------------------------------------------------
# Basemap tiles
#
# See docs/HUB_ARCHITECTURE.md for the full incident history behind this
# module (the CSP/proxy design, the stat()-storm prune outage, and the
# upstream-fetch semaphore outage). Preserved here verbatim, not repeated,
# to avoid drifting two copies of the same narrative.
# --------------------------------------------------------------------------


def _default_tiles_dir() -> Path:
    # Deferred import to avoid a hard dependency at module-import time on
    # core.py's directory layout beyond what is actually needed; core.DATA
    # is itself resolved relative to core.py's own file location and is
    # safe to import eagerly, but keeping this as a function makes the
    # "TILES is overridable" contract (used by tests) explicit.
    from core import DATA
    return DATA / "tiles"


TILES = _default_tiles_dir()
TILE_UPSTREAM = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
TILE_SUBDOMAINS = "abcd"
TILE_MAX_ZOOM = 20
# Tiles are ~5-15 KB. 20k of them is a few hundred MB and covers a town at
# every zoom a viewer will use.
TILE_CACHE_MAX = 20000
_tile_count = None

_tile_prune_lock = threading.Lock()

# How many tile MISSES may be fetching from the upstream CDN at once.
#
# Sized against what the box is: 2 vCPUs whose tile work is almost entirely
# WAITING, so this can be well above the CPU count - but it must exist. With no
# bound at all a purged Cloudflare cache turned every tile into an origin fetch
# and hundreds ran together, which is how a map went 502 while the machine sat
# idle waiting on somebody else's network.
_TILE_FETCH = threading.Semaphore(12)


def _tile_prune() -> None:
    """Keep the tile cache bounded, without stat-ing the tree on every hit.

    The count is held in memory and only recounted when it is unknown (first
    write after start) or when the cap is reached. Walking the cache on every
    tile would turn a 2 ms disk read into a directory crawl at exactly the
    moment a viewer is dragging the map.

    See docs/HUB_ARCHITECTURE.md for the stat()-storm incident this amortised
    design replaced. One thread prunes and the rest carry on; the count is
    corrected in place instead of being thrown away; pruning drops to a low-
    water mark so the next few thousand writes cost nothing at all.
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
        # Oldest first. Tiles are interchangeable and cheap to refetch, so
        # there is no cleverness to buy here.
        files = sorted(TILES.rglob("*.png"), key=lambda f: f.stat().st_mtime)
        # Down to a LOW-WATER MARK, not to the cap. Trimming to exactly the
        # cap leaves the next write over it again.
        target = max(0, len(files) - int(TILE_CACHE_MAX * 0.8))
        removed = 0
        for f in files[:target]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        # Corrected in place, not reset to None (which would force a second
        # full recount on the very next write).
        _tile_count = len(files) - removed
    finally:
        _tile_prune_lock.release()


def serve(handler, path: str) -> None:
    """Serve one basemap tile, from disk if already cached. Formerly Handler._tile.

    Needs only `handler.client_ip` and `handler._send(code, body, ctype, extra)`.

    ⚠️ THE UPSTREAM URL IS BUILT FROM INTEGERS, NEVER FROM THE REQUEST.
    A proxy that forwards a caller-supplied URL is an open proxy: it will
    happily fetch `http://169.254.169.254/` or anything else the box can
    reach, using the box's own network position. So z/x/y are parsed as
    ints, range-checked against the zoom, and formatted into a fixed
    template. There is no code path here that can be pointed somewhere else.
    """
    parts = path[len("/api/tile/"):].split("/")
    if len(parts) != 3 or not parts[2].endswith(".png"):
        return handler._send(404, b"", "text/plain")
    try:
        z = int(parts[0]); x = int(parts[1]); y = int(parts[2][:-4])
    except ValueError:
        return handler._send(404, b"", "text/plain")
    # Reject anything outside the tile grid before it becomes a request.
    if not (0 <= z <= TILE_MAX_ZOOM) or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
        return handler._send(404, b"", "text/plain")

    cached = TILES / str(z) / str(x) / f"{y}.png"
    if cached.exists():
        return handler._send(200, cached.read_bytes(), "image/png",
                              {"Cache-Control": "public, max-age=604800"})

    # Rate-limit only the UPSTREAM path - a cache hit above is cheap, but a
    # miss makes this box fetch from the CDN and hold a thread up to 15s.
    # That is the amplification lever, so the budget guards exactly it.
    if not rate_ok("/api/tile", handler.client_ip):
        return handler._send(429, b"", "text/plain")

    # 🚨 BOUND THE CONCURRENT UPSTREAM FETCHES. THIS TOOK THE SITE DOWN.
    # See docs/HUB_ARCHITECTURE.md for the Cloudflare-purge incident this
    # semaphore was added to prevent. A cache HIT above never reaches here,
    # so this bounds only the amplifying path.
    if not _TILE_FETCH.acquire(timeout=2.0):
        return handler._send(404, b"", "text/plain")

    url = TILE_UPSTREAM.format(s=TILE_SUBDOMAINS[(x + y) % len(TILE_SUBDOMAINS)],
                               z=z, x=x, y=y)

    carto_key = str(CONFIG.get("carto_api_key", "")).strip()
    if carto_key:
        from urllib.parse import quote
        url += "?key=" + quote(carto_key, safe="")

    try:
        raw = urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "SparrowMap/0.1 (+https://sparrowmap.com)"}),
            timeout=15).read()
    except Exception:
        # A missing tile must not be an error page: Leaflet would draw the
        # HTML as a broken image across the map. Fail as a 404 and let it
        # leave that square blank.
        return handler._send(404, b"", "text/plain")
    finally:
        # ⚠️ RELEASED HERE, NOT AFTER THE DISK WRITE. The permit exists to
        # bound UPSTREAM fetches; holding it through the local write would
        # count disk time against a network budget, and a permit leaked on
        # the 404 path would shrink the pool to nothing one failed tile at a
        # time.
        _TILE_FETCH.release()

    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(raw)
        _tile_prune()
    except Exception:
        # Caching is an optimisation. If the disk says no, still serve.
        pass
    return handler._send(200, raw, "image/png",
                          {"Cache-Control": "public, max-age=604800"})
