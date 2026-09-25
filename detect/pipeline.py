"""SparrowMap - the real detection pipeline.

One vehicle passing a camera produces one sighting, not one per frame. That
sentence is most of the design.

    frame -> vehicle detect + track   (YOLO11 + ByteTrack, ultralytics)
          -> plate localise           (YOLOv9 ONNX, open-image-models)
          -> plate read               (CCT ONNX, fast-plate-ocr)
          -> accumulate per track
          -> vote, pick the best frame, emit ONE sighting

Two things here matter more than the model choices:

VOTING. A car crossing a camera is read ten to thirty times. Single-frame OCR
is a coin flip at distance; a confidence-and-area weighted vote across the whole
pass is dramatically better, and the *agreement fraction* it produces is a far
more honest confidence number than anything a single forward pass reports. Real
ALPR systems all do this. Ours reports both so the difference is visible.

COORDINATES. The plate box has to come out of this in FULL FRAME coordinates.
Plates are detected inside a vehicle crop, so every box is offset back before
it leaves. If that offset is wrong, `snapshot.redact_plate` paints a bar over
empty road and publishes the plate it was supposed to destroy. It is a
two-line bug with a one-way consequence, so it is done in exactly one place
(`PlateReader.read`) and asserted in the tests.
"""

from __future__ import annotations

import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# COCO ids that are road vehicles. Bicycles are excluded: they have no plate and
# their riders are people, which is the thing this project tries hardest not to
# be about.
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# A track must be seen this many frames before it counts. Kills one-frame
# flickers that would otherwise each become a "sighting".
MIN_TRACK_FRAMES = 3

# Frames a track can go unseen before we call the pass over and emit it.
TRACK_TIMEOUT_FRAMES = 12

# Shape and scale limits that separate a number plate from a word painted on a
# door. See the long note in PlateReader.read - both are set LOOSE on purpose,
# because dropping a real plate costs a plate and keeping a livery read costs a
# false published claim about a vehicle, and those are not the same price.
PLATE_MAX_ASPECT = 3.5          # a 2:1 plate, plus generous box slop
PLATE_MAX_WIDTH_FRAC = 0.55     # of the vehicle box width

# Whole-read agency words. A plate is never JUST this - a vehicle's door is.
# Legends that genuinely appear on plates (MUNICIPAL, STATE OWNED, EXEMPT...)
# are deliberately absent: classify.py needs those to keep working.
_LIVERY_WORDS = {
    "POLICE", "SHERIFF", "STATEPOLICE", "STATETROOPER", "TROOPER",
    "STATEPATROL", "HIGHWAYPATROL", "PATROL", "CONSTABLE", "MARSHAL",
    "FIRE", "FIREDEPT", "FIREDEPARTMENT", "RESCUE", "AMBULANCE", "EMS",
    "EMERGENCY", "K9", "CANINE", "SECURITY", "DEPUTY", "PD", "SO",
}


def _is_livery_text(text: str) -> bool:
    """True when an OCR read is a word off the bodywork, not a plate."""
    t = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if not t:
        return False
    return t in _LIVERY_WORDS


@dataclass
class PlateRead:
    """One plate observation from one frame."""
    text: str
    conf: float
    box: tuple[int, int, int, int]      # FULL FRAME coordinates
    area: int
    frame_idx: int
    # The recogniser also predicts an issuing region. Useful as a sanity gate:
    # a "United States" plate read on a Michigan street is corroboration, and a
    # confident "Vietnam" on the same street is a sign the crop is not a plate.
    region: Optional[str] = None


def _looks_like_plate(text: str) -> bool:
    """A plate-plausible read. A real plate is the right length and almost always
    MIXES letters and digits (ABC1234); a short one may be a vanity plate. A long
    single-alphabet string is a company WORD on a trailer, or a DOT/phone number -
    the exact 'text mistaken for a plate' that semi-truck livery produces."""
    t = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if not (3 <= len(t) <= 9):
        return False
    has_a = any(c.isalpha() for c in t)
    has_d = any(c.isdigit() for c in t)
    if has_a and has_d:
        return True          # the normal plate shape
    return len(t) <= 5       # all-letters / all-digits only at vanity length


