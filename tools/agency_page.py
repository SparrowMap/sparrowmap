"""Write a self-contained, offline HTML directory of one state's agencies.

    python tools/agency_page.py --state MI
    python tools/agency_page.py --state MI --out D:/somewhere/mi.html
    python tools/agency_page.py --state MI --min-cases 3

🚨 THIS WRITES A LOCAL FILE. IT IS NOT A ROUTE AND IT IS NOT DEPLOYED.
The map's rule is that anything NEW and PUBLIC waits for Matthew's go, and a
page listing which police departments get sued is exactly that. So this
produces something he can open, look at, and decide about - no URL, nothing
served, nothing indexed.

No network, no CDN, no fonts, no build step: one file that works on a laptop
with the wifi off, which is also the only honest way to ship something whose
whole claim is that the reader can check it.

--------------------------------------------------------------------------
WHAT THE PAGE MUST NOT LET A READER BELIEVE
--------------------------------------------------------------------------

Two things, and they are stated on the page itself rather than buried here:

  1. **A case count is not a misconduct rate.** New York City appears 12,056
     times because it is enormous. Sorting agencies by case count and calling
     the top one the worst is the single easiest way to turn a public record
     into a libel, and it is the mistake this page is most likely to invite.
  2. **Only 10.6% of cases name an agency in the caption.** The other 89.4%
     name individuals - "Smith v. Jones" - and cannot be attributed to a
     department until party enrichment fills in who those people were. So
     every count here is a FLOOR, and a small department's low number may mean
     nothing more than that its officers were sued by name.

A filed case is an allegation. Nothing on this page is a finding.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oversight  # noqa: E402
from core import DATA  # noqa: E402
from courtlistener_fetch import STATE_COURTS  # noqa: E402

CL = "https://www.courtlistener.com"


def _case_row(r) -> dict:
    """One case as the page needs it.

    🚨 `u` IS THE STORED absolute_url, NOT A URL BUILT FROM THE ID.
    The first version linked to /docket/<id>/ and every one of those 404s -
    CourtListener requires the slug, and "404 U.S. ___, Page Not Found" is
    what Matthew got when he clicked the two Fenton cases. The correct URL was
    already sitting in the database; the page just rebuilt a worse one.
    A handful of very old rows (51 of 1.79M) have no slug and so no working
    link; those render as plain text rather than a link that cannot work.
    """
    u = r["absolute_url"] or ""
    if u.endswith("//") or not u.strip("/"):
        u = ""
    return {
        "id": r["docket_id"],
        "u": u,
        "n": r["case_name"] or "",
        "d": r["docket_number"] or "",
        "f": r["date_filed"] or "",
        "t": r["date_terminated"] or "",
        "s": r["suit_nature"] or "",
        "p": r["is_prisoner"],
    }


def collect(state: str, min_cases: int) -> dict:
    c = oversight.connect()
    state = state.upper()
    if state not in STATE_COURTS:
        raise SystemExit(f"unknown state {state!r}")
    courts = STATE_COURTS[state]
    marks = ",".join("?" * len(courts))

    agencies = [dict(r) for r in c.execute(
        "SELECT id, kind, place, display, cases, first_filed, last_filed "
        "FROM agencies WHERE state=? AND cases>=? ORDER BY cases DESC",
        (state, min_cases))]
    ids = {a["id"] for a in agencies}

    cases: dict = {a["id"]: [] for a in agencies}
    for r in c.execute(
            "SELECT ca.agency_id, c.docket_id, c.case_name, c.docket_number, "
            "       c.date_filed, c.date_terminated, c.suit_nature, "
            "       c.match_basis, c.is_prisoner, c.absolute_url "
            "FROM case_agencies ca JOIN cases c ON c.docket_id = ca.docket_id "
            "JOIN agencies a ON a.id = ca.agency_id WHERE a.state=? "
            "ORDER BY c.date_filed DESC", (state,)):
        if r["agency_id"] not in ids:
            continue
        cases[r["agency_id"]].append(_case_row(r))

    # Aliases matter to a reader checking our work: they show what the raw
    # court record actually said before it was normalised.
    aliases: dict = {a["id"]: [] for a in agencies}
    for r in c.execute(
            "SELECT x.agency_id, x.raw, x.hits, x.merged FROM agency_aliases x "
            "JOIN agencies a ON a.id = x.agency_id WHERE a.state=? "
            "ORDER BY x.hits DESC", (state,)):
        if r["agency_id"] in aliases and len(aliases[r["agency_id"]]) < 12:
            aliases[r["agency_id"]].append(
                {"r": r["raw"], "h": r["hits"], "m": r["merged"]})

    total_cases = c.execute(
        f"SELECT COUNT(*) FROM cases WHERE court_id IN ({marks})",
        courts).fetchone()[0]
    linked = c.execute(
        f"SELECT COUNT(DISTINCT ca.docket_id) FROM case_agencies ca "
        f"JOIN cases c ON c.docket_id=ca.docket_id "
        f"WHERE c.court_id IN ({marks})", courts).fetchone()[0]
    named = c.execute(
        f"SELECT COUNT(DISTINCT docket_id) FROM case_parties WHERE docket_id "
        f"IN (SELECT docket_id FROM cases WHERE court_id IN ({marks}))",
        courts).fetchone()[0]

    # ---- officer candidates ------------------------------------------------
    # Grouped by the RAW party string, deliberately. Two "Officer Smith" rows in
    # different cases may or may not be the same human being, and this page has
    # no way to know - so it groups what the court literally wrote and says so,
    # rather than inventing a person by merging on a name.
    officers: list = []
    ocases: dict = {}
    seen: dict = {}
    for r in c.execute(
            "SELECT p.raw_name, p.title_guess, p.agency_hint, p.officer_signal,"
            "       c.docket_id, c.case_name, c.docket_number, c.date_filed, "
            "       c.date_terminated, c.suit_nature, c.is_prisoner, "
            "       c.absolute_url "
            "FROM case_parties p JOIN cases c ON c.docket_id = p.docket_id "
            f"WHERE c.court_id IN ({marks}) AND p.officer_signal >= 2 "
            "AND p.officer_id IS NULL "
            "ORDER BY p.officer_signal DESC, c.date_filed DESC", courts):
        key = r["raw_name"]
        if key not in seen:
            seen[key] = len(officers)
            officers.append({"id": len(officers), "name": key,
                             "title": r["title_guess"] or "",
                             "agency": r["agency_hint"] or "",
                             "sig": r["officer_signal"], "cases": 0,
                             "first": "", "last": ""})
            ocases[seen[key]] = []
        oi = seen[key]
        officers[oi]["cases"] += 1
        ocases[oi].append(_case_row(r))
        f = r["date_filed"] or ""
        if f:
            o = officers[oi]
            o["first"] = f if not o["first"] else min(o["first"], f)
            o["last"] = f if not o["last"] else max(o["last"], f)
    # Titled first, then the ones cited most often - a name that recurs across
    # separate cases is the pattern a single case cannot show.
    officers.sort(key=lambda o: (-o["sig"], -o["cases"], o["last"]), reverse=False)
    remap = {o["id"]: i for i, o in enumerate(officers)}
    ocases = {remap[k]: v for k, v in ocases.items()}
    for i, o in enumerate(officers):
        o["id"] = i

    return {"state": state, "courts": courts, "agencies": agencies,
            "cases": cases, "aliases": aliases,
            "officers": officers, "ocases": ocases,
            "total_cases": total_cases, "linked": linked, "named": named}


CSS = """
:root{--bg:#faf9f7;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e2e0dc;--card:#fff;
--accent:#8a3324;--warn:#8a6d24;--warnbg:#fdf8e8}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e6e3;
--dim:#9a9894;--line:#2c2c33;--card:#1d1d22;--accent:#d9906f;
--warn:#d9b96f;--warnbg:#26231a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--dim);margin:0 0 22px;font-size:14px}
.warn{background:var(--warnbg);border-left:3px solid var(--warn);
padding:12px 16px;margin:0 0 24px;font-size:13.5px;line-height:1.6}
.warn b{color:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:26px;margin:0 0 22px;
padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.stat .n{font-size:21px;font-variant-numeric:tabular-nums}
.stat .l{font-size:12px;color:var(--dim);text-transform:uppercase;
letter-spacing:.05em}
input[type=search]{width:100%;padding:10px 13px;font-size:15px;
border:1px solid var(--line);border-radius:6px;background:var(--card);
color:var(--fg);margin:0 0 6px}
.kinds{margin:0 0 14px;font-size:13px;color:var(--dim)}
.kinds button{font:inherit;background:none;border:1px solid var(--line);
color:var(--dim);border-radius:99px;padding:3px 11px;margin:3px 4px 0 0;
cursor:pointer}
.kinds button[aria-pressed=true]{background:var(--fg);color:var(--bg);
border-color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:11.5px;text-transform:uppercase;
letter-spacing:.06em;color:var(--dim);font-weight:600;padding:8px 10px;
border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.ag{cursor:pointer}
tr.ag:hover td{background:var(--card)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.yr{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap;
font-size:13px}
.detail td{background:var(--card);padding:0}
.panel{padding:14px 16px 18px}
.panel h3{margin:0 0 2px;font-size:15px}
.panel .note{color:var(--dim);font-size:12.5px;margin:0 0 12px}
.cases{font-size:13px}
.cases a{color:var(--accent);text-decoration:none}
.cases a:hover{text-decoration:underline}
.tag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:3px;
border:1px solid var(--line);color:var(--dim);margin-left:6px;
vertical-align:1px}
.alias{font-size:12px;color:var(--dim);margin-top:12px;
padding-top:10px;border-top:1px dashed var(--line)}
.alias code{background:var(--bg);padding:1px 5px;border-radius:3px}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);
color:var(--dim);font-size:12.5px;line-height:1.7}
.tabs{display:flex;gap:4px;margin:0 0 18px;border-bottom:1px solid var(--line)}
.tabs button{font:inherit;font-weight:600;background:none;border:0;
border-bottom:2px solid transparent;color:var(--dim);padding:9px 14px;
cursor:pointer;margin-bottom:-1px}
.tabs button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent)}
.rank{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:3px;
border:1px solid var(--accent);color:var(--accent);margin-left:7px;
vertical-align:1px;white-space:nowrap}
.sig2{opacity:.62}
.nolink{color:var(--dim)}
"""

JS = """
const AG=DATA.agencies,CS=DATA.cases,AL=DATA.aliases,
      OF=DATA.officers,OC=DATA.ocases;
