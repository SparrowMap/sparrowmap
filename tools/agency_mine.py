"""Build the agency directory from case captions. No API, no network.

    python tools/agency_mine.py --mine            # extract + cluster + store
    python tools/agency_mine.py --mine --dry-run  # show what it would do
    python tools/agency_mine.py --top 30          # biggest agencies
    python tools/agency_mine.py --state MI        # one state's directory
    python tools/agency_mine.py --merges 40       # audit the fuzzy merges

--------------------------------------------------------------------------
WHY THIS IS WORTH BUILDING BEFORE THE OFFICER NAMES ARRIVE
--------------------------------------------------------------------------

Party enrichment is rate-limited and will take days, so 1.79M cases currently
have a court, a date, a cause and a caption - and no officer names. That is
still enough to answer a real question: **which agency was sued, how often, and
when.** An agency directory needs nothing but the captions already on disk.

It is also the skeleton officer profiles hang off later: an officer is
identified as "Deputy Boucher, Kent County" and the agency has to exist first.

--------------------------------------------------------------------------
🚨 WHY THE RAW CAPTION STRING CANNOT BE THE IDENTITY
--------------------------------------------------------------------------

PACER captions are dirty in ways that matter. All of these are real, from the
two Michigan districts alone:

    Saginaw, County of          word order reversed
    COUNTY MACOMB               reversed with no "of"
    COUNTY OFLENAWEE ETAL       the "OF" ran into the name
    Wayne County Officia        truncated mid-word
    OAKLAND COUNTY CONN         truncated
    WASTENAW COUNTY             misspelled (Washtenaw)
    COUNTY OAKLUND              misspelled (Oakland)
    CITY OF DETROIT ETAL        procedural noise

Counting distinct caption strings would invent thousands of agencies that do
not exist and split the real ones across a dozen spellings each - and every
count computed from it would be quietly, plausibly wrong.

So identity is **(state, kind, place)**, derived:

  * the STATE comes from `court_id`, never from the text. A caption saying
    "Jefferson County" does not say which of the 25 Jefferson Counties it is;
    the court that heard the case does.
  * the KIND and PLACE come from `canon()`, which understands the reversed and
    run-together forms above.
  * TYPOS are resolved by frequency, not by a dictionary. A real agency appears
    hundreds of times and a typo appears once, so a rare spelling that closely
    matches a frequent one in the same state and kind is folded into it.

⚠️ THIS IS THE ONE TABLE CLUSTERING MAY WRITE TO WITHOUT A HUMAN. Everywhere
else in this database a wrong match is a wrong statement about a person; here
it is a wrong count about an organisation. Every fold is still recorded in
`agency_aliases` with its similarity score, because a cluster nobody can
inspect is a number nobody should quote - see --merges.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oversight  # noqa: E402
from courtlistener_fetch import STATE_COURTS  # noqa: E402

COURT_STATE = {c: st for st, ids in STATE_COURTS.items() for c in ids}

# Procedural furniture and truncation debris. Stripped before anything else,
# so "Wayne County Officia" and "CITY OF DETROIT ETAL" reduce to the agency.
NOISE = re.compile(
    r"\b(et\s*al\.?|etal|et\s*ux|jr|sr|iii?|inc\.?|llc|l\.l\.c|corp|co|"
    r"and others|in (his|her|their) official capacit(y|ies)|"
    r"officials?|officia|official|unknown|john doe|jane doe|does?|"
    r"jail|probation|conn|sheriffs?\s+dep(artmen)?t)\b", re.I)
PUNCT = re.compile(r"[^a-z0-9 ]+")

# "Saginaw, County of" / "Hamtramck, City of" - PACER's sort-friendly form.
REVERSED = re.compile(
    r"^(.*?),\s*(county|city|township|village|town|borough)\s+of\s*$", re.I)

# Denver and San Francisco are "City and County of X". Without this the
# extractor reads the kind as county and the place as "city and".
CITY_AND_COUNTY = re.compile(r"^city\s+and\s+county\s+of\s+(.*)$", re.I)

# ⚠️ "CHARTER TOWNSHIP" IS A LEGAL CLASS IN MICHIGAN, NOT A PLACE NAME.
# "Waterford Charter Township" and "Charter Township of Waterford" both mean
# Waterford. The first version read the place as "charter" and produced a
# phantom agency with 94 cases sitting sixth in Michigan's directory.
CHARTER = re.compile(r"\bcharter\s+township\b", re.I)

_KINDS = (
    ("sheriff",  [r"^(.*?)\s+county\s+sheriff\b", r"^sheriff\s+of\s+(.*)$"]),
    ("police",   [r"^(.*?)\s+police\s*(?:dep(?:artmen)?t|dept)?$",
                  r"^(?:city\s+of\s+)?(.*?)\s+police\b"]),
    ("county",   [r"^county\s+of\s+(.*)$", r"^county\s+(.*)$",
                  r"^(.*?)\s+county\b"]),
    ("city",     [r"^city\s+of\s+(.*)$", r"^(.*?)\s+city$"]),
    ("township", [r"^township\s+of\s+(.*)$", r"^township\s+(.*)$",
                  r"^(.*?)\s+township\b"]),
    ("village",  [r"^village\s+of\s+(.*)$", r"^(.*?)\s+village$"]),
)

_STOP = {"the", "and", "of", "a", "an", "city and", "county", "city"}


def canon(raw: str):
    """(kind, place) for an agency-shaped string, else None. All guesses."""
    s = (raw or "").strip()
    m = CITY_AND_COUNTY.match(s)
    if m:
        s = f"county of {m.group(1)}"
    m = REVERSED.match(s)
    if m:
        s = f"{m.group(2)} of {m.group(1)}"
    s = CHARTER.sub(" township ", s)
    s = NOISE.sub(" ", s)
    s = PUNCT.sub(" ", s.lower())
    s = " ".join(s.split())
    if not s:
        return None
    # "county ofoakland" -> "county of oakland"; also a doubled "of of".
    s = re.sub(r"\bof\s*of\b", "of", s)
    s = re.sub(r"\b(county|city|township|village)\s+of([a-z]{3,})\b",
               r"\1 of \2", s)
    for kind, pats in _KINDS:
        for p in pats:
            m = re.match(p, s)
            if m:
                place = " ".join(m.group(1).split())
                if len(place) > 2 and place not in _STOP:
                    return kind, place
    return None


def title(place: str) -> str:
    """'st clair' -> 'St. Clair'. Display only; `place` stays canonical."""
    out = []
    for w in place.split():
        out.append("St." if w == "st" else
                   "Mt." if w == "mt" else w.capitalize())
    return " ".join(out)


def display_for(kind: str, place: str, state: str) -> str:
    p = title(place)
    return {
        "county": f"{p} County, {state}",
        "city": f"City of {p}, {state}",
        "township": f"{p} Township, {state}",
        "village": f"Village of {p}, {state}",
        "police": f"{p} Police Department, {state}",
        "sheriff": f"{p} County Sheriff, {state}",
    }.get(kind, f"{p}, {state}")


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------

MERGE_RATIO = 0.86       # difflib similarity required to fold a rare spelling
ANCHOR_MIN = 8           # a canonical form must be seen at least this often
RARE_MAX = 3             # only spellings this rare are candidates to fold


def prefix_suspects(counts: dict) -> list:
    """Short names that look like TRUNCATIONS of a longer one, for review.

    🚨 THESE ARE REPORTED, NOT MERGED, AND THAT IS THE WHOLE POINT.

    1980s PACER truncated the caption field, so Michigan holds "City of Det"
    (825 cases) alongside "City of Detroit" (184). Frequency clustering cannot
    fix it because the TRUNCATION IS THE MORE COMMON SPELLING - the rare-folds-
    into-frequent rule would run backwards and fold Detroit into Det.

    Prefix matching would catch it, and prefix matching is also how you
    silently merge two real places: `kent` is a prefix of `kentwood`, `clare`
    of `clarence`, `bay` of `bayonne`. Those are different towns with different
    police departments, and folding them would move real cases onto the wrong
    agency's page - a wrong count that reads as authoritative.

    There is no rule that separates the two cases from the strings alone, so
    this hands them to a person instead of guessing. `--prefixes` prints them.
    """
    by_group = defaultdict(list)
    for (state, kind, place), n in counts.items():
        by_group[(state, kind)].append((place, n))
    out = []
    for (state, kind), items in by_group.items():
        places = {p: n for p, n in items}
        for short, ns in items:
            if len(short) < 3 or len(short) > 7:
                continue
            for long, nl in items:
                if long == short or not long.startswith(short):
                    continue
                if len(long) - len(short) < 2:
                    continue
                out.append((state, kind, short, ns, long, nl))
    return sorted(out, key=lambda r: -(r[3] + r[5]))


def cluster(counts: dict) -> dict:
    """Fold rare misspellings into frequent neighbours of the same kind+state.

    counts: {(state, kind, place): hits} -> {place_seen: place_canonical}

    🚨 FREQUENCY IS THE DICTIONARY. There is no offline list of every county
    and municipality in America in this repo, and downloading one would add a
    dependency for a problem the data already answers: a real agency is sued
    hundreds of times and a typo is sued once. So the anchors are the spellings
    that appear often, and only rare spellings are allowed to move.
    ⚠️ Deliberately conservative. Folding a real small agency into a big
    neighbour would silently transfer its cases, so the bar is a high
    similarity AND a large frequency gap AND the same state and kind.
    """
    by_group = defaultdict(list)
    for (state, kind, place), n in counts.items():
        by_group[(state, kind)].append((place, n))
    mapping = {}
    for (state, kind), items in by_group.items():
        anchors = sorted([p for p, n in items if n >= ANCHOR_MIN],
                         key=len, reverse=True)
        if not anchors:
            continue
        for place, n in items:
            if n > RARE_MAX or place in anchors:
                continue
            best, score = None, 0.0
            for a in anchors:
                # Cheap reject before the expensive ratio.
                if abs(len(a) - len(place)) > 4 or a[0] != place[0]:
                    continue
                r = difflib.SequenceMatcher(None, place, a).ratio()
                if r > score:
                    best, score = a, r
            if best and score >= MERGE_RATIO:
                mapping[(state, kind, place)] = (best, score)
    return mapping


# --------------------------------------------------------------------------
# Mine
# --------------------------------------------------------------------------

def mine(dry_run: bool = False, limit: int = 0) -> None:
    c = oversight.connect()
    q = ("SELECT docket_id, court_id, case_name, date_filed FROM cases "
         "WHERE case_name IS NOT NULL AND case_name != ''")
    if limit:
        q += f" LIMIT {int(limit)}"

    counts: dict = defaultdict(int)
    raws: dict = defaultdict(int)
    hits: list = []          # (docket_id, state, kind, place)
    dates: dict = defaultdict(lambda: [None, None])
    n = skipped = 0
    for docket_id, court_id, case_name, filed in c.execute(q):
        n += 1
        state = COURT_STATE.get(court_id or "")
        if not state:
            skipped += 1
            continue
        # Only the defendant side. A plaintiff named Jackson is not Jackson
        # County, and captions put the defendants after " v. ".
        parts = re.split(r"\s+v\.?\s+", case_name, maxsplit=1)
        if len(parts) < 2:
            continue
        seen = set()
        for chunk in re.split(r"\s+(?:and|&)\s+|,(?!\s*(?:county|city|"
                              r"township|village|town|borough)\s+of)", parts[1]):
            got = canon(chunk)
            if not got:
                continue
            kind, place = got
            key = (state, kind, place)
            if key in seen:
                continue
            seen.add(key)
            counts[key] += 1
            raws[(key, " ".join(chunk.split())[:80])] += 1
            hits.append((docket_id, state, kind, place))
            lo, hi = dates[key]
            if filed:
                dates[key][0] = filed if lo is None else min(lo, filed)
                dates[key][1] = filed if hi is None else max(hi, filed)

    print(f"scanned {n:,} captions ({skipped:,} outside the state map)")
    print(f"  {len(counts):,} distinct (state, kind, place) before clustering")

    merges = cluster(counts)
    print(f"  {len(merges):,} rare spellings fold into a frequent neighbour")
    global _SUSPECTS
    _SUSPECTS = prefix_suspects(counts)
    print(f"  {len(_SUSPECTS):,} possible TRUNCATIONS flagged for review "
          f"(see --prefixes) - not merged")

    def resolve(key):
        m = merges.get(key)
        return (key[0], key[1], m[0]) if m else key

    # ⚠️ Fold the counts AND the date range in the SAME single pass.
    # The first version re-scanned every key inside the per-agency write loop
    # to find its dates, which is O(agencies x keys) - fine on a 200k sample
    # (7k x 7k) and roughly a billion iterations on the full 1.79M-case run.
    final: dict = defaultdict(int)
    final_dates: dict = defaultdict(lambda: [None, None])
    for key, v in counts.items():
        tgt = resolve(key)
        final[tgt] += v
        lo, hi = dates.get(key, (None, None))
        cur = final_dates[tgt]
        if lo:
            cur[0] = lo if cur[0] is None else min(cur[0], lo)
        if hi:
            cur[1] = hi if cur[1] is None else max(cur[1], hi)
    print(f"  {len(final):,} agencies after clustering")

    if dry_run:
        print("\n  sample merges:")
        for (st, kind, place), (into, score) in list(merges.items())[:12]:
            print(f"    {st} {kind:<9} {place!r} -> {into!r}  ({score:.2f})")
        print("\n--dry-run: nothing written")
        return

    # ---- write -----------------------------------------------------------
    # 🚨 CLEAR FIRST. The write is an UPSERT, so it only ever touches keys the
    # CURRENT extractor still produces - an agency the last run invented and
    # this one no longer emits just sits there with its old count forever.
    # Caught live: fixing the "Charter Township" bug removed it from the
    # extractor's output and left the phantom agency, 94 cases and all, still
    # sitting sixth in Michigan's directory after a full re-mine.
    #
    # These three tables are entirely derived from `cases`, so rebuilding them
    # is safe and is the only way a fix to canon() actually takes effect.
    if not limit:
        c.execute("DELETE FROM case_agencies")
        c.execute("DELETE FROM agency_aliases")
        c.execute("DELETE FROM agencies")
        c.commit()
    ids: dict = {}
    for (state, kind, place), n_hits in final.items():
        lo, hi = final_dates[(state, kind, place)]
        c.execute(
            "INSERT INTO agencies (state, kind, place, display, cases, "
            "first_filed, last_filed) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(state, kind, place) DO UPDATE SET "
            "cases=excluded.cases, display=excluded.display, "
            "first_filed=excluded.first_filed, last_filed=excluded.last_filed",
            (state, kind, place, display_for(kind, place, state), n_hits,
             lo, hi))
        ids[(state, kind, place)] = c.execute(
            "SELECT id FROM agencies WHERE state=? AND kind=? AND place=?",
            (state, kind, place)).fetchone()[0]
    c.commit()

    for (key, raw), k in raws.items():
        tgt = resolve(key)
        aid = ids.get(tgt)
        if not aid:
            continue
        m = merges.get(key)
        c.execute(
            "INSERT INTO agency_aliases (agency_id, raw, place_seen, hits, "
            "merged, similarity) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(agency_id, raw) DO UPDATE SET hits=excluded.hits",
            (aid, raw, key[2], k, 1 if m else 0, m[1] if m else None))
    c.commit()

    c.executemany(
        "INSERT OR IGNORE INTO case_agencies (docket_id, agency_id) "
        "VALUES (?,?)",
        [(d, ids[resolve((s, k, p))]) for d, s, k, p in hits
         if resolve((s, k, p)) in ids])
    c.commit()
    print(f"  wrote {len(ids):,} agencies, {len(raws):,} aliases, "
          f"{len(hits):,} case links")


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

def top(n: int, state: str = "") -> None:
    c = oversight.connect()
    where, args = "", []
    if state:
        where, args = "WHERE state=?", [state.upper()]
    print(f"{'cases':>7}  {'first':<10} {'last':<10}  agency")
    for r in c.execute(
            f"SELECT display, cases, first_filed, last_filed FROM agencies "
            f"{where} ORDER BY cases DESC LIMIT ?", (*args, n)):
        print(f"{r['cases']:>7,}  {r['first_filed'] or '?':<10} "
              f"{r['last_filed'] or '?':<10}  {r['display']}")
    print("\n⚠️  A count of CAPTIONS naming this agency. Not a count of "
          "findings against it,\n    and not a misconduct rate - a big county "
          "appears often because it is big.")


_SUSPECTS: list = []


def prefixes_report(n: int) -> None:
    """Rebuild the counts from the stored agencies and show the suspects."""
    c = oversight.connect()
    counts, last = {}, {}
    for r in c.execute("SELECT state, kind, place, cases, last_filed "
                       "FROM agencies"):
        k = (r["state"], r["kind"], r["place"])
        counts[k] = r["cases"]
        last[k] = r["last_filed"] or ""
    rows = prefix_suspects(counts)
    if not rows:
        print("no truncation suspects")
        return
    # 🚨 THE DATE RANGE IS THE DISCRIMINATOR, AND IT IS CHECKABLE.
    #
    # PACER truncated caption fields in the 1980s-90s and stopped. So a
    # truncation's LAST filing is stranded in the past while the real name
    # keeps going: "City of Det" ends 2004-01-09, "City of Detroit" runs to
    # 2026-07-13. A genuinely different town has no reason to stop being sued.
    #
    # It is still only a hint, and it is presented as one. Chicago Heights is
    # a real city whose name happens to start with Chicago, and no date rule
    # would ever tell you that - a person has to.
    scored = []
    for st, kind, short, ns, long, nl in rows:
        ls, ll = last.get((st, kind, short), ""), last.get((st, kind, long), "")
        stale = bool(ls and ll and ls < ll and ls[:4] < str(int(ll[:4]) - 8))
        scored.append((stale, ns + nl, st, kind, short, ns, ls,
                       long, nl, ll))
    scored.sort(key=lambda r: (not r[0], -r[1]))
    print("possible truncations - NOT merged. A person decides.\n")
    print(f"{'':<4}{'st':<3} {'short':<13}{'n':>6} {'ends':<11}  "
          f"{'longer':<15}{'n':>6} {'ends':<11}")
    for stale, _, st, kind, short, ns, ls, long, nl, ll in scored[:n]:
        flag = "TRUNC" if stale else " ?   "
        print(f"{flag} {st:<3} {short:<13}{ns:>6} {ls or '?':<11}  "
              f"{long:<15}{nl:>6} {ll or '?':<11}")
    print("\nTRUNC = the short form STOPPED being filed years before the long "
          "one did,\n        which is what a PACER field-width truncation "
          "looks like.")
    print("⚠️  Still a hint, not a verdict. 'det' -> 'detroit' is a "
          "truncation;\n    'chicago' -> 'chicago heights' is a DIFFERENT "
          "CITY. Nothing here is merged\n    automatically, and that is "
          "deliberate.")


def merges_report(n: int) -> None:
    c = oversight.connect()
    rows = list(c.execute(
        "SELECT a.display, x.raw, x.place_seen, x.hits, x.similarity "
        "FROM agency_aliases x JOIN agencies a ON a.id = x.agency_id "
        "WHERE x.merged=1 ORDER BY x.similarity ASC LIMIT ?", (n,)))
    if not rows:
        print("no fuzzy merges recorded")
        return
    print("weakest merges first - these are the ones to eyeball:\n")
    print(f"{'sim':>5}  {'hits':>4}  {'saw':<22} {'folded into':<28} raw")
    for r in rows:
        print(f"{r['similarity']:.2f}  {r['hits']:>4}  "
              f"{r['place_seen']:<22} {r['display']:<28} {r['raw']!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mine", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top", type=int, metavar="N")
    ap.add_argument("--state")
    ap.add_argument("--merges", type=int, metavar="N")
    ap.add_argument("--prefixes", type=int, metavar="N",
                    help="short names that may be truncations of a longer "
                         "one - a human decides, this tool never merges them")
    a = ap.parse_args()
    if a.mine:
        mine(dry_run=a.dry_run, limit=a.limit)
    elif a.prefixes:
        prefixes_report(a.prefixes)
    elif a.merges:
        merges_report(a.merges)
    elif a.top or a.state:
        top(a.top or 40, a.state or "")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
