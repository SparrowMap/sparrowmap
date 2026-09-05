"""Load every federal 42 USC 1983 case in America from CourtListener bulk data.

    python tools/bulk_dockets.py --download          # fetch the .csv.bz2
    python tools/bulk_dockets.py --scan              # count without writing
    python tools/bulk_dockets.py --load              # filter + write to db
    python tools/bulk_dockets.py --verify MI         # bulk vs the API's count

--------------------------------------------------------------------------
WHY THIS EXISTS, AND WHY IT REPLACED THE API AS THE ENUMERATION
--------------------------------------------------------------------------

The search API was the wrong tool for a national case list, and the numbers say
so plainly:

  * Its cursor LOSES CASES. `type=r` walks documents and groups them into
    dockets; Michigan came out 1,202 short of the API's own count and reported
    success. `type=d` fixes that, but it is still a cursor over a live index.
  * It is RATE LIMITED, and authenticating makes it worse. The documented free
    membership tier is 5/minute, 50/hour, **125 requests per day** - anonymous
    sustained ten times that. Michigan alone is ~594 pages; at 125/day that is
    five days for one state. Paid tiers top out at 1,400/day for $100/month.
  * It is 20 rows per request. America is not 20 rows per request.

The bulk export is the same database, dumped:

  * **Free, unauthenticated, unthrottled.** No token, no membership, no cursor.
  * One 5 GB file covers **every federal court**, so "works for every state"
    stops being a sweep to schedule and becomes a filter to run.
  * Verified header (2026-06-30): `id` (the docket id), `case_name`,
    `docket_number`, **`cause`**, **`nature_of_suit`**, `date_filed`,
    `date_terminated`, `court_id`, `jury_demand`, `pacer_case_id`. Everything
    `cases` needs.
  * Regenerated quarterly, on the last day of March, June, September and
    December.

🚨 WHAT BULK DOES NOT HAVE: PARTY NAMES.

There is no parties file. `bulk-data/people-db-*` is JUDGES, not litigants -
checked, along with every plausible prefix. Party strings are where every
officer name in this project comes from, so they still have to come from the
search API, one request per 20 dockets.

That is the division of labour, and it is a good one: **bulk answers "which
cases exist", which must be complete; the API answers "who is named on this
case", which can be filled in gradually and prioritised.** The thing that
cannot tolerate a gap is the thing that no longer depends on a rate limit.

⚠️ AND IT MUST BE VERIFIED, NOT TRUSTED. `cause` is sparsely populated in the
dump - in a 43,000-row sample only 465 rows carried any cause at all. That may
be honest (most of those rows were bankruptcy, which has no cause) or it may be
a gap. `--verify MI` counts what bulk produced for a state and compares it to
the number the API gives for the same query. Same discipline that caught the
1,202 missing cases: the count from a second source, or it did not happen.
"""

from __future__ import annotations

import argparse
import bz2
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import oversight  # noqa: E402
from core import DATA  # noqa: E402

BULK_DIR = DATA / "bulk"
S3 = ("https://com-courtlistener-storage.s3-us-west-2.amazonaws.com"
      "/bulk-data")
DEFAULT_FILE = "dockets-2026-06-30.csv.bz2"
UA = "SparrowMap/0.1 (police accountability research; sparrowmap.com)"

# The court ids we care about, flattened from the state map. Bulk holds EVERY
# court - bankruptcy, appellate, state - and a section 1983 case against a
# police officer is filed in a federal district court. Filtering here keeps the
# scan honest and keeps bankruptcy noise out of the officer queue.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from courtlistener_fetch import STATE_COURTS  # noqa: E402

COURT_STATE = {c: st for st, ids in STATE_COURTS.items() for c in ids}

