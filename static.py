"""static.py - Stage 1B step 5: static/vendor/snap file serving.

Moved out of hub.py with the smallest possible semantic change. This module
owns two things together, deliberately not split apart:

  1. serve() - the actual file read + Content-Type guess + nonce-placeholder
     insertion + _send call (the former Handler._file).
  2. vendor_file_path() / static_file_path() / snap_file_path() - the
     traversal guard.

THE GUARD DOES NOT LIVE INSIDE serve(). It never did: in the original
hub.py, `_file` opened whatever Path it was handed, and the ONLY thing that
made `/static/`, `/vendor/`, and `/snap/` safe was that their call sites
reduced the request path to `Path(...).name` before ever calling `_file` -
collapsing any `../` traversal to a single flat filename. serve() alone is
NOT a safe standalone primitive for attacker-controlled input; nothing in
this module removes that fact.

Per the user's explicit Stage 1B instruction, that restriction is treated as
part of the static-serving security boundary, so this module moves BOTH
halves together: the three *_file_path() functions below are the only
sanctioned way request input becomes a Path passed to serve(), and each one
replicates the exact existing guard logic (verbatim reasoning preserved from
hub.py's _do_GET_inner). hub.py's call sites now call these builders instead
of doing the flattening inline, then hand the result to serve() - so the
guard moves WITH the primitive rather than being left behind as a dangling
caller-side convention.

Nothing here imports hub.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path


def serve(handler, path: Path) -> None:
    """Read `path` from disk and send it, or 404. Formerly Handler._file.

    NOT SAFE FOR ATTACKER-CONTROLLED `path`. Callers must build `path` via
    vendor_file_path()/static_file_path()/snap_file_path() (or an equally
    guarded construction) - see the module docstring.
    """
    if not path.is_file():
        return handler._err(404, "not found")
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    # The browser refuses to instantiate a wasm module served as anything
    # else, and mimetypes on Windows does not know these two.
    ctype = {".wasm": "application/wasm",
             ".onnx": "application/octet-stream"}.get(path.suffix, ctype)
    body = path.read_bytes()
    if path.suffix == ".html":
        # The nonce is generated inside _send, so mark the tags with a
        # placeholder and let _send fill it. Only bare <script> tags are
        # touched; ones with a src attribute are already covered by 'self'.
        body = body.replace(b"<script>", b"<script nonce=\"@@NONCE@@\">")
    handler._send(200, body, ctype)


def vendor_file_path(public_dir: Path, rest_after_prefix: str) -> Path:
    """Resolve a `/vendor/...` request to a safe on-disk Path.

    `.name` FLATTENS THE PATH, WHICH IS THE TRAVERSAL GUARD AND ALSO WHY
    /vendor/images/* 404d if handled naively. Leaflet asks for
    /vendor/images/marker-icon.png; `.name` turned that into
    vendor/marker-icon.png, which does not exist - so the marker on
    /IPCamera rendered as a broken-image box, on the one control the page
    asks a business to drag.

    The guard stays. One subdirectory is allowed and it is named literally,
    so nothing here can walk anywhere else: any segment that is not "images"
    falls through to the flat lookup, and the filename is still reduced to
    its own `.name`.
    """
    rest = [seg for seg in rest_after_prefix.split("/") if seg not in ("", ".", "..")]
    if len(rest) == 2 and rest[0] == "images":
        return public_dir / "vendor" / "images" / Path(rest[1]).name
    return public_dir / "vendor" / Path(rest_after_prefix).name


def static_file_path(public_dir: Path, rest_after_prefix: str) -> Path:
    """Resolve a `/static/...` request to a safe on-disk Path.

    Same `.name`-flattening traversal guard as vendor_file_path.
    """
    return public_dir / Path(rest_after_prefix).name


def snap_file_path(snaps_dir: Path, unquoted_rest_after_prefix: str) -> Path:
    """Resolve a `/snap/...` request to a safe on-disk Path.

    Same `.name`-flattening traversal guard as vendor_file_path. The caller
    is responsible for having already URL-unquoted the segment, exactly as
    the original hub.py call site did before reaching this point.
    """
    return snaps_dir / Path(unquoted_rest_after_prefix).name
