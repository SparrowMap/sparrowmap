"""Phase 2: score the public mirror's phone crops on the home node, then publish.

A public phone contributor's crop lands on the mirror, which cannot tell a
patrol car from a minivan - it has no GPU. The mirror parks the crop, already
destroyed below plate legibility, in its inbox (mirror.quarantine_write). This
runs on the HOME node, where CLIP lives:

    pull the mirror's inbox  ->  CLIP on each crop
      clearly MARKED law enforcement  ->  publish on the public map
      anything else                   ->  discard
    delete every pulled crop from the mirror either way

WHY MARKED-ONLY, AND NOT THE TRAINED HEAD.
⚠️ WRITTEN WHEN THE HEAD ANSWERED IS-IT-GOVERNMENT. Since 2026-09-02 it is
fitted police-only (train/fit_local.POSITIVE), which removes the buses, fire
trucks, ambulances and works trucks that used to reach the pen - they were
correct answers to the wrong question. The Escalade argument below is about
UNMARKED vehicles and still stands unchanged.
The head answered is-it-government, trained on the home camera's catches. It
learned to fire on unmarked black SUVs - it scored a civilian Cadillac Escalade
0.98 and put it on the public map. Measured on the real crops: an Escalade
false-positive reached CLIP police conf 0.923, HIGHER than two genuine patrol
cars - so neither the head nor a loose CLIP threshold can separate marked from
unmarked. What does separate is the strict zero-shot gate whose prompts are all
marked-specific (light bar, door decals, emergency lights): at conf >= 0.96 and
margin >= 0.90 it sits just above every Escalade in the set and admits only
clearly-marked units (about one crop in 335 of ordinary traffic, all genuinely
marked). Low recall, high precision - correct for a pipeline that publishes with
no human in the loop. Unmarked government vehicles are simply not a target; a
photograph of a black SUV is a photograph of somebody's neighbour.

The mirror grows no HTTP surface for this: crops are pulled and the verdict is
applied over the existing SSH admin channel, so a mirror breach still yields
only the public record. Wrong calls are retracted after the fact with
tools/box_retract.py.

    # against your mirror, over ssh:
    python -m detect.box_puller --once \
      --box root@your-mirror-host --key ~/.ssh/your_key
    # against a local staging mirror's inbox (no ssh):
    python -m detect.box_puller --once --inbox /path/to/mirror/data/inbox
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detect import bank as _bank                  # noqa: E402
from detect.vehicle_id import VehicleIdentifier    # noqa: E402

REMOTE_PY = "/opt/sparrowmap/.venv/bin/python"
REMOTE_PUBLISH = "/opt/sparrowmap/tools/box_publish.py"

# 🪟 NO POPUP WINDOW. On Windows every subprocess that launches a console child
# (ssh, tar, a nested python) flashes a console window unless told not to. The
# puller runs on a loop and peeks-then-pulls over ssh each cycle, so without
# this a CMD window blinks on his desktop every pass - reported as annoying, and
# rightly. CREATE_NO_WINDOW exists only on Windows; getattr keeps this a no-op
# everywhere else.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# The marked-law-enforcement gate. These classes' prompts are all marked-
# specific (see vehicle_id.CLASSES); the thresholds are measured to sit above
# every unmarked false positive in the ground-truth set. Deliberately strict:
# this publishes with no human review.
# 🚨 POLICE ONLY. `gov_dot` USED TO BE HERE AND IT PUBLISHED BIN LORRIES.
#
# The zero-shot prompts behind gov_dot are "a municipal works truck with warning
# lights", "a government utility vehicle with high visibility markings", "a snow
# plow or road maintenance truck". A yellow commercial refuse truck matches every
# one of those on sight, and three of them published at 0.97-0.98 with margins
# above 0.94 - the strict gate did not help, because the model was not uncertain,
# it was confident and wrong about WHOSE truck it was.
#
# Municipal and commercial heavy vehicles are not visually separable without
# reading the door, which is exactly the judgement a person makes and a
# zero-shot prompt cannot. So gov_dot now goes to review like everything else,
# and nothing publishes unattended except a clearly-marked patrol car.
MARKED_CLASSES = {"police"}
MIN_CONF, MIN_MARGIN = 0.96, 0.90


# --------------------------------------------------------------------------
# Pulling: either a local staging dir, or the live box over ssh.
# --------------------------------------------------------------------------
class BoxUnreachable(RuntimeError):
    """The inbox could not be read at all.

    Deliberately distinct from "the inbox was empty". Those two states produced
    identical output and identical exit codes, so nothing automated could tell a
    broken puller from a quiet network - and the puller is the ONLY route a
    phone contributor's crop has to a human.
    """


def pull(args) -> tuple[Path, bool]:
    """Return (dir holding the crops, is_local).

    A remote inbox is streamed as a single tar over ssh - not scp'd file by
    file. scp -r opens a fresh round trip per file, and against a high-latency
    box a full inbox (hundreds of tiny files) blows past any timeout while
    transferring only a few hundred KB. One tar = one round trip.
    """
    if args.inbox:
        return Path(args.inbox), True
    tmp = Path(tempfile.mkdtemp(prefix="box_inbox_"))
    # 🚨 "COULD NOT REACH THE BOX" AND "THE BOX HAS NOTHING" LOOKED IDENTICAL.
    #
    # The exit code was never checked and the remote command ends `|| true`, so
    # any ssh failure - host down, key rejected, network gone - returned an
    # empty directory that reads exactly like a healthy empty inbox. The puller
    # then reported success and exited 0. Meanwhile mirror._prune_inbox deletes
    # every phone contributor's crop after 12 hours, so a quiet failure here
    # destroys their contributions unread: the sighting rows still land, but the
    # crop, its scoring and its only route to review go dark.
    #
    # `|| true` stays - it is what stops an EMPTY inbox looking like an error -
    # so the ssh exit code alone cannot distinguish the two. What can: ssh
    # returns 255 for its own failures, and a reachable host with an empty
    # directory still produces a valid (tiny) tar on stdout.
    try:
        proc = subprocess.run(
            ["ssh", "-i", args.key, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", args.box,
             f"tar -C {args.remote} -cf - . 2>/dev/null || true"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
            creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        # 🚨 A STALLED SSH IS "COULD NOT REACH THE BOX THIS CYCLE", NOT A CRASH.
        # His uplink blips, and a hung ssh raised TimeoutExpired straight past
        # the loop's BoxUnreachable handler and KILLED the puller - which then
        # stayed dead while the box's 12h inbox TTL deleted crops unread. The
        # remote tar itself takes ~0.1s; the timeout only ever fires on the
        # link, so convert it to the recoverable error the loop already retries.
        raise BoxUnreachable(f"ssh to {args.box} timed out reading the inbox")
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise BoxUnreachable(
            f"could not read the inbox from {args.box} "
            f"(ssh exit {proc.returncode}): {err or 'no output'}")
    try:
        subprocess.run(["tar", "-C", str(tmp), "-xf", "-"],
                       input=proc.stdout, capture_output=True, timeout=60,
                       check=True, creationflags=_NO_WINDOW)
    except Exception as exc:                                  # noqa: BLE001
        # An unreadable archive is also not an empty inbox.
        raise BoxUnreachable(f"inbox transferred but could not be extracted: {exc}")
    return tmp, False


# --------------------------------------------------------------------------
# Applying the verdict: publish marked crops, discard the rest, and clear every
# processed crop from the mirror.
# --------------------------------------------------------------------------
def apply_verdict(args, publish: list[dict], review: list[dict],
                  discard: list[int], local_dir: Path, is_local: bool) -> dict:
    if not publish and not review and not discard:
        return {"ok": True, "published": 0, "reviewed": 0, "discarded": 0,
                "errors": []}
    payload = json.dumps({"publish": publish, "review": review,
                          "discard": discard})

    if is_local:
        repo = Path(local_dir).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(repo / "tools" / "box_publish.py")],
            input=payload, capture_output=True, text=True, timeout=120,
            cwd=str(repo), creationflags=_NO_WINDOW)
    else:
        proc = subprocess.run(
            ["ssh", "-i", args.key, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", args.box,
             REMOTE_PY + " " + REMOTE_PUBLISH],
            input=payload, capture_output=True, text=True, timeout=180,
            creationflags=_NO_WINDOW)

    out = (proc.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {
            "ok": False, "error": "no output", "stderr": proc.stderr}
    except Exception:
        return {"ok": False, "error": "unparseable output", "raw": out,
                "stderr": proc.stderr}


def run_once(vid: VehicleIdentifier, args) -> dict:
    import cv2
    import numpy as np

    src, is_local = pull(args)
    metas = sorted(src.glob("*.json"))
    if args.limit:
        metas = metas[:args.limit]

    publish, review, discard = [], [], []
    # Crops that arrived corrupt. Counted, never destroyed - see below.
    undecodable = 0
    for jm in metas:
        stem = jm.stem
        jpg_path = jm.with_suffix(".jpg")
        if not jpg_path.exists():
            continue
        try:
            meta = json.loads(jm.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            sid = int(meta.get("sighting_id") or stem)
        except (TypeError, ValueError):
            continue

        img = cv2.imdecode(np.frombuffer(jpg_path.read_bytes(), np.uint8),
                           cv2.IMREAD_COLOR)
        if img is None:
            # ⚠️ DO NOT DISCARD. `discard` is permanent - box_publish.reject_one
            # unlinks the crop on the box - so a truncated transfer would
            # destroy a perfectly good contribution and report it as "the head
            # said ordinary". Leaving it alone costs one re-pull next cycle;
            # discarding costs the contribution and the evidence that anything
            # went wrong.
            undecodable += 1
            continue

        r = vid.classify(img)
        call = VehicleIdentifier.gov_call(r)
        head_pos = call.get("source") == "head" and call.get("gov")
        hc = call["conf"] if call.get("source") == "head" else None
        # 🚨 NOTHING AUTO-PUBLISHES WITHOUT THE TRAINED HEAD AGREEING (his call).
        # `marked` used to be CLIP alone: argmax in MARKED_CLASSES over a
        # confidence and margin bar. That published a stranger's vehicle to the
        # public tier on a zero-shot guess, with no human and no head - and
        # CLIP's argmax alone calls a large fraction of ordinary traffic police.
        #
        # Now the head must have RULED and ruled positive. If the head did not
        # speak at all, this does not fall back to publishing: it declines, and
        # the crop goes to a human instead. "No opinion" is not consent to
        # publish. That is the whole difference between a queue that wastes a
        # reviewer's time and a map that names an innocent driver.
        clip_marked = (r["vclass"] in MARKED_CLASSES
                       and r["conf"] >= MIN_CONF and r["margin"] >= MIN_MARGIN)
        marked = bool(clip_marked and head_pos)

        # 🚨 WHEN THE HEAD HAS RULED, THE HEAD DECIDES.
        # This was `r["vclass"] in MARKED_CLASSES or head_pos`, and the `or`
        # let CLIP's bare argmax overrule a head REJECTION. The head still ran,
        # its number was still written into the `why` string a human reads -
        # it just was not allowed to say no. 477 of the 485 items sitting in the
        # live review pen came in that way, from one browser contributor, with
        # CLIP at ~0.50 and the head at ~0.00: the queue filled with cars the
        # model had already dismissed, which reads to a human as the classifier
        # calling civilians police.
        #
        # CLIP's argmax alone calls a large fraction of ordinary traffic police;
        # rejecting those is the entire job the trained head exists to do. So
        # honour it whenever it spoke, and fall back to the zero-shot gate only
        # when it did not. Same rule hub's own review queue already applies.
        #
        # Nothing is lost by declining: every pulled crop is banked as training
        # data above, BEFORE this decision, and the ones a head rejects are the
        # hard negatives that improve it most.
        if call.get("source") == "head":
            candidate = bool(head_pos)
        else:
            candidate = r["vclass"] in MARKED_CLASSES

        # Store EVERY pulled crop as training data for the head. It is already
        # plate-illegible (sub-resolution), and banking it with the CLIP/head
        # features it was scored on is how the model gets better the longer the
        # network runs - especially the ones a human later calls civilian, which
        # are the hard negatives (the black-SUV false positives) it needs most.
        # Unlabelled here; a review verdict or a labelling pass sets the label.
        try:
            banked = _bank.bank_remote(
                jpg_path.read_bytes(), meta.get("node_name") or "remote",
                {"ts": meta.get("ts"), "cls_name": meta.get("body") or "car",
                 "det_conf": meta.get("det_conf")})
            if banked:
                sc = _bank.sidecar(banked)
                if sc and sc.exists():
                    d = json.loads(sc.read_text(encoding="utf-8"))
                    d["clip"] = {"vclass": r["vclass"], "conf": r["conf"],
                                 "margin": r["margin"], "scores": r["scores"],
                                 "head_conf": hc, "head_gov": bool(head_pos)}
                    d["sighting_id"] = sid    # so a review verdict can label it
                    sc.write_text(json.dumps(d, indent=1), encoding="utf-8")
        except Exception:
            pass          # banking is a bonus; never fail the pull for it

        # 🚨 NOTHING REACHES THE PUBLIC MAP WITHOUT A PERSON (his call, under
        # press scrutiny). `marked` used to auto-publish a contributor's crop
        # the moment CLIP called it clearly-marked and the head agreed. Two
        # models agreeing is a strong signal and it is still not a human, and
        # the project's public position is that a person decides.
        #
        # So `marked` no longer publishes; it raises the crop to the FRONT of
        # the review queue as a strong candidate. The whole difference is who
        # presses the button, and the delay is one review click.
        #
        # Kept as a separate flag rather than folded into `candidate` because
        # the reviewer should be able to see which ones both models liked.
        if marked or candidate:
            vclass = "police" if r["vclass"] == "police" else "gov"
            review.append({
                "id": sid, "vclass": vclass, "head_conf": hc,
                # Carry the operating point the score was judged against, so a
                # pen item is self-describing. The public box cannot load the
                # head at all (no numpy there), so it can never work this out
                # for itself - and a threshold it has to guess is a threshold
                # that drifts away from the model that set it.
                "head_threshold": call.get("threshold")
                if call.get("source") == "head" else None,
                "node_name": meta.get("node_name"),
                # Surfaced so the reviewer can see at a glance which candidates
                # both CLIP and the head called clearly-marked - the ones that
                # used to publish themselves.
                "strong": bool(marked),
                # 🚨 CARRY THE CROP TO THE PEN. The box moves its own inbox
                # copy into the review pen - but mirror._prune_inbox deletes
                # inbox crops after 12h, so if this puller was delayed (a
                # backlog, an outage) the box copy is already gone when the
                # verdict lands, and box_publish.review_one would park a
                # crop-less card the reviewer can never see. Sending the
                # sub-resolution, plate-less crop we just scored makes the pen
                # image independent of whether the box copy survived the wait.
                "crop_b64": base64.b64encode(jpg_path.read_bytes()).decode("ascii"),
                "why": (f"contributor ({meta.get('node_name') or 'a phone'}); "
                        + ("clearly-marked " if marked else "")
                        + f"{r['vclass']} conf {r['conf']:.2f}"
                        + (f", head {hc:.2f}" if hc is not None else "")
                        + " - needs a human")})
            tag = "  -> REVIEW (strong)" if marked else "  -> REVIEW"
        else:
            discard.append(sid)
            tag = ""

        print(f"  #{sid}: {r['vclass']} conf={r['conf']:.2f} "
              f"margin={r['margin']:.2f}"
              f"{f' head={hc:.2f}' if hc is not None else ''}{tag}"
              f"{'  (dry)' if args.dry_run else ''}")

    if args.dry_run:
        return {"pulled": len(metas), "marked": len(publish),
                "review": len(review), "discarded": len(discard),
                "undecodable": undecodable, "dry": True}

    res = apply_verdict(args, publish, review, discard, src, is_local)
    return {"pulled": len(metas), "marked": len(publish),
            "review": len(review), "discarded": len(discard),
            "undecodable": undecodable, "box": res}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--box", default=None,
                    help="ssh target of the public mirror, e.g. root@your-host "
                         "(required unless --inbox is given)")
    ap.add_argument("--key", default=None,
                    help="ssh identity file for the box (e.g. ~/.ssh/id_ed25519)")
    ap.add_argument("--remote", default="/opt/sparrowmap/data/inbox",
                    help="the mirror's inbox path on the box")
    ap.add_argument("--inbox", default=None,
                    help="read a LOCAL inbox dir instead of ssh (staging)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--idle-max", type=float, default=15.0,
                    help="when the inbox has been empty, back off the poll up to "
                         "this many seconds (snaps back to --interval the instant "
                         "a crop arrives). Cuts idle ssh churn; a live drive is "
                         "untouched. Set equal to --interval to disable.")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N crops this run (0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="score and print, but neither publish nor delete")
    ap.add_argument("--singleton", action="store_true",
                    help="exit quietly if another copy is already running, so a "
                         "scheduled task can double as a supervisor")
    args = ap.parse_args()

    # 🚨 THE LOCK IS WHAT LETS THE 5-MINUTE TASK BECOME A WATCHDOG.
    #
    # This used to run as `--once` every 5 minutes, which meant a drive-mode
    # crop waited up to 5 minutes plus a 17.5s cold start before anyone could
    # be told it was a patrol car - and 12.6s of that 17.5 was loading CLIP and
    # the head, paid again on every single run for a job that mostly finds an
    # empty inbox. As a long-running loop the model is loaded once and a cycle
    # costs seconds.
    #
    # A loop needs supervising, though, and the Task Scheduler entry that used
    # to DO the work is already a perfectly good supervisor: keep it firing
    # every 5 minutes, and let the second copy exit here on a held port. If the
    # loop is alive nothing happens; if it died, the next tick restarts it. No
    # new moving parts, and the failure mode is "back within 5 minutes" rather
    # than "off until somebody notices".
    #
    # AN OS FILE LOCK, NOT A PORT, AND NOT A PIDFILE.
    #
    # 🚨 A PORT WAS THE FIRST ATTEMPT AND IT FAILED ON THE FIRST REAL TEST.
    # 8799 was already held by youtube_stream.py on this machine, so the loop
    # bound nothing and took its own "someone else is running" branch: silently
    # dead, which is worse than the 5-minute cold start it replaced. A port
    # cannot tell "another copy of me" from "an unrelated program", and the
    # quiet exit that makes a supervisor tick cheap is exactly what makes that
    # collision invisible.
    #
    # A pidfile is worse still: it survives a crash and then lies about what is
    # running. An advisory lock on a file this program owns has the property
    # that actually matters - the OS drops it the instant the process dies,
    # whatever killed it - and the file cannot be claimed by anything else.
    _lock = None
    if args.singleton:
        from core import DATA
        DATA.mkdir(parents=True, exist_ok=True)
        _lock = open(DATA / "box_puller.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Not an error and must not look like one: this is the supervisor
            # tick finding a healthy loop, which is the normal case. Exit 0 and
            # say nothing that would fill a log with noise every five minutes.
            return
    if not args.inbox and not (args.box and args.key):
        ap.error("give --inbox <dir> for a local mirror, or both --box and --key "
                 "to pull over ssh")

    # Lean: this reloads a ~600MB CLIP per run and fires every few minutes, but
    # the inbox is empty on most runs. Peek first over a cheap ssh, and skip
    # loading the model when there is nothing to score.
    if args.once and not args.inbox:
        try:
            cnt = subprocess.run(
                ["ssh", "-i", args.key, "-o", "ConnectTimeout=15",
                 "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 args.box, f"ls {args.remote}/*.json 2>/dev/null | wc -l"],
                capture_output=True, text=True, timeout=40,
                creationflags=_NO_WINDOW).stdout.strip()
            if cnt == "0":
                print(f"inbox empty at {time.strftime('%H:%M:%S')}; nothing to do")
                return
        except Exception as exc:                              # noqa: BLE001
            print(f"inbox peek failed ({exc}); proceeding to a full run",
                  file=sys.stderr)

    print(f"marked-law-enforcement gate: classes {sorted(MARKED_CLASSES)}, "
          f"conf >= {MIN_CONF}, margin >= {MIN_MARGIN}")
    print("loading CLIP...")
    vid = VehicleIdentifier()

    def report(s: dict) -> None:
        box = s.get("box") or {}
        extra = ""
        if not s.get("dry") and box:
            extra = (f"; box published {box.get('published', '?')}, "
                     f"queued {box.get('reviewed', '?')} for review, "
                     f"discarded {box.get('discarded', '?')}"
                     + (f", ERRORS {box['errors']}" if box.get("errors") else ""))
        if s.get("undecodable"):
            # Corrupt crops are left on the box to be re-pulled, so a number
            # that keeps growing means the transfer is damaging them.
            extra += f", {s['undecodable']} UNDECODABLE (left on the box)"
        print(f"pulled {s['pulled']}, {s['marked']} publish, "
              f"{s.get('review', 0)} review, "
              f"{s['discarded']} discarded{' (dry run)' if s.get('dry') else ''}"
              f"{extra} at {time.strftime('%H:%M:%S')}")

    # 🚨 AN UNREACHABLE BOX MUST BE LOUD AND MUST NOT EXIT 0.
    # Both modes used to swallow it: a failed pull returned an empty directory,
    # got reported as "pulled 0", and exited 0. Nothing supervising this - cron,
    # a task scheduler, a human reading a log - could tell a dead puller from a
    # quiet night, while the box's 12h inbox TTL quietly deleted the crops it
    # was failing to collect.
    if args.interval and not args.once:
        print(f"polling the box every {args.interval:.0f}s "
              f"(backing off to {args.idle_max:.0f}s when idle); Ctrl-C to stop")
        fails = 0
        idle = 0
        while True:
            try:
                result = run_once(vid, args)
                report(result)
                fails = 0
                # An empty pull means nobody is contributing right now. A crop
                # resets this to 0 immediately, so a live drive always polls at
                # the fast rate - only genuine quiet stretches slow down.
                idle = 0 if result.get("pulled") else idle + 1
            except BoxUnreachable as exc:
                fails += 1
                idle = 0                      # a failure is not a quiet inbox
                print(f"BOX UNREACHABLE ({fails} in a row): {exc}",
                      file=sys.stderr)
                # A blip is not an outage; a run of them is. Keep polling either
                # way - stopping would guarantee the backlog is never collected.
                if fails == 3:
                    print("  the inbox has now been unreadable three times "
                          "running. Crops on the box expire after 12 hours, so "
                          "contributions are being lost while this persists.",
                          file=sys.stderr)
            # 🔌 ADAPTIVE BACKOFF, because SSH multiplexing is impossible here:
            # both ssh clients on Windows (MSYS Cygwin sockets, native named
            # pipes) refuse ControlMaster, so every cycle is a fresh connection.
            # Polling an empty inbox every 2s opened ~10 ssh/min around the
            # clock. Instead, double the wait each consecutive empty cycle up to
            # --idle-max, and snap back to the fast rate the instant a crop
            # lands. Idle connections drop several-fold; a live drive is
            # untouched (its first crop waits at most --idle-max, then fast).
            wait = min(args.idle_max, args.interval * (2 ** min(idle, 6)))
            time.sleep(wait)
    else:
        try:
            report(run_once(vid, args))
        except BoxUnreachable as exc:
            print(f"BOX UNREACHABLE: {exc}", file=sys.stderr)
            raise SystemExit(4)


if __name__ == "__main__":
    main()