# 🚨 THE FILTER IS `cause`, AND THE NATURE OF SUIT IS RECORDED BUT NOT TRUSTED
# AS A SELECTOR.
#
# `cause` is the court's own statement of what the suit IS ("42:1983 Civil
# Rights Act"). Nature of suit 440/550 is broader - it covers civil rights
# claims that are not section 1983 at all, including employment and housing.
# Selecting on it would quietly widen the database beyond police accountability
# and make every count incomparable with the API's `cause:(1983)` figure, which
# is the only external number available to check against.
#
# So: select on cause, RECORD the nature of suit, and keep a separate count of
# rows that look like civil rights by NOS alone - a number worth watching, not
# acting on.
CAUSE_1983 = "1983"

# 🚨 "CIVIL RIGHTS" IS NOT THE SAME THING AS "POLICE".
#
# The first version of this filter accepted any nature of suit containing the
# words "civil rights". Measured against the real dump, that pulled in:
#
#     45,213  442 Civil rights jobs          <- employment discrimination
#      2,306  443 Civil rights accomodations
#      1,889  446 Civil Rights: ADA - Other
#        370  441 Civil rights voting
#        163  444 Civil rights welfare
#
# Roughly 45% of everything the NOS tier matched, and not one of those cases is
# about an officer. They would have flooded the review queue a volunteer is
# meant to work through - the single most expensive kind of noise this project
# can produce, because it wastes human attention rather than disk.
#
# The three codes that actually carry section 1983 claims against state actors:
#     440  Civil Rights: Other        <- street policing
#     550  Prisoner: Civil Rights     <- corrections
#     555  Prisoner: Prison Condition
#
# PACER writes these three ways in the same file - "440 Civil rights other",
# "440 Civil Rights: Other", and bare "Civil Rights: Other" - so the code is
# read when it is there and the phrase matched when it is not.
NOS_KEEP_CODES = {"440", "550", "555"}
_NOS_CODE = re.compile(r"^\s*(\d{3})")
# Phrases for rows that carry no numeric code. Deliberately narrow: "Other" and
# the two prisoner categories only.
_NOS_PHRASE = re.compile(
    r"civil\s*rights\s*[:\-]?\s*other"
    r"|prisoner\s*[:\-]?\s*civil\s*rights"
    r"|prison\s*condition", re.I)
# Belt and braces: anything naming one of these is a different fight, whatever
# else the string says.
_NOS_REJECT = re.compile(
    r"\bjobs?\b|employ|americans with disabilities|\bada\b|voting|welfare"
    r"|education|accommodat|accomodat|housing", re.I)


