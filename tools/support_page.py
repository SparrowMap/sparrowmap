"""Generate the public support / transparency page from the live database.

    python tools/support_page.py            # write public/support.html
    python tools/support_page.py --print    # just show the numbers

🚨 GENERATED, NEVER HAND-WRITTEN. A transparency page with numbers typed into it
is a page that is wrong within a week, and being caught with a stale figure on
the one page that exists to be trustworthy costs more than the page is worth.

🚨 AND THE HARD PART IS NOT THE ARITHMETIC, IT IS THE HONESTY.

The database says 15,752 nodes. Publishing that as the size of the network would
be a lie, because 15,218 of them are PUBLIC TRAFFIC CAMERAS this project scraped
and enrolled itself. The volunteer network is 534. Those are different facts and
this page keeps them apart, always, even though the big number is the flattering
one.

Same reason retention is on here at all. Of volunteers who enrolled three or more
days ago, a small minority are still reporting. That number is bad and it is the
truest thing on the page: it is the actual constraint on the project, it is
already known publicly from the reel figures (228,923 views produced 110 cameras
and 19 live within a day), and a supporter who finds it out later feels lied to.

⚠️ COSTS ARE NOT GUESSED. Server SPECS are read from the machines, but the
monthly price comes from the real invoice and lives in `data/costs.json`. An
invented hosting bill on a transparency page is the worst possible thing to be
wrong about, so if that file is missing the section says so instead of
estimating.
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "public" / "support.html"
COSTS = ROOT / "data" / "costs.json"
GOALS = ROOT / "data" / "goals.json"
DAY = 86400.0

# Anything that is not this is a person's camera.
SCRAPED = "public_cam"


def stats() -> dict:
    import db
    conn = db.connect()
    now = time.time()

    def one(sql, args=()):
        r = conn.execute(sql, args).fetchone()
        return (list(r)[0] or 0) if r else 0

    vol = f"(kind IS NULL OR kind <> '{SCRAPED}')"
    s = {
        "generated": now,
        # The volunteer network - the honest headline.
        "vol_enrolled": one(f"SELECT COUNT(*) FROM nodes WHERE {vol}"),
        "vol_live_24h": one(f"SELECT COUNT(*) FROM nodes WHERE {vol} AND last_beat > ?",
                            (now - DAY,)),
        "vol_live_7d": one(f"SELECT COUNT(*) FROM nodes WHERE {vol} AND last_beat > ?",
                           (now - 7 * DAY,)),
        "vol_produced": one(
            "SELECT COUNT(DISTINCT s.node_id) FROM sightings s "
            f"JOIN nodes n ON n.id = s.node_id WHERE (n.kind IS NULL OR n.kind <> '{SCRAPED}')"),
        # The scraped fleet, counted separately and labelled as ours.
        "cam_enrolled": one(f"SELECT COUNT(*) FROM nodes WHERE kind = '{SCRAPED}'"),
        "cam_live_24h": one(
            f"SELECT COUNT(*) FROM nodes WHERE kind = '{SCRAPED}' AND last_beat > ?",
            (now - DAY,)),
        # Output.
        "public_all": one("SELECT COUNT(*) FROM sightings WHERE tier = 'public'"),
        "public_24h": one("SELECT COUNT(*) FROM sightings WHERE tier = 'public' AND ts > ?",
                          (now - DAY,)),
        "sightings_all": one("SELECT COUNT(*) FROM sightings"),
        "sightings_24h": one("SELECT COUNT(*) FROM sightings WHERE ts > ?", (now - DAY,)),
        "hours_watched": one("SELECT COALESCE(SUM(beats),0) FROM nodes") / 120.0,
        "first_ts": one("SELECT MIN(ts) FROM sightings"),
    }
    # 🚨 RETENTION, MEASURED THE ONLY WAY THAT MEANS ANYTHING: of the people who
    # enrolled long enough ago to have stopped, how many have not?
    cut = now - 3 * DAY
    s["ret_total"] = one(f"SELECT COUNT(*) FROM nodes WHERE {vol} AND created < ?", (cut,))
    s["ret_still"] = one(
        f"SELECT COUNT(*) FROM nodes WHERE {vol} AND created < ? AND last_beat > ?",
        (cut, now - DAY))
    s["ret_pct"] = (100.0 * s["ret_still"] / s["ret_total"]) if s["ret_total"] else 0.0

    s["by_day"] = [dict(r) for r in conn.execute(
        f"SELECT DATE(created,'unixepoch') d, COUNT(*) n FROM nodes "
        f"WHERE created > ? AND {vol} GROUP BY d ORDER BY d", (now - 14 * DAY,))]
    return s


def capacity() -> dict:
    """What this machine has left, and what runs out first.

    🚨 THE POINT OF THIS SECTION IS THE BOTTLENECK, NOT THE GAUGES. "We could use
    support" is unfalsifiable; "memory is the thing that fails and here is how
    close it is" is checkable, and it is also true - the hub was OOM-killed
    repeatedly on 2026-08-16 while every health check answered 200.

    ⚠️ MemAvailable, not MemFree. Free memory on a busy Linux box is nearly zero
    by design because the page cache uses the rest, so MemFree would make a
    healthy machine look like it is dying and this page would be crying wolf.
    """
    import shutil
    out = {}
    try:
        mem = {}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k] = float(v.split()[0]) * 1024
        out["mem_total"] = mem.get("MemTotal", 0)
        out["mem_avail"] = mem.get("MemAvailable", 0)
        out["mem_used_pct"] = 100.0 * (1 - out["mem_avail"] / out["mem_total"]) \
            if out.get("mem_total") else 0.0
    except Exception:
        pass
    try:
        du = shutil.disk_usage("/")
        out["disk_total"], out["disk_free"] = du.total, du.free
        out["disk_used_pct"] = 100.0 * du.used / du.total
    except Exception:
        pass
    try:
        out["load1"] = __import__("os").getloadavg()[0]
        out["cpus"] = __import__("os").cpu_count() or 1
    except Exception:
        pass
    # The caps that actually decide whether the site answers, from the code.
    try:
        import hub
        out["max_requests"] = getattr(hub, "MAX_REQUESTS", None)
        out["max_heavy"] = getattr(hub, "MAX_HEAVY", None)
        out["max_ingest"] = getattr(hub, "MAX_INGEST", None)
    except Exception:
        pass
    return out


def goals() -> dict:
    """Hardware the project needs, or an honest blank.

    🚨 HIS IDEA CAME FROM A VOLUNTEER (John Rigler, 2026-08-17): list specific
    hardware with a goal against each, rather than asking for money. It is a
    better frame for a reason worth writing down - a donor can see exactly what
    their money bought, and the ask stops being "support us" and becomes "this
    $70 board answers a question we cannot otherwise answer".

    ⚠️ `raised` IS HAND-ENTERED. There is no API read of the coffee balance, so
    the page prints the figure WITH the date it was entered. It must never render
    a total it cannot source.
    """
    if GOALS.exists():
        try:
            return json.loads(GOALS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def costs() -> dict:
    """Real monthly costs, or an honest blank."""
    if COSTS.exists():
        try:
            return json.loads(COSTS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def render(s: dict, c: dict, cap: dict, g: dict) -> str:
    e = html.escape
    gb = 1024.0 ** 3

    def gauge(label, pct, note):
        # Colour by how close it is to the thing that actually goes wrong.
        col = "var(--cam)" if pct < 70 else ("#ffb547" if pct < 88 else "var(--police)")
        return (f'<div class=g><div class=gl>{e(label)}<b>{pct:.0f}%</b></div>'
                f'<div class=gt><span style="width:{min(100.0, pct):.0f}%;'
                f'background:{col}"></span></div>'
                f'<div class=n>{e(note)}</div></div>')

    if cap.get("mem_total"):
        gauges = (
            gauge("Memory on the map server", cap["mem_used_pct"],
                  f"{cap['mem_avail'] / gb:.1f} GB free of "
                  f"{cap['mem_total'] / gb:.1f} GB. This is the one that has "
                  f"actually failed: the server was killed for running out of "
                  f"memory five times in a day, not for lack of CPU.")
            + gauge("Disk", cap.get("disk_used_pct", 0),
                    f"{cap.get('disk_free', 0) / gb:.0f} GB free of "
                    f"{cap.get('disk_total', 0) / gb:.0f} GB. Grows with the "
                    f"photo archive and the cached map tiles.")
            + gauge("CPU right now",
                    100.0 * cap.get("load1", 0) / max(1, cap.get("cpus", 1)),
                    f"load {cap.get('load1', 0):.2f} across "
                    f"{cap.get('cpus', 0)} cores.")
        )
        need = f"""
