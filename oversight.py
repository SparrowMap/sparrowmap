"""SparrowMap - officer accountability storage.

The map answers "where was a police vehicle seen". This answers a different and
much older question: "what is on the record about the person driving it".

🚨 THIS IS A SEPARATE DATABASE FILE, ON PURPOSE.

`data/oversight.db`, not `sparrow.db`. Three reasons, and none of them is tidiness:

  * DIFFERENT LEGAL EXPOSURE. A sighting is an observation of a vehicle on a
    public road. A record here is a statement about a named human being. Those
    two things need different retention, different deletion, and different
    answers to a lawyer's questions, and a single file makes "show me
    everything you hold about this person, and delete it" a query nobody can
    verify by looking.
  * DIFFERENT PROVENANCE. Everything in sparrow.db was produced by this
    project's own cameras. Everything here was produced by somebody else - a
    federal court, a city clerk, a volunteer - so every row has to carry where
    it came from, and rows that cannot are not allowed in.
  * THE STALE-FILE TRAP. `D:\\LLM\\sparrow\\data\\sparrow.db` is a stale local
    copy of a database that actually lives on the box, and it answers queries
    with confident wrong numbers. Building the oversight database as its own
    file, on the desktop, and shipping it whole means that trap cannot be
    inherited.

--------------------------------------------------------------------------
THE ONE RULE THE SCHEMA EXISTS TO ENFORCE
--------------------------------------------------------------------------

**A document is not an allegation, and an allegation is not a fact about an
officer.** Those are three separate tables and nothing collapses them:

    sources      where a fact came from. Every row below points at one.
    cases        a court case. Exact, copied, never edited.
    case_parties a name that appeared on a case. Raw string preserved forever.
    officers     an identified human being. MINTED BY A PERSON, NEVER BY CODE.
    officer_refs the link "this party string is that officer" - and who said so.
    allegations  what somebody says happened. A claim, with a status, forever.
    vetting      append-only log of every human decision above.

The failure this prevents is the one that would end the project: an automated
name match writes "Officer Smith" onto a profile, the profile page renders it
as a sentence, and the site has just published a defamatory factual assertion
about a real person that no human ever approved. SparrowMap already learned
this shape once - matching a crop to a sighting by timestamp promoted an
unrelated vehicle - and the lesson is the same. Weak similarity is a QUEUE FOR
A HUMAN, never a write.

So: `courtlistener_fetch.py` fills `sources`, `cases` and `case_parties` and
stops. It cannot create an officer. It cannot create an allegation. The only
thing that creates those is a person, through the review surface, and the
`vetting` table records that it was them.

--------------------------------------------------------------------------
AND THE ONE RULE ABOUT WHAT NEVER GOES IN
--------------------------------------------------------------------------

Home address, personal phone, family members, vehicle plates belonging to the
officer personally. There is no column for any of it, for the same reason
db.py has no column for civilian plate text: a field that cannot be populated
cannot be leaked. What goes in is what the officer did **on duty, on the public
record** - which is the only thing this is for.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import Any, Iterable, Optional

from core import DATA, now

DB_PATH = DATA / "oversight.db"

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

-- WHERE A FACT CAME FROM. Every other table points at one of these.
--
-- There is no such thing as a row in this database without a source. A claim
-- whose origin cannot be named is not evidence, it is a rumour, and the whole
-- argument for publishing any of this is that it is checkable by the reader.
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- court | foia | settlement | news |
                                    -- post | report | dataset
    name        TEXT NOT NULL,      -- human label, e.g. "CourtListener RECAP"
    url         TEXT,               -- where a reader can go and check
    retrieved   REAL,               -- when WE fetched it
    detail      TEXT                -- JSON: query used, file hash, FOIA ref
);

-- ONE FEDERAL COURT CASE, copied from CourtListener's RECAP archive.
--
-- Exact transcription only. Nothing in this table is inferred, and nothing in
-- it is ever edited by hand - if it is wrong, it is wrong in PACER and the fix
-- is a correction record, not a quiet UPDATE.
CREATE TABLE IF NOT EXISTS cases (
    docket_id     INTEGER PRIMARY KEY,   -- CourtListener's id. Stable.
    source_id     INTEGER NOT NULL,
    court_id      TEXT NOT NULL,         -- mied | miwd | ...
    court_name    TEXT,
    docket_number TEXT,                  -- 2:16-cv-14103
    case_name     TEXT,
    cause         TEXT,                  -- "42:1983 Civil Rights Act"
    suit_nature   TEXT,                  -- "440 Civil rights other"
    date_filed    TEXT,                  -- ISO. Court dates are days, not
                                         -- instants, so these stay strings.
    date_terminated TEXT,
    assigned_to   TEXT,
    jury_demand   TEXT,
    absolute_url  TEXT,                  -- /docket/5236492/chami-v-carson/
    pacer_case_id TEXT,
    -- 🚨 IS THIS A STREET-POLICING CASE OR A PRISON CASE?
    --
    -- Two thirds of Michigan's federal 1983 docket is prisoner litigation
    -- against corrections staff (11,878 cases statewide, 3,950 of them
    -- non-prisoner, measured 2026-09-05). Both are law enforcement and both
    -- belong here, but they are different populations, different agencies and
    -- different evidentiary weight, and a count that mixes them silently
    -- answers the wrong question. So the split is stored, not recomputed.
    is_prisoner   INTEGER DEFAULT 0,
    first_seen    REAL,
    last_seen     REAL                   -- last time our fetch confirmed it
);

CREATE INDEX IF NOT EXISTS idx_cases_court ON cases(court_id, date_filed);
CREATE INDEX IF NOT EXISTS idx_cases_filed ON cases(date_filed);

-- A NAME THAT APPEARED ON A CASE. One row per party per case.
--
-- 🚨 `raw_name` IS SACRED. It is what the court actually wrote, and every
-- guess in the other columns is derived from it and can be recomputed. If the
-- classifier improves, the fix is to re-derive; if `raw_name` were normalised
-- in place, the evidence would be gone and the improvement impossible.
--
-- Every other column here is a GUESS and is named like one. PACER party lists
-- arrive UNORDERED - the plaintiff is not first, and in one sampled case was
-- fourth - so position carries no information and the role has to be inferred
-- from the string itself. It is often inferred wrong. That is tolerable
-- precisely because nothing downstream is allowed to act on it alone.
CREATE TABLE IF NOT EXISTS case_parties (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    docket_id   INTEGER NOT NULL,
    raw_name    TEXT NOT NULL,
    kind_guess  TEXT,       -- person | entity | placeholder | unclear
    role_guess  TEXT,       -- plaintiff | defendant | unclear
    -- 0..3. How much this string looks like a law enforcement officer.
    -- 3 = carries a rank or title ("Detroit Police Officer Carter")
    -- 2 = a person on a police-cause case, no title
    -- 1 = a surname-only placeholder ("Unknown Barton") - PACER's convention
    --     for a real defendant whose first name the plaintiff did not know
    -- 0 = entity, plaintiff, or junk
    officer_signal INTEGER DEFAULT 0,
    title_guess TEXT,       -- "Police Officer", "Deputy", "Sergeant"
    agency_hint TEXT,       -- "Detroit", from the party string or a co-party
    officer_id  INTEGER,    -- set ONLY by a human. See officer_refs.
    UNIQUE(docket_id, raw_name)
);

CREATE INDEX IF NOT EXISTS idx_parties_docket ON case_parties(docket_id);
CREATE INDEX IF NOT EXISTS idx_parties_signal ON case_parties(officer_signal);
CREATE INDEX IF NOT EXISTS idx_parties_name ON case_parties(raw_name);

-- AN IDENTIFIED HUMAN BEING.
--
-- 🚨 NOTHING IN THIS PROJECT MAY INSERT INTO THIS TABLE AUTOMATICALLY.
--
-- A row here is the project asserting that a specific person exists, works or
-- worked for a specific agency, and is the same person named in some set of
-- records. That is an editorial act. It gets made by a person, it gets logged
-- in `vetting`, and it stays reversible.
--
-- Note what is NOT a primary key: the badge number. A citizen sees a badge and
-- it is the natural identifier, but departments reuse and reassign them, so
-- two officers a decade apart can wear the same number. Badge is an attribute
-- with a validity window (see officer_badges), never an identity.
CREATE TABLE IF NOT EXISTS officers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    display     TEXT NOT NULL,      -- how the profile is titled
    surname     TEXT,
    given       TEXT,
    agency      TEXT,               -- the agency this profile is scoped to
    agency_state TEXT,
    rank        TEXT,
    status      TEXT DEFAULT 'draft',  -- draft | published | retracted
    created     REAL,
    created_by  TEXT,               -- WHO minted it. Never NULL.
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_officers_surname ON officers(surname);

-- A LAW ENFORCEMENT AGENCY, mined from case captions.
--
-- 🚨 THE ONE TABLE HERE THAT IS NOT ABOUT A PERSON, WHICH IS WHY IT CAN BE
-- BUILT AUTOMATICALLY. Everything else in this database describes a human
-- being and therefore waits for a human to approve it. "Oakland County" is an
-- organisation: getting it wrong is a wrong COUNT, not a wrong accusation, so
-- clustering may write here directly.
--
-- Identity is (state, kind, place), never the raw caption string. PACER
-- captions are truncated, typo'd and word-reversed in the same jurisdiction -
-- "COUNTY MACOMB", "Wayne County Officia", "COUNTY OFLENAWEE", "WASTENAW
-- COUNTY" are all real - so the raw string is an ALIAS and the canonical form
-- is derived. `place` is lowercased and unpunctuated for exactly that reason.
--
-- ⚠️ `cases` here is a count of captions that named this agency, which is NOT
-- the same as cases where the agency was a defendant, and NOT a measure of
-- misconduct. A big county appears often because it is big. Any page built on
-- this has to say so or it is publishing a league table it cannot support.
CREATE TABLE IF NOT EXISTS agencies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    state       TEXT NOT NULL,      -- from court_id, never parsed from text
    kind        TEXT NOT NULL,      -- county | city | township | village |
                                    -- police | sheriff
    place       TEXT NOT NULL,      -- 'oakland', 'hazel park'
    display     TEXT,               -- 'Oakland County, MI'
    cases       INTEGER DEFAULT 0,
    first_filed TEXT, last_filed TEXT,
    UNIQUE(state, kind, place)
);

CREATE INDEX IF NOT EXISTS idx_agencies_state ON agencies(state, cases);

-- Every raw caption string that resolved to an agency, and how.
--
-- Kept because the merge is a GUESS and has to be auditable: 'wastenaw'
-- folding into 'washtenaw' is almost certainly right, and the only way to
-- check is to see what was folded. A cluster nobody can inspect is a number
-- nobody should quote.
CREATE TABLE IF NOT EXISTS agency_aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agency_id   INTEGER NOT NULL,
    raw         TEXT NOT NULL,      -- as it appeared in the caption
    place_seen  TEXT,               -- what canon() derived before merging
    hits        INTEGER DEFAULT 0,
    merged      INTEGER DEFAULT 0,  -- 1 = fuzzy-merged, not an exact match
    similarity  REAL,               -- the ratio that justified the merge
    UNIQUE(agency_id, raw)
);

-- Which agency a case named. Many-to-many: a caption can name a county AND
-- a city, and both are true.
CREATE TABLE IF NOT EXISTS case_agencies (
    docket_id   INTEGER NOT NULL,
    agency_id   INTEGER NOT NULL,
    PRIMARY KEY (docket_id, agency_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_case_agencies_a ON case_agencies(agency_id);

-- A badge number, with the window it is known to have been worn.
-- Separate table because it is one-to-many in both directions over time.
CREATE TABLE IF NOT EXISTS officer_badges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    officer_id  INTEGER NOT NULL,
    badge       TEXT NOT NULL,
    agency      TEXT,
    from_date   TEXT,
    to_date     TEXT,
    source_id   INTEGER
);

-- "THIS PARTY STRING IS THAT OFFICER" - and who decided so.
--
-- The link is the editorial act, so it is a row with an author, not a foreign
-- key somebody set. Kept separate from case_parties.officer_id so that undoing
-- an identification leaves a trace instead of erasing one.
CREATE TABLE IF NOT EXISTS officer_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    officer_id  INTEGER NOT NULL,
    party_id    INTEGER,            -- case_parties.id, when it came from a case
    source_id   INTEGER,
    confidence  TEXT,               -- certain | probable | possible
    decided_by  TEXT NOT NULL,
    decided_at  REAL,
    reason      TEXT,
    withdrawn   INTEGER DEFAULT 0
);

-- WHAT SOMEBODY SAYS HAPPENED.
--
-- 🚨 THIS IS A CLAIM AND IT IS RENDERED AS ONE, FOREVER.
--
-- Section 230 protects the host from liability for what a user wrote. It does
-- not protect the host's own summary of what a user wrote. The moment a
-- profile page says "Officer X did Y" in the site's voice, that sentence is
-- the site's speech and it is defamation exposure. So the body is stored
-- verbatim, the status is stored beside it, and the renderer is required to
-- show both - an unverified allegation is displayed as an unverified
-- allegation or it is not displayed.
--
-- `status` is a ladder and every rung above the first needs a named human:
--   submitted   somebody typed it. Not public.
--   reviewing   a volunteer has picked it up.
--   corroborated  a second independent account or a witness. Still a claim.
--   documented  a court filing, settlement, or agency record backs it. The
--               DOCUMENT is the publishable thing; the claim rides along.
--   rejected    checked and not supported. Kept, not deleted - a project that
--               deletes what it disproved cannot show its own error rate.
CREATE TABLE IF NOT EXISTS allegations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    officer_id  INTEGER,            -- may be NULL: "I don't know who it was"
    agency      TEXT,
    incident_date TEXT,             -- may be vague. There is no statute of
                                    -- limitations on being on this list.
    lat REAL, lon REAL,             -- optional, so it can land on the map
    body        TEXT NOT NULL,      -- VERBATIM. Never rewritten.
    status      TEXT DEFAULT 'submitted',
    submitted_at REAL,
    submitter   TEXT,               -- pseudonymous handle or NULL
    contact     TEXT,               -- never served publicly
    reviewed_by TEXT,
    reviewed_at REAL,
    source_id   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_alleg_officer ON allegations(officer_id);
CREATE INDEX IF NOT EXISTS idx_alleg_status ON allegations(status);

-- APPEND-ONLY LOG OF EVERY HUMAN DECISION.
--
-- The project's claim is "no assertion about a person reaches the public
-- without a named person approving it". That is provable or it is marketing,
-- and proving it needs a table nothing updates and nothing deletes.
CREATE TABLE IF NOT EXISTS vetting (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    who         TEXT NOT NULL,
    action      TEXT NOT NULL,      -- mint_officer | link_party | publish |
                                    -- retract | reject | correct
    target      TEXT NOT NULL,      -- "officer:12", "allegation:44"
    detail      TEXT
);

-- INGEST BOOKKEEPING. One row per fetch run.
--
-- 🚨 THE CURSOR IS THE POINT. CourtListener's search API pages 20 at a time
-- and throttles hard (documented at 5 requests/minute for authenticated
-- users). Michigan alone is 11,878 cases = 594 pages, so a full backfill is
-- not one sitting - it is a job that gets interrupted and has to pick up
-- exactly where it stopped. A fetcher that restarts from page one on every
-- run never finishes and quietly re-reads the same 400 cases forever.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT NOT NULL,
    query       TEXT,               -- the exact query, so --resume can verify
                                    -- it is resuming the SAME sweep
    cursor      TEXT,               -- opaque, from the API's `next` link
    started     REAL,
    updated     REAL,
    pages       INTEGER DEFAULT 0,
    seen        INTEGER DEFAULT 0,  -- results returned
    added       INTEGER DEFAULT 0,  -- cases new to us
    total       INTEGER,            -- what the API said the result set was
    uniq        INTEGER DEFAULT 0,  -- DISTINCT dockets the run pulled out.
                                    -- `seen` counts rows and the stream
                                    -- repeats, so this is the honest number.
    state       TEXT DEFAULT 'running'   -- running | done | short | split |
                                         -- throttled | error
);

-- WHICH DOCKETS A RUN ACTUALLY SAW.
--
-- 🚨 THIS IS THE PROOF THAT A SWEEP COVERED WHAT IT CLAIMED.
--
-- `runs.seen` counts RESULT ROWS, and the search stream repeats dockets, so it
-- overstates coverage. The API gives an exact `count` per date window, so the
-- honest check is "how many DISTINCT dockets did we get out, against how many
-- it promised" - and that check has to survive the sweep being interrupted and
-- resumed, which an in-memory set does not.
--
-- Michigan proved why: the first full sweep reported "done", 2,748 rows seen
-- against a promised 3,950, and only ~2,150 of those rows were distinct. The
-- run was 1,202 cases short and said nothing.
--
-- Rows are PRUNED once a run verifies, so this stays a scratch pad and not a
-- second copy of the docket table.
CREATE TABLE IF NOT EXISTS run_docket (
    run_id      INTEGER NOT NULL,
    docket_id   INTEGER NOT NULL,
    PRIMARY KEY (run_id, docket_id)
) WITHOUT ROWID;
"""


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

