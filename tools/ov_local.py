"""Open the officer review surface on this machine.

The oversight database (data/oversight.db, ~760 MB and still being enriched)
lives on the desktop, not the box, so phase 2 review happens here: a hub bound
to 127.0.0.1 on its own port, and a browser tab on /ov signed in with an
operator-issued pool token kept in data/ (gitignored). Run via
`Officer Review.bat` or `python tools/ov_local.py`.
"""
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import review_auth  # noqa: E402

PORT = int(os.environ.get("OV_PORT") or 8171)
TOK = ROOT / "data" / "ov_local_token.txt"


def token() -> str:
    if TOK.is_file() and TOK.read_text().strip():
        return TOK.read_text().strip()
    t = review_auth.issue("matthew (officer review)", "pool")
    TOK.write_text(t)
    return t


def main() -> None:
    t = token()
    env = dict(os.environ, SPARROW_BIND="127.0.0.1")
    p = subprocess.Popen([sys.executable, str(ROOT / "hub.py"), "--port", str(PORT),
                          "--https-port", str(PORT + 1)], cwd=str(ROOT), env=env)
    time.sleep(4)
    url = f"http://127.0.0.1:{PORT}/ov#token={t}"
    print(f"officer review -> {url.split('#')[0]}   (token in {TOK})")
    webbrowser.open(url)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()


if __name__ == "__main__":
    main()
