"""Read what is PRINTED on each published patrol-car crop, and embed it.

    python tools/reid/analyze.py                # all rows in data/reid/rows.json
    python tools/reid/analyze.py --limit 200    # a quick look

Runs on the DESKTOP (GPU), never on the box (2 cores, no GPU). Input is the
dump made by the box-side export (data/reid/reid_rows.json + snaps/); output is
data/reid/features.json - per sighting: the OCR words with confidence, the
digit groups, the dominant body colours, and an index into embeddings.npy.

WHY OCR AND NOT JUST AN EMBEDDING
A CLIP embedding of a white Explorer with a black door says "a white Explorer
with a black door" - which is every car in the fleet. What tells two units
apart is what is PAINTED ON THEM: the agency word (POLICE / SHERIFF / STATE
TROOPER), the city, and the unit number on the roof, doors or fenders. Those
are the identifiers he pointed at, and they are text, so they are read as text.
The embedding is kept for the cheap case (the same car sitting in front of the
same camera for two hours) where nothing needs reading.

The crops are 200 px on the long edge by design (the plate-illegibility cap,
see sparrow_resolution_and_retrain). They are upscaled 3x before OCR because
the recognizer was trained on text ~30 px tall and a roof number here is ~10.
Upscaling adds no information, it just stops the model discarding what is there.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATA = REPO / "data" / "reid"

# Words that name an agency kind. Matched loosely because OCR on a 200 px
# crop mangles letters: "POLICF", "SHERlFF".
AGENCY_WORDS = {
    "police": ["police", "polic", "pouce", "polce", "poiice"],
    "sheriff": ["sheriff", "sherif", "sherff", "sheriif", "sherlff"],
    "trooper": ["trooper", "troope", "state trooper", "highway patrol", "patrol"],
    "state police": ["state police", "statepolice"],
    "constable": ["constable"],
    "marshal": ["marshal"],
    "k9": ["k-9", "k9"],
    "emergency": ["emergency", "911"],
}


def agency_of(words: list[str]) -> str | None:
    text = " ".join(w.lower() for w in words)
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    for kind in ("state police", "trooper", "sheriff", "police", "constable", "marshal"):
        for v in AGENCY_WORDS[kind]:
            if v in text:
                return kind
    return None


def digit_groups(items: list[tuple[str, float, tuple]]) -> list[dict]:
    """Unit-number candidates: 2-4 digit groups, not obviously a phone number."""
    out = []
    for txt, conf, box in items:
        for m in re.finditer(r"(?<!\d)(\d{2,4})(?!\d)", txt):
            g = m.group(1)
            if g in ("911",):
                continue
            # A run of many digit groups in one read is a phone number.
            if len(re.findall(r"\d", txt)) > 5:
                continue
            out.append({"n": g, "conf": round(float(conf), 3), "box": box,
                        "read": txt})
    return out


COLOR_NAMES = {
    "white": (235, 235, 235), "black": (25, 25, 25), "grey": (128, 128, 128),
    "silver": (190, 190, 190), "blue": (40, 70, 160), "navy": (20, 30, 80),
    "red": (180, 30, 30), "green": (30, 110, 60), "tan": (200, 170, 120),
    "yellow": (220, 200, 40), "brown": (110, 70, 40),
}


def dominant_colors(im: Image.Image, k: int = 3) -> list[str]:
    """Named dominant colours of the middle of the crop (the body, not the road)."""
    from sklearn.cluster import KMeans
    w, h = im.size
    body = im.crop((int(w * 0.15), int(h * 0.2), int(w * 0.85), int(h * 0.85)))
    px = np.asarray(body.convert("RGB").resize((48, 32))).reshape(-1, 3).astype(float)
    km = KMeans(n_clusters=k, n_init=3, random_state=0).fit(px)
    names = []
    for centre, share in sorted(zip(km.cluster_centers_, np.bincount(km.labels_)),
                                key=lambda t: -t[1]):
        best = min(COLOR_NAMES.items(),
                   key=lambda kv: np.linalg.norm(np.array(kv[1]) - centre))[0]
        if best not in names:
            names.append(best)
    return names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--cpu", action="store_true",
                    help="the 2070S is shared with BeamNG and the detector; CPU is slower but not starved")
    args = ap.parse_args()

    rows = json.load(open(DATA / "reid_rows.json"))
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} sightings", flush=True)

    import torch
    import open_clip
    dev = "cuda" if (torch.cuda.is_available() and not args.cpu) else "cpu"
    torch.set_num_threads(4)   # leave cores for the camera detector and the cockpit
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k", device=dev)
    model.eval()

    reader = None
    if not args.no_ocr:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=(dev == "cuda"), verbose=False)

    feats, embs = [], []
    t0 = time.time()
    for i, r in enumerate(rows):
        # Prefer the full-resolution picture when a reviewer's yes fetched one.
        fn = r.get("snap_full") or r["snap"]
        p = DATA / "snaps" / fn
        if not p.exists():
            p = DATA / "snaps" / r["snap"]
            if not p.exists():
                continue
        im = Image.open(p).convert("RGB")

        with torch.no_grad():
            e = model.encode_image(preprocess(im).unsqueeze(0).to(dev))
            e = e / e.norm(dim=-1, keepdim=True)
        embs.append(e[0].float().cpu().numpy())

        words, digits = [], []
        if reader is not None:
            big = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
            items = [(t, c, [[int(x), int(y)] for x, y in b])
                     for b, t, c in reader.readtext(np.asarray(big))]
            words = [{"t": t, "c": round(float(c), 3), "b": b} for t, c, b in items]
            digits = digit_groups(items)

        feats.append({
            "id": r["id"], "node_id": r["node_id"], "ts": r["ts"],
            "lat": r["lat"], "lon": r["lon"], "vclass": r["vclass"],
            "place": r.get("place"), "road": r.get("road_name"),
            "node_kind": r.get("node_kind"),
            "file": fn, "size": im.size,
            "words": words,
            "agency": agency_of([w["t"] for w in words]),
            "digits": digits,
            "colors": dominant_colors(im),
            "emb": len(embs) - 1,
        })
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(rows)}  {time.time() - t0:.0f}s", flush=True)

    np.save(DATA / "embeddings.npy", np.stack(embs))
    json.dump(feats, open(DATA / "features.json", "w"))
    n_ag = sum(1 for f in feats if f["agency"])
    n_dg = sum(1 for f in feats if f["digits"])
    print(f"\n{len(feats)} analysed in {time.time() - t0:.0f}s: "
          f"agency word read on {n_ag} ({100 * n_ag / max(1, len(feats)):.0f}%), "
          f"a 2-4 digit group on {n_dg} ({100 * n_dg / max(1, len(feats)):.0f}%)")


if __name__ == "__main__":
    main()
