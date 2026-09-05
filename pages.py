"""pages.py - Stage 2A route adapters: public/static/download page shells.

This is a ROUTE ADAPTER module, not an application-service layer. It owns the
lowest-risk, purely-presentational public routes identified in the Stage 0
inventory: fixed HTML page shells served straight off disk, a couple of
redirect/alias routes, and the desktop-download probe/redirect pair. None of
these routes touch the database, privacy/redaction, node signing, or mirror
policy beyond the existing outer `mirror.route_allowed` gate that hub.py
still performs before dispatching here.

Moved verbatim (smallest possible semantic change) from hub.py's
`_do_GET_inner`:
  * DOWNLOAD_URL / _DL_CACHE / _DL_TTL_S / download_url() - the cached
    GitHub-release HEAD-probe used by both /download and /api/download.
  * The page-shell branches for /, /about, /transparency, /status,
    /checksums, /support|/donate, /business|/ipcamera, /IPCamera,
    /relay.py, /download, /api/download, /hardware, /build16,
    /app|/node|/key|/contribute, /signin|/login/camera, /sw.js.

Each function takes the same duck-typed handler object every other Stage 1B
module takes (only handler._file / handler._json / handler._status /
handler.send_response / handler.send_header / handler.end_headers are used
here) so hub.py's dispatch chain can call straight into this module at the
exact position each route currently occupies, preserving first-match-wins
ordering across the hub.py/pages.py boundary.

Does not import hub. Stdlib plus core only.
"""

from __future__ import annotations

from pathlib import Path

from core import CONFIG, PUBLIC, now

# ---------------------------------------------------------------------------
# Desktop-app download probe
#
# The packaged desktop app is hosted on GitHub releases rather than here -
# see download() for why - and CHECKED rather than assumed: the button on
# /IPCamera appears only when the asset really exists, so the page never
# offers a download that 404s. Cached, because this is a third-party round
# trip on a path a crowd may hit.
# ---------------------------------------------------------------------------

DOWNLOAD_URL = CONFIG.get(
    "download_url",
    "https://github.com/SparrowMap/sparrowmap/releases/latest/download/SparrowMap4Biz.exe")
_DL_CACHE = {"at": 0.0, "ok": False}
_DL_TTL_S = 600.0


def download_url():
    """The URL if a build is actually published, else None."""
    if not DOWNLOAD_URL:
        return None
    if now() - _DL_CACHE["at"] < _DL_TTL_S:
        return DOWNLOAD_URL if _DL_CACHE["ok"] else None
    ok = False
    try:
        # Imported here, not at module scope: this is the only outbound HTTP
        # call this module makes on a page path, and keeping it local makes
        # that obvious to anyone auditing what this server talks to.
        import urllib.request
        req = urllib.request.Request(
            DOWNLOAD_URL, method="HEAD",
            # 🚨 A User-Agent is REQUIRED. GitHub refuses requests without one,
            # and this project has already lost a whole ingest path to exactly
            # that mistake behind Cloudflare.
            headers={"User-Agent": "SparrowMap"})
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    _DL_CACHE.update(at=now(), ok=ok)
    return DOWNLOAD_URL if ok else None


def index(handler) -> None:
    return handler._file(PUBLIC / "index.html")


def about(handler) -> None:
    return handler._file(PUBLIC / "about.html")


def transparency(handler) -> None:
    return handler._file(PUBLIC / "transparency.html")


def status_page(handler) -> None:
    # 🚨 THE ANSWER TO "YOUR SITE IS BLOCKED, SO IT IS FAKE".
    # A filter's block page is served before the request ever leaves the
    # reader's device, so it is evidence about their filter and nothing
    # else. This page exists so the reply is a link rather than an
    # argument: if they can read it, they reached the server.
    return handler._file(PUBLIC / "status.html")


def checksums(handler) -> None:
    # SHA-256 for every published installer. Asked for on Hacker News,
    # and the honest answer at the time was that no checksum existed to
    # check against. Served from THIS host while the binaries live on
    # GitHub, so verifying means trusting two places rather than one.
    return handler._file(PUBLIC / "checksums.html")


def support_or_donate(handler) -> None:
    # Growth, live capacity and what it costs, generated from the
    # database by tools/support_page.py. Asking for money without
    # showing the numbers - including the bad retention one - is the
    # kind of thing this project exists to be the opposite of.
    #
    # ⚠️ GENERATED, SO IT CAN BE ABSENT. It is built on the server by
    # tools/support_page.py and gitignored, so a fresh checkout has
    # no copy. Say that rather than serving a 404, which would look
    # like the page was taken down - on a donations page that reads
    # as something worse than a missing file.
    f = PUBLIC / "support.html"
    if f.is_file():
        return handler._file(f)
    return handler._send(
        503, b"<!doctype html><meta charset=utf-8>"
             b"<title>SparrowMap</title>"
             b"<p style='font:15px system-ui;padding:24px'>"
             b"This page is generated from the live database and has "
             b"not been built on this server yet.<br>"
             b"<a href='/'>Back to the map</a>", "text/html")


