"""Is what is RUNNING the code that is on disk?

2026-08-10. A classifier fix was shipped, the camera node was restarted, and
the node kept running the old code anyway. The launcher has a single-instance
guard: a second detector for the same node refuses to start and says so, then
pauses. If you do not read that window you have a new terminal open, an old
process still running, and every reason to believe the fix is live.

Nothing was broken. The restart simply did not happen, and there was no way to
tell by looking at the map - a stale detector behaves exactly like a fresh one
until it hits the code you changed.

So this compares process start times against source modification times, which
is the one comparison that cannot be fooled by a window that looks right. It is
the same discipline as the rest of this codebase: measure the thing, do not
infer it from something adjacent.

    python tools\\check_running_code.py

Exit code is 1 if anything is stale, so it can gate a deploy step.
"""

from __future__ import annotations

import subprocess
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Which running process cares about which sources. A process is STALE if any
# source it depends on was modified after the process started.
WATCH = {
    "hub.py":      ["hub.py", "classify.py", "db.py", "nodes.py", "privacy.py",
                    "mirror.py", "core.py", "road.py", "snapshot.py"],
    "run_live.py": ["detect/run_live.py", "detect/pipeline.py",
                    "detect/vehicle_id.py", "detect/head.py", "detect/bank.py",
                    "classify.py", "core.py"],
    "camctl.py":   ["camctl/camctl.py", "labelbank.py", "core.py"],
    # 🚨 ADDED WHEN THE PULLER BECAME A LOOP, BECAUSE THAT IS WHAT MADE IT
    # ABLE TO GO STALE. As a `--once` task it re-imported everything on every
    # run and was fresh by construction, so it did not belong here. A process
    # that now stays up for days holds whatever vehicle_id.py said when it
    # started - and it is the only thing standing between a contributor's crop
    # and a human, so a silently stale copy is expensive.
    #
    # ⚠️ THE KEY IS ALSO THE PROCESS MATCH, so it is "box_puller" and NOT
    # "box_puller.py". This is launched as `-m detect.box_puller`, whose command
    # line never contains ".py" - the first version of this entry reported
    # "NOT RUNNING" while the loop was running fine, and the summary line still
    # said everything was up to date. A false all-clear from the tool whose
    # entire job is not giving false all-clears.
    "box_puller":  ["detect/box_puller.py", "detect/vehicle_id.py",
                    "detect/head.py", "detect/bank.py", "core.py"],
}


# 🚨 "hub.py" IS NOT A UNIQUE NAME ON THIS MACHINE (10-27). Three processes have
# it in their command line: this hub (`pythonw hub.py`, :8150), the staging
# mirror (`python hub.py --port 8250`, D:\LLM\sparrow_box) and the Workshop
# (`D:\LLM\workshop\hub.py`, :7800). Matching the name and taking the OLDEST
# could judge THIS hub by another program's start time, and deploy.py would then
# "restart hub.py" by killing all three. A service with a port is identified by
# the port it listens on.
PORTS = {"hub.py": 8150}
# A component whose process may be invisible (elevated) but whose log proves it is alive.
LOGS = {"box_puller": "logs/box_puller.log"}


def _port_owner(port: int) -> str:
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:                              # pragma: no cover
        return ""


def _procs() -> list[dict]:
    """Running python processes, with start time, via PowerShell/CIM."""
    # ⚠️ Uses a literal separator, NOT PowerShell's backtick-t escape. The
    # backtick survives Python and bash and then does not mean what it looks
    # like it means by the time PowerShell parses it, and the whole pipeline
    # silently emits nothing - which this script would have reported as
    # "no python processes found", i.e. a false all-clear from a tool whose
    # entire job is not giving false all-clears.
    SEP = "|~|"
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "ForEach-Object { "
        "  $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "  if ($p) { "
        "    ($_.ProcessId, $p.StartTime.ToString('o'), "
        "     ($_.CommandLine -replace '\\s+',' ')) -join '" + SEP + "' "
        "  } "
        "}"
    )
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60).stdout
    except Exception as exc:                       # pragma: no cover
        print(f"could not enumerate processes: {exc}")
        return []
    out = []
    for line in raw.splitlines():
        parts = line.split(SEP)
        if len(parts) != 3:
            continue
        pid, started, cmd = parts
        out.append({"pid": pid.strip(), "started": started.strip(),
                    "cmd": cmd.strip()})
    return out


