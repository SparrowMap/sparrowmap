# Running a desktop camera node

This is the guide for running SparrowMap's **full recognition pipeline** on a
computer with a camera. It is a different thing from the one-tap browser camera,
and which one you want depends on what you need.

## Three ways to run a camera, and why they differ

| | **Browser camera** | **Add an IP Camera** | **Desktop node** (this guide) |
|---|---|---|---|
| Your camera is | the phone or laptop itself | an **IP / RTSP camera** you already own | a webcam on that PC |
| Install | none — a web page | one 76 MB app, or one Python file | ~2.5 GB of models and Python |
| Detector | YOLO11-small, in the browser | the **same** YOLO11-small, on the CPU | RF-DETR + CLIP + a trained head |
| Tells truck from car | roughly | roughly | reliably |
| Flags government vehicles | no — a human decides later | no — scored on SparrowMap's hardware | **yes, on the machine** |
| Reads plates | no | no | yes (retroreflective plates, daylight or with IR) |
| Runs on | any phone or laptop | any PC, no graphics card | a PC left on, ideally with an NVIDIA GPU |
| Get it | *Add a camera* on the map | [/IPCamera](https://map.sparrowmap.com/IPCamera) | this guide |

The browser camera is deliberately tiny so it runs on any phone with nothing to
install. It finds vehicles and sends a crop, but it cannot run CLIP or the
trained head, so it cannot decide "that was a patrol car" by itself. The desktop
node runs the real models locally and makes that call before it sends anything.

**If you just want to contribute a camera, use the browser** — open the site,
press *Add a camera*. **If a camera already points at the street outside your
shop or house, use [Add an IP Camera](https://map.sparrowmap.com/IPCamera)** — it is the same light detector as the browser
one, pointed at an RTSP stream instead of a webcam, so it costs your machine
almost nothing and needs no models. Use a **desktop node** when you want
accurate classification, plate reading, or you are running the camera that does
the government-vehicle calls for a whole instance.

⚠️ **Two installers, and they are not versions of each other.**
`SparrowMapCameraSetup.exe` is *this* guide's node: a webcam, the full models,
about 2.5 GB. The **Add an IP Camera** app (`SparrowMap.exe`, renamed from
`SparrowMap4Biz.exe`) is the light relay for an IP camera. Neither
replaces the other — see [Which download?](https://github.com/SparrowMap/sparrowmap/releases/tag/v0.1.0)

Everything still holds to the same rule: **recognition happens on your machine,
and only a small detection event leaves it. Video never does.**

---

## Turn-key install (recommended)

One command sets up everything — Python, the models, autostart — and takes you
straight to aiming your camera. It contributes to the public map at
`https://map.sparrowmap.com`; no account, no manual enrollment.

**Windows** (PowerShell):

```powershell
irm https://sparrowmap.com/install-node-windows.ps1 | iex
```

or download **SparrowMapCameraSetup.exe** from the
[latest release](https://github.com/SparrowMap/sparrowmap/releases) and run it.

**Linux** (⚠ untested — help us test, report at
[issues](https://github.com/SparrowMap/sparrowmap/issues)):

```bash
curl -fsSL https://sparrowmap.com/install-node-linux.sh | bash
```

The installer downloads ~2.5 GB of models, installs a **SparrowMap Camera**
shortcut, starts it at login, and opens the setup page. Aim your camera, draw
the stretch of road it watches, and click Save — that enrolls it and it starts
contributing. To review your camera's catches, ask for a reviewer link to
`/rv`.

Running a **private instance** instead of contributing to the public map? Set
`SPARROW_HUB` to your own hub before installing and everything points there.
The manual steps below are the same thing by hand.

---

## What you need

- **Python 3.12** (3.11 works too).
- **A camera the machine can read**: a USB webcam, or an IP camera that speaks
  RTSP / ONVIF / MJPEG / still-snapshot. Cloud-only cameras (Wyze, most Roku
  rebadges) cannot be used — if the vendor app has no RTSP option, it will not
  work here.
- **~3 GB of disk** for the models (CLIP is the large one).
- **Optional but recommended: an NVIDIA GPU.** CPU is fine for one camera; a GPU
  lets the classifier keep up with busier roads.
- **A hub to report to.** Either the public network (`https://map.sparrowmap.com`)
  or your own hub (`python hub.py`, see the main README).

---

## Install

```bash
git clone https://github.com/SparrowMap/sparrowmap
cd sparrowmap

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# Install torch FIRST, matched to your hardware:
#   NVIDIA (CUDA 12.1):
pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
#   CPU only:
pip install torch==2.5.1

# Then everything else:
pip install -r requirements-node.txt
```

On an NVIDIA machine, also swap ONNX Runtime to the GPU build so the detector
uses the card:

```bash
pip uninstall -y onnxruntime
pip install onnxruntime-gpu==1.22.0
```

> Use **1.22.0**, not the latest. Newer builds are compiled for CUDA 13 and
> silently fall back to CPU on a CUDA 12 box. `detect/cuda_setup.py` handles the
> DLL path; you do not need to.

The detection and OCR models download themselves on first run. CLIP
(`openai/clip-vit-base-patch32`, ~600 MB) downloads the first time the
classifier loads.

---

## The three pieces, and the order they start in

A desktop node is three programs, and **the order matters**:

1. **camctl** owns the camera. It opens the webcam, re-serves it as a stream,
   and is where you aim the camera and place the road it watches. It also
   enrolls the camera with the hub.
2. **the hub** holds the map, the review queue, and the data (`python hub.py`).
   Skip running your own if you report to an existing hub.
3. **the detector** (`run_live.py`) reads camctl's stream and does the
   recognition.

On Windows exactly one process can hold a USB camera. Start camctl **first** —
if the detector grabs the camera first, camctl shows a black frame and looks
broken.

### 1. Start camctl and place the camera

```bash
python camctl/camctl.py --index 0    # 0 = first USB camera; serves on :8160
```

Open **http://localhost:8160/**. You will see the live camera. Aim it, then draw
the stretch of public road it watches on the map and save. Saving **enrolls the
camera** with the hub and writes `camctl/placement.json` — your **node id and
token**. That road is what gets published; the camera's own position never is.

Find and vet IP cameras instead of a USB one:

```bash
python tools/find_cameras.py 192.168.1.0/24   # sweep a subnet
python tools/probe_camera.py 192.168.1.50     # ports + ONVIF + RTSP banner
```

A camera is only usable if it hands over a real frame, not just answers on a
port. Shooting through a window? Press the lens flush to the glass, clean the
glass, kill room light behind it. Plates are retroreflective, so night reading
needs an IR illuminator — test in daylight first.

### 2. Start the hub (self-hosting only)

```bash
python hub.py        # map at :8150, https contributor at :8151
```

camctl enrolls against `http://localhost:8150` by default, so a self-hosted node
is fully local: camctl → hub → detector all on your machine. Your hub can then
mirror its public tier out to the shared map (see `DEPLOY.md`). Joining the
public `sparrowmap.com` map directly from a desktop node is the **federation**
feature, which is on the roadmap — for now, run your own hub.

### 3. Run the detector

```bash
python detect/run_live.py --post --visual \
  --node <node-id-from-placement.json> \
  --token <token-from-placement.json>
```

`--source` defaults to camctl's stream (`http://localhost:8160/stream.mjpg`), so
you do not pass it for a USB camera. Point it straight at an RTSP/MJPEG URL for
an IP camera.

| flag | what |
|---|---|
| `--post` | actually send detections to the hub (omit to watch locally first) |
| `--visual` | run the CLIP + head government-vehicle classifier (recommended) |
| `--source` | override the camera: an RTSP/MJPEG URL for an IP camera |
| `--hub` | the hub URL (default `http://localhost:8150`) |
| `--no-bank` | do not keep training crops locally |
| `--seconds N` | run for N seconds then stop (handy for a test) |

Start without `--post` to confirm it sees vehicles, then add it to go live. The
hub shows the camera online while this runs and offline within ~90s of it
stopping. On Windows, `Start Camera Node.bat` wraps this in a restart loop and
reads the node id and token from `placement.json` for you.

**Keep it running:** a computer that sleeps is a camera that stops.

```
# Windows
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

---

## The trained head

The government-vehicle call is not "YOLO with a police label." A vehicle box is
found (RF-DETR), then CLIP scores what it is against a set of prompts, then a
small **logistic head trained on real labelled camera crops** makes the final
call. The head ships as `data/models/vehicle_head.npz` and loads automatically
when present; without it the node falls back to CLIP zero-shot. It is graded, not
a keyword — see `detect/head.py` and `classify.py` for the exact gate, and
`train/README.md` to retrain it on your own footage.

Nothing a single cue asserts can publish a plate on its own. That rule is in
`classify.py`, and it is what stops a confidently wrong classification from
making a stranger public.

---

## Optional: classify phone crops with your desktop head

If your instance also has **phone cameras**, their crops arrive with no
government-vehicle verdict — a phone cannot run the head. A desktop node can
score them after the fact:

```bash
python -m detect.classify_worker --once        # score the current backlog
python -m detect.classify_worker --interval 120 # or keep polling
```

It runs CLIP + the trained head over freshly banked phone crops and records the
verdict. It **publishes nothing** — a government call only makes the crop appear
in your review queue, where a human confirms it before it reaches the map. Run
`--dry-run` first to see the verdicts without writing anything.

---

## Check it is actually working

- `python tools/check_running_code.py` — confirms the process is running your
  current source, not a stale copy.
- Open the hub's review page — your camera's detections show up there.
- No plates reading? That is almost always optics, not code: a plate needs to
  arrive roughly 60 px wide. Dots (a vehicle was here) work at far lower
  resolution than readable plates do.

More hard-won camera and Windows notes are in the project's build history; the
short version is: prefer a wired USB or RTSP camera, do not let the machine
sleep, and test in daylight before blaming the night.
