"""Pre-extraction characterization of Handler._cache_control (Stage 1B step 4).

Per the user's explicit instruction: before moving _cache_control into
response_policy.py, characterize ALL currently-reachable branches of the
pure function and record their exact existing outputs, so the subsequent
extraction can be verified byte-for-byte against this table.

This test imports the CURRENT, unmodified hub.py and drives Handler._cache_control
directly via a minimal fake instance (no socket, no HTTP server) - it only
needs self.path and self._status, which is everything the function reads.

Run BEFORE response_policy.py exists; run again AFTER to prove the exact same
table still holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hub
import response_policy

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


class FakeHandler:
    _CACHEABLE_API = hub.Handler._CACHEABLE_API

    def __init__(self, path, status=200):
        self.path = path
        self._status = status

    _cache_control = hub.Handler._cache_control


# (path, status) -> expected exact Cache-Control string.
# This is the ground-truth table for the CURRENT, unmodified implementation.
CASES = [
    # ---- failure override: ALWAYS no-store regardless of path -----------
    (("/api/tile/3/4/5.png", 404), "no-store"),
    (("/static/app.js", 500), "no-store"),
    (("/api/stats", 400), "no-store"),
    (("/", 404), "no-store"),

    # ---- pinned long-cache content ---------------------------------------
    (("/vendor/leaflet.js", 200), "public, max-age=604800"),
    (("/vendor/detector/model.onnx", 200), "public, max-age=604800"),
    (("/api/tile/3/4/5.png", 200), "public, max-age=604800"),

    # ---- app's own static code --------------------------------------------
    (("/static/app.js", 200), "public, max-age=60"),
    (("/static/sitenav.js", 200), "public, max-age=60"),

    # ---- cacheable public API: per-path overrides --------------------------
    (("/api/stats", 200), "public, max-age=3"),
    (("/api/health", 200), "public, max-age=3"),
    (("/api/nodes", 200), "public, max-age=30"),
    (("/api/places", 200), "public, max-age=60"),
    (("/api/sightings", 200), "public, max-age=4"),
    # cacheable-API fallthrough: /api/policy, /api/leaderboard, /api/heat
    (("/api/policy", 200), "public, max-age=15"),
    (("/api/leaderboard", 200), "public, max-age=15"),
    (("/api/heat", 200), "public, max-age=15"),

    # ---- page shells --------------------------------------------------------
    (("/", 200), "public, max-age=60"),
    (("/about", 200), "public, max-age=60"),
    (("/transparency", 200), "public, max-age=60"),
    (("/status", 200), "public, max-age=60"),
    (("/IPCamera", 200), "public, max-age=60"),
    (("/app", 200), "public, max-age=60"),
    (("/node", 200), "public, max-age=60"),
    (("/key", 200), "public, max-age=60"),
    (("/checksums", 200), "public, max-age=60"),
    (("/support", 200), "public, max-age=60"),
    (("/donate", 200), "public, max-age=60"),
    (("/anything/whatsoever.html", 200), "public, max-age=60"),

    # ---- default no-store: search, per-user, operator, unknown -------------
    (("/api/plate?q=ABC123".split("?")[0], 200), "no-store"),
    (("/api/track", 200), "no-store"),
    (("/api/sighting/42", 200), "no-store"),
    (("/api/review/queue", 200), "no-store"),
    (("/api/operator/whoami", 200), "no-store"),
    (("/api/live", 200), "no-store"),
    (("/api/audit", 200), "no-store"),
    (("/does/not/exist", 200), "no-store"),
]


def main() -> int:
    print("== Handler._cache_control branch characterization (via hub.Handler) ==")
    for (path, status), expected in CASES:
        h = FakeHandler(path, status)
        actual = h._cache_control()
        check(f"{status} {path!r} -> {expected!r}", actual == expected,
              f"got {actual!r}")

    print("\n== response_policy.cache_control direct (post-extraction) ==")
    for (path, status), expected in CASES:
        actual = response_policy.cache_control(path, status)
        check(f"{status} {path!r} -> {expected!r}", actual == expected,
              f"got {actual!r}")

    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print(f"\n{len(CASES)} cache-control branch(es) characterized and passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
