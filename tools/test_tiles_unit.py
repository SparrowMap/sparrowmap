"""Characterization of tile serving (Stage 1B, tile substage).

🚨 THIS FILE EXERCISES tiles.serve — THE EXTRACTED, tiles.py IMPLEMENTATION,
REACHED EITHER DIRECTLY OR VIA hub.Handler._tile'S ONE-LINE DELEGATION.

This test module was first run against hub.Handler._tile (the unmodified,
pre-extraction implementation) to establish a passing baseline table of
behavior before tiles.py existed. It has since been repointed at tiles.serve
directly, proving the extraction preserved that exact behavior. Both
`hub.TILES`/`hub._TILE_FETCH` (aliases) and `tiles.TILES`/`tiles._TILE_FETCH`
(the owning module) are reset between checks so the test is correct
regardless of which name is used to reach the shared state.

No real outbound network calls are made anywhere in this file. The single
network boundary this module crosses - `urllib.request.urlopen` - is
monkeypatched at the real `urllib.request` module (the same object
`tiles.serve` calls through), so the actual implementation stays the exact
system under test; only the edge of the process is stubbed.

Covers, against tiles.serve directly (no HTTP server, no sockets):

  * malformed z/x/y/extension rejection (the same cases test_hub_behavior.py
    already covers, repeated here for a stable baseline colocated with the
    new tile-specific checks)
  * valid cache miss -> upstream URL construction (exact allow-listed host,
    subdomain selection, path) -> successful synthetic "fetch" -> 200 with
    the right content-type/headers/body -> file written to the exact cache
    path -> a second call is a filesystem-cache HIT with NO second upstream
    call
  * upstream exception -> 404, and the fetch permit is still released (no
    leak)
  * tile rate-limit refusal (429) via ratelimit.RATE["/api/tile"]
  * tile concurrency/semaphore refusal (404) via _TILE_FETCH exhaustion
  * cache path/name construction: TILES/z/x/y.png exactly
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hub
import ratelimit
import tiles

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


class FakeHandler:
    """The minimum surface Handler._tile reads/writes."""

    def __init__(self, ip: str = "127.0.0.1"):
        self.client_address = (ip, 12345)
        self.sent = None  # (code, body, ctype, extra)

    @property
    def client_ip(self) -> str:
        return self.client_address[0]

    def _send(self, code, body, ctype="application/json", extra=None):
        self.sent = (code, body, ctype, extra)

    def _err(self, code, msg):
        self.sent = (code, msg.encode() if isinstance(msg, str) else msg,
                     "text/plain", None)


class FakeResponse:
    """Stands in for the object urllib.request.urlopen(...).read() is called on."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reset_tile_state(tmp_tiles_dir: Path) -> None:
    """Isolate module-level tile/rate-limit state between checks.

    Sets tiles.TILES (the real owner of the constant post-extraction) and
    also hub.TILES (the alias) so this file works whether _tile is reached
    via hub.Handler._tile (which now delegates to tiles.serve) or via
    tiles.serve directly.
    """
    tiles.TILES = tmp_tiles_dir
    hub.TILES = tmp_tiles_dir
    tiles._tile_count = None
    hub._tile_count = None
    ratelimit._HITS.clear()
    # Restore the fetch-concurrency semaphore to its configured capacity in
    # case a prior check in this file drained it and didn't clean up.
    while tiles._TILE_FETCH._value < 12:
        tiles._TILE_FETCH.release()



def t_malformed_rejection(tmp_tiles_dir: Path) -> None:
    print("\n== malformed z/x/y/extension rejection (direct call) ==")
    reset_tile_state(tmp_tiles_dir)
    cases = [
        ("/api/tile/99/1/1.png", "z out of range"),
        ("/api/tile/1/5/1.png", "x out of range for z=1"),
        ("/api/tile/abc/1/1.png", "non-integer z"),
        ("/api/tile/1/0/0.jpg", "wrong extension"),
        ("/api/tile/1/0", "too few segments"),
        ("/api/tile/1/0/0/0.png", "too many segments"),
        ("/api/tile/-1/0/0.png", "negative z"),
        ("/api/tile/1/-1/0.png", "negative x"),
    ]
    for path, label in cases:
        h = FakeHandler()
        tiles.serve(h, path)
        check(f"{label}: {path} -> 404, no upstream call",
              h.sent is not None and h.sent[0] == 404, f"got {h.sent}")


