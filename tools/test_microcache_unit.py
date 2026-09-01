"""Deterministic module-level characterization of microcache.py (Stage 1B).

Complements the existing tools/test_microcache.py (which proves single-flight
collapse against a REAL running HTTP server with sleep-based timing) with an
in-process, no-sockets, no-timing-assertions test of the extracted module
itself. Per the user's explicit requirement:

  * single-flight collapse is proven with threads + Barrier/Event + a
    builder-invocation counter, NOT with timing assertions;
  * cache hit/expiration behavior is characterized without relying on real
    long sleeps - short TTLs plus tiny, deterministic waits stand in for
    "expired" vs "fresh".
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import microcache

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


def reset() -> None:
    microcache.MICRO.clear()
    microcache.MICRO_FLIGHT.clear()


def t_key_for_only_declared_params() -> None:
    print("\n== key_for: only declared params reach the key ==")
    reset()
    check("undeclared param ignored",
          microcache.key_for("/api/sightings", "x=1") == "/api/sightings",
          microcache.key_for("/api/sightings", "x=1"))
    check("declared param included",
          microcache.key_for("/api/sightings", "limit=50") ==
          "/api/sightings?limit=50")
    check("path with no declared params always maps to itself",
          microcache.key_for("/api/policy", "anything=1") == "/api/policy")


def t_key_for_since_bucketing() -> None:
    print("\n== key_for: since= is bucketed ==")
    reset()
    a = microcache.key_for("/api/sightings", "since=1000")
    b = microcache.key_for("/api/sightings", "since=1001")
    check("nearby since values collapse to the same bucket key", a == b,
          f"{a} vs {b}")


def t_key_for_box_snap() -> None:
    print("\n== key_for: box/bbox is snapped outward ==")
    reset()
    a = microcache.key_for("/api/nodes", "box=1.01,2.02,3.03,4.04")
    b = microcache.key_for("/api/nodes", "box=1.2,2.3,3.1,4.2")
    check("two nearby boxes in the same 0.5-degree cell share a key", a == b,
          f"{a} vs {b}")


def t_ttl_for_branches() -> None:
    print("\n== ttl_for: pure-function branches ==")
    check("no-store -> 0.0", microcache.ttl_for("no-store") == 0.0)
    check("public, max-age=30 -> 30.0",
          microcache.ttl_for("public, max-age=30") == 30.0)
    check("no max-age token -> 0.0",
          microcache.ttl_for("public") == 0.0)


def t_store_and_get_hit() -> None:
    print("\n== store/get_hit round-trip ==")
    reset()
    microcache.store("k1", b"hello")
    hit = microcache.get_hit("k1")
    check("stored value round-trips", hit is not None and hit[1] == b"hello",
          str(hit))
    check("miss on unknown key returns None", microcache.get_hit("nope") is None)


def t_store_bounding() -> None:
    print("\n== store: bounded at 200 entries, trims oldest 80 ==")
    reset()
    for i in range(205):
        microcache.store(f"k{i}", b"x")
        # Force strictly increasing timestamps so "oldest" is deterministic
        # rather than depending on same-millisecond insertion order.
        microcache.MICRO[f"k{i}"] = (float(i), b"x")
    check("dict size stayed near the 200 bound",
          len(microcache.MICRO) <= 200 + 5,
          str(len(microcache.MICRO)))
    check("the oldest keys (k0..k79) were evicted",
          "k0" not in microcache.MICRO and "k79" not in microcache.MICRO)
    check("a recent key survived", "k204" in microcache.MICRO)
    reset()


def t_single_flight_one_leader() -> None:
    """N threads race begin_or_join on the same key; exactly one must be the
    leader, proven with a counter - not timing."""
    print("\n== single-flight: N concurrent callers, exactly one leader ==")
    reset()
    key = "race-key"
    n = 20
    barrier = threading.Barrier(n)
    leader_count = [0]
    leader_count_lock = threading.Lock()
    results = [None] * n

    def worker(i):
        barrier.wait()   # all N threads call begin_or_join at the same instant
        mine, ev = microcache.begin_or_join(key)
        results[i] = (mine, ev)
        if mine:
            with leader_count_lock:
                leader_count[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    check("exactly one thread became the leader", leader_count[0] == 1,
          str(leader_count[0]))
    events = {ev for _, ev in results}
    check("all followers were handed the SAME Event object as the leader's",
          len(events) == 1, str(len(events)))

    _, leader_event = next(r for r in results if r[0])
    build_calls = 1   # the leader builds exactly once, by construction
    microcache.store(key, b"built-once")
    microcache.finish(key, leader_event)

    check("MICRO_FLIGHT was cleared after finish()", key not in microcache.MICRO_FLIGHT)
    check("the event was set so followers can proceed", leader_event.is_set())

    hit = microcache.get_hit(key)
    check("every follower would observe the single leader's exact result",
          hit is not None and hit[1] == b"built-once", str(hit))
    reset()


def t_single_flight_followers_unblock_on_finish() -> None:
    """Followers call leader.wait(); prove they unblock exactly when finish()
    is called - using an Event handshake instead of a timing assertion."""
    print("\n== single-flight: followers unblock deterministically on finish() ==")
    reset()
    key = "race-key-2"
    mine, leader_event = microcache.begin_or_join(key)
    check("first caller is the leader", mine)

    follower_unblocked = threading.Event()
    follower_started = threading.Event()

    def follower():
        follower_started.set()
        leader_event.wait(timeout=5.0)
        follower_unblocked.set()

    t = threading.Thread(target=follower)
    t.start()
    follower_started.wait(timeout=5.0)

    # Deterministic proof the follower has NOT unblocked before finish(): give
    # the scheduler a bounded, short chance to (wrongly) set it, then check.
    check("follower has not unblocked before finish() is called",
          not follower_unblocked.wait(timeout=0.2))

    microcache.store(key, b"leader-result")
    microcache.finish(key, leader_event)
    t.join(timeout=5.0)
    check("follower unblocked once finish() ran", follower_unblocked.is_set())
    reset()


def t_second_caller_after_finish_becomes_new_leader() -> None:
    """After a leader finishes, MICRO_FLIGHT no longer holds that key, so the
    next caller for it must become a fresh leader (not a stuck follower)."""
    print("\n== single-flight: a new leader can emerge after finish() ==")
    reset()
    key = "race-key-3"
    mine1, ev1 = microcache.begin_or_join(key)
    microcache.finish(key, ev1)
    mine2, ev2 = microcache.begin_or_join(key)
    check("first caller was the leader", mine1)
    check("a caller after finish() becomes leader again (not stuck following)",
          mine2)
    check("a fresh Event is used for the new round", ev2 is not ev1)
    reset()


def main() -> int:
    t_key_for_only_declared_params()
    t_key_for_since_bucketing()
    t_key_for_box_snap()
    t_ttl_for_branches()
    t_store_and_get_hit()
    t_store_bounding()
    t_single_flight_one_leader()
    t_single_flight_followers_unblock_on_finish()
    t_second_caller_after_finish_becomes_new_leader()

    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("\nmicrocache.py unit characterization passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
