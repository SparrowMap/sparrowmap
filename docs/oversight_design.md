# Officer accountability — design

*Started 2026-09-05. Phase 1 shipped; phases 2–5 are the plan, not the code.*

The map answers *where was a police vehicle seen*. This answers a different and
much older question: **what is on the record about the person driving it** — and
it lets somebody who was abused by an officer, at any point in the past, put that
on the record next to it.

---

## The constraint that shaped everything

The obvious design is roster-first: download the state's list of certified
officers, make a profile each, hang complaints off them.

**That road is closed in Michigan.**

| | |
|---|---|
| **MCOLES roster** | A Michigan **Court of Claims** ruling (Judge Christopher Yates) lets MSP/MCOLES withhold the **names and employment histories of every certified officer in the state**. Metro Times + Invisible Institute (U-M Civil Rights Litigation Initiative) are appealing. |
| **National Police Index** | Covers 24 states. Michigan is explicitly listed under *statutory exemptions barring release*. |
| **NLEAD** (federal misconduct db) | **Decommissioned by DOJ 2025-01-24.** It is gone; do not plan around it. |

So identity has to be built **bottom-up from records the state exemption cannot
touch**. Federal court records are federal. A §1983 suit names the officer, the
agency, the date and the allegation, in a document any reader can pull up.

**Measured 2026-09-05, Michigan's two federal districts (`mied` + `miwd`):**

| query | cases |
|---|---:|
| `cause:(1983)` | **11,878** |
| `cause:(1983) NOT suitNature:(Prisoner)` — street policing | **3,950** |

That is the seed. It required no credentials and no FOIA.

---

## The one rule the schema exists to enforce

**A document is not an allegation, and an allegation is not a fact about an
officer.** Three tables, and nothing collapses them.

```
sources        where a fact came from. Every row below points at one.
cases          a court case. Exact, copied, never edited.
case_parties   a name that appeared on a case. raw_name preserved forever.
officers       an identified human being. MINTED BY A PERSON, NEVER BY CODE.
officer_refs   "this party string is that officer" — and who decided so.
allegations    what somebody says happened. A claim, with a status, forever.
vetting        append-only log of every human decision above.
```

The failure this prevents is the one that would end the project: an automated
name match writes *"Officer Smith"* onto a profile, the profile page renders it
as a sentence, and the site has published a factual assertion about a real person
that no human approved.

SparrowMap has already been bitten by the general form of this — matching a crop
to a sighting **by timestamp** promoted an unrelated vehicle to the public map.
**Weak similarity is a queue for a human, never a write.**

### Why it is a separate database file

`data/oversight.db`, not `sparrow.db`:

- **Different legal exposure.** A sighting observes a vehicle on a public road. A
  row here is a statement about a named human being. *"Show me everything you
  hold about this person, and delete it"* has to be a query somebody can verify
  by looking.
- **Different provenance.** Everything in `sparrow.db` came from this project's
  own cameras. Everything here came from somebody else, so every row carries a
  source and rows that cannot are not allowed in.
- **The stale-file trap.** `data/sparrow.db` on the desktop is a stale copy of a
  database that lives on the box and answers queries with confident wrong
  numbers. A separate file cannot inherit that.

### What there is no column for

Home address, personal phone, family members, the officer's personal vehicle.
Same reasoning as `db.py` having no column for civilian plate text: **a field
that cannot be populated cannot be leaked.** What goes in is what the officer did
on duty, on the public record.

### Badge number is an attribute, not a key

A citizen sees a badge and it is the natural identifier — but departments reuse
and reassign them, so two officers a decade apart wear the same number. Identity
is `(agency, name, employment window)`; badge lives in `officer_badges` with a
validity range.

---

## Phase 1 — federal dockets ✅ *shipped 2026-09-05*

### Bulk enumerates, the API enriches

The search API was the wrong tool for a *national case list*, and the numbers say
so plainly: its cursor loses cases, it is 20 rows per request, and **authenticating
makes it slower** — the free membership tier is 5/min, 50/hour, **125/day**, where
anonymous sustained ten times that. Michigan alone is ~594 pages. Paid tiers top
out at 1,400/day for $100/month.

The bulk export is the same database, dumped: **free, unauthenticated,
unthrottled, every federal court in one 4.7 GB file**, regenerated quarterly.
Verified header carries `id`, `case_name`, `docket_number`, **`cause`**,
**`nature_of_suit`**, `date_filed`, `date_terminated`, `court_id`,
`pacer_case_id` — everything `cases` needs.

🚨 **What bulk does not have is party names.** There is no parties file;
`bulk-data/people-db-*` is *judges*, not litigants. Party strings are where every
officer name comes from.

