# Moving the CPU hog off the SparrowMap box

Written 2026-08-14, after SparrowMap suffered congestion collapse on a box whose
CPU was already spoken for.

## The measurement that decides the whole plan

Taken on the live box (`example-host`, **2 vCPU**, 3.8 GB RAM):

| process | CPU |
|---|---|
| `ffmpeg` — YouTube video encode (libx264, 720p24) | **107 %** |
| `ffmpeg` — audio encode to Icecast (libmp3lame 128k) | 3.6 % |
| `radio.py` — the broadcaster | 0.5 % |
| `icecast2` | 0.2 % |

**The entire radio station costs 4.3 % of one core. The YouTube VIDEO encoder costs
107 % — a whole core, permanently.**

So the instinct "move EmberFM off the box" aims at the wrong thing. Icecast,
`radio.py` and the player website are nearly free and can stay where they are.
**One service is 96 % of the problem: `project-stream.service`.**

It already runs at `Nice=5`, which is not helping, because niceness only decides
who wins a contended core and there is nothing else to yield to — the work is
genuinely there.

## Option A — move only the YouTube encoder (recommended)

Frees a full core. Touches nothing that serves a listener.

**What actually moves:** `youtube_stream.py`, and `pngs/` at **5.5 MB**. That is all.

**The one real change:** the encoder currently reads
`http://localhost:8000/ember.mp3`. From another box it reads
`https://example-stream.example.local/ember.mp3` — already public, already live, 128 kbps.

✅ **And no code edit is needed.** `youtube_stream.py:24` is already
`ICECAST = os.environ.get("EMBER_STREAM_URL", "http://localhost:8000/ember.mp3")`,
so the new box only needs that environment variable set in its unit file. The
script stays byte-identical to the one on the old box, which is what makes
rollback a straight copy back rather than a reverse-edit.

**Risk:** YouTube accepts one stream per key, so old must stop before new starts.
Expect a short gap on the YouTube channel. **Icecast, TuneIn, the player site and
every listener are untouched** — they never involve this service.

Side benefit: the old box also stops pushing video upstream to YouTube.

### Steps
1. Hetzner **CPX21** (3 vCPU AMD, ~€8/mo). x264 720p24 needs roughly one solid
   core; three gives headroom. CPX31 if the channel ever goes 1080p.
2. `apt install ffmpeg python3-venv`, create the `ember` user.
3. Copy `/srv/ravenmap/streamer/youtube_stream.py` and `/srv/ravenmap/streamer/pngs/`.
   ⚠️ The stream key is inside `youtube_stream.py` — **treat that file as a secret**,
   move it over SSH, and never let it reach a public repo.
4. Point the input at `https://example-stream.example.local/ember.mp3`.
5. Copy `project-stream.service`, fix paths, drop `Nice=5` (nothing to yield to
   now), drop the `After=` on the upstream service — they live on another machine.
6. **Cutover:** `systemctl stop project-stream` on the old box, start it on the
   new one, confirm the stream is live again.
7. Only then `systemctl disable project-stream` on the old box.
8. Watch `/api/health` on SparrowMap: `threads_peak` and load should fall, and
   the 48-connection cap should stop being reached.

### Rollback
Stop the new box's service, `systemctl start project-stream` on the old box.
Nothing was deleted, so rollback is one command and another short stream gap.

## Option B — move the whole radio

Icecast, `radio.py`, the player site and the encoder all move together.

**Costs much more risk for 4.3 % more CPU.** It needs a DNS change for
`example-stream.example.local`, a Caddy vhost and certificate on the new box, the
upstream credentials, the `radio_library` (171 MB), and it drops every live listener
plus the public feed during cutover.

Worth doing only if the goal is separating the projects for their own sake rather
than reclaiming CPU. If so, do **Option A first** — it is a prerequisite step of B
anyway, and it delivers the entire performance win on its own.

## What is NOT part of this

`/srv/ravenmap/project-audio` (3.1 GB) is the **Project Audio site**, not the stream,
and it costs 0.1 % CPU. `litestream` and `bizcenter` likewise. None of them are why
SparrowMap fell over. Leave them.