def main() -> int:
    import datetime as dt

    procs = _procs()
    if not procs:
        print("no python processes found - is anything running?")
        return 1

    stale = 0
    missing: list = []
    unknown: list = []   # alive, but its code age cannot be read (elevated)
    seen = set()
    for name, sources in WATCH.items():
        # A launcher spawns a stub plus a child with the same command line;
        # both are real, so report the OLDEST, which is the one that would be
        # holding onto old code.
        mine = [p for p in procs if name in p["cmd"]]
        if name in PORTS:
            owner = _port_owner(PORTS[name])
            mine = [p for p in mine if p["pid"] == owner]   # not listening = not running
        if not mine and name in LOGS:
            # 🚨 HIDDEN IS NOT DEAD (10-27). The box_puller task runs ELEVATED, and
            # a non-elevated query reads its command line as empty, so it matched
            # nothing and was reported NOT RUNNING while its log was being written
            # every few seconds. Its log is honest evidence of life. Its CODE AGE
            # cannot be read from here, so say that rather than "current".
            lp = ROOT / LOGS[name]
            age = (time.time() - lp.stat().st_mtime) if lp.exists() else None
            if age is not None and age < 600:
                print(f"  {name:<14} RUNNING (process hidden: elevated) - log written "
                      f"{round(age)}s ago; its code age cannot be checked from here, "
                      f"restart it as admin after changing its sources")
                unknown.append(name)
                continue
        if not mine:
            # 🚨 NOT RUNNING IS A FAILURE, AND THE SUMMARY USED TO SAY OTHERWISE.
            # `stale` counted only components running OLD code, so a component
            # that was not running AT ALL printed "NOT RUNNING" on its own line
            # and then this tool ended with "all running components are up to
            # date" and exit 0. Both statements are technically true and the
            # pair is a lie: it is an all-clear over a dead hub, from the tool
            # whose entire job is not giving false all-clears. Seen live -
            # hub.py was down for an hour behind exactly that line.
            missing.append(name)
            print(f"  {name:<14} NOT RUNNING")
            continue
        mine.sort(key=lambda p: p["started"])
        p = mine[0]
        seen.add(name)
        started = dt.datetime.fromisoformat(p["started"])
        if started.tzinfo:
            started = started.astimezone().replace(tzinfo=None)

        newer = []
        for s in sources:
            f = ROOT / s
            if not f.exists():
                continue
            m = dt.datetime.fromtimestamp(f.stat().st_mtime)
            if m > started:
                newer.append((s, m))

        if newer:
            stale += 1
            print(f"  {name:<14} STALE   pid {p['pid']}, started "
                  f"{started:%Y-%m-%d %H:%M:%S}  ({len(mine)} proc)")
            for s, m in sorted(newer, key=lambda x: -x[1].timestamp()):
                print(f"       modified after it started: {s}  ({m:%H:%M:%S})")
        else:
            print(f"  {name:<14} current pid {p['pid']}, started "
                  f"{started:%Y-%m-%d %H:%M:%S}  ({len(mine)} proc)")

    if stale:
        print(f"\n{stale} component(s) running code older than the files on "
              f"disk. Restart them.")
        print("⚠️ The camera node's launcher REFUSES to start a second "
              "detector for the same node - close the old window first, or "
              "the new one exits and the old one keeps running.")
    if missing:
        print(f"\n{len(missing)} component(s) NOT RUNNING: "
              + ", ".join(missing) + ".")
    if stale or missing:
        return 1
    if unknown:
        print("\nall visible components are up to date; "
              + ", ".join(unknown) + " is running but its code age could not be checked")
        return 0
    print("\nall components are running and up to date with the source on disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
