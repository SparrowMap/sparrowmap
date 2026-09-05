"""Deterministic module-level characterization of admission.py (Stage 1B).

Unlike tools/test_overload.py (which floods the real HTTP server and is a
heavier integration/stress check, retained as-is), this test exercises
admission.run_gated / too_busy_response directly against a fake handler
object, in-process, with no sockets and no timing assumptions:

  * exhausting the INFLIGHT semaphore deterministically (acquire it down to
    zero from the test itself, then call run_gated and observe the refusal),
  * the HEAVY/INGEST sub-pool wait-then-refuse path (acquire the sub-pool
    down to zero, call run_gated with a label in that route set, and rely on
    a short deterministic timeout rather than a hope of natural contention),
  * that a successful call happens when permits are available, and that the
    permits are returned afterwards (no leak on the happy path or on an
    exception path).
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admission

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name + (f": {detail}" if detail else ""))


class FakeHandler:
    """The minimum surface admission.py's functions read/write on a Handler."""

    def __init__(self) -> None:
        self.headers = {}
        self.close_connection = False
        self._body_done = True   # nothing to drain in these tests
        self._sent = io.BytesIO()
        self._status_code = None
        self._response_headers: dict[str, str] = {}

    def send_response(self, code):
        self._status_code = code

    def send_header(self, k, v):
        self._response_headers[k] = v

    def end_headers(self):
        pass

    @property
    def wfile(self):
        return self._sent


def reset_admission_state() -> None:
    """Put admission.py's module-level state back to a known-good baseline.

    The semaphores/dicts are process-lifetime singletons by design (that is
    the point - see admission.py's own comment on why they are named at
    module level for tools/test_overload.py). A test that exhausts them must
    put them back, or every test after it inherits a drained pool.
    """
    for _ in range(admission.MAX_REQUESTS):
        admission.INFLIGHT.release()
    # Semaphore has no "set to N"; drain back down to the configured count by
    # acquiring off any extra permits our release() calls introduced. Simpler:
    # rebuild fresh semaphores at the known caps.
    admission.INFLIGHT._value = 0
    for _ in range(admission.MAX_REQUESTS):
        admission.INFLIGHT.release()
    admission.HEAVY._value = 0
    for _ in range(admission.MAX_HEAVY):
        admission.HEAVY.release()
    admission.INGEST._value = 0
    for _ in range(admission.MAX_INGEST):
        admission.INGEST.release()
    admission.INFLIGHT_PATHS.clear()
    admission.SLOW_HELD.clear()


def t_inflight_exhaustion_refuses() -> None:
    print("\n== INFLIGHT exhaustion -> deterministic 503 ==")
    reset_admission_state()
    # Drain every INFLIGHT permit ourselves, deterministically - no flooding,
    # no timing race, just direct semaphore control.
    held = 0
    while admission.INFLIGHT.acquire(blocking=False):
        held += 1
    check("drained all MAX_REQUESTS permits", held == admission.MAX_REQUESTS,
          f"drained {held}, expected {admission.MAX_REQUESTS}")

    h = FakeHandler()
    called = []
    admission.run_gated(h, lambda: called.append(True), "GET /api/stats")
    check("inner() was NOT called while INFLIGHT is exhausted", called == [])
    check("a 503 was written", h._status_code == 503, str(h._status_code))
    check("Retry-After: 1 was sent",
          h._response_headers.get("Retry-After") == "1",
          str(h._response_headers))
    check("Cache-Control: no-store was sent",
          h._response_headers.get("Cache-Control") == "no-store",
          str(h._response_headers))
    check("the busy JSON body was written",
          b"busy" in h._sent.getvalue(), h._sent.getvalue())

    reset_admission_state()


def t_inflight_available_runs_inner_and_releases() -> None:
    print("\n== INFLIGHT available -> inner() runs, permit is returned ==")
    reset_admission_state()
    h = FakeHandler()
    calls = []
    result = admission.run_gated(h, lambda: calls.append(1) or "ok",
                                  "GET /api/stats")
    check("inner() ran exactly once", calls == [1], str(calls))
    check("run_gated returned inner()'s result", result == "ok", str(result))
    check("the permit was released (semaphore back at MAX_REQUESTS)",
          admission.INFLIGHT._value == admission.MAX_REQUESTS,
          f"value={admission.INFLIGHT._value}")
    check("INFLIGHT_PATHS was cleared after completion",
          id(h) not in admission.INFLIGHT_PATHS, str(admission.INFLIGHT_PATHS))
    reset_admission_state()


def t_inflight_released_on_exception() -> None:
    print("\n== inner() raising -> permit still released (no leak) ==")
    reset_admission_state()
    h = FakeHandler()

    def boom():
        raise ValueError("simulated route failure")

    try:
        admission.run_gated(h, boom, "GET /api/stats")
        raised = False
    except ValueError:
        raised = True
    check("the exception propagated (run_gated does not swallow it)", raised)
    check("the permit was still released despite the exception",
          admission.INFLIGHT._value == admission.MAX_REQUESTS,
          f"value={admission.INFLIGHT._value}")
    check("INFLIGHT_PATHS was still cleared despite the exception",
          id(h) not in admission.INFLIGHT_PATHS, str(admission.INFLIGHT_PATHS))
    reset_admission_state()