def frame_quality(vbox, fw, fh, frame=None) -> float:
    """How well the VEHICLE is framed in this shot, 0..1: whole-in-view, big,
    centred, sharp. This is what a published crop should be chosen on - the old
    rule picked the crispest PLATE and saved cars half out of frame. Pass `frame`
    to include a sharpness term (variance of Laplacian); omit it for a cheap
    pre-check that skips the blur cost on obviously-worse frames."""
    x0, y0, x1, y1 = vbox
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0 or fw <= 0 or fh <= 0:
        return 0.0
    m = 3  # a box hard against an edge = the vehicle is partly out of frame
    clipped = x0 <= m or y0 <= m or x1 >= fw - m or y1 >= fh - m
    edge = 0.35 if clipped else 1.0
    size = min((bw * bh) / (fw * fh) / 0.35, 1.0)         # ~35% of frame = full marks
    off = (abs((x0 + x1) / 2 - fw / 2) / (fw / 2)
           + abs((y0 + y1) / 2 - fh / 2) / (fh / 2)) / 2.0
    center = 1.0 - 0.5 * off                              # mild: edges not disqualifying
    sharp = 1.0
    if frame is not None:
        cy0, cy1 = max(0, int(y0)), min(int(fh), int(y1))
        cx0, cx1 = max(0, int(x0)), min(int(fw), int(x1))
        crop = frame[cy0:cy1, cx0:cx1]
        if crop.size:
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
            sharp = min(cv2.Laplacian(g, cv2.CV_64F).var() / 200.0, 1.0)
    return edge * (0.45 * size + 0.30 * center + 0.25 * sharp)