So the division of labour, and it is a good one:

| | source | why |
|---|---|---|
| **which cases exist** | bulk file | must be complete — and now depends on no rate limit at all |
| **who is named on a case** | search API | can be filled in gradually and prioritised |

`courtlistener_fetch.py` keeps its job, but its *role* changed: it is no longer
the enumeration, it is the **party enricher**. It still upserts cases (idempotent,
they are already there from bulk) and its coverage check still grades it against
the API's own count.

⚠️ **Bulk must be verified, not trusted.** `cause` is sparsely populated in the
dump — in a 43,000-row sample only 465 rows carried any cause at all. That may be
honest (most were bankruptcy, which has no cause) or it may be a gap.
`bulk_dockets.py --verify MI` counts what bulk produced for a state against the
API's number for the same query. The same discipline that caught the 1,202
missing cases: the number from a second source, or it did not happen.

`oversight.py` (schema + classifier) and `tools/courtlistener_fetch.py`.

```bash
python tools/courtlistener_fetch.py --states ALL --plan   # size the job first
python tools/courtlistener_fetch.py --state MI --exclude-prisoner   # 3,950
python tools/courtlistener_fetch.py --state MI --q "suitNature:(Prisoner)"
python tools/courtlistener_fetch.py --states ALL          # all of America
python tools/courtlistener_fetch.py --resume        # after any interruption
python tools/courtlistener_fetch.py --report
python tools/courtlistener_fetch.py --queue 40      # top officer candidates
python tools/courtlistener_fetch.py --reclassify    # re-derive the guesses
```

### Every state, not just Michigan

`STATE_COURTS` maps all **50 states + DC + PR, GU, VI and MP** to their federal
district courts — 103 of the 105 the API returns, generated from
`/courts/?jurisdiction=FD&in_use=true` on 2026-09-05 and checked for gaps.

Three things that would have gone wrong quietly:

- **The map is parsed, not substring-matched.** Court names are
  `District Court, <division> <state>`, so the division prefix is stripped and
  the remainder looked up exactly. Matching state names as substrings files
  `N.D. West Virginia` under **Virginia** — three courts of cases in the wrong
  state, in a database whose whole job is saying which agency someone worked for.
- **`in_use=true` does not mean "still operating".** The API returns historical
  undivided districts (`californiad`, `ohiod`, `pennsylvaniad`, `tennessed`,
  `southcarolinaed`…) beside the live ones. They are kept — the court filter is
  one query whatever its length, so they cost nothing, and a case is a case.
  `orld` (District of Orleans, became Louisiana in 1812) and `canalzoned`
  (abolished 1982) map to no state and are dropped; neither can hold a §1983
  case, since the case law starts in 1961.
- **`--states` is sequential, deliberately.** Two concurrent sweeps is exactly
  what produced the first 429. Parallelising would not make it faster, it would
  make it throttled, and the failure would land mid-state. Each state is its own
  run with its own cursor, finished states are skipped, so the national job is
  just the same command run again until it stops printing new states.

`--plan` costs one request per state and answers *how big is this* before
committing days of requests to a small nonprofit's API.

### API facts, measured not assumed

- **`/api/rest/v4/search/?type=r` answers without a token.** `/dockets/` and
  `/parties/` return **401**. The whole of phase 1 therefore needs no credentials.
- Search results already carry the party list, cause, nature of suit and docket
  id — exactly the set needed.
- 8 rapid requests all returned 200, so the anonymous ceiling is **not** the
  documented 5/min. The 2 s default delay is **politeness toward a small
  nonprofit**, not a measured limit. 429 handling exists anyway, because an
  undocumented limit can change without warning.
- Cursor pagination, 20/page. Michigan is 594 pages, so a full sweep **will** be
  interrupted — the cursor is checkpointed to `runs` after every page.

### 🚨 The cursor loses cases, silently — caught on the first real sweep

The full Michigan street sweep reported **`done: 2748 results`** against a
promised **3,950**. It ended naturally (`next` returned null on a *partial*
page), so nothing errored and nothing warned. **1,202 cases short, filed as a
success.** The seen-vs-total coverage line is the only reason anyone knows.

The count is not the thing that is wrong: splitting the identical query at
2015-01-01 gives **2,648 + 1,302 = 3,950 exactly**, so `count` is precise and
`filed_before` is a supported filter. It is the cursor —
`cause:(1983) NOT suitNature:(Prisoner)` is a pure filter, so essentially every
hit carries the same relevance score, the cursor encodes that score
(`s=27.464989&s=5473155…`), and paging through thousands of tied rows both
repeats and skips. Those 2,748 rows held only ~2,150 distinct dockets.

