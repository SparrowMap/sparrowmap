"""Measure which OCR settings actually read the number off a patrol car.

    D:\\LLM\\reid_venv\\Scripts\\python.exe tools/reid/ocr_sweep.py <jpg> [<jpg>...] --want 4715

Sweeps model size (mobile vs server) against upscale factor and prints what each
combination read, with how long it took. Exists because "better text detection"
has to be chosen on a measurement, not a guess: 2026-09-23 the shipped setting
(mobile, scale 2) read "25" and "aonod" off a crop where 4715 is painted on the
roof, the tailgate and the fender, in white, in daylight.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image


def main() -> None:
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--want", default="")
    ap.add_argument("--scales", default="2,3,4,6")
    ap.add_argument("--models", default="mobile,server")
    args = ap.parse_args()
    from paddleocr import PaddleOCR

    scales = [float(s) for s in args.scales.split(",")]
    engines = {}
    for m in args.models.split(","):
        kw = dict(lang="en", use_doc_orientation_classify=False,
                  use_doc_unwarping=False, use_textline_orientation=True,
                  device="cpu", enable_mkldnn=False)
        if m == "mobile":
            kw.update(text_detection_model_name="PP-OCRv5_mobile_det",
                      text_recognition_model_name="PP-OCRv5_mobile_rec")
        print(f"loading {m} ...", flush=True)
        engines[m] = PaddleOCR(**kw)

    for f in args.files:
        im0 = Image.open(f).convert("RGB")
        print(f"\n=== {Path(f).name}  {im0.width}x{im0.height}", flush=True)
        for m, eng in engines.items():
            for sc in scales:
                im = im0.resize((int(im0.width * sc), int(im0.height * sc)), Image.LANCZOS)
                t0 = time.time()
                reads = []
                try:
                    for res in eng.predict(np.asarray(im)[:, :, ::-1].copy()):
                        for txt, s in zip(res["rec_texts"], res["rec_scores"]):
                            if txt:
                                reads.append(f"{txt}({s:.2f})")
                except Exception as exc:
                    reads = [f"{exc.__class__.__name__}"]
                hit = "  <== HIT" if args.want and any(args.want in r for r in reads) else ""
                print(f"  {m:6s} x{sc:<4g} {time.time() - t0:5.1f}s  "
                      f"{' '.join(reads) or '(nothing)'}{hit}", flush=True)


if __name__ == "__main__":
    main()
