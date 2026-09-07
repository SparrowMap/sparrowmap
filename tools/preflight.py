"""Pre-deploy checklist that RUNS, so it cannot be forgotten.

    python tools/preflight.py            # check the working tree
    python tools/preflight.py --box      # also compare against the live box

A checklist in a document has the same failure mode as remembering: it works
until the night it matters. Every check here exists because the thing it checks
for actually went wrong, and each one failed SILENTLY - the deploy looked fine
and the fix simply did not reach anybody.

  1. VERSION BUMPS. An edited .js served with ?v=N must have N bumped in the
     same change, or every browser keeps the cached old file. Shipped
     sparrow-app.js with the one-tap review link and left it at v21: the fix
     was on the server and not one volunteer would have loaded it.
  2. SYNTAX. A page's inline script is not compiled by anything. A stray
     ReferenceError kills the function that draws the queue and the page just
     sits there looking empty.
  3. PYTHON COMPILES. Cheap, and a SyntaxError on the box is a dead service.
  4. PERSONAL DATA. Coordinates, emails, home paths, hostnames - the repo is
     public, and this project has already had the operator's home coordinates
     hardcoded in it once.
  5. DRIFT (--box). The box and the repo must never silently diverge.

Exit code is non-zero if anything FAILED, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 🚨 NOT HARDCODED. The box address and the key path must never be published -
# run_box_puller.bat is kept OUTSIDE this repo for exactly that reason. This
# file caught its own violation on its first run, which is the best argument
# for it existing. Set RAVEN_BOX and RAVEN_KEY to use --box.
BOX = os.environ.get("RAVEN_BOX") or os.environ.get("SPARROW_BOX") or ""
KEY = os.environ.get("RAVEN_KEY") or os.environ.get("SPARROW_KEY") or ""

PATTERNS = {
    "coordinate pair": r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}",
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "home path": r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}[A-Za-z]",
    # Generic on purpose: matches any workstation hostname of this shape
    # without writing his actual machine name into a public file.
    "workstation hostname": r"(?:desktop|laptop|pc)-[a-z0-9]{6,}",
    "private key": r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY",
}

# 🚨 DELIBERATELY PUBLISHED, AND DELIBERATELY A LITERAL LIST.
#
# This check reads the WHOLE of every changed file rather than the diff, which
# is the right call - a secret that was already committed is still a secret
# that is published. But it means a file carrying the project's own contact
# address can never pass again, and every future edit to the guide or the
# landing page reports the same two "failures". A gate that fails on a clean
# change is a gate people learn to run with --force in their head, and then it
# is not protecting anything.
#
# So: exact strings only, never patterns. Anything that is not on this list is
# still a failure. Adding to it should feel like a decision to publish,
# because that is what it is.
PUBLIC_OK = {
    "sparrowmap@icloud.com",   # the project's own inbox, printed on /guide
}

ok = True


def say(status: str, msg: str) -> None:
    global ok
    if status == "FAIL":
        ok = False
    print(f"  [{status:^4}] {msg}")


def changed_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    out += subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                          cwd=ROOT, capture_output=True, text=True).stdout.split()
    return sorted(set(out))


def check_versions(changed: list[str]) -> None:
    """An edited versioned asset must have its ?v= bumped in the same change."""
    refs: dict[str, list[tuple[Path, int]]] = {}
    for html in ROOT.glob("public/*.html"):
        for m in re.finditer(r'/static/([\w.-]+\.js)\?v=(\d+)', html.read_text(
                encoding="utf-8", errors="replace")):
            refs.setdefault(m.group(1), []).append((html, int(m.group(2))))

    js_changed = [f for f in changed if f.endswith(".js") and "/public/" in "/" + f]
    if not js_changed:
        say("skip", "no versioned JS changed")
        return
    for f in js_changed:
        base = Path(f).name
        if base not in refs:
            say("ok", f"{base} is not loaded with a ?v= (inline or unversioned)")
            continue
        for html, ver in refs[base]:
            # Did the HTML that references it also change?
            if str(html.relative_to(ROOT)).replace("\\", "/") in changed:
                say("ok", f"{base} changed and {html.name} ?v={ver} was bumped")
            else:
                say("FAIL", f"{base} changed but {html.name} still pins ?v={ver} "
                            f"- bump it or browsers keep the old file")


def check_syntax(changed: list[str]) -> None:
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        say("skip", "node not available; scripts not syntax-checked")
        return
    import tempfile

    # 🚨 STANDALONE .js FILES TOO, NOT JUST INLINE BLOCKS.
    # This check was written the morning after an inline-script bug, so it only
    # looked at <script> blocks in HTML - and that same night I shipped an
    # app.js whose template literal was closed early by a backtick inside an
    # HTML comment. The file did not parse, so NOTHING on the map ran: the page
    # sat on "connecting", "everything quiet", no sightings, while the server
    # was healthy. A checker shaped like the last bug is blind to the next one.
    for f in changed:
        if not f.endswith(".js"):
            continue
        p = ROOT / f
        if not p.exists():
            continue
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        if r.returncode == 0:
            say("ok", f"{f} parses")
        else:
            say("FAIL", f"{f}: {r.stderr.strip().splitlines()[0][:90]}")

    for f in changed:
        if not f.endswith(".html"):
            continue
        p = ROOT / f
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, m in enumerate(re.finditer(r"<script>(.*?)</script>", text, re.S)):
            js = m.group(1)
            tmp = Path(tempfile.gettempdir()) / f"preflight_{Path(f).stem}_{i}.js"
            tmp.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(tmp)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                say("ok", f"{f} inline script #{i + 1} parses")
            else:
                say("FAIL", f"{f} inline script #{i + 1}: "
                            f"{r.stderr.strip().splitlines()[-1][:80]}")
            # An IIFE that closes early leaves later functions in global scope,
            # where the module's private helpers are invisible to them. That
            # cost a whole evening on rv.html: render() died on a ReferenceError
            # before it ever drew the queue.
            #
            # ⚠️ ONLY FOR THE WHOLE-FILE-WRAPPED PATTERN. The first version of
            # this check flagged any code after the last `})();` and failed
            # node.html, which has no wrapping IIFE at all - its `})();` closes
            # something else and the 325 lines after it are ordinary top-level
            # code. A check that fails a healthy file is a check that gets
            # switched off, and then it is not there for the real one.
            #
            # The real failure needs BOTH halves: a script that opens with an
            # IIFE (so its helpers are private) and has code appended after the
            # matching close (so that code cannot see them).
            lines = js.splitlines()
            body = [ln for ln in lines if ln.strip()
                    and not ln.strip().startswith(("//", "/*", "*"))]
            wrapped = bool(body) and re.match(r"^\(\s*(function|async|\(|\w+\s*=>)",
                                              body[0].strip())
            closes = [n for n, ln in enumerate(lines) if ln.strip() == "})();"]
            if wrapped and closes:
                after = [ln for ln in lines[closes[-1] + 1:] if ln.strip()]
                if after:
                    say("FAIL", f"{f}: {len(after)} lines of code AFTER the "
                                f"wrapping IIFE closes - they cannot see its "
                                f"private helpers")
                else:
                    say("ok", f"{f}: wrapping IIFE encloses the whole script")


def check_python(changed: list[str]) -> None:
    """Compiles, AND every name it uses actually exists.

    🚨 py_compile CANNOT CATCH A NameError, AND THAT GAP TOOK THE FLEET DOWN.
    Adding a User-Agent to the node clients, a regex appended `, NODE_UA` after
    an existing `# noqa: E402` comment - so the name landed INSIDE the comment
    and was never imported. Every file compiled cleanly, preflight passed, it
    deployed, and every fixed camera died with NameError on its first post. The
    launcher then misreported it as "another detector is already running",
    because the exit code collided with the single-instance guard.

    Syntax was never the risk. An undefined name is, and it is exactly the class
    a compile check is blind to. Only ERRORS fail the gate - unused imports and
    unused locals are reported quietly, because a gate that cries wolf is a gate
    people learn to skip.
    """
    import py_compile
    any_py = False
    files = [f for f in changed if f.endswith(".py")]
    for f in files:
        any_py = True
        try:
            py_compile.compile(str(ROOT / f), doraise=True)
            say("ok", f"{f} compiles")
        except Exception as exc:
            say("FAIL", f"{f}: {str(exc).splitlines()[0][:90]}")

    if files:
        try:
            out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                                 cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
        except Exception as exc:
            say("info", f"name check unavailable ({exc.__class__.__name__})")
        else:
            hard, soft = [], 0
            for line in (out.stdout or "").splitlines():
                if "undefined name" in line or "syntax" in line.lower():
                    hard.append(line.strip())
                elif line.strip():
                    soft += 1
            for h in hard:
                say("FAIL", h[:110])
            if not hard:
                say("ok", f"no undefined names"
                          + (f" ({soft} style note(s) ignored)" if soft else ""))

    if not any_py:
        say("skip", "no python changed")


def check_personal_data(changed: list[str]) -> None:
    hits = 0
    for f in changed:
        p = ROOT / f
        if not p.exists() or p.suffix not in (".py", ".js", ".html", ".json", ".md"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for name, pat in PATTERNS.items():
            for m in re.finditer(pat, text):
                if m.group(0) in PUBLIC_OK:
                    continue
                say("FAIL", f"{f}: possible {name}: {m.group(0)[:48]}")
                hits += 1
    if not hits:
        say("ok", "no coordinates, emails, home paths, hostnames or keys in the diff")


def check_drift() -> None:
    if not BOX or not KEY:
        say("skip", "set RAVEN_BOX and RAVEN_KEY to check box drift")
        return
    files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    # 🚨 ONLY FILES THAT ACTUALLY RUN ON THE BOX.
    # camctl and detect/ are node-side and live on the machine with the
    # camera; landing/ is served by Cloudflare; train/ and tools/ are not
    # part of the running service. Listing them made the drift check report
    # 14 differences on a perfectly synced box - and a warning that is
    # always noisy is a warning nobody reads, which is worse than none.
    NOT_ON_BOX = ("camctl/", "detect/", "landing/", "train/", "tools/",
                  "sources/", "docs/")
    watch = [f for f in files
             if f.endswith((".py", ".js", ".html"))
             and not f.startswith(NOT_ON_BOX)]
    remote = subprocess.run(
        ["ssh", "-i", KEY, "-o", "BatchMode=yes", BOX,
         "cd /opt/sparrowmap && md5sum " + " ".join(watch) + " 2>/dev/null"],
        capture_output=True, text=True).stdout
    theirs = {}
    for line in remote.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            theirs[parts[1].strip()] = parts[0]
    import hashlib
    drifted = []
    for f in watch:
        p = ROOT / f
        if not p.exists() or f not in theirs:
            continue
        # RAW bytes, not newline-normalised. Deployment is scp of this exact
        # file, so the box holds these bytes including any CRLF. Normalising one
        # side and not the other reported 7 files as drifted that had been
        # deployed an hour earlier and verified identical by md5 at the time -
        # a drift check that cries wolf is one nobody reads.
        mine = hashlib.md5(p.read_bytes()).hexdigest()
        if mine != theirs[f]:
            drifted.append(f)
    if drifted:
        say("warn", f"{len(drifted)} file(s) differ from the box: "
                    f"{', '.join(drifted[:6])}{' ...' if len(drifted) > 6 else ''}")
    else:
        say("ok", "box matches the repo")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--box", action="store_true", help="also diff against the live box")
    a = ap.parse_args()

    changed = changed_files()
    print(f"preflight: {len(changed)} changed file(s)\n")
    print(" 1. version bumps");     check_versions(changed)
    print(" 2. inline script syntax"); check_syntax(changed)
    print(" 3. python compiles");   check_python(changed)
    print(" 4. personal data");     check_personal_data(changed)
    if a.box:
        print(" 5. box drift");     check_drift()

    print()
    if ok:
        print("  ALL CLEAR - safe to commit and deploy.")
    else:
        print("  ⛔ SOMETHING FAILED - fix it before deploying.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