**Consequence for every number this tool prints: `seen` counts result ROWS and
overstates coverage.** Only distinct docket ids mean anything, they are recorded
in `run_docket` so the check survives a resume, and they are pruned once a run
verifies. `--report` shows `?` for runs that predate the column rather than a
number that means nothing.

The response is three layers, in order of preference:

1. **Enumerate dockets directly.** `type=d` returns one row per docket rather
   than documents grouped under dockets, which removes the cause instead of
   working around it.
2. **Date windows.** Ask the API how many cases fall in a window; if it is over
   `WINDOW_MAX`, split and ask again. One request per split decision against the
   many pages it saves.
3. **Verify and re-split.** Because `count` is exact *per window*, the sweep
   grades its own work: distinct dockets out vs promised, and any window still
   short gets split again, down to a single day — which reports loudly rather
   than swallowing a gap.

`--no-windows` keeps the single deep cursor for deliberately small slices, and
it is what proved the loss.

### The party classifier

PACER party lists arrive **unordered** — in one sampled case the plaintiff was
fourth — so position carries no information and everything must be inferred from
the string. Every derived column is named `*_guess` and nothing downstream may
act on one alone.

`officer_signal` ranks the review queue:

| | meaning |
|:-:|---|
| **3** | carries a rank — `Detroit Police Officer Carter`, `Deputy Ryan Boucher` |
| **2** | a named human defendant on a §1983 case, no title |
| **1** | PACER's surname-only form — `Unknown Barton` is a real officer named Barton |
| **0** | entity, plaintiff, or placeholder |

Signals that were **not** obvious until real data arrived:

- **A prisoner number is a plaintiff signal.** `Ade Brown #884273`,
  `Kyle 872579`. Missing it would have put every prisoner-plaintiff in Michigan
  into an officer review queue — the exact inversion this project must not make.
- **A child or decedent is named twice.** `Nadi Bazzi, on behalf of her minor son
  Ibrahim Bazzi` is one party and `Ibrahim Bazzi` is another. Standing alone the
  son came out as a *defendant with an officer signal* — a minor, filed as a
  victim, queued for review as a police officer. Fixed by checking whether a name
  is quoted inside a co-party carrying a next-friend or estate phrase.
- **A placeholder can wear a rank.** `Officers Jane/John Doe`,
  `Police Officer John Doe`, `OFFICER JOHN DOE 1` all matched a title and filled
  the top of the queue with three spellings of nobody. The Doe test now runs over
  the whole string and beats the title test.
- **An office is not a person.** `Wayne County Sheriff` outranked real named
  deputies. Distinguished from `Sheriff Bouchard`, which is a human.

`raw_name` is never normalised in place — that is what makes `--reclassify`
possible when the heuristics improve. **Rows a human has already linked are
skipped:** a better heuristic is not a reason to overwrite a person's decision.

---

## Phases 2–5 — the rest

**2 · Review surface.** `/ov` — work the queue, mint officers, link parties,
publish or reject. Every action writes to `vetting`. Reuses the existing
`review_auth` / trusted-reviewer model. **Nothing reaches the public without a
named person, and the audit table has to be able to prove it.**

**3 · Public report intake.** *"An officer did this to me."* Verbatim body,
`status='submitted'`, not public. Optional lat/lon so it can land on the map.
No age limit on incidents — that is the point.

**4 · Profiles.** A page per officer: documents first, allegations second and
visibly labelled as claims. Correction and takedown path on every page.
**Section 230 protects the user's verbatim words, not the site's summary of
them** — so the site never asserts, it attributes.

**5 · More sources.** City-level FOIA (a *different* legal path than MCOLES, and
often honoured): rosters, IA summary logs, and **settlement / risk-fund payouts**
— the strongest evidence type there is, because a city writing a cheque is not an
allegation. Published POST/decertification lists in FL, GA, WA, CA, TX catch
officers who left Michigan. **PDAP** (`data-sources.pdap.io/api`) indexes which
agency publishes what — use it as the FOIA target list, not as data.

A CourtListener token (free) raises the rate limits and unlocks `/parties/` for
**authoritative** plaintiff/defendant roles and attorney records, which would
retire most of the classifier's guessing. Set `COURTLISTENER_TOKEN` and it is
already sent on search; the `/parties/` path is deliberately **not** written
blind against an endpoint that cannot be tested without one.

### Prior art worth taking rather than rebuilding

**OpenOversight** (Lucy Parsons Labs, open source, already forked for Virginia,
Seattle and Portland) implements officer profiles, badge search and photo
galleries. **LLEAD** (Louisiana) and **NYC CCRB** open data are the schemas to
steal from — they solved this in jurisdictions that publish.