@dataclass
class VehiclePass:
    """Everything we saw of one vehicle while it crossed the camera."""
    track_id: int
    cls_name: str
    first_frame: int
    last_frame: int = 0
    frames_seen: int = 0
    reads: list[PlateRead] = field(default_factory=list)
    best_frame: Optional[np.ndarray] = None
    best_vehicle_box: Optional[tuple] = None
    best_plate_box: Optional[tuple] = None
    # 🚨 EVERY plate-like box found in the best frame, not just the winner.
    #
    # `best_plate_box` is chosen by conf x sqrt(area), which favours SIZE - and
    # on a marked patrol car the door livery is a much bigger patch of white
    # text on dark paint than the plate is. The detector fired on the door
    # livery - the force name in white capitals - that false box won on area,
    # and redaction blacked out the LIVERY while leaving the real plate fully
    # readable.
    #
    # Redacting only the best box means redacting only the detector's favourite
    # mistake. All of them get covered now, because a plate left visible is the
    # failure this project cannot have, and a partly-obscured livery is merely
    # annoying.
    best_plate_boxes: list = field(default_factory=list)
    best_score: float = -1.0
    boxes: list[tuple] = field(default_factory=list)
    # ⚠️ WHEN THE VEHICLE PASSED, not when we got around to reporting it.
    # run_live.py used to post the whole run in one batch at the end and stamp
    # every sighting with time.time() at post time, so 31 passes spread over
    # five minutes all landed inside the same second. A live traffic view built
    # on that shows nothing for five minutes and then a single pile of dots.
    first_ts: float = 0.0
    last_ts: float = 0.0

    # 🚨 THE STRONGEST GOVERNMENT READ SEEN ON ANY LIVE FRAME OF THIS PASS.
    #
    # The red box in the viewfinder and the confirm popup use the SAME rule
    # (VehicleIdentifier.gov_call) on DIFFERENT images: the overlay classifies
    # each live frame's crop as the car crosses, while the popup classifies the
    # single crop the finished pass ends up carrying. A vehicle can read
    # government at its closest and clearest and then score under the bar on
    # whichever frame won `best_score` - so the operator watched a patrol car
    # light up red and was never asked about it. Reported exactly that way:
    # "just watched a cop drive by, got a red box but no popup".
    #
    # Same lesson gov_call itself was written for, one level up: it stopped the
    # two consumers using different RULES, and they were still using different
    # INPUTS. Remembering the live verdict on the pass is what lets the prompt
    # ask about what the operator was actually shown.
    gov_live: bool = False
    gov_live_conf: float = 0.0

    # ---- the vote ------------------------------------------------------
    def vote(self) -> tuple[str, float, float]:
        """Return (plate, confidence, agreement).

        Each read votes with weight = OCR confidence x sqrt(plate pixel area).
        Area matters because a plate read from twelve pixels across is a guess
        and a plate read from ninety is close to certain, and the square root
        keeps one very close frame from outvoting eight decent ones.

        `agreement` is the winning share of total weight. It is reported
        separately from OCR confidence because they answer different questions:
        confidence is "how sure was the network about this image", agreement is
        "did the network say the same thing every time it looked". Agreement is
        the one that catches a confidently-wrong read.
        """
        if not self.reads:
            return "", 0.0, 0.0
        weights: dict[str, float] = defaultdict(float)
        confs: dict[str, list[float]] = defaultdict(list)
        for r in self.reads:
            if not r.text:
                continue
            w = max(r.conf, 0.01) * (r.area ** 0.5)
            weights[r.text] += w
            confs[r.text].append(r.conf)
        if not weights:
            return "", 0.0, 0.0
        total = sum(weights.values())
        best = max(weights, key=weights.get)
        agreement = weights[best] / total if total else 0.0
        mean_conf = float(np.mean(confs[best]))
        return best, mean_conf, agreement

    def heading(self) -> Optional[float]:
        """Direction of travel in image space, degrees clockwise from up.

        Not a compass bearing. The node's own heading has to be added to turn
        this into one, which `run_video` does when a node is supplied.
        """
        if len(self.boxes) < 2:
            return None
        (x0, y0, x1, y1), (a0, b0, a1, b1) = self.boxes[0], self.boxes[-1]
        dx = ((a0 + a1) / 2) - ((x0 + x1) / 2)
        dy = ((b0 + b1) / 2) - ((y0 + y1) / 2)
        if abs(dx) < 4 and abs(dy) < 4:
            return None
        import math
        return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0


    def motion(self, frame_w: int = 0, frame_h: int = 0,
               full: bool = False) -> dict:
        """How this vehicle crossed the frame: the TIME half of a speed.

        🚨 THIS IS A MEASUREMENT, NOT A SPEED, AND THE DIFFERENCE IS THE WHOLE
        POINT. Speed is distance over time. The seconds here are real - capture
        timestamps - and so are the pixels. The METRES are missing until a
        camera has been calibrated against the road markings in its own view
        (US lane dashes are a federally standardised 10 ft painted / 30 ft gap,
        which is a free ruler lying in almost every frame).

        Nothing here is ever written to `speed_mph`. That column is carried all
        the way out to the public export, so a number in it is PUBLISHED, and
        an uncalibrated estimate published against a named officer is an
        accusation this project cannot support. Banking the measurement now
        means every pass recorded before calibration exists can still have a
        speed computed afterwards; not banking it throws that away for ever.

        🚨 `full` KEEPS EVERY SAMPLE, AND IT IS WHAT MAKES THE CLAIM CHECKABLE.
        His call: a sighting should be able to SHOW the lines, the
        measurements and the arithmetic, not just assert a number. Two points
        can only ever produce an average; the whole track is what lets a reader
        see the vehicle accelerate, see which samples the calculation used, and
        disagree with it. That is the same standard as the rest of the map -
        every published claim sits next to the photograph it came from.

        It is only worth the bytes where something is published, so the caller
        passes full=True for a government pass and leaves it off for the
        anonymous traffic that fades in forty-five seconds. Measured on the
        live fleet that is ~79 published rows a day against ~327,000 passes.

        Centres are normalised 0-1 so a camera that changes resolution does not
        invalidate what was already banked.
        """
        if self.first_ts is None or self.last_ts is None:
            return {}
        dt = float(self.last_ts) - float(self.first_ts)
        if dt <= 0 or len(self.boxes) < 2:
            return {}
        w = float(frame_w or 0) or 1.0
        h = float(frame_h or 0) or 1.0

        def centre(b):
            x0, y0, x1, y1 = (float(v) for v in b)
            return (round((x0 + x1) / 2.0 / w, 4), round((y0 + y1) / 2.0 / h, 4))

        x0, y0 = centre(self.boxes[0])
        x1, y1 = centre(self.boxes[-1])
        out = {"dt_s": round(dt, 3), "frames": int(self.frames_seen),
               "x0": x0, "y0": y0, "x1": x1, "y1": y1,
               "w": int(frame_w or 0), "h": int(frame_h or 0)}
        if full:
            # One row per sample: [seconds since the first sighting, cx, cy,
            # box width, box height] - all normalised except the time. The box
            # SIZE is kept because it is the cheapest depth cue there is: a car
            # twice as wide in pixels is roughly half as far away, which is
            # what turns a flat pixel path into a distance along the road.
            step = max(1, len(self.boxes) // 120)   # a long pass stays bounded
            t0 = float(self.first_ts)
            per = dt / max(1, len(self.boxes) - 1)
            track = []
            for i in range(0, len(self.boxes), step):
                bx0, by0, bx1, by1 = (float(v) for v in self.boxes[i])
                track.append([round(i * per, 3),
                              round((bx0 + bx1) / 2.0 / w, 4),
                              round((by0 + by1) / 2.0 / h, 4),
                              round((bx1 - bx0) / w, 4),
                              round((by1 - by0) / h, 4)])
            out["track"] = track
        return out

# --------------------------------------------------------------------------
# Plate detection + OCR
# --------------------------------------------------------------------------

class PlateReader:
    """Localise plates inside a vehicle crop, then read them."""

    def __init__(self,
                 det_model: str = "yolo-v9-s-608-license-plate-end2end",
                 ocr_model: str = "cct-s-v2-global-model",
                 det_conf: float = 0.30) -> None:
        # BEFORE onnxruntime is imported: without a CUDA runtime on the DLL
        # search path its GPU provider fails to load, prints a warning, and
        # falls back to CPU while still LISTING CUDA as available. See
        # detect/cuda_setup.py - this node ran on CPU for its whole life
        # believing otherwise.
        from detect.cuda_setup import enable_cuda, report
        self.cuda_note = enable_cuda()

        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import create_detector

        import onnxruntime as ort
        avail = ort.get_available_providers()
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                     if p in avail]

        self.det = create_detector(det_model, conf_thresh=det_conf, providers=providers)
        self.ocr = LicensePlateRecognizer(ocr_model, device="auto")

        # ⚠️ WHAT THE SESSION IS REALLY USING, not what was compiled in.
        # `ort.get_available_providers()` lists build-time providers and
        # reported CUDA on a machine that could not load it, which is precisely
        # how the CPU fallback stayed invisible.
        real = self._session_providers()
        self.providers = real or providers
        self.device = report(real)

        # Ask the model what it wants rather than assuming. The v1 recognisers
        # are grayscale and the v2 ones are RGB; feeding the wrong one raises
        # InvalidArgument deep inside onnxruntime. Reading it off the config
        # means swapping models later cannot silently break the reader.
        cfg = getattr(self.ocr, "config", None)
        self.color_mode = getattr(cfg, "image_color_mode", "rgb") or "rgb"
        self.ocr_errors = 0

        # Agency words read off BODYWORK rather than off a plate. They are not
        # plates and never become sightings, but they are evidence about what
        # the vehicle IS - "SECURITY" in particular says private company, not
        # government. See the note where they are collected in read().
        # ⚠️ Cleared per vehicle by reset_livery(); a set that accumulates over
        # a whole run would attribute one car's markings to the next one.
        self.livery_words: set[str] = set()

    def _session_providers(self) -> list:
        """Dig out the real InferenceSession's providers.

        The wrapper libraries hide the session at different depths, so this
        looks rather than assumes; an empty list means we could not tell, and
        the caller falls back to reporting the requested providers instead of
        inventing an answer.
        """
        for obj in (self.det, getattr(self.det, "model", None),
                    getattr(self.det, "session", None)):
            if obj is None:
                continue
            for attr in ("session", "sess", "_session", "ort_session", "model"):
                s = getattr(obj, attr, None)
                if s is not None and hasattr(s, "get_providers"):
                    try:
                        return list(s.get_providers())
                    except Exception:
                        pass
            if hasattr(obj, "get_providers"):
                try:
                    return list(obj.get_providers())
                except Exception:
                    pass
        return []
        self._warned = False

    def reset_livery(self) -> set:
        """Take the livery words seen since the last call, and start fresh.

        🚨 A CALLER THAT NEVER CALLS THIS WILL EVENTUALLY MARK EVERY VEHICLE AS
        SECURITY. The reader is reused across a whole run, so the set has to be
        drained per vehicle by whoever is assembling that vehicle's evidence.
        Returning the words as it clears them makes the correct usage the
        convenient one.
        """
        seen, self.livery_words = self.livery_words, set()
        return seen

    def read(self, frame: np.ndarray, vbox: tuple[int, int, int, int]
             ) -> list[PlateRead]:
        """Find and read plates on the vehicle at `vbox` in `frame`.

        Returns boxes in FULL FRAME coordinates. See the module docstring for
        why that is stated three times.
        """
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = (max(0, int(vbox[0])), max(0, int(vbox[1])),
                          min(w, int(vbox[2])), min(h, int(vbox[3])))
        if x1 - x0 < 24 or y1 - y0 < 24:
            return []

        crop = self.deveil(frame[y0:y1, x0:x1])
        try:
            dets = self.det.predict(crop)
        except Exception:
            return []

        out: list[PlateRead] = []
        for d in dets:
            b = d.bounding_box
            # <<< the offset back to frame coordinates. Nothing else does this.
            fx0, fy0 = x0 + int(b.x1), y0 + int(b.y1)
            fx1, fy1 = x0 + int(b.x2), y0 + int(b.y2)
            pw, ph = fx1 - fx0, fy1 - fy0
            if pw < 12 or ph < 6:
                continue

            # 🚨 THE DOOR LIVERY WAS BEING READ AS A NUMBER PLATE.
            # 2026-08-10: the operator spotted a published sighting whose plate was
            # "PO415". There was no such plate. It is the word POLICE on the
            # vehicle's door, OCR'd. Others in the same batch: P0411, P0116,
            # P04GE, W111, 1111, 111, P, 0_399.
            #
            # This is the SAME root cause the redaction bug above documents -
            # the detector fires on livery text - but it lands somewhere far
            # worse. There the consequence was blacking out the wrong patch;
            # here the false read is stored as `plate_text`, published, and made
            # SEARCHABLE as a government plate. And because "POLICE" is in
            # classify._PLATE_WORDS it also fired `gov_plate_word`, so the same
            # paint that fired the `livery` signal ALSO supplied its
            # "independent" corroboration. One observation voting twice is what
            # defeats a two-marker rule. See classify.py.
            #
            # Two geometric facts separate a plate from a word on a door, and
            # neither depends on reading it:
            #
            #   SHAPE. A US plate is 12x6in - 2:1. Perspective only ever makes
            #   it look NARROWER (rotation foreshortens the width), so the ratio
            #   falls toward 1 and never climbs. A word in capitals across a
            #   door runs 4:1 upward, and a force name with a town in it far
            #   more. The cap is set well above any real plate so that a badly
            #   fitted box is kept and a word is not.
            #
            #   SCALE. A plate is a small part of the vehicle it is bolted to -
            #   roughly a fifth of the width from behind, and less from an
            #   angle. Livery is designed to be read across a street and spans
            #   most of the door.
            #
            # Deliberately NOT filtering on height up the vehicle: it is the
            # obvious third rule and it is the one that breaks, because a porch
            # camera looking down and a phone held at waist height disagree
            # about where "low on the vehicle" is in pixels.
            aspect = pw / max(1, ph)
            if aspect > PLATE_MAX_ASPECT:
                continue
            if pw > (x1 - x0) * PLATE_MAX_WIDTH_FRAC:
                continue

            plate_img = frame[max(0, fy0):fy1, max(0, fx0):fx1]
            if plate_img.size == 0:
                continue

            text, conf, region = self._ocr_one(plate_img)

            # The geometry catches the shape; this catches the meaning. A read
            # that is ENTIRELY an agency word is livery no matter how
            # plate-shaped the box was - no jurisdiction issues a plate whose
            # whole content is "POLICE". Legends like MUNICIPAL or STATE OWNED
            # are left alone: those really are printed on real plates, which is
            # why classify.py looks for them.
            if _is_livery_text(text):
                # 🚨 DROPPED AS A PLATE, KEPT AS EVIDENCE.
                # This used to `continue` and lose the word entirely, which
                # meant the system could read SECURITY off the bodywork and use
                # it only to reject a plate candidate. That word is the single
                # best evidence that a marked vehicle is a private security
                # firm rather than police, and classify._looks_private_security
                # exists to act on it - so record it instead of forgetting it.
                self.livery_words.add(
                    "".join(ch for ch in text.upper() if ch.isalnum()))
                continue

            # Geometry can pass a trailer's company name or DOT number if the box
            # happens to be plate-shaped; the READ shape catches it. A plate mixes
            # letters and digits (or is short); a long single-alphabet word is not
            # one, so it stops counting as a plate (and inflating "plate legible").
            if not _looks_like_plate(text):
                continue

            out.append(PlateRead(text=text, conf=conf, region=region,
                                 box=(fx0, fy0, fx1, fy1),
                                 area=pw * ph, frame_idx=-1))
        return out

    @staticmethod
    def deveil(img: np.ndarray, black_point_thresh: int = 45) -> np.ndarray:
        """Remove veiling glare, but only when there is glare to remove.

        A camera behind glass suffers veiling glare: light scatters off the
        pane and lays a uniform milky sheet over the whole frame. It does not
        destroy detail, it compresses it - measured on this node, the black
        point sat at 68/255 instead of near 0, and local contrast restoration
        recovered 3.4x the edge energy.

        Applied ADAPTIVELY rather than always. On a clean image this is a
        no-op, because stretching an already-full histogram just amplifies
        noise and would make good cameras worse to help a bad one. The gate is
        the black point: if the darkest 1% of the crop is not actually dark,
        something is veiling it.
        """
        g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        black = float(np.percentile(g, 1))
        if black < black_point_thresh:
            return img                       # already clean, leave it alone

        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB) if img.ndim == 3 else None
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        if lab is None:
            return clahe.apply(g)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    def _ocr_one(self, plate_bgr: np.ndarray) -> tuple[str, float, Optional[str]]:
        """Run the recogniser on one plate crop.

        Returns (text, worst-character probability, region).

        ⚠️ NOTE ON THE EXCEPTION HANDLING. An earlier version wrapped this in a
        bare `except: return "", 0.0`. The recogniser was then fed grayscale
        when it wanted RGB, raised InvalidArgument on every single call, and the
        handler turned that into a silent empty string. The result read as "the
        OCR cannot read any plates" across a whole test run, which is a
        completely wrong conclusion drawn from a two-line bug. Errors are
        counted and the first one is printed. A pipeline that hides its own
        failures will eventually convince you of something false.
        """
        plate_bgr = self.deveil(plate_bgr)
        if self.color_mode == "grayscale":
            img = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2GRAY)
        else:
            img = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2RGB)

        try:
            preds = self.ocr.run(img, return_confidence=True)
        except Exception as exc:
            self.ocr_errors += 1
            if not self._warned:
                self._warned = True
                print(f"  ! OCR error (further errors counted silently): "
                      f"{type(exc).__name__}: {exc}")
            return "", 0.0, None

        if not preds:
            return "", 0.0, None
        p = preds[0]
        text = (p.plate or "").strip().upper()
        conf = 0.0
        if p.char_probs is not None and len(p.char_probs):
            probs = np.asarray(p.char_probs, dtype=float).ravel()
            # The per-character MINIMUM, not the mean. A plate is only as good
            # as its worst character: "ABC1234" with one character at 0.2 is a
            # wrong plate, and averaging hides that behind six confident ones.
            conf = float(probs.min())
        return text, conf, getattr(p, "region", None)


