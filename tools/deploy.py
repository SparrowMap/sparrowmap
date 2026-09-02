"""The only way to put code on the box.

    python tools/deploy.py                 # deploy origin/main to the box
    python tools/deploy.py --dry-run       # say what it would do
    python tools/deploy.py --no-restart    # deploy, skip the restart decision

Needs SPARROW_BOX (user@host) and SPARROW_KEY (path to the key). The address is
not in this repo.

🚨 WHY THIS EXISTS: `git checkout origin/main -- <paths>` IS HOW THE BOX DRIFTED.
Deploying by naming paths works, right up until it doesn't:

  * it never advances HEAD, so the box sat on a commit from days earlier while
    serving current code - the git state stopped being a record of anything;
  * it leaves every change STAGED, so `git status` was permanently dirty and
    therefore permanently ignorable;
  * it only copies the paths you remember, so files nobody thought about
    (tools/, a hand-installed review_auth.py) silently diverged;
  * and a future `git pull` would then land in a conflict, during whatever
    emergency made you reach for it.

None of that was visible from the outside. The site was serving byte-identical
code the whole time, which is exactly why it went unnoticed for days.

THE RULES THIS ENFORCES, in order:

  1. Local must be CLEAN and PUSHED. You cannot deploy something that only
     exists on this machine - if it is not in origin, the box cannot have it
     and nobody else can see what shipped.
  2. Preflight must pass. Same gate as committing.
  3. The box pulls with --ff-only. A fast-forward cannot silently rewrite
     local edits: if the box has diverged it FAILS, loudly, before anything
     changes, and you go and look at why.
  4. Afterwards the box tree must equal origin/main exactly. Verified, not
     assumed - `git diff --stat origin/main` has to be empty.
  5. Anything imported at startup gets a restart, because a fix that is not
     running is not a fix ([[feedback-never-run-stale-code]]). Static files are
     read from disk per request and deliberately do NOT trigger one.
  6. The site is checked after. A deploy that ends with a 502 is not a deploy.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOX = os.environ.get("SPARROW_BOX", "")
KEY = os.environ.get("SPARROW_KEY", "")
REMOTE = "/opt/sparrowmap"
SERVICE = "sparrowmap.service"
SITE = "https://map.sparrowmap.com"

# Imported once at startup: changing these means the running process is stale.
# Everything else on the box (public/*.html, *.js, *.css, *.json) is read from
# disk per request and needs no restart - restarting for those would be a
# gratuitous outage.
RESTART_TRIGGERS = (".py",)

ok = True


def say(tag: str, msg: str) -> None:
    global ok
    if tag == "fail":
        ok = False
    print(f"  [{tag:^4}] {msg}")


# 🚨 ALWAYS NAME THE ENCODING WHEN CAPTURING OUTPUT ON WINDOWS.
# text=True decodes with the ANSI codepage (cp1252 here), so one emoji in a
# child process's output raises UnicodeDecodeError inside subprocess's reader
# thread and .stdout comes back as None - not an error you can see, just a
# value that is suddenly nothing. check_running_code.py prints a warning sign,
# and that alone crashed this tool.
#
# Same bug family as the health watch at midnight, arriving from the other
# direction: that one could not ENCODE an emoji to a redirected log, this one
# could not DECODE one from a pipe.
def _run(cmd: list, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace")


def git(*args: str, cwd: Path = ROOT) -> str:
    return _run(["git", *args], cwd=cwd).stdout.strip()


def ssh(cmd: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", KEY, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", BOX, cmd],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


def scp(local: Path, remote: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["scp", "-i", KEY, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", str(local), f"{BOX}:{remote}"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


BUNDLE_REMOTE = "/tmp/sparrowmap-deploy.bundle"


def deliver_by_bundle(box_head: str) -> bool:
    """Hand the box the commits over SSH, because GitHub will not give them to it.

    🚨 THE BOX CANNOT FETCH OBJECTS FROM GITHUB ANY MORE, AND THIS IS NOT A
    PERMISSIONS PROBLEM.

    Traced 2026-09-02 with GIT_CURL_VERBOSE on a PUBLIC repo:

        GET  /SparrowMap/sparrowmap.git/info/refs?service=git-upload-pack -> 200
        POST /SparrowMap/sparrowmap.git/git-upload-pack                   -> 401
             www-authenticate: Basic realm="GitHub"

    The ref advertisement is served anonymously and the object download is
    challenged, so `git ls-remote` succeeds and `git fetch` fails - which is
    exactly the shape that makes this so confusing to diagnose by hand. It is
    also not flaky: five fetches in a row got 401, while a pull earlier the same
    evening had worked, so GitHub is throttling this host rather than refusing
    it. There is no key to fix: the `sparrow` user has no ~/.ssh at all and the
    `stage_pull` deploy key is rejected.

    📌 SO STOP DEPENDING ON IT. The desktop can reach GitHub and already holds
    an SSH channel to the box, so the objects travel that way instead - a thin
    bundle of exactly the commits the box is missing. Nothing about the trust
    model changes: the bundle is built from the local `main`, which step 1 has
    already proven identical to origin/main, and step 5 still verifies the box
    ends up at origin/main's commit with a matching tree. The box advances only
    to a commit that exists in origin, the same as before.
    """
    tmp = ROOT / ".deploy.bundle"
    try:
        r = _run(["git", "bundle", "create", str(tmp), f"{box_head}..main"])
        if r.returncode != 0 or not tmp.exists():
            say("fail", "could not build the bundle: " + (r.stderr or "")[-200:])
            return False
        size = tmp.stat().st_size
        s = scp(tmp, BUNDLE_REMOTE)
        if s.returncode != 0:
            say("fail", "could not send the bundle: " + (s.stderr or "")[-200:])
            return False
        # sparrow does the git work, so sparrow has to be able to read it.
        v = ssh(f"chmod 644 {BUNDLE_REMOTE} && cd {REMOTE} && "
                f"sudo -u sparrow git bundle verify {BUNDLE_REMOTE} 2>&1")
        if v.returncode != 0:
            say("fail", "the box rejected the bundle: " + (v.stdout or "")[-300:])
            return False
        say(" ok ", f"delivered {size / 1024:.0f} KB of objects over SSH "
                    f"(GitHub refused the box)")
        return True
    finally:
        tmp.unlink(missing_ok=True)



# What runs HERE, and how to start it again. The box is not the only place that
# can drift: the local hub, the detector and camctl all import modules at
# startup, so editing core.py leaves three processes running yesterday's rules
# while the repo says otherwise.
LOCAL = {
    # `port` is what makes a restart VERIFIABLE rather than merely issued: the
    # hub can leave a process behind and still not be serving, which is exactly
    # how a deploy came to report success over a dead :8150.
    "hub.py":      {"match": "*hub.py*", "args": "hub.py", "camera": False,
                    "port": 8150},
    # 🚨 THE PULLER CAN GO STALE NOW THAT IT IS A LOOP. As a `--once` task it
    # re-imported everything every five minutes and was fresh by construction;
    # a process that stays up for days holds whatever vehicle_id.py said when
    # it started, and it is the only route a contributor's crop has to a human.
    #
    # `bat` rather than `args`: its launcher lives OUTSIDE the repo because it
    # carries the box address and the ssh key path, so this must not try to
    # reconstruct the command line - it would have to hardcode exactly what
    # that file exists to keep out of here.
    "box_puller":  {"match": "*box_puller*", "bat": r"D:\LLM\run_box_puller.bat",
                    "args": None, "camera": False},
    "run_live.py": {"match": r"*detect\run_live.py*", "args": None, "camera": True},
    "camctl.py":   {"match": r"*camctl\camctl.py*", "args": r"camctl\camctl.py",
                    "camera": True},
}


def _ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, encoding="utf-8",
                          errors="replace").stdout.strip()


# 🚨 `pythonw.exe` IS A PYTHON PROCESS AND THIS TOOL COULD NOT SEE IT.
#
# Every process query here matched `Name='python.exe'` exactly. run_box_puller.bat
# launches **pythonw.exe** (no console window), so for that service:
#
#   * the STOP matched nothing, so the old copy was never killed;
#   * the launcher then started a fresh copy, which found the singleton lock
#     still held by the copy that was never killed, and exited quietly by
#     design;
#   * and _came_up() looked for python.exe, found none, and reported
#     "was stopped and did NOT come back".
#
# So the message was wrong twice over - it was never stopped, and a copy WAS
# running - and every deploy printed it, which is what made it read as a known
# cosmetic wart instead of a fault. Meanwhile the process it could not see went
# on running: box_puller had been up since 25 August holding a head from the
# 18th, so retraining the classifier changed nothing about what reached the
# review queue. That is the whole reason the stale head survived.
#
# 📌 A PROCESS FILTER THAT NAMES ONE EXECUTABLE IS A FILTER THAT LIES ABOUT THE
# OTHER ONE. `Name LIKE '%python%'` covers python.exe and pythonw.exe both.
def _came_up(name: str, spec: dict, wait_s: int = 12) -> bool:
    """Did the thing we just started actually start?

    Two questions, because they fail separately. A process can exist and still
    be unable to serve - the hub binds a port, and the most likely reason a
    restarted hub dies is that the old one had not released :8150 yet, which
    leaves a process that exits a second later into a minimised window nobody
    reads. So for anything with a port, the port is the answer that counts.
    """
    port = spec.get("port")
    deadline = time.time() + wait_s
    seen_proc = False
    while time.time() < deadline:
        got = _ps(f"(Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
                  f"Where-Object {{ $_.CommandLine -like '{spec['match']}' -and "
                  f"$_.CommandLine -notlike '*Get-CimInstance*' }} | "
                  f"Measure-Object).Count")
        if (got or "0").strip() not in ("", "0"):
            seen_proc = True
            if not port:
                return True
            listening = _ps(f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                            f"-ErrorAction SilentlyContinue | Measure-Object).Count")
            if (listening or "0").strip() not in ("", "0"):
                return True
        time.sleep(1.5)
    if seen_proc and port:
        print(f"         (a {name} process exists but nothing is listening "
              f"on {port} - it is probably exiting on a bind error)")
    return False


def sync_local(a) -> None:
    """Restart anything here that is running code older than the repo.

    🚨 "IT ALL SHOULD MATCH REPO" INCLUDES THIS MACHINE. Deploying to the box
    and leaving the local hub on last night's core.py is the same drift in a
    different place, and harder to notice because nothing serves the public
    from here.

    ⚠️ THE CAMERA STACK IS OPT-IN. run_live and camctl own the USB capture
    graph, and restarting them can wedge the C920 into needing a physical
    replug. Doing that automatically, from a deploy, while nobody is standing
    at the camera, would turn a routine push into a dead camera. So they are
    REPORTED by default and restarted only with --restart-camera - not skipped
    quietly, which is the failure this rule exists to prevent.
    """
    r = _run([sys.executable, str(ROOT / "tools" / "check_running_code.py")])
    stale = [ln.split()[0] for ln in (r.stdout or "").splitlines()
             if "STALE" in ln]
    if not stale:
        say(" ok ", "everything here already runs the current code")
        return
    for name in stale:
        spec = LOCAL.get(name)
        if not spec:
            say("info", f"{name} is stale (not managed here - restart it yourself)")
            continue
        if spec["camera"] and not a.restart_camera:
            say("warn", f"{name} is STALE - owns the camera, so it needs "
                        f"--restart-camera (do it when you can reach the camera)")
            continue
        args = spec["args"]
        if spec.get("bat"):
            # Stop it, then let its own launcher start it again. The launcher
            # holds the credentials and the flags; this only decides WHEN.
            _ps(f"Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
                f"Where-Object {{ $_.CommandLine -like '{spec['match']}' -and "
                f"$_.CommandLine -notlike '*Get-CimInstance*' }} | "
                f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
            _ps(f"Start-Sleep -Seconds 2; Start-Process -FilePath 'cmd.exe' "
                f"-ArgumentList '/c','{spec['bat']}' -WindowStyle Minimized")
            if not _came_up(name, spec):
                say("fail", f"{name} was stopped and did NOT come back - "
                            f"the 5-minute watchdog will retry, or run "
                            f"{spec['bat']} yourself")
            else:
                say(" ok ", f"restarted {name} (verified running)")
            continue
        # 🚨 RESTART WITH THE INTERPRETER IT WAS ALREADY USING, NOT OURS.
        #
        # This used `sys.executable` - the python running THIS script - and it
        # took the local hub down twice. The services run under
        # D:\LLM\.venv\Scripts\python.exe; deploy.py was invoked with the
        # system python. So the stop worked, the start ran hub.py under an
        # interpreter without the dependencies, it died immediately, and the
        # deploy reported "did NOT come back" with no clue as to why.
        #
        # The running process already knows the right answer, so ask it before
        # stopping it. sys.executable stays only as the fallback for a service
        # that was not running to begin with.
        cmd = _ps(f"(Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
                  f"Where-Object {{ $_.CommandLine -like '{spec['match']}' -and "
                  f"$_.CommandLine -notlike '*Get-CimInstance*' }} | "
                  f"Select-Object -First 1).CommandLine")
        exe = sys.executable
        if cmd:
            head = cmd.split(".exe", 1)[0] + ".exe"
            exe = head.strip().strip('"')
        if args is None:      # the detector carries its node token in argv
            if not cmd:
                say("info", f"{name} not running")
                continue
            args = cmd.split(".exe", 1)[1].strip()
        # STOP FIRST. The launcher refuses a second detector for the same node,
        # so start-then-stop leaves the OLD one running and looks like success.
        _ps(f"Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
            f"Where-Object {{ $_.CommandLine -like '{spec['match']}' -and "
            f"$_.CommandLine -notlike '*Get-CimInstance*' }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
        _ps("Start-Sleep -Seconds 3; Start-Process -FilePath "
            f"'{exe}' -ArgumentList '{args}' "
            f"-WorkingDirectory '{ROOT}' -WindowStyle Minimized")
        # 🚨 ISSUING THE COMMAND IS NOT THE SAME AS THE THING RUNNING, AND THIS
        # LINE USED TO CLAIM OTHERWISE.
        #
        # It printed "restarted hub.py" unconditionally, straight after
        # Start-Process. Observed live: the deploy reported "✅ restarted
        # hub.py" and the local hub was NOT RUNNING afterwards - the old
        # process had been force-stopped and nothing replaced it, so :8150 was
        # simply dead and the deploy said it had succeeded. A stop that works
        # and a start that does not is strictly worse than doing nothing, and
        # reporting it as success is how it stays unnoticed.
        #
        # Step 7 already refuses to believe the BOX is up without asking it.
        # This machine gets the same treatment: wait for the process, and for
        # anything that listens, wait for the port to answer.
        if not _came_up(name, spec):
            say("fail", f"{name} was stopped and did NOT come back - "
                        f"start it yourself and check the window for the error")
        else:
            say(" ok ", f"restarted {name} (verified running)")

    check = _run([sys.executable, str(ROOT / "tools" / "check_running_code.py")])
    if "up to date" in (check.stdout or ""):
        say(" ok ", "verified: everything here matches the source")
    else:
        left = [ln.split()[0] for ln in (check.stdout or "").splitlines()
                if "STALE" in ln]
        say("info", "still stale: " + ", ".join(left))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--restart-camera", action="store_true",
                    help="also restart run_live/camctl here (they own the "
                         "camera; do it when you can reach it)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="only for deploying a revert in an emergency")
    a = ap.parse_args()

    if not BOX or not KEY:
        sys.exit("set SPARROW_BOX and SPARROW_KEY (the address is not in this repo)")

    print("1. local state")
    dirty = [l for l in git("status", "--porcelain").splitlines()
             if not l.endswith(".bak")]
    if dirty:
        say("fail", f"{len(dirty)} uncommitted change(s) - commit or stash first")
        for l in dirty[:5]:
            print(f"         {l}")
    else:
        say(" ok ", "working tree clean")
    git("fetch", "--quiet", "origin")
    ahead = git("rev-list", "--count", "origin/main..HEAD")
    behind = git("rev-list", "--count", "HEAD..origin/main")
    if ahead and ahead != "0":
        say("fail", f"{ahead} commit(s) not pushed - the box can only get what "
                    f"origin has")
    elif behind and behind != "0":
        say("fail", f"local is {behind} behind origin/main - pull first")
    else:
        say(" ok ", f"in sync with origin/main ({git('rev-parse', '--short', 'HEAD')})")

    if not a.skip_preflight:
        print("\n2. preflight")
        r = _run([sys.executable, str(ROOT / "tools" / "preflight.py")])
        if r.returncode == 0:
            say(" ok ", "preflight passed")
        else:
            say("fail", "preflight FAILED - not deploying")
            print(r.stdout[-800:])

    if not ok:
        sys.exit("\n⛔ refusing to deploy")

    print("\n3. box state before")
    before = ssh(f"cd {REMOTE} && sudo -u sparrow git rev-parse --short HEAD").stdout.strip()
    say("info", f"box HEAD {before or '?'}")
    target = git("rev-parse", "--short", "HEAD")
    if before == target:
        say(" ok ", "box already at this commit")
    # 🚨 THE FETCH AND THE DIFF USED TO BE ONE `A && B`, AND THAT SHIPPED STALE
    # CODE TO PRODUCTION ON 2026-09-02.
    #
    # When the fetch fails, `&&` short-circuits, stdout is empty, and `changed`
    # is []. An empty list does not read as "I could not find out" - it reads as
    # "nothing changed", so `needs_restart` came out False and step 6 skipped
    # the restart. The deploy then reported success at every step: the pull
    # worked, the box tree matched origin/main, the site answered 200. Only the
    # PROCESS was old. `grep` found the fix in snapshot.py on disk while `ps`
    # showed the hub had been up 15h43m - serving the fix from disk and the bug
    # from memory.
    #
    # (What made the fetch fail is worth knowing too: GitHub answered
    # `www-authenticate: Basic realm="GitHub"` to this box's protocol-v2
    # negotiation on a PUBLIC repo. Pinned with `git config --local
    # protocol.version 0` in /opt/sparrowmap. Anonymous curl of the same
    # info/refs endpoint returned 200 throughout, which is what proved it was
    # neither permissions nor the network.)
    #
    # 📌 THE RULE: a check that cannot answer must not answer "no". Fetch and
    # diff are now separate, both exit codes are looked at, and an unknown
    # answer means RESTART - the wrong guess there costs a few seconds of
    # downtime, while the other wrong guess costs a silently stale server.
    via_bundle = False
    fetched = ssh(f"cd {REMOTE} && sudo -u sparrow git fetch origin 2>&1")
    if fetched.returncode != 0:
        say("info", "the box could not fetch from GitHub - falling back to "
                    "delivering the objects over SSH")
        full_head = ssh(f"cd {REMOTE} && sudo -u sparrow "
                        f"git rev-parse HEAD").stdout.strip()
        if not full_head or not deliver_by_bundle(full_head):
            say("fail", "the box could not get the new commits at all - it "
                        "cannot know what is about to change, so this deploy "
                        "is not safe")
            print((fetched.stdout or "").strip()[-400:])
            sys.exit("\n⛔ refusing to deploy")
        via_bundle = True
        # Point the box's own origin/main at what the bundle carried, so every
        # check below (and the ff-only merge) compares against the same thing it
        # always did rather than a special case.
        upd = ssh(f"cd {REMOTE} && sudo -u sparrow git fetch {BUNDLE_REMOTE} "
                  f"refs/heads/main:refs/remotes/origin/main 2>&1")
        if upd.returncode != 0:
            say("fail", "the bundle verified but would not fetch: "
                        + (upd.stdout or "")[-300:])
            sys.exit("\n⛔ refusing to deploy")
    r = ssh(f"cd {REMOTE} && sudo -u sparrow git diff --name-only HEAD origin/main")
    if r.returncode != 0:
        say("info", "could not list what will change - assuming a restart is "
                    "needed rather than assuming it is not")
        changed, needs_restart = [], True
    else:
        changed = r.stdout.split()
        needs_restart = any(f.endswith(RESTART_TRIGGERS) for f in changed)
        if changed:
            say("info", f"{len(changed)} file(s) will change: "
                        + ", ".join(changed[:6]) + ("…" if len(changed) > 6 else ""))
        else:
            say("info", "no file differences against origin/main")
    say("info", "restart needed: " + ("YES (python changed)" if needs_restart
                                      else "no (static files only)"))

    if a.dry_run:
        print("\ndry run - nothing sent.")
        return

    print("\n4. pull (--ff-only: cannot silently rewrite the box)")
    # ⚠️ WHEN THE OBJECTS CAME BY BUNDLE, DO NOT REACH FOR GITHUB AGAIN.
    # `git pull origin main` re-runs the very fetch that just failed. The box
    # already has the commits and its own origin/main already points at them, so
    # the merge is purely local - and it is still --ff-only, so it still cannot
    # rewrite anything.
    if via_bundle:
        r = ssh(f"cd {REMOTE} && sudo -u sparrow "
                f"git merge --ff-only origin/main 2>&1")
    else:
        r = ssh(f"cd {REMOTE} && sudo -u sparrow git pull --ff-only origin main 2>&1")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or "error" in out.lower() or "fatal" in out.lower():
        print(out[-600:])
        sys.exit("\n⛔ the box could not fast-forward. It has diverged - go and "
                 "look BEFORE forcing anything.")
    say(" ok ", out.splitlines()[-1] if out else "up to date")

    # Don't leave a copy of the repository's objects sitting in /tmp. It is not
    # a secret - the repo is public - but a deploy artefact that outlives the
    # deploy is how a later run ends up merging a stale one.
    if via_bundle:
        ssh(f"rm -f {BUNDLE_REMOTE}")

    print("\n5. verify the box matches origin/main exactly")
    head = ssh(f"cd {REMOTE} && sudo -u sparrow git rev-parse --short HEAD").stdout.strip()
    drift = ssh(f"cd {REMOTE} && sudo -u sparrow git diff --stat origin/main").stdout.strip()
    say(" ok " if head == target else "fail", f"box HEAD {head} (want {target})")
    say(" ok " if not drift else "fail",
        "tree matches origin/main" if not drift else f"TREE DIFFERS:\n{drift[:400]}")

    if needs_restart and not a.no_restart:
        print("\n6. restart (python changed, so the running process is stale)")
        r = ssh(f"systemctl restart {SERVICE} && sleep 4 && systemctl is-active {SERVICE}")
        say(" ok " if "active" in r.stdout else "fail", r.stdout.strip() or "no reply")
    else:
        print("\n6. restart")
        say("skip", "not needed" if not needs_restart else "--no-restart given")

    # 🚨 ASK THE PROCESS HOW OLD IT IS, BECAUSE EVERY OTHER CHECK HERE PASSES
    # WHEN THE SERVER IS STALE.
    #
    # Steps 5 and 7 look at the FILES and at whether the site answers. Both were
    # green on 2026-09-02 while the hub had been running for 15h43m against code
    # from the previous commit. Nothing in a green deploy distinguished "the fix
    # is live" from "the fix is on disk and nobody has read it" - which is the
    # only thing a deploy is FOR.
    #
    # So this is not another restart, it is the audit: if the process is older
    # than the commit it is supposed to be running, say so loudly. It runs
    # whether or not a restart was thought necessary, because the case that hurt
    # is exactly the one where the tool decided it was not.
    print("\n6b. is the RUNNING process actually the new code?")
    age = ssh("systemctl show -p ActiveEnterTimestampMonotonic --value "
              f"{SERVICE}; awk '{{print $1}}' /proc/uptime")
    try:
        started_mono, uptime = age.stdout.split()
        # Both are monotonic seconds since boot, so the difference is how long
        # the unit has been up without any clock or timezone in the way.
        running_for = float(uptime) - float(started_mono) / 1_000_000
    except Exception:
        say("info", "could not read the service start time - check it by hand")
        running_for = None
    if running_for is not None:
        if not needs_restart:
            say("info", f"{SERVICE} has been up {running_for / 3600:.1f}h "
                        f"(no python changed, so that is expected)")
        elif running_for > 120:
            say("fail", f"{SERVICE} has been up {running_for / 3600:.1f}h but "
                        f"python changed in this deploy - THE FIX IS ON DISK "
                        f"AND NOT RUNNING. Restart it.")
        else:
            say(" ok ", f"{SERVICE} restarted {running_for:.0f}s ago, so it "
                        f"loaded this commit")

    print("\n7. is it actually up?")
    import json
    import urllib.request
    for path in ("/", "/api/stats"):
        try:
            req = urllib.request.Request(SITE + path,
                                         headers={"User-Agent": "SparrowMap/deploy"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                say(" ok " if resp.status == 200 else "fail",
                    f"{path} -> {resp.status}")
                if path == "/api/stats":
                    s = json.loads(resp.read())
                    say("info", f"{s.get('nodes_online')}/{s.get('nodes_active')} "
                                f"cameras online, {s.get('sightings_24h')} sightings/24h")
        except Exception as exc:
            say("fail", f"{path} -> {exc}")

    print("\n8. this machine")
    sync_local(a)

    print()
    print("  ✅ deployed" if ok else "  ⛔ DEPLOY FINISHED WITH FAILURES - check above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