# Columns added after this database first existed. CREATE TABLE IF NOT EXISTS
# is a no-op on a table that is already there, so a new column in SCHEMA above
# never reaches a live file. Same trap, and same fix, as db.MIGRATIONS.
MIGRATIONS = [
    ("runs", "uniq", "INTEGER DEFAULT 0"),
    # 🚨 HOW WE DECIDED THIS CASE BELONGS HERE: 'cause' | 'nos' | 'api'.
    #
    # The bulk dump's `cause` field ("42:1983 Civil Rights Act") is the court's
    # own statement of what the suit is, and it is the only filter comparable
    # with the API's `cause:(1983)` count - the one external number available
    # to check against. But it is SPARSE: in a 200,000-row slice, 60 rows
    # carried a 1983 cause while 3,939 carried a civil-rights NATURE OF SUIT
    # (440 Civil Rights Other, 550/555 Prisoner Civil Rights) with no cause
    # recorded at all.
    #
    # Nearly all of those are section 1983 cases - it is the vehicle for
    # essentially all civil-rights suits against state actors - and dropping
    # them would throw away most of the country to keep one number tidy.
    # Loading them silently would be worse: every count would stop being
    # comparable to the API, and the next coverage check would be meaningless.
    #
    # So both are loaded and the basis is RECORDED. Counts that need to be
    # checked against the API filter on 'cause'; the review queue uses
    # everything; and nobody has to guess which is which later.
    ("cases", "match_basis", "TEXT"),
    # 🚨 IS A POLICE OFFICER ACTUALLY INVOLVED IN THIS CASE?
    #
    # 'police'      evidenced: a ranked defendant, a police/sheriff agency, or a
    #               caption that names one.
    # 'corrections' prison or jail staff. Law enforcement, but not police, and a
    #               different institution with different records - kept apart so
    #               it can be included or excluded deliberately.
    # 'other'       evidenced NOT police: the named defendants are prosecutors,
    #               judges, schools, hospitals, attorneys.
    # NULL          UNKNOWN. Not "no" - just no evidence yet.
    #
    # The distinction between 'other' and NULL is the whole point. Section 1983
    # reaches every state actor, so most of this database is not about police -
    # but at the time of writing, party names exist for only 7.4% of Michigan
    # cases, and a caption reading "Smith v. Jones" is silent about whether
    # Jones wore a badge. Marking those 'other' would be a guess dressed as a
    # finding, and deleting them would throw away police cases we simply cannot
    # identify yet. They stay NULL and get re-judged as enrichment fills in.
    ("cases", "police", "TEXT"),
    ("cases", "police_why", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def connect() -> sqlite3.Connection:
    """One connection per thread, same contract as db.connect().

    The schema runs once per process, not once per connection - see the long
    note in db.py about a 165-second write-lock stall. Same reasoning, and this
    database will eventually be read by the same threaded hub.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        global _SCHEMA_READY
        if not _SCHEMA_READY:
            with _SCHEMA_LOCK:
                if not _SCHEMA_READY:
                    conn.executescript(SCHEMA)
                    _migrate(conn)
                    _SCHEMA_READY = True
        _local.conn = conn
    return conn


def close_thread() -> None:
    """Release this thread's connection. Safe when there isn't one."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        _local.conn = None
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Party classification
#
# Everything below is a guess about a string a court clerk typed. It is worth
# doing because it turns 11,878 undifferentiated cases into a ranked queue, and
# it is worth distrusting because the strings are genuinely ambiguous.
# --------------------------------------------------------------------------

# Ranks and titles that appear INSIDE a PACER party name. Seen live in
# E.D./W.D. Michigan party lists: "Detroit Police Officer Carter".
_TITLES = [
    ("police officer", "Police Officer"),
    ("correctional officer", "Correctional Officer"),
    ("corrections officer", "Correctional Officer"),
    ("probation officer", "Probation Officer"),
    ("parole officer", "Parole Officer"),
    ("deputy sheriff", "Deputy Sheriff"),
    ("undersheriff", "Undersheriff"),
    ("sheriff", "Sheriff"),
    ("trooper", "Trooper"),
    ("detective", "Detective"),
    ("sergeant", "Sergeant"),
    ("sgt.", "Sergeant"),
    ("sgt ", "Sergeant"),
    ("lieutenant", "Lieutenant"),
    ("captain", "Captain"),
    ("corporal", "Corporal"),
    ("patrolman", "Patrolman"),
    ("marshal", "Marshal"),
    ("constable", "Constable"),
    ("warden", "Warden"),
    ("chief of police", "Chief of Police"),
    ("police chief", "Chief of Police"),
    ("deputy", "Deputy"),
    ("officer", "Officer"),
]

# Strings that mean "an organisation", not "a human being". A profile page
# about "Kent, County of" is a category error, and worse, it dilutes the
# queue a volunteer is meant to work through.
_ENTITY_WORDS = (
    "county of", "city of", "township", "village of", "state of", "borough",
    "department", "dept", "police dep", "sheriff's office", "sheriffs office",
    "corrections", "mdoc", "jail", "prison", "correctional facility",
    "district court", "circuit court", "court", "board", "commission",
    "authority", "bureau", "agency", "municipality",
    "university", "college", "school district", "hospital", "medical center",
    "healthcare", "health care", "clinic",
    "inc.", " inc", "llc", "l.l.c", "corp", "company", " co.", "ltd",
    "association", "union", "trust", "insurance", "services", "systems",
    "united states", "u.s.a", "usa", "federal bureau", "f.b.i", "fbi",
    "homeland security", "immigration",
    # 🚨 LAW OFFICES ARE NOT PEOPLE. "Saginaw County Prosecuting Attorney's
    # Office" was reaching the officer queue as a signal-2 PERSON, because the
    # entity list knew "sheriff's office" but not any other kind of office.
    # Matthew caught it on Clark v. County of Saginaw.
    "attorney's office", "attorneys office", "attorney general",
    "prosecuting attorney", "prosecutor", "district attorney",
    "corporation counsel", "public defender", "clerk's office",
    "office of the", "'s office", "s office",
)

# Placeholders and procedural artefacts. Not people, not organisations - noise.
_JUNK_EXACT = {
    "unknown party", "unknown parties", "unknown party 1", "unknown party 2",
    "unknown", "john doe", "jane doe", "john does", "jane does", "does",
    "mediator", "interested party", "amicus curiae", "amicus",
    "all defendants", "all plaintiffs", "et al", "et al.",
    "unknown defendants", "unknown officers", "unnamed defendants",
}

_JUNK_PREFIX = ("unknown party", "unknown parties", "john doe", "jane doe",
                "unknown defendant", "unknown officer", "unnamed ")

# 🚨 A PLACEHOLDER CAN CARRY A RANK, AND THE RANK IS WHAT FOOLED THE FIRST
# VERSION. Real rows out of the live fetch: "Officers Jane/John Doe",
# "Police Officer John Doe", "OFFICER JOHN DOE 1". Every one of them matched a
# title, so every one came out as a signal-3 officer candidate - the top of
# the review queue filled with three different spellings of nobody.
#
# Matching a PREFIX was never going to catch these; the placeholder sits after
# the title. So the Doe test runs over the whole string, and it beats the
# title test rather than losing to it.
_DOE = re.compile(r"\b(?:john|jane)\s*/?\s*(?:john|jane)?\s*doe\b"
                  r"|\bdoes?\s*#?\s*\d*\s*(?:-|through|to)?\s*\d*$"
                  r"|\bunnamed\b|\bunidentified\b", re.I)

# An OFFICE, not the human holding it. "Wayne County Sheriff" is a party in
# their official capacity - a profile page about it would be a category error,
# and it outranked real named deputies in the queue. Distinguished from
# "Sheriff Bouchard", which is a person.
_OFFICE = re.compile(
    r"\bcounty\s+(?:sheriff|prosecutor|clerk|jail|executive)\b"
    r"|\bsheriff'?s?\s+(?:office|department|dept)\b"
    r"|\b(?:police|fire)\s+(?:department|dept)\b"
    r"|\bin\s+(?:his|her|their)\s+official\s+capacity\b", re.I)

# 🚨 A PRISONER NUMBER IS A PLAINTIFF SIGNAL, NOT AN OFFICER SIGNAL.
#
# PACER attaches the MDOC or BOP register number to the incarcerated party:
# "Kyle 872579", "Ade Brown #884273", "Terrell Churchwell #21369-040". Whoever
# carries one is the person who filed, not the person they filed against, and
# missing that would put every prisoner-plaintiff in the state into an
# officer-review queue - the exact inversion this project must not make.
_PRISON_NUM = re.compile(r"(#\s*\d{5,}|(?<![\d#])\b\d{6,}\b|\b\d{5}-\d{3}\b)")

# "on behalf of her minor son", "as next friend of", "as personal
# representative of the estate of" - civil-procedure phrasing that only ever
# attaches to a plaintiff.
_PLAINTIFF_PHRASES = ("on behalf of", "next friend", "personal representative",
                      "as guardian", "estate of", "individually and on behalf")

# PACER's convention for a defendant whose first name the plaintiff never
# learned: the given name is literally "Unknown". "Unknown Barton" is a real
# officer named Barton, and those rows are some of the most valuable in the
# database - a surname a plaintiff knew well enough to sue.
#
# ⚠️ The whole trick is telling that apart from "Unknown Parties", which is
# nobody. A bare /^unknown \w+$/ matches BOTH, and did: "Unknown Parties" came
# out of the first version as a person with an officer signal. The second word
# has to be checked against the words the courts use for a placeholder.
_UNKNOWN_GIVEN = re.compile(r"^unknown\s+([a-z][a-z'\-\.]+)$", re.I)
_NOT_A_SURNAME = {
    "party", "parties", "defendant", "defendants", "plaintiff", "plaintiffs",
    "officer", "officers", "person", "persons", "individual", "individuals",
    "employee", "employees", "agent", "agents", "staff", "entity", "entities",
    "corporation", "company", "others", "name", "names", "doe", "does",
    "respondent", "respondents", "deputies", "supervisors",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def classify_party(raw: str, case_name: str = "", cause: str = "",
                   co_parties: Optional[Iterable[str]] = None) -> dict:
    """Guess what a PACER party string is. Every value returned is a guess.

    Returns kind_guess / role_guess / officer_signal / title_guess /
    agency_hint. Nothing here is allowed to create an officer - the caller
    stores these so a human can work the queue in a useful order.
    """
    name = _norm(raw)
    low = name.lower()
    out = {"kind_guess": "unclear", "role_guess": "unclear",
           "officer_signal": 0, "title_guess": None, "agency_hint": None}
    if not name:
        return out

    # --- is this PACER's "surname known, first name not" form? -------------
    m = _UNKNOWN_GIVEN.match(name)
    unknown_given = bool(m) and m.group(1).lower().rstrip(".") \
        not in _NOT_A_SURNAME

    # --- junk, so nothing below wastes a signal on it ----------------------
    # The Doe test is first and unconditional: a placeholder wearing a rank is
    # still a placeholder, and it is the one that reached the top of the queue.
    if _DOE.search(name):
        out["kind_guess"] = "placeholder"
        return out
    if not unknown_given and (low in _JUNK_EXACT
                              or low.startswith(_JUNK_PREFIX)):
        out["kind_guess"] = "placeholder"
        return out

    # The caption's left-hand side. "Chami v. Carson" -> "chami".
    caption_left = ""
    if case_name and " v. " in case_name.lower():
        caption_left = _PRISON_NUM.sub(
            "", case_name.lower().split(" v. ", 1)[0]).strip(" ,.")

    # --- organisation? -----------------------------------------------------
    if _OFFICE.search(name) or any(w in low for w in _ENTITY_WORDS):
        out["kind_guess"] = "entity"
        out["role_guess"] = "plaintiff" if (
            caption_left and caption_left in low) else "defendant"
        # An agency named as a co-defendant is the best clue we get about
        # which department the human co-defendants worked for. It is a HINT.
        return out

    # --- title / rank ------------------------------------------------------
    # 🚨 WORD BOUNDARIES, NOT SUBSTRINGS. `"marshal" in low` matches the surname
    # MARSHALL, which put a plain "Marshall" (5 cases) and a prisoner-plaintiff
    # "Marshall 732012" at the TOP of Michigan's officer queue - the highest-
    # visibility place in the whole project to be wrong about a person.
    for needle, label in _TITLES:
        m = re.search(r"\b" + re.escape(needle.strip()) + r"\b", low)
        if m:
            out["title_guess"] = label
            # Whatever sits before the title is usually the agency:
            # "Detroit Police Officer Carter" -> "Detroit".
            #
            # ⚠️ Slice at the MATCH position, not low.index(needle). Several
            # needles carry a trailing space ("sgt "), so after the switch to
            # word-boundary matching `\bsgt\b` happily matched "Sgt. Bryan"
            # while low.index("sgt ") raised ValueError and killed the whole
            # reclassify pass.
            head = _norm(name[: m.start()])
            if head and len(head) < 40:
                out["agency_hint"] = head
            break

    # --- plaintiff signals -------------------------------------------------
    is_plaintiff = False
    if _PRISON_NUM.search(name):
        is_plaintiff = True
    if any(p in low for p in _PLAINTIFF_PHRASES):
        is_plaintiff = True
    if caption_left and (caption_left in low or low in caption_left):
        is_plaintiff = True
    # 🚨 A CHILD OR A DECEDENT IS NAMED TWICE, AND ONLY ONE OF THEM LOOKS LIKE
    # A PLAINTIFF. Real caption: "Nadi Bazzi, on behalf of her minor son
    # Ibrahim Bazzi" is one party, and "Ibrahim Bazzi" is ANOTHER party in the
    # same list. The mother is caught by the phrase; the son, standing alone,
    # was coming out as a defendant with an officer signal - a minor filed as
    # a victim, queued for review as a police officer. The fix is already in
    # the data: if this name is quoted inside a co-party that carries a
    # next-friend or estate phrase, this is the person being represented.
    if not is_plaintiff and co_parties:
        for other in co_parties:
            ol = (other or "").lower()
            if low != ol and low in ol and any(p in ol
                                               for p in _PLAINTIFF_PHRASES):
                is_plaintiff = True
                break

    # --- settle ------------------------------------------------------------
    # 🚨 AN INMATE NUMBER BEATS A TITLE. Everything else defers to a rank -
    # plaintiffs are not "Sergeant" - but a party carrying an MDOC/BOP register
    # number is the person who FILED, and no officer defendant is ever booked
    # into the prison they are being sued over. "Marshall 732012" matched the
    # rank `marshal` on a surname and outranked its own prisoner number.
    if _PRISON_NUM.search(name):
        out["kind_guess"] = "person"
        out["role_guess"] = "plaintiff"
        out["officer_signal"] = 0
        return out
    if is_plaintiff and not out["title_guess"]:
        # A title beats the caption match: plaintiffs are not "Sergeant".
        out["kind_guess"] = "person"
        out["role_guess"] = "plaintiff"
        out["officer_signal"] = 0
        return out

    out["kind_guess"] = "person"
    out["role_guess"] = "defendant"
    if out["title_guess"]:
        out["officer_signal"] = 3
    elif unknown_given:
        out["officer_signal"] = 1
    elif "1983" in (cause or ""):
        # A named human defendant on a section 1983 case. Usually an officer,
        # sometimes a prosecutor, a nurse, a landlord or a judge - which is
        # exactly why this is a 2 and not a 3.
        out["officer_signal"] = 2

    if not out["agency_hint"] and co_parties:
        for c in co_parties:
            cl = (c or "").lower()
            if any(w in cl for w in ("police dep", "sheriff", "county of",
                                     "city of", "township", "corrections")):
                out["agency_hint"] = _norm(c)[:60]
                break
    return out


# --------------------------------------------------------------------------
# Writes - only the ones phase 1 needs. Officers and allegations are minted
# through the review surface, by a person, not from here.
# --------------------------------------------------------------------------

def add_source(kind: str, name: str, url: str = "", detail: str = "") -> int:
    c = connect()
    cur = c.execute(
        "INSERT INTO sources (kind, name, url, retrieved, detail) "
        "VALUES (?,?,?,?,?)", (kind, name, url, now(), detail))
    c.commit()
    return int(cur.lastrowid)


def upsert_case(row: dict) -> bool:
    """Insert or refresh one case. Returns True if it was new to us.

    Refresh rather than ignore, because a live docket changes: a case filed
    last year gets terminated, gets reassigned, gets its nature-of-suit code
    corrected. `first_seen` is preserved so the project can always say how long
    it has been holding a record.
    """
    c = connect()
    existing = c.execute("SELECT first_seen FROM cases WHERE docket_id=?",
                         (row["docket_id"],)).fetchone()
    t = now()
    if existing:
        c.execute(
            "UPDATE cases SET court_name=?, docket_number=?, case_name=?, "
            "cause=?, suit_nature=?, date_filed=?, date_terminated=?, "
            "assigned_to=?, jury_demand=?, absolute_url=?, pacer_case_id=?, "
            "is_prisoner=?, last_seen=? WHERE docket_id=?",
            (row.get("court_name"), row.get("docket_number"),
             row.get("case_name"), row.get("cause"), row.get("suit_nature"),
             row.get("date_filed"), row.get("date_terminated"),
             row.get("assigned_to"), row.get("jury_demand"),
             row.get("absolute_url"), row.get("pacer_case_id"),
             int(row.get("is_prisoner", 0)), t, row["docket_id"]))
        c.commit()
        return False
    c.execute(
        "INSERT INTO cases (docket_id, source_id, court_id, court_name, "
        "docket_number, case_name, cause, suit_nature, date_filed, "
        "date_terminated, assigned_to, jury_demand, absolute_url, "
        "pacer_case_id, is_prisoner, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["docket_id"], row["source_id"], row.get("court_id"),
         row.get("court_name"), row.get("docket_number"), row.get("case_name"),
         row.get("cause"), row.get("suit_nature"), row.get("date_filed"),
         row.get("date_terminated"), row.get("assigned_to"),
         row.get("jury_demand"), row.get("absolute_url"),
         row.get("pacer_case_id"), int(row.get("is_prisoner", 0)), t, t))
    c.commit()
    return True


def upsert_cases(batch: list) -> int:
    """Insert or refresh many cases in ONE transaction. Returns rows new to us.

    ⚠️ `upsert_case` commits per call, which is right for a 20-row API page and
    catastrophic for a bulk load: tens of thousands of individual commits are
    tens of thousands of fsyncs, and they dominate a run that should be bound
    by decompression. Same logic, one transaction.
    """
    if not batch:
        return 0
    c = connect()
    t = now()
    ids = [row["docket_id"] for row in batch]
    marks = ",".join("?" * len(ids))
    known = {r[0] for r in c.execute(
        f"SELECT docket_id FROM cases WHERE docket_id IN ({marks})", ids)}
    new = [r for r in batch if r["docket_id"] not in known]
    old = [r for r in batch if r["docket_id"] in known]
    if new:
        c.executemany(
            "INSERT INTO cases (docket_id, source_id, court_id, court_name, "
            "docket_number, case_name, cause, suit_nature, date_filed, "
            "date_terminated, assigned_to, jury_demand, absolute_url, "
            "pacer_case_id, is_prisoner, match_basis, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["docket_id"], r["source_id"], r.get("court_id"),
              r.get("court_name"), r.get("docket_number"), r.get("case_name"),
              r.get("cause"), r.get("suit_nature"), r.get("date_filed"),
              r.get("date_terminated"), r.get("assigned_to"),
              r.get("jury_demand"), r.get("absolute_url"),
              r.get("pacer_case_id"), int(r.get("is_prisoner", 0)),
              r.get("match_basis"), t, t)
             for r in new])
    if old:
        c.executemany(
            "UPDATE cases SET court_name=?, docket_number=?, case_name=?, "
            "cause=?, suit_nature=?, date_filed=?, date_terminated=?, "
            "assigned_to=?, jury_demand=?, absolute_url=?, pacer_case_id=?, "
            "is_prisoner=?, match_basis=COALESCE(match_basis,?), last_seen=? "
            "WHERE docket_id=?",
            [(r.get("court_name"), r.get("docket_number"), r.get("case_name"),
              r.get("cause"), r.get("suit_nature"), r.get("date_filed"),
              r.get("date_terminated"), r.get("assigned_to"),
              r.get("jury_demand"), r.get("absolute_url"),
              r.get("pacer_case_id"), int(r.get("is_prisoner", 0)),
              r.get("match_basis"), t,
              r["docket_id"]) for r in old])
    c.commit()
    return len(new)