<div class=big>
 <div><b>{cap.get('cpus', 0)}</b><span>CPU cores, total</span></div>
 <div><b>{cap['mem_total'] / gb:.0f} GB</b><span>memory, total</span></div>
 <div><b>{cap.get('max_ingest') or '?'}</b><span>cameras that can upload at once</span></div>
 <div><b>{cap.get('max_heavy') or '?'}</b><span>map builds at once</span></div>
</div>
<p>Those last two are deliberate ceilings, not hardware limits. The whole map is
built in memory to answer a request, so more simultaneous builds means more
memory, and memory is what runs out. Raising them needs a bigger machine, which
is the honest answer to "what would money buy".</p>"""
    else:
        gauges, need = "", ("<p class=n>Live capacity is only readable when this "
                            "page is generated on the server itself.</p>")

    # ---- hardware goals -------------------------------------------------
    gl = g.get("goals") or []
    if gl:
        rows = []
        for it in gl:
            usd = float(it.get("usd") or 0)
            got = float(it.get("raised") or 0)
            pct = min(100.0, (100.0 * got / usd) if usd else 0.0)
            rows.append(
                f'<div class=goal><div class=gh><b>{e(str(it.get("what","")))}</b>'
                f'<span class=num>${usd:,.0f}</span></div>'
                f'<div class=gt><span style="width:{pct:.0f}%"></span></div>'
                f'<div class=n>{"$%,.0f of $%,.0f" % (got, usd) if got else "not funded yet"}'
                f'{" &middot; " + e(str(it.get("unlocks",""))) if it.get("unlocks") else ""}</div>'
                f'<p>{e(str(it.get("why","")))}</p></div>')
        tot = sum(float(x.get("usd") or 0) for x in gl)
        got = sum(float(x.get("raised") or 0) for x in gl)
        later = "".join(
            f"<li><b>{e(str(x.get('what','')))}</b> {e(str(x.get('why','')))}</li>"
            for x in (g.get("later") or []))
        goals_block = f"""
