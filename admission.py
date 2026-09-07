"""Admission/semaphore accounting extracted from hub.py (Stage 1B, step 2).

This module owns the three concurrency pools (whole-request, "heavy"
map-building routes, and camera ingest), their route-set constants, the
who-is-holding-a-permit bookkeeping used by /api/health, and the gate
(`run_gated`) that wraps a route handler in them. None of this has any
domain knowledge - `run_gated` takes an arbitrary zero-argument callable and
a label, exactly as `Handler._gated` did.

`too_busy_response` writes the fixed 503 body directly to a socket-like
handler object, calling back into `transport.drain_body` first - the same
one-directional dependency `transport.py`'s `send_error` already has on the
handler's own `_send`/wire-write machinery, just expressed the other way:
here it is `admission.py` depending on `transport.py`, never the reverse.
"""

from __future__ import annotations

import threading
import time

import transport

# How many requests may be BEING SERVED at once. See the note that used to
# live beside Handler._INFLIGHT for why this counts requests rather than
# connections - the two earlier attempts counted connections and locked real
# visitors out of an idle box twice.
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
# paths hold them (see INFLIGHT_PATHS). Tune it from that, never from argument.
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

# Named at module level, exactly as they were named on Handler, so
# tools/test_overload.py and tools/test_slowloris.py can size a flood off the
# real semaphores instead of a copy that silently drifts out of step.
INFLIGHT = threading.Semaphore(MAX_REQUESTS)
HEAVY = threading.Semaphore(MAX_HEAVY)
INGEST = threading.Semaphore(MAX_INGEST)

# Who is holding a permit right now, and the worst hold time seen per path.
# Both are published by /api/health so the cap can be tuned from evidence.
INFLIGHT_PATHS: dict = {}
INFLIGHT_LOCK = threading.Lock()
SLOW_HELD: dict = {}
SLOW_S = 2.0


def too_busy_response(handler) -> None:
    """Write the fixed 503 "too many requests in flight" response.

    Writes straight to the socket rather than through Handler._send, exactly
    as before - see hub.Handler._send's own comment about this being the one
    path that can leak a micro-cache key across a keep-alive connection,
    because it never enters _send to clear it (Handler._send pops the key
    unconditionally on every call precisely because of this path).
    """
    transport.drain_body(handler)
    body = b'{"error": "busy - too many requests in flight"}'
    try:
        handler.send_response(503)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Retry-After", "1")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        pass


def run_gated(handler, inner, label: str):
    """Run `inner` holding one admission permit, and RECORD that it holds it.

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
    if label in HEAVY_ROUTES:
        extra, wait = HEAVY, HEAVY_WAIT_S
    elif label in INGEST_ROUTES:
        extra, wait = INGEST, INGEST_WAIT_S
    if extra is not None and not extra.acquire(timeout=wait):
        return too_busy_response(handler)
    try:
        if not INFLIGHT.acquire(blocking=False):
            return too_busy_response(handler)
        started = time.time()
        with INFLIGHT_LOCK:
            INFLIGHT_PATHS[id(handler)] = (label, started)
        try:
            return inner()
        finally:
            INFLIGHT.release()
            with INFLIGHT_LOCK:
                INFLIGHT_PATHS.pop(id(handler), None)
                held = time.time() - started
                if held > SLOW_S:
                    # A permit held this long is the shape of every outage
                    # so far: something waiting on a third party, not
                    # working.
                    #
                    # ⚠️ BOUNDED. The label is a route, but a route can be
                    # unbounded - /api/tile/{z}/{x}/{y} alone is millions of
                    # distinct strings - so an unbounded dict here is a slow
                    # memory leak fed by exactly the traffic that causes an
                    # outage. Keep the worst offenders; the tail is noise.
                    SLOW_HELD[label] = round(max(held, SLOW_HELD.get(label, 0)), 1)
                    if len(SLOW_HELD) > 40:
                        for k, _ in sorted(SLOW_HELD.items(),
                                           key=lambda kv: kv[1])[:20]:
                            SLOW_HELD.pop(k, None)
    finally:
        if extra is not None:
            extra.release()