def nos_in_scope(nos: str) -> bool:
    """Is this nature of suit a police / corrections civil-rights case?"""
    if not nos:
        return False
    if _NOS_REJECT.search(nos):
        return False
    m = _NOS_CODE.match(nos)
    if m:
        return m.group(1) in NOS_KEEP_CODES
    return bool(_NOS_PHRASE.search(nos))


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def download(name: str = DEFAULT_FILE) -> Path:
    """Fetch the bulk file, resuming a partial download rather than restarting.

    5 GB over a domestic connection is twenty minutes on a good day and a lost
    evening if a blip means starting again.
    """
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    dest = BULK_DIR / name
    have = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(f"{S3}/{name}",
                                 headers={"User-Agent": UA})
    if have:
        req.add_header("Range", f"bytes={have}-")
        print(f"resuming at {have / 1e9:.2f} GB")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0) + have
        mode = "ab" if have and r.status == 206 else "wb"
        if mode == "wb":
            have = 0
        with open(dest, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                if have % (100 << 20) < (1 << 20):
                    el = time.time() - t0
                    print(f"  {have / 1e9:.2f} GB of {total / 1e9:.2f} "
                          f"({have / max(el, 1) / 1e6:.1f} MB/s)", flush=True)
    print(f"{dest} - {dest.stat().st_size / 1e9:.2f} GB")
    return dest


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def rows(path: Path):
    """Stream the bz2 as CSV rows, yielding (index_map, row_list).

    🚨 DECOMPRESSED THIS FILE IS ROUGHLY 30 GB - six times the 4.7 GB download,
    measured on a 3 MB slice that expanded to 18 MB. It is never written out
    and never read whole.

    🚨 IT IS PARSED AS CSV, NOT SPLIT ON NEWLINES. `case_name_full` and the
    appellate fields contain embedded newlines inside quotes, so line-splitting
    shears rows in half and produces plausible-looking garbage - the worst kind
    of bug this project gets, a wrong answer that looks like an answer.

    ⚠️ `csv.reader`, NOT `csv.DictReader`. DictReader builds a fresh dict of 54
    keys for every row, in Python, and there are tens of millions of rows. The
    reader is C. Columns are resolved once from the header and addressed by
    index, which is uglier to read and roughly twice as fast over the file.
    """
    fh = bz2.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    try:
        r = csv.reader(fh)
        header = next(r)
        idx = {name: i for i, name in enumerate(header)}
        yield idx, None
        for row in r:
            yield idx, row
    finally:
        fh.close()


def _f(row: list, i: int) -> str:
    """One field, tolerating a short row rather than dying on it."""
    return row[i] if i is not None and i < len(row) else ""


def scan(path: Path, load: bool = False, limit: int = 0) -> dict:
    src_id = None
    if load:
        src_id = oversight.add_source(
            "dataset", "CourtListener bulk dockets", f"{S3}/{path.name}",
            json.dumps({"file": path.name, "filter": "cause contains 1983",
                        "courts": "federal district, all states"}))
    stat = {"rows": 0, "with_cause": 0, "hits": 0, "nos_only": 0,
            "out_of_scope_court": 0, "added": 0, "by_state": {},
            "prisoner": 0, "street": 0, "no_date": 0}
    t0 = time.time()
    stream = rows(path)
    idx, _ = next(stream)
    i_cause, i_nos, i_court = idx["cause"], idx["nature_of_suit"], \
        idx["court_id"]
    i_id, i_name, i_num = idx["id"], idx["case_name"], idx["docket_number"]
    i_filed, i_term = idx["date_filed"], idx["date_terminated"]
    i_judge, i_jury = idx.get("assigned_to_str"), idx.get("jury_demand")
    i_slug, i_pacer = idx.get("slug"), idx.get("pacer_case_id")
    pending = []
    for _, r in stream:
        stat["rows"] += 1
        if limit and stat["rows"] > limit:
            break
        if stat["rows"] % 2_000_000 == 0:
            el = time.time() - t0
            print(f"  {stat['rows']:,} rows  {stat['hits']:,} hits  "
                  f"({stat['rows'] / max(el, 1) / 1000:.0f}k rows/s)",
                  flush=True)
        cause = _f(r, i_cause)
        if cause:
            stat["with_cause"] += 1
        nos = _f(r, i_nos)
        if CAUSE_1983 in cause:
            basis = "cause"
        elif nos_in_scope(nos):
            basis = "nos"
            stat["nos_only"] += 1
        else:
            continue
        court = _f(r, i_court)
        st = COURT_STATE.get(court)
        if not st:
            # Appellate, bankruptcy, or a state court. A section 1983 claim
            # against an officer starts in a federal district court.
            stat["out_of_scope_court"] += 1
            continue
        stat["hits"] += 1
        stat[f"basis_{basis}"] = stat.get(f"basis_{basis}", 0) + 1
        stat["by_state"][st] = stat["by_state"].get(st, 0) + 1
        prisoner = int("prisoner" in nos.lower() or "prisoner" in cause.lower())
        stat["prisoner" if prisoner else "street"] += 1
        filed = _f(r, i_filed)
        if not filed:
            stat["no_date"] += 1
        if not load:
            continue
        pending.append({
            "docket_id": int(_f(r, i_id)),
            "source_id": src_id,
            "court_id": court,
            "court_name": "",
            "docket_number": _f(r, i_num),
            "case_name": _f(r, i_name),
            "cause": cause,
            "suit_nature": nos,
            "date_filed": filed,
            "date_terminated": _f(r, i_term),
            "assigned_to": _f(r, i_judge),
            "jury_demand": _f(r, i_jury),
            "absolute_url": f"/docket/{_f(r, i_id)}/{_f(r, i_slug)}/",
            "pacer_case_id": _f(r, i_pacer),
            "is_prisoner": prisoner,
            "match_basis": basis,
        })
        # ⚠️ Batched. upsert_case commits per call, and a commit per case over
        # tens of thousands of hits is an fsync storm that dominates the run.
        if len(pending) >= 2000:
            stat["added"] += oversight.upsert_cases(pending)
            pending.clear()
    if load and pending:
        stat["added"] += oversight.upsert_cases(pending)
    stat["seconds"] = round(time.time() - t0, 1)
    return stat


def show(stat: dict) -> None:
    print(f"\nscanned {stat['rows']:,} dockets in {stat['seconds']:,}s")
    print(f"  with a cause field   {stat['with_cause']:,}")
    print(f"  cause contains 1983  {stat['hits'] + stat['out_of_scope_court']:,}"
          f"  ({stat['out_of_scope_court']:,} outside federal district courts)")
    print(f"  IN SCOPE             {stat['hits']:,}  "
          f"({stat['street']:,} street / {stat['prisoner']:,} prison)")
    print(f"    by cause '1983'    {stat.get('basis_cause', 0):,}  "
          f"<- the only figure comparable with the API")
    print(f"    by civil-rights NOS{stat.get('basis_nos', 0):>8,}  "
          f"<- no cause recorded; tagged match_basis='nos'")
    print(f"  written to oversight {stat['added']:,} new")
    if stat["no_date"]:
        print(f"  ⚠️ {stat['no_date']:,} with no filing date")
    if stat["by_state"]:
        top = sorted(stat["by_state"].items(), key=lambda kv: -kv[1])
        print(f"\n  {len(top)} states/territories. Largest:")
        for st, n in top[:12]:
            print(f"    {st}  {n:,}")
        print("    " + ", ".join(f"{s} {n:,}" for s, n in top[12:]))


# --------------------------------------------------------------------------
# Verify - the number, from a second source, or it did not happen
# --------------------------------------------------------------------------

def verify(state: str, token: str = "") -> None:
    """Compare what bulk gave us for a state against the API's own count.

    ⚠️ These are not expected to match exactly. Bulk is a quarterly snapshot
    and the API is live, so the API should be a little HIGHER by the age of the
    dump. A bulk number that is materially LOWER, or higher, means the filter
    is wrong - and that is the failure this exists to catch.
    """
    courts = STATE_COURTS.get(state.upper())
    if not courts:
        raise SystemExit(f"unknown state {state!r}")
    c = oversight.connect()
    marks = ",".join("?" * len(courts))
    mine = c.execute(
        f"SELECT COUNT(*) FROM cases WHERE court_id IN ({marks})",
        courts).fetchone()[0]
    street = c.execute(
        f"SELECT COUNT(*) FROM cases WHERE court_id IN ({marks}) "
        f"AND is_prisoner=0", courts).fetchone()[0]

    def api(q: str) -> int:
        p = {"type": "d", "q": q, "court": " ".join(courts)}
        url = "https://www.courtlistener.com/api/rest/v4/search/?" + \
            urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if token:
            req.add_header("Authorization", f"Token {token}")
        with urllib.request.urlopen(req, timeout=90) as r:
            return int(json.loads(r.read().decode())["count"])

    print(f"{state.upper()}  ({', '.join(courts)})")
    try:
        a_all = api("cause:(1983)")
        time.sleep(4)
        a_street = api("cause:(1983) NOT suitNature:(Prisoner)")
    except Exception as e:
        print(f"  API unreachable ({e}); bulk holds {mine:,} "
              f"({street:,} street)")
        return
    print(f"  all 1983    bulk {mine:>7,}   api {a_all:>7,}   "
          f"delta {mine - a_all:+,}")
    print(f"  street only bulk {street:>7,}   api {a_street:>7,}   "
          f"delta {street - a_street:+,}")
    worst = max(abs(mine - a_all), abs(street - a_street))
    ok = worst <= max(50, 0.03 * max(a_all, 1))
    print("  ✅ consistent" if ok else
          "  ⚠️ MATERIAL DISAGREEMENT - do not trust the bulk filter yet")


def purge(dry_run: bool = False) -> None:
    """Drop already-loaded rows the tightened NOS filter would now reject.

    🚨 IT RE-USES `nos_in_scope`, IT DOES NOT RE-WRITE THE RULE IN SQL.
    A hand-written DELETE ... LIKE '%jobs%' would be a second copy of the
    filter, free to drift from the real one, and the drift would show up as a
    review queue quietly full of employment cases again. So the distinct
    nature-of-suit values are pulled out, run through the same function the
    loader uses, and only the rejects are deleted.

    ⚠️ Rows matched by `cause` are never touched. The court said the case is a
    section 1983 claim; a nature-of-suit code does not get to overrule that.
    """
    c = oversight.connect()
    vals = [r[0] for r in c.execute(
        "SELECT DISTINCT suit_nature FROM cases WHERE match_basis='nos'")]
    bad = [v for v in vals if not nos_in_scope(v or "")]
    if not bad:
        print("nothing to purge - every loaded NOS value is still in scope")
        return
    marks = ",".join("?" * len(bad))
    n = c.execute(
        f"SELECT COUNT(*) FROM cases WHERE match_basis='nos' "
        f"AND suit_nature IN ({marks})", bad).fetchone()[0]
    print(f"{len(bad)} out-of-scope nature-of-suit values, {n:,} cases:")
    for v in sorted(bad)[:15]:
        cnt = c.execute("SELECT COUNT(*) FROM cases WHERE match_basis='nos' "
                        "AND suit_nature=?", (v,)).fetchone()[0]
        print(f"   {cnt:>7,}  {v!r}")
    if len(bad) > 15:
        print(f"   ... and {len(bad) - 15} more values")
    if dry_run:
        print("\n--dry-run: nothing deleted")
        return
    # Parties go with them. An orphaned party row is a name with no case
    # behind it, which is exactly the kind of unsourced claim this database
    # is built to refuse.
    c.execute(
        f"DELETE FROM case_parties WHERE docket_id IN ("
        f"  SELECT docket_id FROM cases WHERE match_basis='nos' "
        f"  AND suit_nature IN ({marks}))", bad)
    parties = c.total_changes
    c.execute(f"DELETE FROM cases WHERE match_basis='nos' "
              f"AND suit_nature IN ({marks})", bad)
    c.commit()
    print(f"\ndeleted {n:,} cases and their party rows")
    print(f"remaining: "
          f"{c.execute('SELECT COUNT(*) FROM cases').fetchone()[0]:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--scan", action="store_true",
                    help="count matches without writing anything")
    ap.add_argument("--load", action="store_true",
                    help="filter and write into oversight.db")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N rows (for a quick smoke test)")
    ap.add_argument("--purge", action="store_true",
                    help="drop loaded rows the current NOS filter rejects")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", metavar="STATE",
                    help="compare a state's bulk count against the API")
    ap.add_argument("--token", default="")
    a = ap.parse_args()

    if a.purge:
        purge(dry_run=a.dry_run)
        return
    if a.verify:
        verify(a.verify, a.token)
        return
    path = BULK_DIR / a.file
    if a.download:
        path = download(a.file)
    if not (a.scan or a.load):
        return
    if not path.exists():
        raise SystemExit(f"{path} not here - run --download first")
    show(scan(path, load=a.load, limit=a.limit))


if __name__ == "__main__":
    main()