<p>Rather than asking for support in the abstract, here is the actual hardware
this needs and what each piece would answer. Prices checked
{e(str(g.get('priced_on','')))}, not remembered: they moved a long way this year.</p>
{''.join(rows)}
<p class=n><b>${got:,.0f} of ${tot:,.0f}</b> toward the list.
{('Figure entered by hand on ' + e(str(g.get('raised_updated'))) + '.') if g.get('raised_updated') else 'Nothing received toward it yet.'}
There is no automatic read of the donation balance, so this is updated manually
rather than shown as something it is not.</p>
{f'<h3>After that, and deliberately unpriced</h3><ul>{later}</ul>' if later else ''}
<p class=n>If none of it arrives, the project carries on. Everything above makes it
faster or answers a question sooner; none of it is keeping the lights on. The
running costs below are the part that does that.</p>"""
    else:
        goals_block = ""

    return _page(s, c, gauges, need, goals_block)


def _page(s: dict, c: dict, gauges: str, need: str, goals_block: str) -> str:
    e = html.escape
    days = (time.time() - s["first_ts"]) / DAY if s["first_ts"] else 0
    items = c.get("items") or []
    total = sum(float(i.get("usd_month") or 0) for i in items)

    if items:
        rows = "\n".join(
            f"<tr><td><b>{e(str(i.get('what','')))}</b><br>"
            f"<span class=n>{e(str(i.get('detail','')))}</span></td>"
            f"<td class=num>${float(i.get('usd_month') or 0):,.2f}</td></tr>"
            for i in items)
        # 🚨 "TAKEN FROM THE ACTUAL INVOICES" WAS NOT TRUE YET, AND THIS IS
        # THE ONE PAGE WHERE THAT MATTERS. The camera machines were created on
        # 2026-08-15 and Hetzner does not bill until the first month closes, so
        # these are the per-month prices the console states - which costs.json
        # has recorded as `invoiced: false` all along while the page said
        # otherwise. Read the flag and say the true thing; a transparency page
        # claiming a stronger source than it has is worse than a weaker claim.
        srcline = ("Taken from the actual invoices, not estimated."
                   if c.get("invoiced")
                   else "Taken from the per-month price each server shows in the "
                        "hosting console, not estimated. The first month has not "
                        "closed yet, so these are not invoiced figures.")
        cost_block = f"""