def business_redirect(handler) -> None:
    # 🚨 THE ROUTE SOMEBODY WITH THEIR OWN CAMERA IS SENT TO. Everything
    # it needs is already public (enrol, aim, review); what did not exist
    # was one page that walks somebody who owns a shop - not a terminal -
    # from "I have a camera outside" to a running relay without them
    # having to know which of these pages to visit in which order.
    #
    # ⚠️ IT WAS CALLED /business AND THE NAME WAS THE PROBLEM. People
    # read "business" as "the paid tier" or "not for me", and asked. It
    # describes who we imagined using it rather than what it does, so it
    # is /IPCamera now - which is the thing they actually have.
    #
    # 🚨 /business STILL ANSWERS, FOREVER. It is printed in a viral reel's
    # comments, in DMs and in older builds of the desktop app, and none of
    # those can be edited. A rename that breaks them costs more than the
    # rename gains. Accept the lower-case spelling too: nobody types
    # capitals in the middle of a URL from memory.
    #
    # ⚠️ 301 SO SEARCH MOVES THE PAGE ACROSS, BUT WITH A SHORT
    # Cache-Control. A bare 301 is cached by browsers indefinitely
    # and this name has already changed twice; an hour is plenty for
    # search engines and leaves us able to change our minds without
    # having poisoned every visitor's browser.
    handler._status = 301
    handler.send_response(301)
    handler.send_header("Location", "/IPCamera")
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.end_headers()


def ipcamera(handler) -> None:
    return handler._file(PUBLIC / "ipcamera.html")


def relay_py(handler) -> None:
    # 🚨 THE RELAY, AS ONE FILE. It imports nothing from this project
    # and fetches its own model, so a business needs this file and three
    # pip packages - not a git checkout. Telling somebody to clone a
    # repository to run a background service is where most of them stop.
    return handler._file(PUBLIC.parent / "detect" / "relay.py")


def download(handler) -> None:
    # The packaged desktop app, when a build has been placed here. Kept
    # OUT of git (it is a 60 MB derived artefact full of absolute build
    # paths - preflight caught exactly that), so this 404s cleanly until
    # somebody uploads one, and /IPCamera hides its button accordingly.
    #
    # 🚨 REDIRECTED TO A GITHUB RELEASE, NOT SERVED FROM HERE.
    # The app is 76 MB. This box is 2 vCPUs on a 13 Mbps uplink and
    # also serves the map, so handing that file to a crowd would
    # take the site down at exactly the moment attention arrives -
    # which is the moment the download matters. GitHub carries it
    # for free and is built for it.
    #
    # `/releases/latest/download/` is a stable URL that always
    # points at the newest release's asset of that name, so cutting
    # a new version needs no change here.
    url = download_url()
    if not url:
        return handler._err(404, "no desktop build is published yet")
    handler._status = 302
    handler.send_response(302)
    handler.send_header("Location", url)
    handler.send_header("Cache-Control", "public, max-age=300")
    handler.end_headers()


def api_download(handler) -> None:
    # Same-origin, so the page can ask without CORS - a HEAD from
    # the browser straight to GitHub is opaque and would leave the
    # button hidden even when the file is there.
    url = download_url()
    return handler._json({"available": bool(url), "url": url})


def hardware(handler) -> None:
    # What a node costs in compute, and how to measure your own board
    # rather than take this page's word for it.
    return handler._file(PUBLIC / "hardware.html")


def build16(handler) -> None:
    # Building a long-lens node from salvaged CCTV optics: the range
    # geometry against this project's own two thresholds (120px of
    # vehicle, ~60px of plate), and the assembly order. Public because
    # the interesting half is the arithmetic, which applies to any lens
    # somebody already owns - not just the one this was written for.
    return handler._file(PUBLIC / "build16.html")


def app_alias(handler) -> None:
    # One program, three modes. /node and /key are kept as aliases
    # because keys, QR codes and bookmarks already point at them - a
    # link a volunteer printed must not stop working because the pages
    # were reorganised.
    #
    # /contribute is kept for the same reason, but it no longer has a
    # page of its own: log-by-hand was removed, so the alias lands on
    # the app rather than 404ing a printed link.
    return handler._file(PUBLIC / "app.html")


def signin(handler) -> None:
    return handler._file(PUBLIC / "signin.html")


def sw_js(handler) -> None:
    # Served at the ROOT so its scope covers /app and /node - a
    # service worker only controls pages under its own path. Its
    # Cache-Control is the default no-store, which is right: the
    # browser must re-check it to pick up an updated worker.
    return handler._file(PUBLIC / "sw.js")