let tab='agencies', oq='', titledOnly=true;
let kind='', q='';
const tb=document.getElementById('rows');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;',
'>':'&gt;','"':'&quot;'}[c]));
function yr(d){return d?d.slice(0,4):'?'}
function caseRow(c){
  // Link ONLY when we have the stored absolute_url. A rebuilt /docket/<id>/
  // is a guaranteed 404 on CourtListener.
  const nm = esc(c.n||'(no caption)');
  const cell = c.u ? `<a href="${CL}${esc(c.u)}" target="_blank" rel="noopener">${nm}</a>`
                   : `<span class="nolink" title="no slug on record">${nm}</span>`;
  return `<tr><td class="yr">${esc(c.f||'?')}</td>
    <td>${cell}${c.p?'<span class="tag">prisoner</span>':''}</td>
    <td class="yr">${esc(c.d||'')}</td>
    <td class="yr">${esc(c.s||'')}</td></tr>`;
}
function drawOfficers(){
  const ql=oq.toLowerCase();
  const rows=OF.filter(o=>(!titledOnly||o.sig===3)&&
    (!ql||o.name.toLowerCase().includes(ql)||(o.agency||'').toLowerCase().includes(ql)));
  document.getElementById('oshown').textContent=rows.length.toLocaleString();
  document.getElementById('orows').innerHTML=rows.slice(0,600).map(o=>`
    <tr class="ag off" data-id="${o.id}">
      <td class="${o.sig===3?'':'sig2'}">${esc(o.name)}
        ${o.title?`<span class="rank">${esc(o.title)}</span>`:''}
        ${o.agency?`<span class="tag">${esc(o.agency)}</span>`:''}</td>
      <td class="num">${o.cases.toLocaleString()}</td>
      <td class="yr">${yr(o.first)}&ndash;${yr(o.last)}</td>
    </tr><tr class="detail" id="od${o.id}" hidden><td colspan="3"></td></tr>`).join('');
}
function opanel(o){
  const cs=OC[o.id]||[];
  let h=`<div class="panel"><h3>${esc(o.name)}</h3>
  <p class="note">${cs.length} case${cs.length==1?'':'s'} name this exact string.
  ${o.sig===3?'The court recorded a rank, which is the strongest signal here.'
             :'No rank in the record &mdash; a named human defendant on a civil-rights case, which is weaker.'}
  Nobody has verified this is one person, or which person.</p>
  <table class="cases"><tbody>`;
  for(const c of cs.slice(0,300)) h+=caseRow(c);
  return h+'</tbody></table></div>';
}
function draw(){
  const ql=q.toLowerCase();
  const rows=AG.filter(a=>(!kind||a.kind===kind)&&
    (!ql||a.display.toLowerCase().includes(ql)));
  document.getElementById('shown').textContent=rows.length.toLocaleString();
  tb.innerHTML=rows.map(a=>`<tr class="ag" data-id="${a.id}">
    <td>${esc(a.display)}<span class="tag">${a.kind}</span></td>
    <td class="num">${a.cases.toLocaleString()}</td>
    <td class="yr">${yr(a.first_filed)}&ndash;${yr(a.last_filed)}</td>
  </tr><tr class="detail" id="d${a.id}" hidden><td colspan="3"></td></tr>`)
    .join('');
}
function panel(a){
  const cs=CS[a.id]||[], al=AL[a.id]||[];
  const open=cs.filter(c=>!c.t).length;
  let h=`<div class="panel"><h3>${esc(a.display)}</h3>
  <p class="note">${cs.length.toLocaleString()} case${cs.length==1?'':'s'}
  name this agency in the caption &middot; ${open.toLocaleString()} with no
  termination date recorded. Every one is an allegation, not a finding.</p>
  <table class="cases"><tbody>`;
  for(const c of cs.slice(0,300)) h+=caseRow(c);
  h+='</tbody></table>';
  if(cs.length>300) h+=`<p class="note" style="margin-top:10px">
    showing the 300 most recent of ${cs.length.toLocaleString()}.</p>`;
  if(al.length){
    h+=`<div class="alias"><b>As the court actually wrote it:</b> `+
      al.map(x=>`<code>${esc(x.r)}</code>&times;${x.h}`+
        (x.m?' <i>(merged)</i>':'')).join(' &middot; ')+'</div>';
  }
  return h+'</div>';
}
tb.addEventListener('click',e=>{
  const tr=e.target.closest('tr.ag'); if(!tr)return;
  const id=+tr.dataset.id, d=document.getElementById('d'+id);
  if(!d.hidden){d.hidden=true;return;}
  const a=AG.find(x=>x.id===id);
  d.firstElementChild.innerHTML=panel(a); d.hidden=false;
});
document.getElementById('q').addEventListener('input',e=>{
  q=e.target.value; draw();});