<table>
<tr><th>What</th><th class=num>Per month</th></tr>
{rows}
<tr class=tot><td><b>Total</b></td><td class=num><b>${total:,.2f}</b></td></tr>
</table>
<p class=n>Last updated {e(str(c.get('updated','')))}. {srcline}</p>"""
        # His framing, with the arithmetic done rather than asserted.
        need = int(total // 1) + (1 if total % 1 else 0)
        supporters = f"""
<p><b>{need:,} people at $1 a month covers all of it.</b> That is the whole
funding model. Not a subscription, not a tier, no feature held back for payers -
the map is public and stays public either way.</p>"""
    else:
        cost_block = ("<p class=warn>The cost breakdown is not published yet. It "
                      "will be exact figures from the invoices or nothing at all, "
                      "because a guessed number on this page would defeat its "
                      "purpose.</p>")
        supporters = ""

    bars = ""
    if s["by_day"]:
        top = max(x["n"] for x in s["by_day"]) or 1
        bars = "".join(
            f'<div class=bar><span style="width:{100.0 * x["n"] / top:.0f}%"></span>'
            f'<i>{e(x["d"][5:])}</i><b>{x["n"]:,}</b></div>'
            for x in s["by_day"])

    return f"""<!doctype html><meta charset="utf-8">