def upsert_parties(docket_id: int, names: Iterable[str], case_name: str = "",
                   cause: str = "") -> int:
    """Store a case's party strings with their guesses. Returns rows added.

    ⚠️ Re-running this does NOT overwrite an existing row. A volunteer may have
    already linked that party to an officer, and a re-fetch must never quietly
    undo human work - the classifier improving is not a reason to discard a
    decision a person made.
    """
    names = [_norm(n) for n in names if _norm(n)]
    if not names:
        return 0
    c = connect()
    added = 0
    for n in names:
        g = classify_party(n, case_name=case_name, cause=cause,
                           co_parties=[x for x in names if x != n])
        cur = c.execute(
            "INSERT OR IGNORE INTO case_parties (docket_id, raw_name, "
            "kind_guess, role_guess, officer_signal, title_guess, agency_hint) "
            "VALUES (?,?,?,?,?,?,?)",
            (docket_id, n, g["kind_guess"], g["role_guess"],
             g["officer_signal"], g["title_guess"], g["agency_hint"]))
        added += cur.rowcount or 0
    c.commit()
    return added


# --------------------------------------------------------------------------
# Run bookkeeping
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Is a police officer involved?
# --------------------------------------------------------------------------

# Words that mean POLICE specifically. Deliberately not "law enforcement" in
# the broad sense: a prosecutor enforces the law and is not what this is for.
_POLICE_RE = re.compile(
    r"\bpolice\b|\bsheriff|\bstate\s+police\b|\btrooper\b|\bconstable\b"
    r"|\bmarshal\b|\bpublic\s+safety\b|\bhighway\s+patrol\b|\bp\.?d\.?\b"
    r"|\bdetective\b|\bpatrolman\b|\bdeputy\b", re.I)

