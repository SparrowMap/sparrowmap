"""A ThreadingHTTPServer that answers on IPv6 *and* IPv4.

⚠️ THIS FIXES A 2-4 SECOND DELAY ON EVERY REQUEST. It is not a nicety.

Binding to 0.0.0.0 listens on IPv4 only. Modern clients resolve a name to its
AAAA record first, so `localhost` is tried as ::1 and `example-host` as its
IPv6 address BEFORE either falls back to IPv4. Nothing is listening there, soevery request pays a connection-failure timeout first.

Measured against an IPv4-only bind on this machine:

    http://127.0.0.1:8160/api/presets          1 ms
    http://localhost:8160/api/presets       2062 ms
    http://example-host:8160/api/presets 4100 ms

The endpoint was identical in all three cases and never touched the camera, so
the entire difference is the failed IPv6 attempt. It is invisible in testing
because everyone tests on 127.0.0.1, and it lands squarely on the one case that
matters - a phone opening the page by hostname.

Binding to :: with IPV6_V6ONLY cleared accepts both families on one socket, so
IPv4 clients keep working unchanged and IPv6 clients stop waiting.
"""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer


# 🚨 ADMISSION CONTROL. A thread-per-connection server with no ceiling does not
# degrade under overload, it collapses - and the collapse feeds itself.
#
# Measured on the live box: with zero visitors on 443, Caddy held 1098
# ESTABLISHED upstream connections, 195 in CLOSE-WAIT, 85 in SYN-SENT, and was
# opening ~12 more per second. 588 Python threads on a 2-core box that also runs
# a video encoder. Every one of those threads makes every other one slower, so
# responses get later, so the proxy dials again. Nothing recovers from that on
# its own; the watchdog restarted it three times in ten minutes and each time it
# climbed straight back.
#
# 📌 The descriptor limit had been hiding this. At 1024 files the process died
# at roughly 340 connections, which LOOKED like a leak bug and was ALSO an
# accidental load shedder. Raising the limit to 65536 was correct and it removed
# the brake, so the real ceiling - CPU - is now the one that binds.
#
# Refusing quickly is the kind thing to do. A 503 in 5ms lets the proxy retry
# something that will work; a 40-second wait behind 500 thrashing threads
# helps nobody and costs everybody.
#
# 🚨 THIS COUNTS CONNECTIONS, NOT REQUESTS, AND 48 WAS WRONG BECAUSE OF IT.
#
# The permit is held for the whole life of the connection, because that is the
# unit socketserver hands to a thread. With HTTP/1.1 keep-alive a connection
# outlives its request by design - Caddy keeps a pool of them open and idle -
# so 48 permits does NOT mean "48 requests at once", it means "48 open sockets,
# busy or not". Measured steady state here is 32-64 open connections doing
# essentially nothing, which means the cap sat BELOW normal traffic and started
# refusing real visitors while the box was idle. deploy.py caught it: / and
# /api/stats both 503 while the server was at 34 threads and load 5.
#
# 📌 A limiter tuned to the wrong unit does not fail loudly, it fails
# intermittently - which is worse, because it looks like flakiness.
#
# So the number is chosen against what this actually bounds: THREADS. Normal is
# 32-64, the congestion collapse reached 1098, and 2 vCPUs cannot usefully
# thrash 250 Python threads either. 250 sits far above real traffic and far
# below the runaway, and IDLE_TIMEOUT_S is what keeps the steady state near the
# bottom of that range rather than drifting up to the ceiling.
#
# 🚨 RAISED TO A BACKSTOP, BECAUSE GATING CONNECTIONS WAS THE WRONG IDEA TWICE.
# At 48 it refused real visitors while idle. At 250 it refused them again the
# moment Caddy's pool grew to 250 - measured live: threads pegged at 254 and
# /api/health returning "busy - too many connections" while the box was fine.
# Raising the number does not fix a limiter pointed at the wrong noun; it only
# moves the point where it starts lying.
#
# A connection is not work. An idle keep-alive socket costs a thread and no CPU,
# and it is a proxy's job to hold a pool of them. What must be bounded is
# REQUESTS IN FLIGHT, and that gate lives in the handler (hub.Handler), after
# the request line is read - so an idle connection waiting for its next request
# holds nothing.
#
# What stays here is a genuine backstop against unbounded THREADS, set far above
# anything real traffic produces. The measured collapse was 1098 connections;
# normal is 32-64. IDLE_TIMEOUT_S is what actually keeps the steady state low.
MAX_INFLIGHT = 1200

