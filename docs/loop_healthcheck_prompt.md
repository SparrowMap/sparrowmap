# The /loop health-check prompt

Paste the block below after `/loop` and an interval. Recommended:

```
/loop 30m <paste the block>
```

30 minutes is the sweet spot: long enough that a quiet night costs almost
nothing, short enough that an outage is caught before people notice. Use `20m`
if something is actively unstable.

⚠️ **The prompt is deliberately written to STAY QUIET when nothing is wrong.**
A monitor that reports "all fine" every half hour is a monitor you stop reading,
and then it fails silently the one time it matters.

---

## THE PROMPT

```
SparrowMap health + bug sweep. map.sparrowmap.com is LIVE with real traffic and
keeping it up outranks any feature work. Measure before concluding, and never
restart anything before you know why it broke.

Run these checks. Do NOT ask permission first - they are all read-only.

1. OUTSIDE THE BOX (what a visitor actually gets). Fetch, with a browser
   User-Agent (no UA = Cloudflare 1010):
     https://map.sparrowmap.com/
     https://map.sparrowmap.com/api/stats
     https://map.sparrowmap.com/api/nodes
     https://map.sparrowmap.com/api/sightings?since=0&vclass=public&limit=2000
   PASS = all 200 and under ~3s. Any 503, or /api/sightings returning no rows,
   means the map is showing "reconnecting" or losing its markers.

2. THE HUB. ssh -i /path/to/ravenmap-key root@deploy-host and read
   http://127.0.0.1:8150/api/health. Report anything outside these:
     db == "ok"
     heavy_free  > 5   (cap 48)
     inflight    < 60  (cap 200)
     road_cells_failing < 60 and NOT climbing across samples
     fd_used_pct < 20
     inflight_now: nothing held more than ~10s
   ⚠️ Pools read healthy BETWEEN bursts, so a single sample can miss a real
   fault. If anything looks off, sample 6 times 20s apart before concluding.

3. HIS CAMERA. In sparrow.db, node n_0f9b78ab ("Bridge St, DIR SOUTH") must
   have beaten within 90s. If not, the camera on his desktop is down - check
   run_live.py and camctl.py are running locally and restart them (standing
   instruction: restart the camera without asking).

4. THE SCRAPER, which is the thing that has taken the map down most often.
     journalctl -u sparrowmap-cams --since "-20 min" | grep "cycle done"
   Cycle should be roughly 100-130s. Count 503s in that window: a handful out
   of ~18,000 sends per cycle is fine, hundreds is not. If it is starving the
   hub again, confirm CPU priority survived:
     systemctl show sparrowmap sparrowmap-cams -p CPUWeight
   Expect hub 800, cams 60.

5. LOCAL SERVICES on this machine: run_live (camera), camctl, box_puller,
   youtube_stream. Report any that died.

6. THE PEN. Count sightings awaiting review (tier=private, reviewed IS NULL,
   vclass in police/gov). A sudden jump means the classifier changed behaviour
   or something is spamming.

7. DISK + MEMORY on the box: df -h / and free -m. Flag over 85% used.

KNOWN CAUSES - check these before inventing a new theory:
  * 503 with the general pool nearly empty  -> a NARROWER pool is refusing
    (heavy, ingest, or road_lookup). Find which. Do not raise the first cap.
  * road_cells_failing climbing             -> check tools/road_fill.py is
    still running on his desktop (scheduled task "SparrowMap road cache
    fill"). Overpass REFUSES the box but answers his desktop, so the desktop
    resolves cells and copies them over. A value that oscillates (0..60) is
    the normal working state - new areas appear and get filled. Only a value
    climbing across several samples, with the cache NOT growing, is a fault.
  * bursty 503s, idle between bursts        -> public_cams.py starving the hub
    for CPU. Fix is systemd CPUWeight, not a bigger cap.
  * map loads but no markers                -> /api/sightings, not /api/nodes.
  * a fix that "should" work but does not   -> check the process is actually
    running the new code. pkill -f matches NOTHING on Windows.

REPORTING RULE:
  * Everything healthy -> reply with ONE line: "healthy - <the 3-4 numbers>".
    Nothing else. Do not summarise the checks you ran.
  * Something wrong -> diagnose it, fix it if the cause is known and the fix is
    one you have already made before (cache copy, CPU weight, restart a dead
    local service), deploy via tools/deploy.py with DEPLOY_HOST and
    DEPLOY_KEY set, verify the fix externally, and THEN report what broke and
    what you did. Send a PushNotification for anything user-visible.  * Something wrong that you are NOT confident about -> do not guess and do not
    restart. Report what you measured and stop.

Never run a drive-wide grep or a recursive grep over D:/LLM/sparrow - the data
directories are huge and it hangs. Use the Grep tool with a glob instead.
```

---

## Why each check is there

| check | the failure it catches | when it bit us |
|---|---|---|
| external fetch | the map is down for real users while health says ok | 2026-08-18, "reconnecting and nothing but planes" |
| `road_cells_failing` | a missing `data/roadcache` after a move | 2026-08-18 migration, 2,760 failing cells |
| `heavy_free` | an admission cap sized for a retired box | `MAX_HEAVY=12` on a machine with 4x the cores |
| held-request age | a slow request pinning a pool while only one build runs | `/api/places` held 328s |
| scraper cycle | `public_cams.py` eating the hub's CPU | 17,168 posts/cycle, 503ing its own hub |
| camera beat | his own node silently dead after a power cut | 2026-08-19 |
| sampling 6x | pools look fine in the trough between bursts | the reason the scraper fault hid for hours |
