"""Read the text painted on each published patrol-car crop (PaddleOCR).

    D:\\LLM\\reid_venv\\Scripts\\python.exe tools/reid/ocr.py            # all
    ... --limit 50                                                    # a look
    ... --mobile                                                      # faster models

Its OWN venv (D:\\LLM\\reid_venv) because paddle wants a different opencv than
the shared D:\\LLM\\.venv, whose cv2.pyd is held open by the running camera
detector. analyze.py (CLIP + colours) stays in the shared venv; link.py joins
the two outputs by sighting id.

WHY PADDLE, MEASURED 2026-09-12: EasyOCR read NOTHING on a 512 px crop with
"POLICE" in 40 px white letters across the door and "SHERIFF" in gold on a
black Silverado; PaddleOCR read SHERIFF at 1.00, POLICE at 1.00 and
"CITY OF LINDEN" at 0.96 on the same files. ⚠️ paddle 3.3 on Windows needs
enable_mkldnn=False or the detector dies inside oneDNN.

Resumable: writes data/reid/ocr.json after every 25 crops and skips ids it
already holds, so a killed run loses under a minute.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
DATA = REPO / "data" / "reid"
OUT = DATA / "ocr.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("--scale", type=float, default=2.0)
    args = ap.parse_args()

    from paddleocr import PaddleOCR
    kw = dict(lang="en", use_doc_orientation_classify=False,
              use_doc_unwarping=False, use_textline_orientation=True,
              device="cpu", enable_mkldnn=False)
    if args.mobile:
        kw.update(text_detection_model_name="PP-OCRv5_mobile_det",
                  text_recognition_model_name="PP-OCRv5_mobile_rec")
    ocr = PaddleOCR(**kw)

    rows = json.load(open(DATA / "reid_rows.json"))
    done = json.load(open(OUT)) if OUT.exists() else {}
    todo = [r for r in rows if str(r["id"]) not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(rows)} rows, {len(done)} already read, {len(todo)} to do", flush=True)

    t0 = time.time()
    for i, r in enumerate(todo):
        fn = r.get("snap_full") or r["snap"]
        p = DATA / "snaps" / fn
        if not p.exists():
            p = DATA / "snaps" / r["snap"]
            if not p.exists():
                done[str(r["id"])] = []
                continue
        im = Image.open(p).convert("RGB")
        if args.scale != 1:
            im = im.resize((int(im.width * args.scale), int(im.height * args.scale)), Image.LANCZOS)
        items = []
        try:
            for res in ocr.predict(np.asarray(im)[:, :, ::-1].copy()):
                for txt, sc, poly in zip(res["rec_texts"], res["rec_scores"], res["rec_polys"]):
                    if not txt:
                        continue
                    pts = np.asarray(poly).reshape(-1, 2) / args.scale
                    x0, y0 = pts.min(axis=0); x1, y1 = pts.max(axis=0)
                    items.append([txt, round(float(sc), 3),
                                  [int(x0), int(y0), int(x1), int(y1)]])
        except Exception as exc:          # one bad crop must not end the run
            print(f"  {r['id']}: {exc.__class__.__name__}: {exc}", flush=True)
        done[str(r["id"])] = items
        if (i + 1) % 25 == 0:
            json.dump(done, open(OUT, "w"))
            print(f"  {i + 1}/{len(todo)}  {time.time() - t0:.0f}s  "
                  f"({(time.time() - t0) / (i + 1):.1f}s each)", flush=True)
    json.dump(done, open(OUT, "w"))
    n = sum(1 for v in done.values() if v)
    print(f"\n{len(done)} read in {time.time() - t0:.0f}s; text found on {n} "
          f"({100 * n / max(1, len(done)):.0f}%)", flush=True)


if __name__ == "__main__":
    main()