# How long a connection may sit idle before the server hangs up. Keep-alive is
# worth having - it is why the per-thread database connection is cached - but an
# idle connection still pins a thread, and Caddy's pool will happily hold
# hundreds open. Without this the thread count only ever goes up.
IDLE_TIMEOUT_S = 20

_INFLIGHT = None      # created lazily so importing this module starts nothing


class _ClosesDbPerThread:
    """Close this thread's sqlite connection when the thread finishes.

    🚨 WITHOUT THIS THE PUBLIC SITE DIES ROUGHLY EVERY 45 MINUTES.

    ThreadingHTTPServer runs one thread per CONNECTION, and db.connect() caches
    one sqlite connection per thread because sqlite objects are not
    thread-portable. Both halves are correct on their own; together they leak
    two file descriptors (the db and its -wal) per visitor, because nothing
    closed the connection when the thread ended. At 1024 open files every route
    starts failing with "unable to open database file" and never recovers.

    The close belongs HERE rather than in the handler because a keep-alive
    thread serves many requests on one connection: closing per REQUEST would
    throw away the cache this design exists to provide, and closing per THREAD
    is exactly the lifetime db.connect() ties itself to.

    Mixed into both server classes deliberately. The box binds 127.0.0.1 behind
    Caddy and therefore uses the IPv4 `_Queued` class below, never
    DualStackServer - fixing only the dual-stack one would have fixed
    everything except the server that was actually falling over. That exact
    mistake has already been made once here (see request_queue_size).
    """

    def process_request_thread(self, request, client_address):
        global _INFLIGHT
        if _INFLIGHT is None:
            _INFLIGHT = threading.Semaphore(MAX_INFLIGHT)

        # An idle keep-alive connection still pins a thread. Without a timeout
        # the only thing that ends one is the client, and a proxy's pool has no
        # reason to hurry. This is what actually drains the thread count.
        try:
            request.settimeout(IDLE_TIMEOUT_S)
        except Exception:
            pass

        # Non-blocking: if the server is already at its ceiling, say so NOW.
        # Blocking here would just move the queue from the proxy into this
        # process, where each waiter costs a thread - which is the problem.
        if not _INFLIGHT.acquire(blocking=False):
            try:
                # Built rather than written out, so Content-Length cannot drift
                # away from the body when someone edits the message. A wrong
                # Content-Length on an overload response makes the proxy hang
                # waiting for bytes that are not coming - turning load shedding
                # into another way to be slow.
                body = b'{"error": "busy - too many connections"}'
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Retry-After: 1\r\n"
                    b"Connection: close\r\n\r\n" + body)
            except Exception:
                pass
            finally:
                self.shutdown_request(request)
            return

        try:
            super().process_request_thread(request, client_address)
        finally:
            _INFLIGHT.release()
            try:
                import db
                db.close_thread()
            except Exception:
                pass      # a hub that cannot import db has bigger problems