# --------------------------------------------------------------------------
# Vehicle detection + tracking
# --------------------------------------------------------------------------

def _iou(a: tuple, b: tuple) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / ua if ua > 0 else 0.0


class _OneEuro:
    """1-Euro filter (Casiez et al. 2012): near-zero jitter when the signal is
    still, near-zero lag when it moves fast. The right tool for a box that should
    be snappy AND smooth, not jumpy."""
    def __init__(self, freq=15.0, mincutoff=1.2, beta=0.35, dcutoff=1.0):
        self.freq, self.mincutoff, self.beta, self.dcutoff = freq, mincutoff, beta, dcutoff
        self.x_prev = None
        self.dx_prev = 0.0

    def _alpha(self, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        te = 1.0 / self.freq
        return 1.0 / (1.0 + tau / te)

    def __call__(self, x):
        if self.x_prev is None:
            self.x_prev = x
            return x
        dx = (x - self.x_prev) * self.freq
        ad = self._alpha(self.dcutoff)
        dx_hat = ad * dx + (1 - ad) * self.dx_prev
        cutoff = self.mincutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev = x_hat, dx_hat
        return x_hat


class _BoxSmoother:
    """Smooth a box by its centre + size (auto-aims centre-of-mass, keeps shape)."""
    def __init__(self):
        self.f = [_OneEuro() for _ in range(4)]

    def __call__(self, box):
        x0, y0, x1, y1 = box
        cx, cy = self.f[0]((x0 + x1) / 2.0), self.f[1]((y0 + y1) / 2.0)
        w, h = self.f[2](x1 - x0), self.f[3](y1 - y0)
        return (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))


