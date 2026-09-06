"""Pull federal civil-rights (42 USC 1983) dockets into data/oversight.db.

    python tools/courtlistener_fetch.py --states ALL --plan     # size the job
    python tools/courtlistener_fetch.py --state MI --exclude-prisoner
    python tools/courtlistener_fetch.py --state MI              # + prison
    python tools/courtlistener_fetch.py --states ALL            # America
    python tools/courtlistener_fetch.py --resume                # pick up
    python tools/courtlistener_fetch.py --report                # what we hold
    python tools/courtlistener_fetch.py --queue 40              # review queue
    python tools/courtlistener_fetch.py --reclassify            # re-derive

`--states` sweeps one state at a time and SKIPS the ones already finished, so
the national job is just the same command run again until it stops printing
new states. `--state` does one state in this process. Everything is resumable:
the cursor is checkpointed after every page.

--------------------------------------------------------------------------
WHY THIS SOURCE, AND WHY IT IS FIRST
--------------------------------------------------------------------------

An officer accountability database normally starts from a state roster: get the
list of certified officers, make a profile each, hang complaints off them.

**That road is closed in Michigan.** A Michigan Court of Claims ruling lets MSP
and MCOLES withhold the names and employment histories of every certified
officer in the state; the Invisible Institute's National Police Index covers 24
states and lists Michigan under statutory exemptions barring release. The
federal fallback is gone too - DOJ decommissioned NLEAD on 2025-01-24.

So the roster is not the starting point. **Federal court records are**, because
they are federal, and no state FOIA exemption touches them. A section 1983 suit
names the officer, the agency, the date and the allegation, in a document
anybody can pull up and read. In Michigan's two federal districts that is
11,878 cases - 3,950 of them non-prisoner - measured 2026-09-05.

--------------------------------------------------------------------------
WHAT THIS TOOL IS ALLOWED TO DO
--------------------------------------------------------------------------

Fill `sources`, `cases` and `case_parties`. That is the whole mandate.

🚨 IT CANNOT CREATE AN OFFICER AND IT CANNOT CREATE AN ALLEGATION. There is no
code path here that writes to those tables. A party string is stored with a
guess about what it looks like, and a human turns guesses into identifications
through the review surface, which logs who did it.

The reason is the failure mode that would end the project: an automatic name
match writes "Officer Smith" onto a profile, the profile renders it as a
sentence, and the site has published a factual assertion about a real person
that nobody approved. SparrowMap has already been bitten by the general form of
this - matching a crop to a sighting by timestamp promoted an unrelated vehicle
to the public map. Weak similarity is a queue, not a write.

--------------------------------------------------------------------------
API NOTES (measured 2026-09-05, not assumed)
--------------------------------------------------------------------------

  * `/api/rest/v4/search/` answers WITHOUT a token. `/dockets/` and
    `/parties/` return 401 without one. So the whole of phase 1 runs on the
    unauthenticated search endpoint, and needs no credentials at all.
  * Search results already carry the party list, the cause, the nature of suit
    and the docket id - which is exactly the set this tool needs.
  * 🚨 USE `type=d`, NOT `type=r`. See SEARCH_TYPE below: `r` walks DOCUMENTS
    and groups them under dockets, which repeats dockets across pages and
    loses others entirely - Michigan came out 1,202 cases short of the API's
    own count and reported success. `d` is one row per docket, returns the
    same count, and carries the same fields including `party`.
  * 8 rapid requests in a row all returned 200, so the anonymous limit is not
    the documented 5/minute. The default 2s delay here is POLITENESS toward a
    small nonprofit, not a measured ceiling. 429 handling exists anyway,
    because an undocumented limit is one that can change without warning.
  * Pagination is cursor-based, 20 results per page. Michigan is 594 pages, so
    a full sweep is a job that WILL be interrupted. The cursor is checkpointed
    to `runs` after every page and `--resume` continues the same sweep.
  * A token (free, courtlistener.com) raises the limits and unlocks the
    /parties/ endpoint for authoritative plaintiff/defendant roles. Set
    COURTLISTENER_TOKEN and it is sent on search; the parties upgrade is a
    later phase and is deliberately not written blind here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import oversight  # noqa: E402

API = "https://www.courtlistener.com/api/rest/v4"
UA = "SparrowMap/0.1 (police accountability research; sparrowmap.com)"
TOOL = "courtlistener_fetch"

# 🚨 `d`, NOT `r`. THIS IS THE ROOT-CAUSE FIX FOR THE LOST CASES.
#
# `type=r` is "dockets with up to three nested documents": the stream walks
# DOCUMENTS and collapses them into docket groups. That is why the same docket
# came back on several pages, why `seen` overstated coverage, and - with a
# filter query where every hit carries an identical relevance score - why the
# cursor both repeated and skipped. Michigan finished 1,202 cases short of the
# API's own count and called it done.
#
# `type=d` is "federal cases (dockets) from PACER, excluding filing metadata":
# ONE ROW PER DOCKET. Verified 2026-09-05 against the same query - it returns
# the same count (3,950), and it still carries `party`, which is the entire
# reason this tool reads search results at all. The field names are identical
# to type=r; only `recap_documents` is absent, and nothing here used it.
#
# Windowing and the distinct-docket coverage check stay ON regardless. This
# removes the known cause; they catch the unknown one.
SEARCH_TYPE = "d"

# EVERY FEDERAL DISTRICT COURT IN AMERICA, by state.
#
# Generated from /api/rest/v4/courts/?jurisdiction=FD&in_use=true on
# 2026-09-05 and checked: 103 of the 105 courts map to a state, all 50 states
# plus DC, Puerto Rico, Guam, the US Virgin Islands and the Northern Mariana
# Islands are present, and nothing is missing. It is a literal rather than a
# live lookup because a state should resolve without a network call.
#
# 🚨 IT IS PARSED, NOT SUBSTRING-MATCHED. The court names are
# "District Court, <division> <state>", so the division prefix is stripped and
# the remainder looked up exactly. Matching state names as substrings puts
# "District Court, N.D. West Virginia" under VIRGINIA - three courts' worth of
# cases filed in the wrong state, silently, in a database whose entire purpose
# is saying which agency an officer worked for.
#
# ⚠️ `in_use=true` DOES NOT MEAN "still operating". The API returns historical
# undivided districts (californiad, illinoisd, indianad, ohiod,
# pennsylvaniad, tennessed, southcarolinaed/wd) alongside the live ones. They
# are kept: they cost no extra requests - the court filter is one query
# whatever its length - and a case is a case. Two entries do not map to a
# state and are dropped: `orld` (District of Orleans, became Louisiana in
# 1812) and `canalzoned` (Canal Zone, abolished 1982). Neither can hold a
# section 1983 case; the statute is from 1871 and the case law from 1961.
STATE_COURTS = {
    "AK": ["akd"],
    "AL": ["almd", "alnd", "alsd"],
    "AR": ["ared", "arwd"],
    "AZ": ["azd"],
    "CA": ["cacd", "caed", "californiad", "cand", "casd"],
    "CO": ["cod"],
    "CT": ["ctd"],
    "DC": ["dcd"],
    "DE": ["ded"],
    "FL": ["flmd", "flnd", "flsd"],
    "GA": ["gamd", "gand", "gasd"],
    "GU": ["gud"],
    "HI": ["hid"],
    "IA": ["iand", "iasd"],
    "ID": ["idd"],
    "IL": ["ilcd", "illinoisd", "illinoised", "ilnd", "ilsd"],
    "IN": ["indianad", "innd", "insd"],
    "KS": ["ksd"],
    "KY": ["kyed", "kywd"],
    "LA": ["laed", "lamd", "lawd"],
    "MA": ["mad"],
    "MD": ["mdd"],
    "ME": ["med"],
    "MI": ["mied", "miwd"],
    "MN": ["mnd"],
    "MO": ["moed", "mowd"],
    "MP": ["nmid"],
    "MS": ["msnd", "mssd"],
    "MT": ["mtd"],
    "NC": ["nced", "ncmd", "ncwd"],
    "ND": ["ndd"],
    "NE": ["ned"],
    "NH": ["nhd"],
    "NJ": ["njd"],
    "NM": ["nmd"],
    "NV": ["nvd"],
    "NY": ["nyed", "nynd", "nysd", "nywd"],
    "OH": ["ohiod", "ohnd", "ohsd"],
    "OK": ["oked", "oknd", "okwd"],
    "OR": ["ord"],
    "PA": ["paed", "pamd", "pawd", "pennsylvaniad"],
    "PR": ["prd"],
    "RI": ["rid"],
    "SC": ["scd", "southcarolinaed", "southcarolinawd"],
    "SD": ["sdd"],
    "TN": ["tennessed", "tned", "tnmd", "tnwd"],
    "TX": ["txed", "txnd", "txsd", "txwd"],
    "UT": ["utd"],
    "VA": ["vaed", "vawd"],
    "VI": ["vid"],
    "VT": ["vtd"],
    "WA": ["waed", "wawd"],
    "WI": ["wied", "wiwd"],
    "WV": ["wvnd", "wvsd"],
    "WY": ["wyd"],
}
ALL_STATES = sorted(STATE_COURTS)


# 🚨 429 HANDLING BELONGS HERE, NOT AT EACH CALL SITE.
#
# It was written into the page loop only, so the paging survived throttling and
# every other request - the window counts, the court list - died on a raw
# traceback the moment the limit was hot. Half a dozen call sites cannot each
# remember to be polite; one transport can.
#
# The waits are long because the ceiling is undocumented and bursty: 8 rapid
# requests never throttled, two concurrent sweeps did. When the answer is "slow
# down", the useful response is to actually slow down.
# ⚠️ THESE ADD UP TO AN HOUR, ON PURPOSE. The limit appears to be a rolling
# window, so once it is exhausted the only cure is time. A national sweep is a
# multi-hour unattended job; giving up after ~15 minutes of retries turns a
# recoverable pause into a dead run that has to be noticed and restarted by a
# human. Waiting is cheaper than being watched.
_BACKOFF = (30, 60, 120, 240, 300, 300, 600, 600, 900, 900)

# 🚨 PACE TO SUSTAINABILITY, NOT TO SPEED.
#
# Party enrichment is inherently a multi-day job - roughly 2,450 pages for
# Michigan and days for the country - and the ceiling is a rolling window, not
# a per-second rate. Bursting at --delay 2 wins for a few hundred pages and
# then buys a lockout that outlasts the whole backoff ladder: measured
# 2026-09-05, the limiter did not forgive a burst across 30/60/120/240/300/
# 300/600/600/900s of waiting.
#
# For a job like this, giving up after an hour is the wrong behaviour - there
# is nobody watching to restart it, and the cursor is already checkpointed.
# --patient makes the last rung repeat forever, so a lockout costs time and
# never costs the run. Combine it with a delay that stays under the ceiling
# rather than discovering the ceiling every few hundred pages.
PATIENT = False


def _get(url: str, token: str = "", timeout: int = 60) -> dict:
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Token {token}"
    last = None
    attempt = 0
    # ⚠️ A `while`, not a `for`. The patient branch has to STAY on the last
    # rung, and `attempt -= 1` inside a for loop does nothing at all - the loop
    # variable is reassigned from the range on every pass, so the decrement is
    # silently discarded and the ladder marches on to SystemExit anyway.
    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 🚨 RETRY 5xx TOO, NOT JUST 429.
            #
            # The first version retried only 429 and re-raised everything else, which killed
            # a --patient overnight run on a single HTTP 502 Bad Gateway - a momentary blip
            # at CourtListener's gateway, gone by the next second, and arguably MORE clearly
            # transient than the throttle it did handle.
            #
            # 4xx stays fatal on purpose: a 400 or 404 is our bug and will still be our bug
            # in fifteen minutes, so retrying it just hides it behind an hour of waiting.
            if e.code != 429 and not (500 <= e.code < 600):
                raise
            last, why = e, ("429 throttled" if e.code == 429
                            else f"HTTP {e.code} upstream")
        except (urllib.error.URLError, TimeoutError) as e:
            # A blip must not end a multi-hour sweep. The cursor is
            # checkpointed either way, but retrying is cheaper than resuming.
            last, why = e, f"network: {e}"
        if attempt < len(_BACKOFF):
            wait = _BACKOFF[attempt]
            attempt += 1
            print(f"    {why} - waiting {wait}s", flush=True)
        elif PATIENT:
            # The ladder is exhausted but the job is not. Hold at the last
            # rung forever rather than throwing away a checkpointed sweep that
            # nobody is watching.
            wait = _BACKOFF[-1]
            print(f"    {why} - patient, holding at {wait}s", flush=True)
        else:
            raise SystemExit(
                f"gave up after {len(_BACKOFF)} retries: {last}\n"
                f"  the cursor is checkpointed - re-run the same command, or "
                f"use --patient for an unattended job")
        time.sleep(wait)


def list_courts(state_word: str) -> None:
    """Print the federal district courts whose name mentions `state_word`.

    The API rejects a name filter, so this pages the 105 in-use federal
    districts and matches locally. Cheap, and it means adding a state is a
    lookup rather than a guess about court ids.
    """
    url = f"{API}/courts/?jurisdiction=FD&in_use=true"
    hits = []
    while url:
        d = _get(url)
        for c in d.get("results", []):
            if state_word.lower() in (c.get("full_name") or "").lower():
                hits.append((c["id"], c["full_name"]))
        url = d.get("next")
        if url:
            time.sleep(1)
    for cid, name in sorted(hits):
        print(f"  {cid:8s} {name}")
    if not hits:
        print(f"  no federal district court matched {state_word!r}")


# 🚨 THE ENRICHMENT QUERY MUST MATCH THE POPULATION BULK LOADED, OR MOST CASES
#    NEVER GET PARTY NAMES.
#
# `cause:(1983)` finds 11,878 cases in Michigan. The same jurisdiction under a
# nature-of-suit query is 62,989 - 5.3x more - because the `cause` field is
# sparsely populated and most police cases are identifiable only by their NOS
# code. Bulk loads both tiers (see bulk_dockets.nos_in_scope), so an enricher
# still asking `cause:(1983)` would leave roughly 96% of the database with no
# officer names in it at all.
#
# ⚠️ The codes here are 440/550/555 ONLY, matching nos_in_scope exactly.
# `suitNature:("Civil Rights")` alone returns 51,742 for Michigan and drags in
# employment, ADA, voting and welfare cases - 45% noise in the queue a human
# has to read.
# ⚠️ BOTH SPELLINGS OF "PRISON CONDITION(S)". PACER writes it singular AND
# plural in the same jurisdiction - Michigan has 4,532 rows of "555 Prisoner -
# prison condition" and 606 of "Prisoner: Prison Conditions". The bulk filter
# matches a substring so it catches both; a Lucene PHRASE does not, so the
# plural rows would have been loaded by bulk and then never enriched with party
# names - present in the database, permanently nameless, and nothing would have
# reported it as a gap.
BROAD_NOS = ('suitNature:(440 OR 550 OR 555 OR "Civil Rights: Other" '
             'OR "Prisoner: Civil Rights" OR "Prison Condition" '
             'OR "Prison Conditions")')


def build_query(exclude_prisoner: bool, extra: str = "",
                broad: bool = False) -> str:
    """The Lucene query. 42:1983 is the cause code PACER stamps on the docket.

    Filtering on `cause` rather than on free text is what makes this precise:
    "excessive force" as a search term also matches a case that merely cites
    one, while cause:(1983) is the court's own classification of what the suit
    IS. Nature-of-suit codes 440/550 are recorded per case for the same reason.
    """
    q = f"(cause:(1983) OR {BROAD_NOS})" if broad else "cause:(1983)"
    if exclude_prisoner:
        # 🚨 This drops roughly two thirds of the Michigan docket, and it is a
        # deliberate ORDERING choice, not a judgement that prison cases matter
        # less. They are a different population against different agencies; a
        # street-policing sweep that silently includes them produces counts
        # nobody can interpret. Run both, tagged - `cases.is_prisoner` keeps
        # them apart in one database.
        q += " NOT suitNature:(Prisoner)"
    if extra:
        q += f" AND ({extra})"
    return q


def run_key(courts: list, query: str, since: str, before: str = "") -> str:
    """The identity of a sweep.

    🚨 EVERYTHING THAT CHANGES THE RESULT SET GOES IN HERE. A cursor is only
    meaningful against the query that produced it, so resuming under a
    different key would continue somebody else's pagination and report success
    over a gap it never fetched. It is also what lets a 55-state job skip the
    states it already finished instead of re-fetching the country.
    """
    d = {"q": query, "court": " ".join(courts), "since": since,
         # The search TYPE changes the result set, so it belongs in the key.
         # Without it, switching r -> d would inherit the runs that were
         # already marked done under the broken enumeration and skip them.
         "type": SEARCH_TYPE}
    if before:
        d["before"] = before
    return json.dumps(d, sort_keys=True)


# --------------------------------------------------------------------------
# 🚨 DEEP PAGINATION LOSES CASES, SILENTLY. THIS IS THE WHOLE REASON FOR
#    WINDOWING, AND IT WAS CAUGHT BY THE COVERAGE LINE ON THE FIRST REAL RUN.
#
# Michigan, non-prisoner: the API said `count` = 3,950. The sweep followed the
# cursor to exhaustion - `next` came back null on a PARTIAL page, so it ended
# naturally, not at any limit we imposed - and had seen 2,748 rows, of which
# ~2,150 were distinct dockets. **1,202 cases short, reported as "done".**
#
# The count is not the thing that is wrong. Splitting the same query at
# 2015-01-01 gives 2,648 + 1,302 = 3,950 exactly, so `count` is precise and
# `filed_before` works. What fails is the cursor: `cause:(1983) NOT
# suitNature:(Prisoner)` is a filter, so essentially every hit carries the SAME
# relevance score, and the cursor encodes that score. Paging through thousands
# of tied rows both repeats and skips.
#
# The fix is to never page deep. Ask the API how many cases fall in a date
# window; if that is more than a window can safely carry, split the window and
# ask again. Each window is then shallow enough to enumerate honestly.
#
# And because `count` is exact PER WINDOW, the sweep can check its own work:
# collect the distinct docket ids actually returned, compare to the count the
# API promised, and if it is still short, split that window and go again. That
# turns silent under-coverage into a self-correcting loop instead of a number
# nobody would have questioned.
WINDOW_MAX = 2000        # cases per window before splitting up front
WINDOW_MIN_DAYS = 1      # a single day is the floor; below that, report it
EPOCH = "1970-01-01"     # section 1983 case law starts 1961; PACER, later


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def count_for(courts: list, query: str, after: str, before: str,
              token: str = "", delay: float = 0.0) -> int:
    """How many cases the API says fall in this window. One request.

    Exact, and verified so: splitting Michigan's non-prisoner query at
    2015-01-01 gave 2,648 + 1,302 = 3,950, the same as the unsplit count.
    """
    p = {"type": SEARCH_TYPE, "q": query, "court": " ".join(courts)}
    if after:
        p["filed_after"] = after
    if before:
        p["filed_before"] = before
    url = f"{API}/search/?" + urllib.parse.urlencode(p)
    n = _get(url, token=token).get("count") or 0
    if delay:
        time.sleep(delay)
    return int(n)


def plan_windows(courts: list, query: str, after: str, before: str,
                 token: str = "", delay: float = 2.0,
                 depth: int = 0) -> list:
    """Split [after, before] until every window is small enough to enumerate.

    Costs one request per split decision, which is nothing against the pages it
    saves - and it is the difference between a sweep that covers its range and
    one that quietly stops 30% short.
    """
    n = count_for(courts, query, after, before, token=token, delay=delay)
    span = (_d(before) - _d(after)).days
    pad = "  " * depth
    if n <= WINDOW_MAX or span <= WINDOW_MIN_DAYS:
        if n > WINDOW_MAX:
            # A single day over the limit. Nothing left to split, so say so
            # rather than pretend - one loud line beats a silent gap.
            print(f"{pad}  ⚠️ {after} alone holds {n:,} cases, more than a "
                  f"window can enumerate; expect a shortfall here")
        print(f"{pad}  window {after} .. {before}  {n:,}")
        return [(after, before, n)]
    mid = _d(after) + dt.timedelta(days=span // 2)
    print(f"{pad}  split {after} .. {before} ({n:,}) at {mid}")
    return (plan_windows(courts, query, after, mid.isoformat(), token, delay,
                         depth + 1)
            + plan_windows(courts, query,
                           (mid + dt.timedelta(days=1)).isoformat(), before,
                           token, delay, depth + 1))


def sweep_range(courts: list, query: str, after: str, before: str,
                delay: float = 2.0, token: str = "", depth: int = 0) -> tuple:
    """Sweep one date window, verify coverage, and split it if it came up short.

    This is the self-correcting half. `plan_windows` guesses a safe size up
    front; this checks the guess against what actually came out and splits
    again when the guess was wrong. A window that cannot be split any further
    reports its shortfall instead of swallowing it.
    """
    got, promised = sweep(courts, query, since=after, before=before,
                          delay=delay, token=token, resume=True)
    if got >= promised or depth >= 8:
        return got, promised
    span = (_d(before) - _d(after)).days
    if span <= WINDOW_MIN_DAYS:
        print(f"  ⚠️ {after} is one day and still {promised - got:,} short - "
              f"cannot split further")
        return got, promised
    mid = _d(after) + dt.timedelta(days=span // 2)
    print(f"  ↳ {after}..{before} came back {promised - got:,} short; "
          f"splitting at {mid}")
    parent = oversight.find_run(TOOL, run_key(courts, query, after, before))
    if parent is not None:
        oversight.update_run(parent["id"], state="split")
        oversight.prune_run_dockets(parent["id"])
    a = sweep_range(courts, query, after, mid.isoformat(), delay, token,
                    depth + 1)
    b = sweep_range(courts, query, (mid + dt.timedelta(days=1)).isoformat(),
                    before, delay, token, depth + 1)
    return a[0] + b[0], promised


def sweep_windowed(courts: list, query: str, after: str = "",
                   before: str = "", delay: float = 2.0,
                   token: str = "") -> tuple:
    """The safe way to sweep a jurisdiction: plan, sweep, verify, split."""
    after = after or EPOCH
    before = before or (dt.date.today() + dt.timedelta(days=1)).isoformat()
    print(f"planning windows for [{' '.join(courts)}] ...")
    windows = plan_windows(courts, query, after, before, token=token,
                           delay=delay)
    promised = sum(w[2] for w in windows)
    # 🚨 NEWEST FIRST, AND IT IS NOT A COSMETIC CHOICE.
    #
    # Party enrichment for one state is thousands of pages and hours of
    # polite fetching; nationally it is days. A job that long WILL be stopped
    # part-way, so the order decides what you have when it stops.
    #
    # An officer sued in 2024 is probably still wearing a badge. One sued in
    # 1994 is a historical record. Working oldest-first means an interruption
    # at 20% leaves the 20% that matters least - and this database exists to
    # answer questions about officers people are meeting today.
    windows.reverse()
    print(f"{len(windows)} window(s), {promised:,} cases promised "
          f"(newest first)\n")
    got = 0
    for i, (a, b, n) in enumerate(windows, 1):
        if n == 0:
            continue
        print(f"[{i}/{len(windows)}] {a} .. {b}  ({n:,})")
        g, _ = sweep_range(courts, query, a, b, delay=delay, token=token)
        got += g
    short = promised - got
    print(f"\ncoverage: {got:,} distinct dockets of {promised:,} promised"
          + (f"  ⚠️ {short:,} SHORT" if short > 0 else "  ✅ complete"))
    return got, promised


def plan(states: list, exclude_prisoner: bool, since: str, extra: str,
         delay: float, token: str, broad: bool = False) -> None:
    """How big is this job? One request per state, no ingest.

    Worth its own mode. A national sweep is days of requests against a small
    nonprofit's API, and starting one without knowing whether it is 200,000
    cases or 2,000,000 is how you find out by being throttled at 3am. 55
    requests buys the whole answer, and the per-state numbers are what decide
    where to point volunteers first.
    """
    query = build_query(exclude_prisoner, extra, broad=broad)
    print(f"query: {query}" + (f"  filed_after={since}" if since else ""))
    print(f"{'st':<4}{'courts':>7}{'cases':>10}   {'done':>6}")
    total = 0
    rows = []
    for st in states:
        courts = STATE_COURTS[st]
        params = {"type": SEARCH_TYPE, "q": query, "court": " ".join(courts)}
        if since:
            params["filed_after"] = since
        url = f"{API}/search/?" + urllib.parse.urlencode(params)
        try:
            n = _get(url, token=token).get("count") or 0
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"{st:<4}  throttled - waiting 60s")
                time.sleep(60)
                n = _get(url, token=token).get("count") or 0
            else:
                raise
        done = oversight.completed_run(TOOL, run_key(courts, query, since))
        total += n
        rows.append((st, len(courts), n, bool(done)))
        print(f"{st:<4}{len(courts):>7}{n:>10,}   "
              f"{'yes' if done else '-':>6}")
        time.sleep(delay)
    pages = -(-total // 20)
    secs = pages * (delay + 4.0)      # measured ~4s of API time per page
    print(f"\n{'':4}{'':>7}{total:>10,} cases, {pages:,} pages")
    print(f"at --delay {delay} that is about {secs / 3600:.1f} hours of "
          f"fetching (measured 6.1s/page at delay 2)")
    biggest = sorted(rows, key=lambda r: -r[2])[:5]
    print("largest: " + ", ".join(f"{r[0]} {r[2]:,}" for r in biggest))


def sweep_states(states: list, query: str, since: str = "", delay: float = 2.0,
                 max_pages: int = 0, token: str = "") -> None:
    """Sweep states one at a time, skipping the ones already finished.

    🚨 SEQUENTIAL, DELIBERATELY. Two sweeps running at once is how the 429
    ceiling was found - the anonymous limit tolerates a burst from one client
    and not two. Parallelising this would not make it faster, it would make it
    throttled, and the failure would land in the middle of a state.

    Each state is its own run with its own cursor, so an interruption anywhere
    costs at most the page in flight, and re-running the command picks up from
    the first unfinished state.
    """
    done_already = 0
    for i, st in enumerate(states, 1):
        courts = STATE_COURTS[st]
        key = run_key(courts, query, since)
        if oversight.completed_run(TOOL, key):
            done_already += 1
            continue
        print(f"\n=== [{i}/{len(states)}] {st} "
              f"({', '.join(courts)}) ===")
        got, promised = sweep_windowed(courts, query, after=since,
                                       delay=delay, token=token)
        # A state-level marker so the next run of the national command skips
        # this state outright, instead of re-planning its windows. It records
        # the coverage it achieved, so a state that ended short stays visible
        # in --report rather than being quietly filed as finished.
        mid = oversight.start_run(TOOL, key, total=promised)
        oversight.update_run(mid, uniq=got, seen=got, added=got,
                             state="done" if got >= promised else "short")
    if done_already:
        print(f"\nskipped {done_already} state(s) already complete")


def sweep(courts: list, query: str, since: str = "", before: str = "",
          delay: float = 2.0, max_pages: int = 0, token: str = "",
          resume: bool = False) -> tuple:
    """Paginate one query to exhaustion. Returns (distinct dockets, promised).

    The return value is the coverage check: `seen` counts result ROWS and the
    stream repeats dockets, so only the distinct count means anything.
    """
    court_param = " ".join(courts)
    key = run_key(courts, query, since, before)

    # A window that has already been settled - fully covered, or found short
    # and split into children that carry their own keys - is not re-fetched.
    # This is what makes re-running the national command cheap instead of
    # replaying the country.
    prior = oversight.find_run(TOOL, key) if resume else None
    if prior is not None and prior["state"] in ("done", "short", "split"):
        return (prior["uniq"] or 0), (prior["total"] or 0)

    run = oversight.resumable_run(TOOL, key) if resume else None
    if resume and not run:
        print("  (fresh window)")
    cursor = run["cursor"] if run else None
    run_id = run["id"] if run else None
    pages = run["pages"] if run else 0
    seen = run["seen"] if run else 0
    added = run["added"] if run else 0

    if cursor:
        url = cursor
        print(f"resuming run {run_id} at page {pages + 1} "
              f"({seen} results already seen)")
    else:
        params = {"type": SEARCH_TYPE, "q": query, "court": court_param}
        if since:
            params["filed_after"] = since
        if before:
            params["filed_before"] = before
        url = f"{API}/search/?" + urllib.parse.urlencode(params)

    total = run["total"] if run else None
    src_id = None
    # 🚨 --max-pages IS PER INVOCATION, NOT CUMULATIVE. `pages` is restored
    # from the run and is the total across every sitting, so comparing it to
    # max_pages meant a resumed sweep already past the limit fetched exactly
    # ONE page and stopped - measured: `--resume --max-pages 2` on a run at
    # page 3 did one page. The intent of the flag is "do this much work now".
    pages_now = 0
    while url:
        try:
            # _get retries 429s and network blips on its own; anything that
            # reaches here is real, and the cursor is already checkpointed.
            d = _get(url, token=token)
        except Exception:
            if run_id:
                oversight.update_run(run_id, state="error", cursor=url)
            raise

        if run_id is None:
            total = d.get("count")
            run_id = oversight.start_run(TOOL, key, total=total)
            print(f"run {run_id}: {total} cases match "
                  f"[{court_param}] {query}"
                  + (f" filed_after={since}" if since else ""))
        if src_id is None:
            # One source row per invocation, not per page. It records a
            # retrieval - the query, when, and which run - so a reader can
            # reproduce exactly the request these cases came from. A resumed
            # sweep mints a second one, which is correct: it was a second
            # retrieval, on a different day, and saying so is the point.
            src_id = oversight.add_source(
                "court", "CourtListener RECAP",
                "https://www.courtlistener.com/recap/",
                json.dumps({"query": query, "court": court_param,
                            "since": since, "run": run_id}))

        results = d.get("results", [])
        new_here = 0
        for r in results:
            nature = r.get("suitNature") or ""
            cause = r.get("cause") or ""
            is_prisoner = int("prisoner" in nature.lower()
                              or "prisoner" in cause.lower())
            row = {
                "docket_id": r["docket_id"],
                "source_id": src_id,
                "court_id": r.get("court_id"),
                "court_name": r.get("court"),
                "docket_number": r.get("docketNumber"),
                "case_name": r.get("caseName"),
                "cause": cause,
                "suit_nature": nature,
                "date_filed": r.get("dateFiled"),
                "date_terminated": r.get("dateTerminated"),
                "assigned_to": r.get("assignedTo"),
                "jury_demand": r.get("juryDemand"),
                "absolute_url": r.get("docket_absolute_url"),
                "pacer_case_id": r.get("pacer_case_id"),
                "is_prisoner": is_prisoner,
            }
            if oversight.upsert_case(row):
                new_here += 1
            oversight.upsert_parties(r["docket_id"], r.get("party") or [],
                                     case_name=row["case_name"] or "",
                                     cause=cause)
        oversight.mark_seen(run_id, [r["docket_id"] for r in results])

        pages += 1
        pages_now += 1
        seen += len(results)
        added += new_here
        url = d.get("next")
        oversight.update_run(run_id, cursor=url or "", pages=pages, seen=seen,
                             added=added, state="running" if url else "done")
        pct = f" {100.0 * seen / total:.0f}%" if total else ""
        print(f"  page {pages}: {len(results)} results, {new_here} new "
              f"({seen} seen{pct}, {added} cases)")

        if max_pages and pages_now >= max_pages:
            print(f"stopped at --max-pages {max_pages}; run --resume "
                  f"to continue")
            oversight.update_run(run_id, state="running")
            return oversight.run_unique(run_id), (total or 0)
        if url:
            time.sleep(delay)

    uniq = oversight.run_unique(run_id)
    short = (total or 0) - uniq
    oversight.update_run(run_id, state="short" if short > 0 else "done",
                         uniq=uniq)
    mark = f"⚠️ {short:,} short" if short > 0 else "✅"
    print(f"  {uniq:,} distinct of {total or 0:,} promised "
          f"({seen} rows, {added} new)  {mark}")
    if short <= 0:
        # The cases table is the record now; the scratch rows have done their
        # job. Left in place, a national sweep would grow a second copy of
        # every docket id it ever saw.
        oversight.prune_run_dockets(run_id)
    return uniq, (total or 0)


def report() -> None:
    s = oversight.stats()
    print("oversight.db")
    print(f"  cases           {s['cases']:>7,}  "
          f"({s['cases_street']:,} street / {s['cases_prison']:,} prison)")
    print(f"  filed between   {s['earliest']} and {s['latest']}")
    print(f"  party rows      {s['parties']:>7,}")
    print(f"  titled officers {s['titled']:>7,}  "
          f"({s['distinct_titled']:,} distinct names)")
    print(f"  review queue    {s['candidates']:>7,}  (signal >= 2)")
    print(f"  officers minted {s['officers']:>7,}  "
          f"<- humans only; 0 is correct until someone reviews")
    print(f"  allegations     {s['allegations']:>7,}")

    # 🚨 COVERAGE, BECAUSE UNDER-COVERAGE IS SILENT. A sweep that stops early,
    # or a result stream that repeats dockets across pages, finishes with a
    # cheerful "done" and a database missing cases nobody will ever know were
    # missing. The API told us how many matched; print that next to what we
    # actually hold, and the gap has to explain itself.
    c = oversight.connect()
    runs = c.execute(
        "SELECT id, state, pages, seen, added, uniq, total, query FROM runs "
        "ORDER BY id").fetchall()
    if runs:
        print("\n  runs")
        for r in runs:
            q = json.loads(r["query"] or "{}")
            # ⚠️ `seen` counts RESULT ROWS and the stream repeats dockets, so
            # it OVERSTATES coverage - that is how a run 1,202 cases short
            # printed a reassuring number. `uniq` is the distinct docket count
            # and is the only one worth reading; a run from before that column
            # existed shows "?" rather than a number that means nothing.
            got = r["uniq"] if r["uniq"] else None
            shown = f"{got:,}" if got is not None else "?"
            gap = ""
            if r["total"] and got is not None and got < r["total"] \
                    and r["state"] in ("done", "short"):
                gap = f"  ⚠️ {r['total'] - got:,} short"
            span = ""
            if q.get("since") or q.get("before"):
                span = f" {q.get('since', '')}..{q.get('before', '')}"
            print(f"    {r['id']:>3} {r['state']:<9} "
                  f"{shown:>7}/{r['total'] or 0:<7,} distinct  "
                  f"{r['pages']:>4} pages  {r['seen']:>6,} rows  "
                  f"[{q.get('court', '?')}]{span}{gap}")
            if r["state"] in ("running", "throttled", "error"):
                print("        -> unfinished; re-run the same command")


def queue(limit: int) -> None:
    """The strongest officer candidates, most-cited first.

    A name that appears as a titled defendant across several separate cases is
    the highest-value thing in the database and the obvious place for a
    volunteer to start - it is the pattern a single case cannot show.
    """
    c = oversight.connect()
    rows = c.execute(
        "SELECT p.raw_name, p.title_guess, p.agency_hint, "
        "       COUNT(*) AS cases, MIN(c.date_filed) AS first, "
        "       MAX(c.date_filed) AS last "
        "FROM case_parties p JOIN cases c ON c.docket_id = p.docket_id "
        "WHERE p.officer_signal = 3 AND p.officer_id IS NULL "
        "GROUP BY p.raw_name ORDER BY cases DESC, last DESC LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        print("queue empty - fetch some cases first")
        return
    print(f"{'cases':>5}  {'first':<10} {'last':<10}  name")
    for r in rows:
        print(f"{r['cases']:>5}  {r['first'] or '?':<10} {r['last'] or '?':<10}"
              f"  {r['raw_name']}")
    print("\n⚠️  These are UNVERIFIED party strings from court captions. "
          "Nothing here is\n    an officer until a person says so - same name "
          "does not mean same human.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Seed the officer accountability database from federal "
                    "civil-rights dockets.")
    ap.add_argument("--state", help="two-letter state, e.g. MI")
    ap.add_argument("--states",
                    help="comma-separated states, or ALL for every state, "
                         "DC and territory. Swept one at a time; finished "
                         "states are skipped, so re-running resumes.")
    ap.add_argument("--plan", action="store_true",
                    help="with --states: count the cases per state and stop. "
                         "One request each - do this before a big sweep.")
    ap.add_argument("--courts", help="comma-separated CourtListener court ids")
    ap.add_argument("--since", help="only cases filed on/after YYYY-MM-DD")
    ap.add_argument("--before", help="only cases filed on/before YYYY-MM-DD")
    ap.add_argument("--no-windows", action="store_true",
                    help="one deep cursor instead of date windows. Loses "
                         "cases past ~2,700 - only for small slices.")
    ap.add_argument("--exclude-prisoner", action="store_true",
                    help="street policing only (drops ~2/3 of the docket)")
    ap.add_argument("--broad", action="store_true",
                    help="match what bulk loaded: cause 1983 OR nature of "
                         "suit 440/550/555. Use this for party enrichment - "
                         "cause alone misses ~96%% of the database.")
    ap.add_argument("--q", default="", help="extra Lucene terms, ANDed")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="stop after N pages (resumable)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (default 2, be polite)")
    ap.add_argument("--patient", action="store_true",
                    help="never give up on a 429 - hold at the longest "
                         "backoff forever. For unattended multi-day jobs.")
    ap.add_argument("--resume", action="store_true",
                    help="continue the last unfinished sweep of this query")
    ap.add_argument("--token", default=os.environ.get("COURTLISTENER_TOKEN",
                                                      ""),
                    help="CourtListener API token (optional; env "
                         "COURTLISTENER_TOKEN)")
    ap.add_argument("--list-courts", metavar="STATE",
                    help="print federal district court ids for a state name")
    ap.add_argument("--reclassify", action="store_true",
                    help="re-derive party guesses from the stored raw names "
                         "(skips rows a human has linked)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --reclassify, count changes without writing")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--queue", type=int, metavar="N",
                    help="show the top N officer candidates")
    a = ap.parse_args()
    global PATIENT
    PATIENT = a.patient

    if a.list_courts:
        list_courts(a.list_courts)
        return
    if a.reclassify:
        r = oversight.reclassify(dry_run=a.dry_run)
        verb = "would change" if a.dry_run else "changed"
        print(f"{r['examined']:,} party rows examined, {verb} "
              f"{r['changed']:,} ({r['demoted']:,} lost officer signal)")
        if not a.dry_run:
            print()
            report()
        return
    if a.report:
        report()
        return
    if a.queue:
        queue(a.queue)
        return

    if a.states:
        if a.states.strip().upper() == "ALL":
            states = list(ALL_STATES)
        else:
            states = [s.strip().upper() for s in a.states.split(",")
                      if s.strip()]
        bad = [s for s in states if s not in STATE_COURTS]
        if bad:
            raise SystemExit(f"unknown state code(s): {', '.join(bad)}")
        query = build_query(a.exclude_prisoner, a.q, broad=a.broad)
        if a.plan:
            plan(states, a.exclude_prisoner, a.since or "", a.q, a.delay,
                 a.token, broad=a.broad)
            return
        sweep_states(states, query, since=a.since or "", delay=a.delay,
                     max_pages=a.max_pages, token=a.token)
        print()
        report()
        return

    courts = []
    if a.courts:
        courts = [c.strip() for c in a.courts.split(",") if c.strip()]
    elif a.state:
        courts = STATE_COURTS.get(a.state.upper(), [])
        if not courts:
            raise SystemExit(
                f"unknown state {a.state!r}. Known: "
                f"{', '.join(ALL_STATES)}")
    if not courts and not a.resume:
        raise SystemExit("need --state or --courts (or --resume/--report)")

    query = build_query(a.exclude_prisoner, a.q, broad=a.broad)
    if a.no_windows:
        # The old single-cursor path. Kept because it is the right thing for a
        # deliberately small slice (--since last week, a --max-pages probe),
        # and because it is what proved the cursor loses cases at depth.
        sweep(courts, query, since=a.since or "", before=a.before or "",
              delay=a.delay, max_pages=a.max_pages, token=a.token,
              resume=a.resume)
    else:
        sweep_windowed(courts, query, after=a.since or "",
                       before=a.before or "", delay=a.delay, token=a.token)
    print()
    report()


if __name__ == "__main__":
    main()