# Prison and jail staff. Law enforcement, different institution.
_CORRECTIONS_RE = re.compile(
    r"\bcorrection|\bwarden\b|\bprison\b|\bjail\b|\bpenitentiary\b|\bmdoc\b"
    r"|\bdepartment\s+of\s+corrections\b|\bparole\b|\bprobation\b", re.I)

# State actors that are emphatically NOT police, used only to say 'other' when
# nothing police-shaped is present.
_NONPOLICE_RE = re.compile(
    r"\battorney|\bprosecut|\bjudge\b|\bcourt\b|\bclerk\b|\bschool\b"
    r"|\buniversity\b|\bhospital\b|\bmedical\b|\bnurse\b|\bdoctor\b"
    r"|\bsocial\s+work|\bchild\s+protective\b|\bhousing\b|\bwelfare\b", re.I)


def classify_case_police(case_name: str, cause: str, nos: str,
                         party_names=(), agency_kinds=(),
                         has_rank: bool = False) -> tuple:
    """(verdict, why) for one case. verdict: police | corrections | other | None.

    🚨 None MEANS "NO EVIDENCE YET", NOT "NO".
    The commonest caption in this database is two surnames, which says nothing
    about anyone's job. Calling that 'other' would be inventing a finding, and
    it would hide real police cases the moment the enrichment that identifies
    them finally runs. Absence of evidence gets its own value.
    """
    hay = " | ".join([case_name or ""] + [p or "" for p in party_names])
    kinds = set(agency_kinds or ())

    if has_rank:
        return "police", "a party carries a police rank"
    if kinds & {"police", "sheriff"}:
        return "police", "linked to a police or sheriff agency"
    m = _POLICE_RE.search(hay)
    if m:
        return "police", f"names {m.group(0).strip().lower()!r}"
    m = _CORRECTIONS_RE.search(hay)
    if m:
        return "corrections", f"names {m.group(0).strip().lower()!r}"
    # Only call it 'other' when we can actually SEE who was sued and none of
    # them is police-shaped. With no party names, we know nothing.
    if party_names:
        m = _NONPOLICE_RE.search(hay)
        if m:
            return "other", f"named defendants are {m.group(0).strip().lower()!r}"
        return "other", "parties known, none police-shaped"
    return None, "no party names yet - unknown, not ruled out"


