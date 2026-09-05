"""Stateless HTTP transport primitives extracted from hub.py (Stage 1A).

🚨 STAGE 1A SCOPE, AND ONLY THIS SCOPE.

This module holds the parts of hub.Handler's request/response plumbing that
do not depend on rate limiting, admission/semaphore state, the micro-cache,
static/tile serving, CORS/CSRF policy, security-header policy, or
cache/privacy policy - all of which stay in hub.py for Stage 1B. What moved
here:

  * request-body reading, draining and the slow-loris-safe deadline loop
    (`read_body`, `drain_body`);
  * JSON response/error-body serialization (`send_json`, `send_error`) -
    these still call back into the handler's own `_send`, which remains in
    hub.py because it carries the cache/security-header policy that is out
    of scope for this stage;
  * the stable per-route label used to key counters (`route_label`);
  * the HEAD-via-GET plumbing (`handle_head`).

Every function here takes the live `Handler` instance (or the values it
would have read off one) rather than owning any state of its own, so moving
them changes where the code lives without changing what it does. hub.py's
Handler methods now delegate to these functions; nothing about the messages,
status codes, timing, or attribute names changed.
"""

from __future__ import annotations

import json
import socket
import time

# A sighting carries a base64 vehicle crop, which is the only large body this
# server has any reason to accept. 8 MB covers a generous JPEG with base64's
# 33% overhead; everything else is a few hundred bytes. Bigger than this is
# not a real submission, it is memory pressure on a 2-vCPU/3-GB box.
MAX_BODY = 8 * 1024 * 1024


def route_label(path: str) -> str:
    """A stable label for a path, so counters key on ROUTES not URLs.

    /api/sighting/45746 and /api/tile/12/1096/1521.png are one route each,
    not one label each. Keying on the raw path turns any per-id endpoint
    into an unbounded set of dictionary keys.
    """
    parts = path.split("/")
    out = []
    for seg in parts:
        if seg and (seg.isdigit() or seg.rstrip(".png").isdigit()):
            out.append("{n}")
        else:
            out.append(seg)
    return "/".join(out)[:48]


def drain_body(handler) -> None:
    """Read and discard a request body we are about to refuse.

    🚨 REFUSING WITHOUT READING DESYNCS A POOLED CONNECTION.
    Caddy keeps upstream connections alive and reuses them. If a 503 or 429
    is written while the request body is still unread, those bytes stay in
    the socket and are parsed as the START of the next request on that same
    connection - so the resulting 400 lands on some unrelated visitor's
    request, and log_message is suppressed here so nothing records it.

    The 429 paths matter more than the 503 one: a global 600/hour bucket is
    far easier to trip than 200 concurrent requests.
    """
    # ⚠️ IDEMPOTENT, OR IT HANGS THE REQUEST. Handlers that read the body
    # and THEN refuse are common (unknown node, bad token, bad json). A
    # second read of an exhausted stream blocks waiting for bytes that have
    # already been consumed - turning a tidy-up into a stall on the very
    # path that is shedding load.
    if getattr(handler, "_body_done", False):
        return
    handler._body_done = True
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        # Nothing to drain. A GET has no body, and closing the connection
        # here would make every refusal on a GET kill keep-alive - turning
        # a fix for a desync into a much larger performance bug. Caught by
        # tools/test_cache_key_leak.py, which reuses a connection after a
        # 503 and started aborting.
        return
    if n > MAX_BODY:
        # More than we are willing to read just to be polite. The bytes
        # cannot be left in the socket, so closing is the honest option.
        handler.close_connection = True
        return
    try:
        handler.rfile.read(n)
    except Exception:
        handler.close_connection = True