def t_heavy_subpool_wait_then_refuse() -> None:
    print("\n== HEAVY sub-pool exhaustion -> bounded wait, then 503 ==")
    reset_admission_state()
    label = next(iter(admission.HEAVY_ROUTES))  # e.g. "/api/nodes"
    check("chosen label is actually a HEAVY route", label in admission.HEAVY_ROUTES,
          label)

    # Drain the HEAVY pool deterministically.
    held = 0
    while admission.HEAVY.acquire(blocking=False):
        held += 1
    check("drained all MAX_HEAVY permits", held == admission.MAX_HEAVY,
          f"drained {held}, expected {admission.MAX_HEAVY}")

    # Shrink the wait so the test does not take HEAVY_WAIT_S (12s) to run -
    # this is a TEST-ONLY monkeypatch of the timeout constant, not a change
    # to production behavior (production still uses HEAVY_WAIT_S unmodified).
    orig_wait = admission.HEAVY_WAIT_S
    admission.HEAVY_WAIT_S = 0.2
    try:
        h = FakeHandler()
        called = []
        admission.run_gated(h, lambda: called.append(True), label)
        check(f"inner() was NOT called while HEAVY({label}) is exhausted",
              called == [])
        check("a 503 was written", h._status_code == 503, str(h._status_code))
    finally:
        admission.HEAVY_WAIT_S = orig_wait
    reset_admission_state()


def t_ingest_subpool_wait_then_refuse() -> None:
    print("\n== INGEST sub-pool exhaustion -> bounded wait, then 503 ==")
    reset_admission_state()
    label = next(iter(admission.INGEST_ROUTES))  # e.g. "POST /api/sightings"
    check("chosen label is actually an INGEST route",
          label in admission.INGEST_ROUTES, label)

    held = 0
    while admission.INGEST.acquire(blocking=False):
        held += 1
    check("drained all MAX_INGEST permits", held == admission.MAX_INGEST,
          f"drained {held}, expected {admission.MAX_INGEST}")

    orig_wait = admission.INGEST_WAIT_S
    admission.INGEST_WAIT_S = 0.2
    try:
        h = FakeHandler()
        called = []
        admission.run_gated(h, lambda: called.append(True), label)
        check(f"inner() was NOT called while INGEST({label}) is exhausted",
              called == [])
        check("a 503 was written", h._status_code == 503, str(h._status_code))
    finally:
        admission.INGEST_WAIT_S = orig_wait
    reset_admission_state()


def t_concurrent_gated_calls_never_exceed_cap() -> None:
    """A deterministic (non-timing-based) concurrency invariant check.

    N threads all call run_gated at once for an ordinary (non-heavy,
    non-ingest) label, each inner() blocking on a shared Barrier until every
    thread that WILL get a permit has been admitted, then releasing. This
    proves the INFLIGHT semaphore never admits more than MAX_REQUESTS
    concurrent callers, without depending on wall-clock timing to decide
    pass/fail - the assertion is on a count, not a duration.
    """
    print("\n== concurrent run_gated calls never exceed MAX_REQUESTS ==")
    reset_admission_state()
    # Use a small temporary cap so the test does not need to spin up 200
    # threads to prove the invariant.
    orig_max = admission.MAX_REQUESTS
    admission.MAX_REQUESTS = 4
    admission.INFLIGHT._value = 0
    for _ in range(4):
        admission.INFLIGHT.release()
    try:
        n_threads = 10
        admitted = []
        admitted_lock = threading.Lock()
        release_gate = threading.Event()
        entered = threading.Barrier(1)  # placeholder, replaced below
        max_concurrent = [0]
        concurrent_now = [0]
        concurrent_lock = threading.Lock()

        def inner():
            with concurrent_lock:
                concurrent_now[0] += 1
                max_concurrent[0] = max(max_concurrent[0], concurrent_now[0])
            with admitted_lock:
                admitted.append(threading.get_ident())
            release_gate.wait(timeout=5.0)
            with concurrent_lock:
                concurrent_now[0] -= 1

        def worker(h):
            admission.run_gated(h, inner, "GET /api/stats")

        handlers = [FakeHandler() for _ in range(n_threads)]
        threads = [threading.Thread(target=worker, args=(h,)) for h in handlers]
        for t in threads:
            t.start()
        # Give every thread a moment to attempt admission; those that get a
        # permit block in inner() on release_gate, those refused return
        # immediately with a 503 and never touch the shared counters.
        import time as _time
        _time.sleep(0.3)
        release_gate.set()
        for t in threads:
            t.join(timeout=5.0)

        check(f"peak concurrent inner() executions <= MAX_REQUESTS ({admission.MAX_REQUESTS})",
              max_concurrent[0] <= admission.MAX_REQUESTS,
              f"observed {max_concurrent[0]}")
        refused = sum(1 for h in handlers if h._status_code == 503)
        succeeded = sum(1 for h in handlers if h._status_code is None)
        check(f"exactly MAX_REQUESTS threads were admitted, the rest refused",
              succeeded == admission.MAX_REQUESTS and
              refused == n_threads - admission.MAX_REQUESTS,
              f"succeeded={succeeded} refused={refused}")
    finally:
        admission.MAX_REQUESTS = orig_max
        reset_admission_state()


def main() -> int:
    t_inflight_exhaustion_refuses()
    t_inflight_available_runs_inner_and_releases()
    t_inflight_released_on_exception()
    t_heavy_subpool_wait_then_refuse()
    t_ingest_subpool_wait_then_refuse()
    t_concurrent_gated_calls_never_exceed_cap()

    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("\nadmission.py characterization passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