def classify_police(dry_run: bool = False, state_courts=None) -> dict:
    """Re-derive `police` for every case from whatever evidence exists now.

    Cheap and repeatable on purpose: every time party enrichment adds names,
    running this again promotes cases out of UNKNOWN. It is never destructive.
    """
    c = connect()
    where, args = "", []
    if state_courts:
        where = f"WHERE court_id IN ({','.join('?' * len(state_courts))})"
        args = list(state_courts)
    rows = c.execute(f"SELECT docket_id, case_name, cause, suit_nature, police "
                     f"FROM cases {where}", args).fetchall()
    parties: dict = {}
    ranks: set = set()
    for r in c.execute("SELECT docket_id, raw_name, officer_signal "
                       "FROM case_parties"):
        parties.setdefault(r["docket_id"], []).append(r["raw_name"])
        if r["officer_signal"] == 3:
            ranks.add(r["docket_id"])
    akinds: dict = {}
    for r in c.execute("SELECT ca.docket_id, a.kind FROM case_agencies ca "
                       "JOIN agencies a ON a.id = ca.agency_id"):
        akinds.setdefault(r["docket_id"], set()).add(r["kind"])

    stat = {"examined": len(rows), "police": 0, "corrections": 0,
            "other": 0, "unknown": 0, "changed": 0}
    batch = []
    for r in rows:
        did = r["docket_id"]
        v, why = classify_case_police(
            r["case_name"] or "", r["cause"] or "", r["suit_nature"] or "",
            parties.get(did, []), akinds.get(did, set()), did in ranks)
        stat[v or "unknown"] += 1
        if v != r["police"]:
            stat["changed"] += 1
            batch.append((v, why, did))
    if not dry_run and batch:
        c.executemany("UPDATE cases SET police=?, police_why=? WHERE docket_id=?",
                      batch)
        c.commit()
    return stat