document.getElementById('oq').addEventListener('input',e=>{
  oq=e.target.value; drawOfficers();});
document.getElementById('titledOnly').addEventListener('click',e=>{
  titledOnly=!titledOnly;
  e.target.setAttribute('aria-pressed', titledOnly);
  e.target.textContent = titledOnly ? 'showing ranked only' : 'showing all candidates';
  drawOfficers();});
document.getElementById('orows').addEventListener('click',e=>{
  const tr=e.target.closest('tr.off'); if(!tr)return;
  const id=+tr.dataset.id, d=document.getElementById('od'+id);
  if(!d.hidden){d.hidden=true;return;}
  d.firstElementChild.innerHTML=opanel(OF.find(x=>x.id===id)); d.hidden=false;
});
document.querySelectorAll('.tabs button').forEach(b=>{
  b.addEventListener('click',()=>{
    tab=b.dataset.tab;
    document.querySelectorAll('.tabs button').forEach(x=>
      x.setAttribute('aria-selected', x.dataset.tab===tab));
    document.getElementById('paneAgencies').hidden = tab!=='agencies';
    document.getElementById('paneOfficers').hidden = tab!=='officers';
  });
});
drawOfficers();
document.querySelectorAll('.kinds button').forEach(b=>{
  b.addEventListener('click',()=>{
    kind = (kind===b.dataset.k) ? '' : b.dataset.k;
    document.querySelectorAll('.kinds button').forEach(x=>
      x.setAttribute('aria-pressed', x.dataset.k===kind));
    draw();});
});
draw();
"""


def render(d: dict) -> str:
    st = d["state"]
    pct = 100.0 * d["linked"] / max(d["total_cases"], 1)
    named_pct = 100.0 * d["named"] / max(d["total_cases"], 1)
    kinds = sorted({a["kind"] for a in d["agencies"]})
    payload = json.dumps({"agencies": d["agencies"], "cases": d["cases"],
                          "aliases": d["aliases"], "officers": d["officers"],
                          "ocases": d["ocases"]}, separators=(",", ":"))
    btns = "".join(f'<button data-k="{k}" aria-pressed="false">{k}</button>'
                   for k in kinds)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{st} &mdash; federal civil-rights cases by agency</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>{st} &mdash; federal civil-rights cases by agency</h1>
<p class="sub">Section 1983 and related civil-rights dockets from
{", ".join(d["courts"])}, grouped by the agency named in the case caption.
Built from CourtListener's public bulk data. Every case links back to the
public docket so you can read it yourself.</p>

<div class="warn">
<b>Read this before you read the numbers.</b><br>
A case count is <b>not</b> a misconduct rate. A large city appears often
because it is large, and every case here is an <b>allegation</b> &mdash;
a filing, not a finding. Nothing on this page says anyone did anything.<br><br>
These counts are a <b>floor, not a total</b>. Only {pct:.1f}% of this state's
cases name an agency in the caption; the rest name individuals
(&ldquo;Smith v. Jones&rdquo;) and cannot be attributed to a department until
the officer names are filled in &mdash; currently {named_pct:.1f}% done. A
small department&rsquo;s low number may mean only that its officers were sued
by name.
</div>

<div class="stats">
  <div class="stat"><div class="n">{d["total_cases"]:,}</div>
    <div class="l">civil-rights cases</div></div>
  <div class="stat"><div class="n">{d["linked"]:,}</div>
    <div class="l">name an agency</div></div>
  <div class="stat"><div class="n">{len(d["agencies"]):,}</div>
    <div class="l">agencies found</div></div>
  <div class="stat"><div class="n" id="shown">0</div>
    <div class="l">shown</div></div>
</div>

<div class="tabs">
  <button data-tab="agencies" aria-selected="true">Agencies</button>
  <button data-tab="officers" aria-selected="false">Officer candidates ({len(d["officers"]):,})</button>
</div>

<div id="paneAgencies">
  <input type="search" id="q" placeholder="Search agencies &mdash; try a county or city&hellip;"
   autocomplete="off">
  <div class="kinds">{btns}</div>
  <table><thead><tr><th>Agency</th><th class="num">Cases</th>
  <th>Filed between</th></tr></thead><tbody id="rows"></tbody></table>
</div>

<div id="paneOfficers" hidden>
  <div class="warn" style="margin-top:0">
  <b>These are names the court wrote down. They are not identified people.</b><br>
  Each row groups one exact party string. Two cases naming &ldquo;Officer Smith&rdquo; may be
  two different people, and nothing here has checked. A person appearing on this list has
  been <b>sued</b>, which is an allegation by whoever filed &mdash; not a finding, not a
  complaint upheld, and not misconduct. Read the docket before believing anything.
  </div>
  <input type="search" id="oq" placeholder="Search officer names or agencies&hellip;"
   autocomplete="off">
  <div class="kinds">
    <button id="titledOnly" aria-pressed="true">showing ranked only</button>
    <span style="margin-left:10px"><b id="oshown">0</b> shown</span>
  </div>
  <table><thead><tr><th>Name as written by the court</th><th class="num">Cases</th>
  <th>Filed between</th></tr></thead><tbody id="orows"></tbody></table>
</div>

<footer>
Source: CourtListener bulk dockets, filtered to nature of suit 440 (Civil
Rights: Other), 550 (Prisoner: Civil Rights) and 555 (Prison Condition), plus
any case whose recorded cause names 42 U.S.C. &sect;1983.<br>
Agency names are normalised from court captions, which are frequently
truncated, misspelled or word-reversed. Expand any agency to see exactly what
the court record said. Nothing here is a finding of wrongdoing.<br>
Generated locally by <code>tools/agency_page.py</code>. Not published.
</footer>

</div>
<script>const CL={json.dumps(CL)};const DATA={payload};</script>
<script>{JS}</script>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--out")
    ap.add_argument("--min-cases", type=int, default=1)
    a = ap.parse_args()
    d = collect(a.state, a.min_cases)
    out = Path(a.out) if a.out else DATA / f"agencies_{d['state']}.html"
    out.write_text(render(d), encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({kb:,.0f} KB)")
    print(f"  {len(d['agencies']):,} agencies, "
          f"{sum(len(v) for v in d['cases'].values()):,} case rows")
    print(f"  {d['linked']:,} of {d['total_cases']:,} cases name an agency "
          f"({100.0 * d['linked'] / max(d['total_cases'], 1):.1f}%)")
    print("\n  open it with:  start " + str(out))


if __name__ == "__main__":
    main()