class VehicleTracker:
    """ONNX vehicle detection plus a motion-predicted IoU tracker.

    Deliberately no torch. The obvious choice here is ultralytics YOLO with
    ByteTrack, and it was the first implementation, but it drags in torch and
    torchvision - which is 2.5 GB and, on this machine, a torchvision build
    that does not match its torch and cannot even import.

    Dropping to onnxruntime is not a workaround for that, it is the better
    answer for the product. The thing SparrowMap actually ships is a node that a
    volunteer runs on a Raspberry Pi or an old Android in a window. onnxruntime
    installs there in seconds; torch does not install there happily at all. So
    the whole detection path is ONNX end to end, and the same code runs on the
    workstation and on a $60 board.

    The tracker traded for that is simple, and simple is sufficient here: a
    fixed camera watching a road produces near-linear motion with no occlusion
    crossings to speak of. Boxes are predicted forward by their last velocity
    and matched greedily by IoU. Where this WOULD fall down is a crowded
    intersection with vehicles passing behind each other; if that shows up in
    the footage, that is the moment to bring back a real ByteTrack, in ONNX.
    """

    def __init__(self, model: str = "rf-detr-small-512-coco",
                 conf: float = 0.40,
                 iou_thresh: float = 0.20, max_age: int = TRACK_TIMEOUT_FRAMES) -> None:
        from open_image_models import create_detector
        import onnxruntime as ort

        avail = ort.get_available_providers()
        self.providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                          if p in avail]
        self.det = create_detector(model, conf_thresh=conf, providers=self.providers)
        self.iou_thresh = iou_thresh
        self.max_age = max_age
        self._tracks: dict[int, dict] = {}
        self._next_id = 1

    def track(self, frame: np.ndarray) -> list[tuple[int, str, tuple, float]]:
        """Return [(track_id, class_name, (x0,y0,x1,y1), conf), ...]."""
        try:
            dets = self.det.predict(frame)
        except Exception:
            dets = []

        obs = []
        for d in dets:
            if d.label not in VEHICLE_CLASSES.values():
                continue
            b = d.bounding_box
            obs.append(((int(b.x1), int(b.y1), int(b.x2), int(b.y2)),
                        d.label, float(d.confidence)))

        # Predict each live track forward by its last velocity.
        preds = {}
        for tid, t in self._tracks.items():
            x0, y0, x1, y1 = t["box"]
            vx, vy = t["vel"]
            preds[tid] = (x0 + vx, y0 + vy, x1 + vx, y1 + vy)

        # Greedy IoU matching, strongest pair first.
        pairs = sorted(
            ((_iou(preds[tid], box), tid, i)
             for tid in preds for i, (box, _l, _c) in enumerate(obs)),
            reverse=True)
        used_t, used_o, out = set(), set(), []

        for score, tid, i in pairs:
            if score < self.iou_thresh or tid in used_t or i in used_o:
                continue
            used_t.add(tid); used_o.add(i)
            box, label, conf = obs[i]
            t = self._tracks[tid]
            ox0, oy0, ox1, oy1 = t["box"]
            t["vel"] = ((box[0] - ox0 + box[2] - ox1) / 2,
                        (box[1] - oy0 + box[3] - oy1) / 2)
            t["box"], t["label"], t["age"] = box, label, 0
            # Track on the RAW box (velocity/IoU); emit a 1-Euro SMOOTHED box so
            # the overlay and crop lock onto centre-mass without jitter.
            out.append((tid, label, t["smoother"](box), conf))

        for i, (box, label, conf) in enumerate(obs):
            if i in used_o:
                continue
            tid = self._next_id; self._next_id += 1
            sm = _BoxSmoother()
            self._tracks[tid] = {"box": box, "label": label, "vel": (0.0, 0.0),
                                 "age": 0, "smoother": sm}
            out.append((tid, label, sm(box), conf))

        for tid in list(self._tracks):
            if tid not in used_t and tid not in {o[0] for o in out}:
                self._tracks[tid]["age"] += 1
                if self._tracks[tid]["age"] > self.max_age:
                    del self._tracks[tid]
        return out