def reclassify(dry_run: bool = False) -> dict:
    """Re-derive every party guess from the preserved `raw_name`.

    🚨 THIS IS WHY raw_name IS NEVER NORMALISED IN PLACE. The classifier is a
    pile of heuristics about strings court clerks typed, and it WILL be wrong
    in ways only real data reveals - the first live fetch put "Officers
    Jane/John Doe" and "Wayne County Sheriff" at the top of the officer queue.
    Fixing that has to be a re-derivation over rows already stored, because
    `upsert_parties` is INSERT OR IGNORE and a re-fetch will not touch them.

    ⚠️ Rows a human has already linked to an officer are SKIPPED. A better
    heuristic is not a reason to overwrite a decision a person made, and a
    volunteer's identification surviving a code change is the whole difference
    between a database and a scratchpad.
    """
    c = connect()
    rows = c.execute(
        "SELECT p.id, p.docket_id, p.raw_name, p.kind_guess, p.role_guess, "
        "       p.officer_signal, c.case_name, c.cause "
        "FROM case_parties p LEFT JOIN cases c ON c.docket_id = p.docket_id "
        "WHERE p.officer_id IS NULL").fetchall()
    by_docket: dict = {}
    for r in rows:
        by_docket.setdefault(r["docket_id"], []).append(r["raw_name"])
    changed, demoted = 0, 0
    for r in rows:
        peers = [n for n in by_docket.get(r["docket_id"], [])
                 if n != r["raw_name"]]
        g = classify_party(r["raw_name"], case_name=r["case_name"] or "",
                           cause=r["cause"] or "", co_parties=peers)
        if (g["kind_guess"] == r["kind_guess"]
                and g["role_guess"] == r["role_guess"]
                and g["officer_signal"] == r["officer_signal"]):
            continue
        changed += 1
        if g["officer_signal"] < (r["officer_signal"] or 0):
            demoted += 1
        if not dry_run:
            c.execute(
                "UPDATE case_parties SET kind_guess=?, role_guess=?, "
                "officer_signal=?, title_guess=?, agency_hint=? WHERE id=?",
                (g["kind_guess"], g["role_guess"], g["officer_signal"],
                 g["title_guess"], g["agency_hint"], r["id"]))
    if not dry_run:
        c.commit()
    return {"examined": len(rows), "changed": changed, "demoted": demoted}