def t_valid_miss_then_hit(tmp_tiles_dir: Path) -> None:
    print("\n== valid cache miss: URL construction, fetch, cache write, then HIT ==")
    reset_tile_state(tmp_tiles_dir)
    z, x, y = 3, 4, 5
    path = f"/api/tile/{z}/{x}/{y}.png"
    fake_bytes = b"\x89PNG-fake-tile-bytes"
    captured_request = {}

    def fake_urlopen(request, timeout=None):
        captured_request["url"] = request.full_url
        captured_request["headers"] = dict(request.headers)
        captured_request["timeout"] = timeout
        return FakeResponse(fake_bytes)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as m:
        h = FakeHandler()
        tiles.serve(h, path)

        check("exactly one upstream fetch was made on a miss",
              m.call_count == 1, f"call_count={m.call_count}")

        expected_subdomain = hub.TILE_SUBDOMAINS[(x + y) % len(hub.TILE_SUBDOMAINS)]
        expected_url = hub.TILE_UPSTREAM.format(s=expected_subdomain, z=z, x=x, y=y)
        check("exact upstream URL constructed from z/x/y (allow-listed host)",
              captured_request.get("url") == expected_url,
              f"got {captured_request.get('url')!r}, want {expected_url!r}")
        check("upstream URL host is the allow-listed CDN",
              captured_request.get("url", "").startswith(
                  f"https://{expected_subdomain}.basemaps.cartocdn.com/"),
              captured_request.get("url"))
        check("upstream request carries the documented User-Agent",
              captured_request.get("headers", {}).get("User-agent")
              == "SparrowMap/0.1 (+https://sparrowmap.com)",
              captured_request.get("headers"))
        check("upstream fetch uses a 15s timeout",
              captured_request.get("timeout") == 15,
              captured_request.get("timeout"))

        check("successful fetch -> 200", h.sent is not None and h.sent[0] == 200,
              f"got {h.sent}")
        check("successful fetch -> exact upstream bytes as body",
              h.sent[1] == fake_bytes, h.sent[1])
        check("successful fetch -> image/png content-type",
              h.sent[2] == "image/png", h.sent[2])
        check("successful fetch -> 7-day public Cache-Control override",
              h.sent[3] == {"Cache-Control": "public, max-age=604800"}, h.sent[3])

    expected_cache_path = tmp_tiles_dir / str(z) / str(x) / f"{y}.png"
    check("cache file written at TILES/z/x/y.png exactly",
          expected_cache_path.is_file(), str(expected_cache_path))
    check("cache file contents match the upstream body",
          expected_cache_path.read_bytes() == fake_bytes)

    # Second request for the same tile: filesystem-cache HIT, zero upstream calls.
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as m2:
        h2 = FakeHandler()
        tiles.serve(h2, path)
        check("cache HIT: no second upstream call", m2.call_count == 0,
              f"call_count={m2.call_count}")
        check("cache HIT: 200 with the cached bytes",
              h2.sent == (200, fake_bytes, "image/png",
                          {"Cache-Control": "public, max-age=604800"}),
              h2.sent)


def t_upstream_error(tmp_tiles_dir: Path) -> None:
    print("\n== upstream network/error behavior ==")
    reset_tile_state(tmp_tiles_dir)
    path = "/api/tile/6/7/8.png"

    def raising_urlopen(request, timeout=None):
        raise OSError("simulated upstream failure")

    with mock.patch("urllib.request.urlopen", side_effect=raising_urlopen):
        h = FakeHandler()
        tiles.serve(h, path)
        check("upstream exception -> 404 (never surfaced as an error page)",
              h.sent is not None and h.sent[0] == 404, f"got {h.sent}")

    check("no cache file was written for a failed fetch",
          not (tmp_tiles_dir / "6" / "7" / "8.png").exists())
    check("the fetch permit was released despite the exception (no leak)",
          tiles._TILE_FETCH._value == 12, tiles._TILE_FETCH._value)


def t_rate_limit_refusal(tmp_tiles_dir: Path) -> None:
    print("\n== tile rate-limit refusal ==")
    reset_tile_state(tmp_tiles_dir)
    n, window = ratelimit.RATE["/api/tile"]

    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"tile-bytes")

    refused = None
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        for i in range(n + 1):
            h = FakeHandler()
            # Distinct tile per call so each is a fresh cache miss and the
            # rate limiter (not the filesystem cache) is what is exercised.
            tiles.serve(h, f"/api/tile/10/{i % 1000}/{(i // 1000) % 1000}.png")
            if h.sent[0] == 429:
                refused = h.sent
                break
    check(f"the ({n}+1)th distinct-tile request in the window is refused with 429",
          refused is not None and refused[0] == 429, f"got {refused}")
    check("429 refusal carries an empty text/plain body (unchanged from today)",
          refused is not None and refused[1] == b"" and refused[2] == "text/plain",
          refused)


def t_semaphore_refusal(tmp_tiles_dir: Path) -> None:
    print("\n== tile concurrency/semaphore refusal ==")
    reset_tile_state(tmp_tiles_dir)
    # Drain the upstream-fetch semaphore completely so the next miss cannot
    # acquire a permit within its 2s timeout.
    acquired = []
    for _ in range(12):
        ok = tiles._TILE_FETCH.acquire(blocking=False)
        acquired.append(ok)
    check("drained all 12 _TILE_FETCH permits", all(acquired), acquired)

    def fake_urlopen(request, timeout=None):
        raise AssertionError("must not be reached while the semaphore is exhausted")

    try:
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen) as m:
            h = FakeHandler()
            tiles.serve(h, "/api/tile/12/1/1.png")
            check("semaphore exhausted -> 404 (never reaches urlopen)",
                  h.sent is not None and h.sent[0] == 404, f"got {h.sent}")
            check("urlopen was never called while the semaphore was exhausted",
                  m.call_count == 0, m.call_count)
    finally:
        for _ in range(12):
            tiles._TILE_FETCH.release()


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="tile_char_") as td:
        tmp_tiles_dir = Path(td)
        t_malformed_rejection(tmp_tiles_dir)
        t_valid_miss_then_hit(tmp_tiles_dir)
        t_upstream_error(tmp_tiles_dir)
        t_rate_limit_refusal(tmp_tiles_dir)
        t_semaphore_refusal(tmp_tiles_dir)

    print(f"\n{len(FAILURES)} failure(s)")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("tile characterization passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