class DualStackServer(_ClosesDbPerThread, ThreadingHTTPServer):
    """Listens on :: for both IPv6 and IPv4 (falls back to IPv4 if unavailable)."""

    address_family = socket.AF_INET6
    daemon_threads = True

    # 🚨 THE DEFAULT BACKLOG IS 5, AND IT SILENTLY DROPS CAMERAS.
    #
    # socketserver ships request_queue_size = 5, so once five connections are
    # waiting to be accepted the kernel refuses the sixth. Behind Caddy that
    # surfaces at the far end as "EOF occurred in violation of protocol" - a
    # TLS error, on a machine whose TLS is fine.
    #
    # Measured on the live box while volunteers were testing: 14 of the last 40
    # posts from a single camera failed that way, and ingest went 267 -> 241 ->
    # 50 -> 0 sightings per ten minutes while every camera stayed ONLINE,
    # because heartbeats are small and lucky and a sighting carries a JPEG.
    # The hub was listening with a queue of 5 next to caddy's 4096 and uvicorn's
    # 2048.
    #
    # A burst is exactly the normal shape of this traffic: cameras post when a
    # vehicle LEAVES frame, so a busy road delivers clumps, and a wave of new
    # volunteers testing at once delivers a much bigger one. Nothing here is
    # slow - the connections were never accepted at all.
    request_queue_size = 256

    # ⚠️ MUST BE FALSE ON WINDOWS.
    #
    # SO_REUSEADDR does NOT mean the same thing on Windows as it does on Unix.
    # On Unix it lets you rebind a port still in TIME_WAIT. On Windows it lets a
    # SECOND LIVE PROCESS bind a port another process is already listening on,
    # and the two then split incoming connections unpredictably.
    #
    # That is not theoretical: it let three copies of the camera app run at
    # once, each fighting the others for exclusive access to the webcam, which
    # presented as "the camera only manages 5 fps" and "the camera will not
    # open". With this False, a second instance fails immediately and loudly,
    # which is the correct behaviour for anything owning a single device.
    allow_reuse_address = False

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            # Some stacks refuse; the IPv4-only path below still works.
            pass
        return super().server_bind()


def serve(handler, port: int, host: str = "::"):
    """Build a dual-stack server, degrading to IPv4 if IPv6 is unavailable.

    Raises SystemExit with a clear message if the port is already taken, so a
    second copy of a single-device app cannot start silently.

    🚨 AN EXPLICIT HOST IS HONOURED EXACTLY, NEVER WIDENED.
    This used to fall back to "0.0.0.0" whenever the IPv6 bind failed - and an
    IPv4 address like 127.0.0.1 ALWAYS fails on an AF_INET6 socket. So asking
    for loopback-only silently produced a listener on every interface: the
    deployment would have exposed 8150 straight to the internet, past nginx and
    therefore past every security header, the rate limiter and the auth cookie.
    A bind that quietly listens more widely than asked is the worst direction
    for that mistake to go.
    """
    # An explicit IPv4 address gets an IPv4 socket. No dual-stack, no fallback,
    # no widening.
    if host and ":" not in host and host not in ("", "0.0.0.0"):
        # ⚠️ THIS BRANCH IS THE DEPLOYED ONE. The box runs SPARROW_BIND=127.0.0.1
        # behind Caddy, so it never touches DualStackServer - raising the
        # backlog only on that class would have fixed everything except the
        # server that was actually dropping cameras.
        class _Queued(_ClosesDbPerThread, ThreadingHTTPServer):
            request_queue_size = DualStackServer.request_queue_size
        try:
            srv = _Queued((host, port), handler)
        except OSError as exc:
            raise SystemExit(f"cannot bind {host}:{port}: {exc}")
        srv.daemon_threads = True
        return srv
    try:
        return DualStackServer((host, port), handler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or exc.errno in (48, 98):
            raise SystemExit(
                f"port {port} is already in use - another copy is running.\n"
                f"  Stop it first:  Get-NetTCPConnection -LocalPort {port} "
                f"-State Listen | %{{ Stop-Process -Id $_.OwningProcess }}")
        try:
            # Only reached for a wildcard request, so widening to 0.0.0.0 is
            # what was asked for rather than a surprise. Still needs the
            # per-thread close: a fallback path that leaks descriptors is a
            # fallback path that dies after 45 minutes.
            class _V4(_ClosesDbPerThread, ThreadingHTTPServer):
                request_queue_size = DualStackServer.request_queue_size

            srv = _V4(("0.0.0.0", port), handler)
        except OSError:
            raise SystemExit(f"cannot bind port {port}: {exc}")
        srv.daemon_threads = True
        srv.allow_reuse_address = False
        print(f"[http] IPv6 unavailable, IPv4 only on {port} "
              f"(expect a delay for clients that resolve AAAA first)")
        return srv