def start_run(tool: str, query: str, total: Optional[int] = None) -> int:
    c = connect()
    cur = c.execute(
        "INSERT INTO runs (tool, query, started, updated, total, state) "
        "VALUES (?,?,?,?,?, 'running')", (tool, query, now(), now(), total))
    c.commit()
    return int(cur.lastrowid)


def resumable_run(tool: str, query: str) -> Optional[sqlite3.Row]:
    """The most recent unfinished run for exactly this query, if any.

    Matching on the query string is deliberate. Resuming a cursor that belongs
    to a DIFFERENT sweep would silently skip whatever the new query was meant
    to cover, and the run would report success.
    """
    c = connect()
    return c.execute(
        "SELECT * FROM runs WHERE tool=? AND query=? AND state!='done' "
        "ORDER BY id DESC LIMIT 1", (tool, query)).fetchone()


def find_run(tool: str, query: str) -> Optional[sqlite3.Row]:
    """The latest run for this exact query, whatever state it ended in."""
    c = connect()
    return c.execute(
        "SELECT * FROM runs WHERE tool=? AND query=? ORDER BY id DESC LIMIT 1",
        (tool, query)).fetchone()


def completed_run(tool: str, query: str) -> Optional[sqlite3.Row]:
    """A finished run for exactly this query, if there is one.

    This is what makes a 55-state sweep restartable. Without it, re-running the
    national job re-fetches every state that already finished - tens of
    thousands of requests against a small nonprofit's API to learn nothing.
    """
    c = connect()
    return c.execute(
        "SELECT * FROM runs WHERE tool=? AND query=? AND state='done' "
        "ORDER BY id DESC LIMIT 1", (tool, query)).fetchone()