def read_body(handler) -> dict:
    """Read and JSON-decode the request body, bounded and deadline-guarded.

    See MAX_BODY and the wall-clock deadline note below - both carried over
    unchanged from hub.Handler._body.
    """
    # Marked before any read, so a refusal AFTER this point never tries to
    # drain an already-consumed stream. See drain_body.
    handler._body_done = True
    n = int(handler.headers.get("Content-Length") or 0)
    if not n:
        return {}
    # 🚨 CAP BEFORE READING. Trusting Content-Length and calling read(n) with
    # no ceiling lets one request ask the box to buffer gigabytes; a handful
    # of those, or a slow-loris trickle, exhausts a thread-per-connection
    # server. Never buffer more than MAX_BODY, and on an oversize claim close
    # the connection (unread bytes would otherwise corrupt the next
    # keep-alive request). The handler then sees {} and answers 400. No
    # response is sent from here, so the caller can never double-respond.
    if n > MAX_BODY:
        handler.close_connection = True
        return {}
    # 🚨 A WALL-CLOCK DEADLINE, NOT JUST A PER-RECV TIMEOUT.
    # The socket timeout (dualstack.IDLE_TIMEOUT_S) applies to each recv
    # individually, so a client sending one byte every 19 seconds resets it
    # forever and holds an admission permit for as long as it likes. A few
    # hundred of those at ~10 B/s and every POST, every no-store route and
    # all map data past its micro-TTL returns 503 - while the page shell and
    # tiles keep serving, which makes it harder to diagnose rather than
    # milder. This is a slow-loris on the one resource the whole site
    # shares.
    #
    # Read in chunks against a total budget instead. A legitimate body here
    # is a few hundred KB of JPEG from a camera on a domestic uplink, so ten
    # seconds is generous; anything slower is not a camera.
    # ⚠️ THE DEADLINE ONLY WORKS IF EACH READ RETURNS. Checking the clock
    # between reads is not enough: rfile.read() blocks until it has the
    # bytes, and every trickled byte resets the socket timeout, so the very
    # first read never comes back and the deadline is never consulted. The
    # socket timeout must be shortened for the duration of the body read so
    # control returns to this loop regularly. (Found by
    # tools/test_slowloris.py, which failed identically before and after the
    # first version of this fix - a test earning its place.)
    deadline = time.time() + 10.0
    prev_timeout = None
    try:
        prev_timeout = handler.connection.gettimeout()
        handler.connection.settimeout(1.0)
    except Exception:
        pass
    chunks, got = [], 0
    try:
        while got < n:
            if time.time() > deadline:
                # The bytes cannot be left in the socket for the next
                # request on this connection.
                handler.close_connection = True
                return {}
            try:
                part = handler.rfile.read(min(65536, n - got))
            except (TimeoutError, socket.timeout, OSError):
                continue          # nothing yet; the deadline decides
            if not part:
                handler.close_connection = True
                return {}
            chunks.append(part)
            got += len(part)
    finally:
        try:
            if prev_timeout is not None:
                handler.connection.settimeout(prev_timeout)
        except Exception:
            pass
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return {}


def send_json(handler, obj, code: int = 200) -> None:
    handler._send(code, json.dumps(obj, default=str).encode(), "application/json")


def send_error(handler, code: int, msg: str) -> None:
    # 🚨 DRAIN BEFORE REFUSING. Several refusals - every 429, the unknown
    # node, the bad token - return BEFORE the body has been read, and
    # Caddy reuses upstream connections. Unread bytes are then parsed as the
    # start of the NEXT request on that connection, so the resulting 400
    # lands on an unrelated visitor and log_message is suppressed here so
    # nothing records it. Doing it here rather than at each call site means a
    # refusal path added later cannot forget.
    if code >= 400:
        drain_body(handler)
    send_json(handler, {"error": msg}, code)


def handle_head(handler) -> None:
    """HEAD, which this server answered with 501 for its whole life.

    🚨 IT LOOKS LIKE AN EDGE CASE AND IS NOT. BaseHTTPRequestHandler has no
    default do_HEAD, so every HEAD got "501 Unsupported method" - and HEAD is
    what link checkers, uptime monitors, CDNs and link-preview crawlers use
    BEFORE they fetch anything. A site that 501s them looks broken to
    exactly the tools that decide whether a shared link is worth showing,
    which matters most at the moment a link is spreading.

    Found by my own test doing `curl -I` and reading 501 as a broken route.

    Handled by running the ordinary GET path and dropping the body in
    _send, so a HEAD can never disagree with the GET it describes - the
    status, the headers and the Content-Length are all the real ones.
    """
    handler._head_only = True
    try:
        handler.do_GET()
    finally:
        handler._head_only = False
