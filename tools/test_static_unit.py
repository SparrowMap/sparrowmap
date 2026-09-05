"""Deterministic module-level tests for static.py (Stage 1B step 5).

No HTTP server involved: exercises static.vendor_file_path/static_file_path/
snap_file_path (the traversal guard) and static.serve (the file-I/O +
Content-Type + nonce-placeholder primitive) directly, against a temporary
on-disk fixture tree.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import static  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name}  {detail}")
    else:
        print(f"  [ok]   {name}")


class FakeHandler:
    """Minimal duck-typed stand-in for hub.Handler, capturing _send/_err."""

    def __init__(self):
        self.sent = None   # (status, body, ctype)
        self.errs = None   # (status, msg)

    def _send(self, status, body, ctype):
        self.sent = (status, body, ctype)

    def _err(self, status, msg):
        self.errs = (status, msg)


def t_vendor_file_path():
    print("\n== vendor_file_path traversal guard ==")
    public = Path("/tmp/public")
    # Plain filename, flattened but unchanged.
    check("vendor plain file",
          static.vendor_file_path(public, "leaflet.js") == public / "vendor" / "leaflet.js")
    # Two-segment images/ carve-out.
    check("vendor images/ carve-out",
          static.vendor_file_path(public, "images/marker-icon.png")
          == public / "vendor" / "images" / "marker-icon.png")
    # Traversal segments are filtered before counting, so this no longer has
    # exactly 2 segments ("images", "etc", "passwd") and falls through to the
    # flat single-name lookup below "vendor/" directly - same as any other
    # non-2-segment path. Still flattened, just via the other branch.
    check("vendor images/ traversal falls through to flat name",
          static.vendor_file_path(public, "images/../../../etc/passwd")
          == public / "vendor" / "passwd")
    # Traversal outside images/ falls to flat single-name lookup.
    check("vendor traversal flattened (non-images)",
          static.vendor_file_path(public, "../../../etc/passwd")
          == public / "vendor" / "passwd")
    # Nested-but-not-images two-segment form still flattens to single name
    # (only "images" is special-cased; this reproduces the documented
    # /vendor/images/* dependency on exact literal match).
    check("vendor other/two-segment falls through to flat name",
          static.vendor_file_path(public, "sub/dir/file.js")
          == public / "vendor" / "file.js")


def t_static_file_path():
    print("\n== static_file_path traversal guard ==")
    public = Path("/tmp/public")
    check("static plain file",
          static.static_file_path(public, "app.js") == public / "app.js")
    check("static traversal flattened",
          static.static_file_path(public, "../../hub.py") == public / "hub.py")
    check("static traversal flattened (encoded-looking, already decoded by caller)",
          static.static_file_path(public, "..\\..\\hub.py").name == "hub.py")


def t_snap_file_path():
    print("\n== snap_file_path traversal guard ==")
    snaps = Path("/tmp/snaps")
    check("snap plain file",
          static.snap_file_path(snaps, "abc123.jpg") == snaps / "abc123.jpg")
    check("snap traversal flattened",
          static.snap_file_path(snaps, "../../../etc/passwd") == snaps / "passwd")


def t_serve():
    print("\n== static.serve file-I/O primitive ==")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "plain.js").write_bytes(b"console.log(1);")
        (root / "page.html").write_bytes(b"<html><script>x=1</script></html>")
        (root / "model.wasm").write_bytes(b"\x00asm")
        (root / "model.onnx").write_bytes(b"onnx-bytes")

        h = FakeHandler()
        static.serve(h, root / "plain.js")
        check("serve .js -> 200 + javascript ctype",
              h.sent is not None and h.sent[0] == 200 and "javascript" in h.sent[2],
              f"got {h.sent}")

        h = FakeHandler()
        static.serve(h, root / "page.html")
        check("serve .html injects nonce placeholder",
              h.sent is not None and b'<script nonce="@@NONCE@@">' in h.sent[1],
              f"got {h.sent}")
        check("serve .html ctype",
              h.sent[2] == "text/html", f"got {h.sent[2]}")

        h = FakeHandler()
        static.serve(h, root / "model.wasm")
        check("serve .wasm -> application/wasm",
              h.sent is not None and h.sent[2] == "application/wasm",
              f"got {h.sent}")

        h = FakeHandler()
        static.serve(h, root / "model.onnx")
        check("serve .onnx -> application/octet-stream",
              h.sent is not None and h.sent[2] == "application/octet-stream",
              f"got {h.sent}")

        h = FakeHandler()
        static.serve(h, root / "does-not-exist.js")
        check("serve missing file -> _err(404, ...)",
              h.errs == (404, "not found"), f"got errs={h.errs} sent={h.sent}")


def main() -> int:
    t_vendor_file_path()
    t_static_file_path()
    t_snap_file_path()
    t_serve()
    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("static.py unit characterization passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