def mark_seen(run_id: int, docket_ids: Iterable[int]) -> None:
    """Record which dockets a run pulled out, for the coverage check."""
    c = connect()
    c.executemany("INSERT OR IGNORE INTO run_docket (run_id, docket_id) "
                  "VALUES (?,?)", [(run_id, int(d)) for d in docket_ids])
    c.commit()


def run_unique(run_id: int) -> int:
    c = connect()
    return int(c.execute("SELECT COUNT(*) FROM run_docket WHERE run_id=?",
                         (run_id,)).fetchone()[0])


def prune_run_dockets(run_id: int) -> None:
    """Drop a verified run's scratch rows. The `cases` table is the record."""
    c = connect()
    c.execute("DELETE FROM run_docket WHERE run_id=?", (run_id,))
    c.commit()


def update_run(run_id: int, **kw: Any) -> None:
    if not kw:
        return
    kw["updated"] = now()
    cols = ", ".join(f"{k}=?" for k in kw)
    c = connect()
    c.execute(f"UPDATE runs SET {cols} WHERE id=?",
              (*kw.values(), run_id))
    c.commit()


def stats() -> dict:
    c = connect()
    q = lambda s, *a: c.execute(s, a).fetchone()[0]  # noqa: E731
    return {
        "cases": q("SELECT COUNT(*) FROM cases"),
        "cases_street": q("SELECT COUNT(*) FROM cases WHERE is_prisoner=0"),
        "cases_prison": q("SELECT COUNT(*) FROM cases WHERE is_prisoner=1"),
        "parties": q("SELECT COUNT(*) FROM case_parties"),
        "titled": q("SELECT COUNT(*) FROM case_parties WHERE officer_signal=3"),
        "candidates": q(
            "SELECT COUNT(*) FROM case_parties WHERE officer_signal>=2"),
        "distinct_titled": q(
            "SELECT COUNT(DISTINCT raw_name) FROM case_parties "
            "WHERE officer_signal=3"),
        "officers": q("SELECT COUNT(*) FROM officers"),
        "allegations": q("SELECT COUNT(*) FROM allegations"),
        "earliest": q("SELECT MIN(date_filed) FROM cases WHERE date_filed>''"),
        "latest": q("SELECT MAX(date_filed) FROM cases WHERE date_filed>''"),
    }