# --------------------------------------------------------------------------
# Running a whole video
# --------------------------------------------------------------------------

def run_video(path: str,
              plate_every: int = 2,
              max_frames: int = 0,
              on_pass=None,
              progress=None) -> list[VehiclePass]:
    """Process a video and return one VehiclePass per vehicle that crossed it.

    ``plate_every`` runs plate detection on every Nth frame. Vehicle tracking
    still runs on every frame, because the track is what stitches the pass
    together; only the expensive plate stage is decimated.
    """
    tracker = VehicleTracker()
    reader = PlateReader()

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {path}")

    passes: dict[int, VehiclePass] = {}
    done: list[VehiclePass] = []
    last_seen: dict[int, int] = {}
    idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if max_frames and idx > max_frames:
            break

        alive = set()
        for tid, cls_name, vbox, vconf in tracker.track(frame):
            alive.add(tid)
            last_seen[tid] = idx
            vp = passes.get(tid)
            if vp is None:
                vp = passes[tid] = VehiclePass(track_id=tid, cls_name=cls_name,
                                               first_frame=idx)
            vp.last_frame = idx
            vp.frames_seen += 1
            vp.boxes.append(vbox)

            if idx % plate_every:
                continue

            for pr in reader.read(frame, vbox):
                pr.frame_idx = idx
                vp.reads.append(pr)
                # Keep the frame where the plate was biggest and clearest.
                # That is the one worth storing, and the one to redact.
                score = pr.conf * (pr.area ** 0.5)
                if score > vp.best_score:
                    vp.best_score = score
                    vp.best_frame = frame.copy()
                    vp.best_vehicle_box = vbox
                    vp.best_plate_box = pr.box

        # Retire tracks that have gone quiet: the pass is over, emit it.
        for tid in [t for t, f in last_seen.items()
                    if idx - f > TRACK_TIMEOUT_FRAMES and t in passes]:
            vp = passes.pop(tid)
            if vp.frames_seen >= MIN_TRACK_FRAMES:
                done.append(vp)
                if on_pass:
                    on_pass(vp)

        if progress and idx % 25 == 0:
            progress(idx, len(passes), len(done))

    cap.release()
    for vp in passes.values():
        if vp.frames_seen >= MIN_TRACK_FRAMES:
            done.append(vp)
            if on_pass:
                on_pass(vp)
    return done