<title>SparrowMap - what it costs and how it is going</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/style.css?v=105">
<style>
 /* 🚨 UNDO BOTH, NOT JUST overflow. style.css pins `html,body` to height:100%
    with `overflow:hidden` because the MAP is a fixed full-screen app that
    scrolls its own panel, and every document page borrowing the stylesheet has
    to hand that back. This page never did: on a desktop it stopped at the fold
    with no scrollbar, so the costs, the hardware goals and the donate link -
    the entire point of the page - were unreachable. Reported by him.
    hardware.html already carries this exact fix and says why body alone is not
    enough: it leaves html clamped and the page still refuses to scroll. */
 html,body{{height:auto;overflow:visible}}
 body{{padding:0}}
 .wrap{{max-width:820px;margin:0 auto;padding:20px 18px 70px}}
 h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:15px;margin:30px 0 10px;
   color:var(--dim);text-transform:uppercase;letter-spacing:.11em;
   font:600 11.5px/1 var(--mono)}}
 .n{{color:var(--dim2);font-size:12.5px;line-height:1.6}}
 p{{font-size:14px;color:var(--dim);line-height:1.68}}
 .big{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
 .big div{{background:var(--bg2);border:1px solid var(--line);border-radius:10px;
   padding:13px 15px}}
 .big b{{display:block;font:600 22px/1.2 var(--mono);color:var(--ink)}}
 .big span{{font-size:12px;color:var(--dim2)}}
 table{{width:100%;border-collapse:collapse;margin:10px 0}}
 td,th{{text-align:left;padding:9px 6px;border-bottom:1px solid var(--line);
   font-size:13.5px;vertical-align:top}}
 th{{color:var(--dim2);font:600 11px/1 var(--mono);text-transform:uppercase;
   letter-spacing:.1em}}
 .num{{text-align:right;font-family:var(--mono);white-space:nowrap}}
 .tot td{{border-bottom:0;border-top:1px solid var(--line2)}}
 .bar{{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}}
 .bar i{{font-style:normal;color:var(--dim2);font-family:var(--mono);width:44px}}
 .bar b{{color:var(--dim);font-family:var(--mono);width:52px;text-align:right}}
 .bar span{{display:block;height:9px;background:var(--police);border-radius:3px;
   min-width:2px;order:2;flex:0 0 auto}}
 .bar{{position:relative}} .bar span{{flex:1 1 auto;max-width:60%}}
 .warn{{background:#1a1408;border:1px solid #5c4a1c;border-radius:9px;
   padding:12px 14px;color:#ffc866;font-size:13px}}
 .cta{{display:inline-block;background:var(--police);color:#0b0e13;font-weight:600;
   padding:12px 20px;border-radius:10px;text-decoration:none;margin:6px 0}}
 .hon{{border-left:2px solid var(--line2);padding:2px 0 2px 13px;margin:14px 0}}
 .g{{margin:12px 0}}
 .gl{{display:flex;justify-content:space-between;font-size:13px;color:var(--dim);
   margin-bottom:5px}} .gl b{{font-family:var(--mono);color:var(--ink)}}
 .gt{{height:8px;background:var(--bg2);border:1px solid var(--line);
   border-radius:5px;overflow:hidden}}
 .gt span{{display:block;height:100%}}
 .goal{{border:1px solid var(--line);border-radius:11px;padding:14px 16px;margin:11px 0;
   background:var(--bg2)}}
 .gh{{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
 .gh b{{font-size:15px}} .gh .num{{color:var(--cam);font-weight:600}}
 .goal .gt{{margin:8px 0 6px}}
 .goal .gt span{{background:var(--cam)}}
 .goal p{{margin:8px 0 0;font-size:13.5px}}
</style>
<div class=wrap>
<p><a href="/" class=n>&larr; back to the map</a></p>
<h1>What this costs, and how it is actually going</h1>
<p class=n>Generated from the live database on
{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['generated']))}. Every number
here is produced by <code>tools/support_page.py</code> reading the database, not
typed in by hand.</p>
<p><a class=cta href="https://cash.app/$sparrowmap">Chip in &mdash; $sparrowmap on Cash App</a></p>

<h2>The volunteer network</h2>
<div class=big>
 <div><b>{s['vol_enrolled']:,}</b><span>cameras enrolled by people</span></div>
 <div><b>{s['vol_live_24h']:,}</b><span>reported in the last 24h</span></div>
 <div><b>{s['vol_produced']:,}</b><span>have ever sent a sighting</span></div>
 <div><b>{s['public_all']:,}</b><span>government vehicles published, all time</span></div>
</div>

<div class=hon>
<p><b>The number we are not going to inflate.</b> The database also holds
{s['cam_enrolled']:,} public traffic cameras, {s['cam_live_24h']:,} of them
reporting. Those are not volunteers - this project found them and enrolled them
itself, and they are counted separately everywhere on this page. Adding them to
the number above would make the network look thirty times larger than it is.</p>
<p><b>And the number that actually matters.</b> Of the {s['ret_total']:,}
volunteer cameras enrolled more than three days ago, {s['ret_still']:,} were
still reporting in the last 24 hours. That is {s['ret_pct']:.0f}%. Reach has not
been the problem; people set a camera up and stop. That is the real constraint on
this project and it would be dishonest to leave it off a page asking for
money.</p>
</div>

<h2>Cameras enrolled per day, people only</h2>
{bars or '<p class=n>No enrolments in the last 14 days.</p>'}

<h2>Output</h2>
<div class=big>
 <div><b>{s['public_24h']:,}</b><span>government sightings, 24h</span></div>
 <div><b>{s['sightings_24h']:,}</b><span>vehicles seen, 24h (all tiers)</span></div>
 <div><b>{s['hours_watched']:,.0f}</b><span>hours of camera time, total</span></div>
 <div><b>{days:.0f}</b><span>days old</span></div>
</div>
<p class=n>Most of what the cameras see is ordinary traffic, and that is
deliberate: those rows carry no plate text, are not searchable, and exist only so
the map can show that a road is busy. The government-vehicle count is the small
one because publishing one takes a human confirming it.</p>

{('<h2>What this needs, and what each piece unlocks</h2>' + goals_block) if goals_block else ''}

<h2>What it is running on right now</h2>
{gauges}
{need}

<h2>What it costs to run</h2>
{cost_block}
{supporters}
<p><a class=cta href="https://cash.app/$sparrowmap">Chip in &mdash; $sparrowmap on Cash App</a></p>
<p class=n>If money ever exceeds what the servers cost, the surplus goes to the
same place: more polling capacity and bandwidth. There is no salary in this.</p>

<h2>What your money does not buy</h2>
<p>No access to anything a visitor cannot see. No private tier, no early
sightings, no plate lookups, no removals. The two-tier design is not a paid
feature and cannot be bought around: a private vehicle's plate is painted out on
the camera before it is uploaded, and there is nothing on the server to sell.</p>
<p class=n><a href="/transparency">Transparency</a> &middot;
<a href="/checksums">Checksums</a> &middot;
<a href="/status">Status</a> &middot;
<a href="https://github.com/SparrowMap/sparrowmap">Source</a></p>
</div>
<script src="/static/sitenav.js?v=39"></script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="show")
    a = ap.parse_args()

    s = stats()
    c = costs()
    cap = capacity()
    g = goals()
    print(f"volunteer cameras   {s['vol_enrolled']:,} enrolled, "
          f"{s['vol_live_24h']:,} live 24h, {s['vol_produced']:,} ever produced")
    print(f"scraped traffic cams {s['cam_enrolled']:,} enrolled, "
          f"{s['cam_live_24h']:,} live")
    print(f"retention           {s['ret_still']:,}/{s['ret_total']:,} "
          f"= {s['ret_pct']:.1f}%")
    print(f"published           {s['public_all']:,} all time, "
          f"{s['public_24h']:,} in 24h")
    print(f"goals               "
          + (f"{len(g.get('goals') or [])} item(s), "
             f"${sum(float(x.get('usd') or 0) for x in (g.get('goals') or [])):,.0f} total"
             if g.get("goals") else "NOT PUBLISHED (data/goals.json missing)"))
    print(f"costs               "
          + (f"${sum(float(i.get('usd_month') or 0) for i in (c.get('items') or [])):,.2f}/mo"
             if c.get("items") else "NOT PUBLISHED (data/costs.json missing)"))
    if cap.get("mem_total"):
        print(f"memory              {cap['mem_used_pct']:.0f}% used, "
              f"{cap['mem_avail'] / 1024**3:.1f} GB free")
        print(f"disk                {cap.get('disk_used_pct', 0):.0f}% used")
    else:
        print("capacity            not readable (run this ON the server)")
    if a.show:
        return 0
    OUT.write_text(render(s, c, cap, goals()), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
