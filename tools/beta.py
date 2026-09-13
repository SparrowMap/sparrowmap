"""Run the contributor beta from the desktop.

    python tools/beta.py setup                 stand up / refresh the beta on the box
    python tools/beta.py invite <name>         mint a beta token for a contributor
    python tools/beta.py revoke <name>         revoke every token with that label
    python tools/beta.py tokens                list beta tokens
    python tools/beta.py board [status]        list the request board
    python tools/beta.py board show <id>
    python tools/beta.py board add "<title>" ["<body>"] [--kind bug|note]
    python tools/beta.py board note <id> "<text>"
    python tools/beta.py board status <id> <status> ["<why>"]

Needs SPARROW_BOX + SPARROW_KEY like deploy.py. The board commands talk HTTPS to
BETA_URL with the operator's own beta token, kept in data/beta_token.txt
(gitignored); `setup` mints it if missing.

One invite = one token, labelled with the person's name, `own` scope with no
cameras: it opens the beta door and signs the board, and grants nothing on the
live map. Revoke is by label, so a name maps to one switch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOX = os.environ.get("SPARROW_BOX", "")
KEY = os.environ.get("SPARROW_KEY", "")
BETA_URL = os.environ.get("BETA_URL", "https://beta.sparrowmap.com")
BETA_DATA = "/opt/sparrowmap-beta/data"
BETA_CFG = "/opt/sparrowmap-beta/config.json"
PY = "/opt/sparrowmap/.venv/bin/python"
TOKEN_FILE = ROOT / "data" / "beta_token.txt"


def ssh(cmd: str, stdin: str = "") -> subprocess.CompletedProcess:
    if not BOX or not KEY:
        sys.exit("set SPARROW_BOX and SPARROW_KEY (see the sparrow-deploy skill)")
    # Bytes, not text=True: on Windows a text-mode pipe rewrites every "\n" as
    # "\r\n", and bash on the box then reads "pipefail\r" and stops on line 1.
    r = subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", BOX, cmd],
                       input=stdin.encode(), capture_output=True)
    r.stdout = r.stdout.decode(errors="replace")   # type: ignore[assignment]
    r.stderr = r.stderr.decode(errors="replace")   # type: ignore[assignment]
    return r


def box_py(code: str) -> str:
    """Run python on the box AGAINST THE BETA DATA DIR, as the sparrow user."""
    r = ssh(f"cd /opt/sparrowmap && sudo -u sparrow env SPARROW_DATA={BETA_DATA} "
            f"SPARROW_CONFIG={BETA_CFG} {PY} -", stdin=code)
    if r.returncode:
        sys.exit((r.stdout + r.stderr).strip() or f"ssh exit {r.returncode}")
    return r.stdout.strip()


def cmd_setup() -> None:
    # Git on Windows checks the script out with CRLF; bash on the box reads
    # "pipefail\r" and stops on line 1. Send it as the box expects it.
    script = (ROOT / "tools" / "beta_setup.sh").read_text().replace("\r\n", "\n")
    r = ssh("bash -s", stdin=script)
    print(r.stdout)
    if r.returncode:
        sys.exit(r.stderr.strip() or f"setup failed ({r.returncode})")
    if not TOKEN_FILE.is_file():
        tok = box_py("import review_auth; print(review_auth.issue('matthew', 'pool'))")
        TOKEN_FILE.parent.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(tok)
        print(f"operator beta token minted -> {TOKEN_FILE}")
        print(f"sign in at {BETA_URL}/beta with:\n  {tok}")


def cmd_invite(name: str) -> None:
    label = name.strip()
    if not label:
        sys.exit("name required")
    tok = box_py(f"import review_auth; print(review_auth.issue({label!r}, 'own'))")
    print(f"""
Invite for {label}
==================
Beta:   {BETA_URL}/beta
Token:  {tok}
Board:  {BETA_URL}/board

Everything on the beta is synthetic (invented town, simulated cameras). The
token is yours by name; every action is logged under it. Do not share it.
Chat: Sparrow Send, https://map.sparrowmap.com/send - claim a handle and
message @claudebot or Matthew's handle. Code: fork + PR to
github.com/SparrowMap/sparrowmap; PRs land on the beta first.
""")


def cmd_revoke(name: str) -> None:
    out = box_py(f"""
import review_auth
n = 0
for t in review_auth.listing():
    if (t.get('label') or '') == {name!r} and not t.get('revoked_at'):
        review_auth.revoke(t['id']); n += 1
print(n)
""")
    print(f"revoked {out} token(s) labelled {name!r}")


def cmd_tokens() -> None:
    out = box_py("""
import json, review_auth
print(json.dumps([{k: t.get(k) for k in ('id','label','scope','created_by','created_at','last_used_at','revoked_at')}
                  for t in review_auth.listing()], default=str))
""")
    for t in json.loads(out):
        flag = "REVOKED" if t["revoked_at"] else ""
        print(f"{t['id']:>4}  {t['label'] or '':<24} {t['scope']:<5} {t['created_by'] or '':<9} {flag}")


# --- the board over HTTPS ------------------------------------------------------
def api(path: str, body: dict | None = None) -> dict:
    if not TOKEN_FILE.is_file():
        sys.exit(f"no {TOKEN_FILE}; run `python tools/beta.py setup` first")
    tok = TOKEN_FILE.read_text().strip()
    req = urllib.request.Request(
        BETA_URL + path, method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                 "User-Agent": "sparrow-beta-cli"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{e.code}: {e.read().decode(errors='replace')[:300]}")


def cmd_board(args: list) -> None:
    kind = None
    if "--kind" in args:
        i = args.index("--kind")
        kind = args[i + 1]
        del args[i:i + 2]
    sub = args[0] if args else "list"
    if sub == "show":
        it = api(f"/api/board/item?id={int(args[1])}")
        print(f"#{it['id']} [{it['status']}] {it['title']}  ({it['kind']}, {it['by']})\n{it['body']}\n")
        for n in it["notes"]:
            print(f"  - {n['by']}: {n['body']}")
        return
    if sub == "add":
        r = api("/api/board/add", {"title": args[1], "body": args[2] if len(args) > 2 else "",
                                   "kind": kind or "request"})
        print(r)
        return
    if sub == "note":
        print(api("/api/board/note", {"id": int(args[1]), "body": args[2]}))
        return
    if sub == "status":
        print(api("/api/board/status", {"id": int(args[1]), "status": args[2],
                                        "why": args[3] if len(args) > 3 else ""}))
        return
    status = sub if sub != "list" else (args[1] if len(args) > 1 else "")
    r = api(f"/api/board/list?status={status}")
    print("counts:", r["counts"])
    for it in r["rows"]:
        print(f"#{it['id']:<4} {it['status']:<9} {it['kind']:<7} {it['title']}  — {it['by']} ({it['notes']} notes)")


def main() -> None:
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "setup":
        cmd_setup()
    elif a[0] == "invite":
        cmd_invite(" ".join(a[1:]))
    elif a[0] == "revoke":
        cmd_revoke(" ".join(a[1:]))
    elif a[0] == "tokens":
        cmd_tokens()
    elif a[0] == "board":
        cmd_board(a[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
