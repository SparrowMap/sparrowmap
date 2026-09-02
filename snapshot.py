"""SparrowMap - snapshot handling.

THE RULE HERE: SparrowMap stores a crop of the vehicle, not the frame it came
from. That single choice does most of the privacy work that face blurring is
usually asked to do, and it does it without needing a face detector to be
correct. The pedestrian on the sidewalk, the number on the house behind, the
kid in the yard - none of them are in a tight crop of a car's rear end.

Full-frame storage is possible but must be switched on deliberately, and when
it is on, face blurring becomes mandatory rather than optional.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from core import CONFIG, SNAPS

# Crop is expanded by this fraction beyond the detected vehicle box, so the
# vehicle is not clipped. Small on purpose.
# 🚨 ASYMMETRIC, AND THE TOP ONE IS THE ONE THAT MATTERS.
# This is the SERVER-side crop, applied to the whole frame a camera node sends
# (store_submitted) and to the full-resolution evidence copy. A roof light bar
# sits just outside the detector's box, so a symmetric 12% clipped the single
# most diagnostic feature on a marked vehicle - reported from the road, where a
# patrol car was detected head-on and still classified as ordinary because the
# bar was not in the picture.
# Nothing diagnostic hangs off the bottom of a car, and widening the sides only
# brings in pavement and other people's vehicles - a privacy cost as well as a
# wasted one. CROP_PAD stays as the side value so existing callers read the
# same; the vertical pair is explicit.
CROP_PAD = 0.10
CROP_PAD_TOP = 0.28
CROP_PAD_BOTTOM = 0.08
MAX_EDGE = 900          # stored snapshots are downscaled to this longest edge
JPEG_QUALITY = 82


def _font(size: int):
    for name in ("consola.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


#: The most of a photograph the caption strip may ever cover, and the most the
#: watermark may. The picture is the evidence; the stamp is the label on it.
_CAPTION_MAX_FRAC = 0.24
_WATERMARK_MAX_FRAC = 0.13
#: …and the most of the WIDTH the watermark may span. Height alone did not
#: stop "CONFIRMED" being drawn clean across the roof of the car.
_WATERMARK_MAX_WIDTH = 0.55
#: Below this the glyphs are mush - drawing them costs pixels and returns
#: nothing legible, so a line that cannot reach this size is dropped instead.
_MIN_READABLE_PX = 7


def _fit_line(d, text: str, font, avail: int) -> str:
    """Shorten `text` with an ellipsis until it fits `avail` pixels."""
    if d.textlength(text, font=font) <= avail:
        return text
    while text and d.textlength(text + "…", font=font) > avail:
        text = text[:-1]
    return (text + "…") if text else ""


def _stamp(img: Image.Image, lines: list[str], watermark: str = "") -> Image.Image:
    """Burn provenance into the image itself.

    A snapshot that travels without its origin is a rumour. Time, camera and
    the fact that the picture came from an automated system are part of the
    evidence, so they are drawn on the pixels rather than left in a sidecar
    that gets stripped the first time somebody screenshots it.

    🚨 THE STAMP GETS A BUDGET AS A FRACTION OF THE PICTURE, AND THE TEXT FITS
    THE BUDGET OR LINES ARE DROPPED. Never the other way around.

    This used to size the fonts as `max(11, width // 55)` and `max(14, width //
    22)`. The ratios were tuned on a full frame - at 1100px wide that is a 20px
    caption, which is right. But the images this actually runs on are capped at
    SUBRES_MAX_EDGE (200px), so `width // 55` is 3 and the FLOOR always won. A
    floor is an absolute number of pixels on an image that cannot grow, so it
    did not make the text legible, it made the text DOMINATE: on a real 176x109
    published crop the caption strip came to 40px and the watermark another 14,
    which is HALF THE PHOTOGRAPH covered in writing - and the caption was still
    too wide for the image, so it rendered as "Bridge St, DIR SOUTH | Sparro".
    Reported as "details are covering too much of the police car", which is
    exactly what it was: the vehicle is the evidence and the label had eaten it.

    So the budget is proportional, lines that will not fit the height are
    dropped from the END (the timestamp matters most, the plate line least),
    and every line is ellipsised to the width rather than overflowing.
    """
    d = ImageDraw.Draw(img, "RGBA")
    pad = max(2, img.width // 60)
    avail = img.width - pad * 2

    # Caption: the largest font whose strip stays inside the budget, then drop
    # lines that still will not fit. A one-line legible stamp beats a two-line
    # unreadable one.
    budget = int(img.height * _CAPTION_MAX_FRAC)
    keep, f = list(lines), None
    while keep:
        # width // 55 is the ORIGINAL ratio and it was right - a 1100px frame
        # gets a 20px caption. Only the floor was wrong. The budget clamp is
        # what small images need; the ratio is what big ones need.
        size = max(_MIN_READABLE_PX,
                   min(img.width // 55, (budget - pad) // len(keep) - 2))
        f = _font(size)
        if (f.size + 2) * len(keep) + pad <= budget or len(keep) == 1:
            break
        keep = keep[:-1]                  # drop the least important line
    if keep and f:
        h = (f.size + 2) * len(keep) + pad
        d.rectangle([0, img.height - h, img.width, img.height],
                    fill=(0, 0, 0, 165))
        y = img.height - h + pad // 2
        for ln in keep:
            d.text((pad, y), _fit_line(d, ln, f, avail), font=f,
                   fill=(235, 235, 235, 255))
            y += f.size + 2

    if watermark:
        # 🚨 BOUNDED BY WIDTH TOO, NOT JUST HEIGHT. "CONFIRMED" at 14px on a
        # 176px-wide crop is only 13% of the height but over 40% of the WIDTH,
        # and it is drawn straight across the roofline - which is what actually
        # read as covering the car. Shrink until it sits inside its share of
        # the width; ellipsising a one-word watermark ("CONFIRM…") would be
        # worse than a smaller one that still says what it says.
        size = max(_MIN_READABLE_PX,
                   min(img.width // 22, int(img.height * _WATERMARK_MAX_FRAC)))
        wf = _font(size)
        while size > _MIN_READABLE_PX and \
                d.textlength(watermark, font=wf) > avail * _WATERMARK_MAX_WIDTH:
            size -= 1
            wf = _font(size)
        d.text((pad, pad), _fit_line(d, watermark, wf, avail), font=wf,
               fill=(255, 90, 90, 210))
    return img


def redact_plate(img: Image.Image, plate_box: tuple) -> Image.Image:
    """Destroy the plate characters in an image. Pixelate, then bar.

    THIS IS THE FIX FOR THE MOST OBVIOUS HOLE IN THE WHOLE DESIGN. Hashing a
    plate in the database accomplishes nothing if the snapshot stored beside it
    is a photograph of that plate at 24 point. Every plate-reader system takes a
    picture of the plate - that is the entire point of the picture - so a
    private tier that keeps the image keeps the plate.

    We already know exactly where the characters are, because the detector had
    to localise them in order to read them. So we paint over them.

    Pixelation ALONE is not enough: a plate has a tiny character set and a fixed
    layout, which makes pixelated text recoverable by brute force (render every
    candidate, downsample, compare). So the pixelation is followed by an opaque
    bar. The pixelation is what survives if a future edit removes the bar; the
    bar is what actually does the work today.
    """
    x0, y0, x1, y1 = (int(v) for v in plate_box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return img

    region = img.crop((x0, y0, x1, y1))
    small = region.resize((max(1, region.width // 14), max(1, region.height // 8)),
                          Image.BILINEAR)
    img.paste(small.resize(region.size, Image.NEAREST), (x0, y0))

    d = ImageDraw.Draw(img)
    d.rectangle([x0, y0, x1, y1], fill=(24, 26, 30))

    # Label the bar so a person looking at the image knows the plate was
    # deliberately destroyed rather than merely unreadable. Shrink to fit;
    # a plate box can be small at distance.
    msg = "PLATE NOT STORED"
    bw, bh = x1 - x0, y1 - y0
    for size in range(max(6, bh // 2), 5, -1):
        f = _font(size)
        if d.textlength(msg, font=f) <= bw - 4:
            d.text((x0 + (bw - d.textlength(msg, font=f)) / 2,
                    y0 + (bh - size) / 2), msg, font=f, fill=(122, 132, 148))
            break
    return img


def store_crop(frame: Image.Image, bbox: Optional[tuple], meta: dict,
               plate_box: Optional[tuple] = None,
               plate_boxes: Optional[list] = None,
               stamp: bool = True,
               isolate: bool = True) -> tuple[str, str]:
    """Crop to the vehicle, redact if private, stamp it, write it.

    ``bbox`` is the vehicle box (x0, y0, x1, y1) in frame pixels.
    ``plate_box`` is the plate box in the SAME frame coordinates. It is
    required whenever the sighting is private tier; passing None there raises,
    because silently storing a readable plate is the failure this whole module
    exists to prevent.

    🚨 ``isolate=False`` FOR AN IMAGE WHOSE BACKGROUND IS ALREADY GONE.
    Same shape as ``stamp=False`` below and reachable on the same path. The
    strip is not idempotent: it keeps a rectangle and destroys the rest, so
    running it on its own output keeps a rectangle OF a rectangle. A publicly
    flagged photo goes back into the review pen via ``subres_from_stored``,
    which takes the STORED file - already masked when it was first stored - and
    confirming it comes back through here. Every trip round the report-confirm
    loop ate another slice of the photograph, and because the loop ends in a
    picture that still looks like a picture, nothing said so.

    🚨 ``stamp=False`` FOR AN IMAGE THAT IS ALREADY STAMPED.
    The caption and watermark are drawn at a position and size derived from the
    image, so stamping the same picture twice lands the second copy almost
    exactly on the first: the strip goes darker, the glyphs go bolder, and it
    reads as one stamp rather than two. Invisible, and it happens on a real
    path - a publicly flagged photo goes into the review pen via
    subres_from_stored, which takes the STORED (already captioned) file, and
    confirming it calls back through here. Every trip round that loop burns
    another layer of text into the evidence.
    """
    img = frame.convert("RGB")

    # Redact BEFORE cropping, while the boxes are still in frame coordinates.
    if meta.get("tier") != "public":
        # EVERY candidate box, not just the detector's favourite. It picks by
        # area, and a marked vehicle's door livery is a larger patch of white
        # text than its plate - so the "best" box covered the livery and left
        # the plate readable. A plate surviving is the one failure this module
        # exists to prevent; an over-painted livery is cosmetic.
        boxes = list(plate_boxes or [])
        if plate_box and tuple(plate_box) not in {tuple(b) for b in boxes}:
            boxes.append(plate_box)
        for _b in boxes:
            img = redact_plate(img, tuple(_b))
        if not boxes and meta.get("plate_text"):
            raise ValueError(
                "private-tier snapshot with a known plate but no plate_box to "
                "redact. Refusing to store a readable plate.")

    if bbox:
        x0, y0, x1, y1 = bbox
        pw = (x1 - x0) * CROP_PAD
        box = (max(0, int(x0 - pw)),
               max(0, int(y0 - (y1 - y0) * CROP_PAD_TOP)),
               min(img.width, int(x1 + pw)),
               min(img.height, int(y1 + (y1 - y0) * CROP_PAD_BOTTOM)))
        img = img.crop(box)

        # 🚨 REMOVE THE SURROUNDINGS. See isolate.py for why this matters more
        # than it looks: the node's coordinates are jittered so nobody learns
        # which house is watching, and a photograph of the neighbours' porch
        # hands that straight back. the operator caught it, and he is currently the
        # only person exposed by it.
        #
        # Done AFTER the crop: segmentation runs on a much smaller image, and
        # the vehicle of interest is by construction the one in the middle.
        if isolate and CONFIG.get("strip_snapshot_background", True):
            # 🚨 COMPUTED BEFORE THE TRY, BECAUSE THE FALLBACK NEEDS IT.
            # Only the instance in the middle of the crop - a street scene holds
            # several vehicles and this snapshot is about one of them. It used
            # to be worked out after `import cv2`, which is the one line that
            # fails; the no-cv2 path below would then have raised NameError on
            # `centre` instead of masking anything, turning a fix into the same
            # silent failure it was written to remove.
            w2, h2 = img.width, img.height
            centre = (w2 * 0.18, h2 * 0.18, w2 * 0.82, h2 * 0.82)
            # 🚨 THE GEOMETRIC FALLBACK NEEDS ITS OWN BOX, AND `centre` WAS THE
            # WRONG ONE. It is an INSTANCE-SELECTION box - "which of the several
            # vehicles in this street scene is the subject" - and 0.18 is right
            # for that. Using the same rectangle to decide what SURVIVES made
            # every published photograph the middle 64% of itself with a flat
            # border painted round it.
            #
            # Measured on the live map 2026-09-02: 18 of 18 published police
            # snaps carried a ~16% backdrop border on all four sides and were
            # ~58% backdrop, while the pen crop the reviewer approved was 1.7%.
            # That is his report - "after review and they post they are cropped
            # tighter and sometimes makes it hard to tell it was even a cop
            # sighting" - and it is not a matter of taste:
            #
            # 🚨 THE BLANKED TOP BAND IS WHERE THE LIGHT BAR LIVES. CROP_PAD_TOP
            # is 0.28 and asymmetric for exactly one reason (see the constant):
            # a roof light bar sits OUTSIDE the detector's box, so the crop
            # reaches above it to keep the single most diagnostic feature on a
            # marked vehicle. A blind 18% top inset covers 0.245/0.28 = 87% of
            # that pad, so the fallback was deleting the evidence the pad was
            # widened to capture. Two rules in this file, pulling opposite ways,
            # and the later one silently won.
            #
            # So the fallback box is DERIVED FROM THE PADS instead of guessed:
            # trim the side and bottom pads (kerb, pavement, the neighbours'
            # parked cars - what isolate.py is actually written to remove) and
            # keep the top pad whole. Keeps 78% of the picture instead of 64%,
            # and keeps the roofline.
            #
            # ⚠️ IF THE PAD CONSTANTS CHANGE, THIS FOLLOWS THEM. That is the
            # point of deriving it - the same coupling that made 0.18 wrong.
            fx = CROP_PAD / (1 + 2 * CROP_PAD)
            fy1 = 1 - CROP_PAD_BOTTOM / (1 + CROP_PAD_TOP + CROP_PAD_BOTTOM)
            frame = (w2 * fx, 0.0, w2 * (1 - fx), h2 * fy1)
            try:
                import cv2
                import numpy as np

                import isolate
                arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                arr, method = isolate.strip(arr, centre, fallback=frame)
                img = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
                meta["_isolation"] = method
                if method != "segment":
                    # Never let a downgrade pass unremarked. A partially-masked
                    # image still carries background inside the box, and a
                    # function that quietly weakens its own privacy guarantee
                    # is the failure this whole feature exists to correct.
                    print(f"[snapshot] background only partly removed "
                          f"(method={method})")
            except ImportError as exc:
                # 🚨 THIS USED TO RAISE, AND ON THE ONE MACHINE THAT SERVES THE
                # PUBLIC MAP IT RAISED EVERY SINGLE TIME.
                #
                # The mirror has Pillow and no OpenCV on purpose - the detector
                # runs at home, the box carries claims (DEPLOY.md). So `import
                # cv2` failed here for every stored image, this turned it into a
                # ValueError, and `/api/node/confirm` caught it best-effort and
                # created the sighting with `snap=None`: on the map, confirmed by
                # a human, with no photograph behind it. The operator was told
                # `{"ok": true}`. Measured on the box, not inferred - a real
                # 512x229 banked crop through `store_subresolution` raises
                # `No module named 'cv2'`, and 16,597 sightings carry 76 images,
                # every one of them published from home.
                #
                # Refusing was written to stop an UNMASKED image reaching the
                # public. It is still not allowed to. But there is a third
                # option between "segment it" and "publish nothing", and this
                # file already accepts it: `isolate.strip` falls back to method
                # "box" - blank everything outside the vehicle's box - whenever
                # segmentation fails. That fallback is pure geometry and needs
                # no OpenCV at all, so do it in Pillow instead of giving up.
                #
                # ⚠️ It is a DOWNGRADE, and it goes down the same loud path as
                # any other downgrade below: stamped into `_isolation`, printed,
                # and never assumed. A "none" result is still a refusal, because
                # an image nothing was done to must not be stored.
                import isolate_pil
                img, method = isolate_pil.strip_box(img, frame)
                meta["_isolation"] = method
                if method == "none":
                    raise ValueError(
                        f"cannot remove snapshot background ({exc}) and the "
                        f"geometry fallback could not run either; refusing to "
                        f"store an image that would show the camera's "
                        f"surroundings. Set strip_snapshot_background=false to "
                        f"publish anyway."
                    ) from exc
                print(f"[snapshot] no cv2 ({exc}); background removed by "
                      f"geometry only (method={method}) - weaker than "
                      f"segmentation, and it shows")
    elif CONFIG.get("crop_only", True):
        raise ValueError(
            "store_crop called with no vehicle box while crop_only is on. "
            "Refusing to store a full frame; that is how bystanders end up in "
            "a public database.")
    elif CONFIG.get("blur_faces", True):
        img = blur_faces(img)

    if max(img.size) > MAX_EDGE:
        s = MAX_EDGE / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)

    ts = meta.get("ts", time.time())

    # 🚨 THE CAPTION IS GONE, AND IT WAS REMOVED FOR BEING WRONG.
    #
    # Every stored crop carried a burned-in strip: timestamp, camera name,
    # sometimes the plate, plus a CONFIRMED/UNVERIFIED/CONTRIBUTED watermark.
    # HIS REPORT: "8 hours off and a day in the future".
    #
    # He was right, and the cause is one call. The line was built with
    # time.localtime(), which is the SERVER's timezone - and the server is in
    # Helsinki while the cameras are in Michigan. So every photograph of a
    # Michigan street was stamped with Finnish local time, seven or eight hours
    # ahead, which after his early evening is also the next day. A false fact
    # burned into the evidence itself, where it cannot be corrected later.
    #
    # 📌 And the caption was redundant even when it was right: the timestamp,
    # the camera and the plate are all printed in plain text beside the image
    # on every surface that shows one. A caption that repeats the page and
    # contradicts it is worse than no caption, because a reader trusts the
    # thing that looks like part of the photograph.
    #
    # ⚠️ Fixing the timezone was the obvious move and is the wrong one. A
    # burned-in caption cannot be corrected once written, cannot be
    # translated, and is the one part of a published record that cannot be
    # audited against the database. Deleting it removes the class of bug.
    #
    # ⚠️ EXCEPT "SIMULATED", WHICH STAYS AND MUST STAY. That watermark is not a
    # label about provenance, it is a safety property: a synthetic image that
    # does not say it is synthetic is a fake photograph of a police vehicle.
    # It carries no timestamp, so it never had this bug.
    watermark = meta.get("watermark", "")
    if stamp and watermark == "SIMULATED":
        img = _stamp(img, [], watermark=watermark)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    name = f"{int(ts)}_{secrets.token_hex(4)}.jpg"
    (SNAPS / name).write_bytes(data)
    return name, sha


MAX_UPLOAD_BYTES = 6 * 1024 * 1024


def decode_bytes(data_url: str) -> bytes:
    """The re-encoded JPEG bytes of a submitted data URL.

    Goes through decode_upload so the EXIF strip is not bypassed - a phone
    photo's EXIF carries the exact GPS fix and the device identity of whoever
    sent it, and the bank is not exempt from that.
    """
    img = decode_upload(data_url)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def decode_upload(data_url: str) -> Image.Image:
    """Turn a submitted data URL into an image, with the size cap enforced."""
    import base64
    head, _, b64 = data_url.partition(",")
    if "image" not in head:
        raise ValueError("not an image data URL")
    raw = base64.b64decode(b64, validate=False)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"image over {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    img = Image.open(io.BytesIO(raw))
    img.load()
    # Re-encode rather than storing what arrived: strips EXIF, which on a phone
    # photo carries the exact GPS fix and the device identity of whoever
    # submitted it. Contributors should not be doxxed by their own evidence.
    return img.convert("RGB")


def store_submitted(data_url: str, meta: dict, vehicle_box: tuple,
                    plate_box: Optional[tuple] = None,
                    plate_boxes: Optional[list] = None) -> str:
    """Store a frame a CAMERA NODE sent, cropped to the vehicle it detected.

    🚨 THIS EXISTS BECAUSE CAMERA NODES WERE STORING WHOLE FRAMES.
    run_live.py posts its best frame, and the ingest path sent every submitted
    image through `store_prepared` - which crops to (0, 0, w, h), i.e. does not
    crop. So each sighting from his window stored a 900x506 view of the street:
    the neighbours' houses, whoever was on their porch, and the plates of OTHER
    parked vehicles, which are not redacted because only the tracked vehicle's
    plate box is passed in.

    Note how it got past the guard. `store_crop` refuses a missing bbox while
    crop_only is on - but a full-frame box is not missing, it is present and
    meaningless, so the check passed and did nothing. A guard that tests for
    presence cannot catch a value that is the wrong shape.

    The node knows exactly which box it detected, so it sends it and we crop
    to it here.
    """
    img = decode_upload(data_url)
    if not vehicle_box:
        raise ValueError("camera submission with no vehicle_box to crop to")
    name, _sha = store_crop(img, tuple(vehicle_box), meta, plate_box=plate_box,
                            plate_boxes=plate_boxes)
    return name


def store_prepared(data_url: str, meta: dict,
                   plate_box: Optional[tuple] = None) -> str:
    """Store an image a HUMAN framed and sent from a phone.

    ⚠️ PHONE SUBMISSIONS ONLY. There is no detector box to crop to because a
    person aimed the camera at the vehicle deliberately - their framing IS the
    crop, and it is the only case where storing the submitted extent is right.
    A camera node has a detector and must use `store_submitted`; routing one
    through here stores the whole street. See that function.
    """
    img = decode_upload(data_url)
    name, _sha = store_crop(img, (0, 0, img.width, img.height), meta,
                            plate_box=plate_box)
    return name


# A plate is roughly a tenth of a vehicle's width. Below this longest edge the
# plate occupies about 20px, which is under what any OCR - or any person -
# recovers characters from. Measured against the project's own reader: it stops
# returning anything at all well above this size.
SUBRES_MAX_EDGE = 200


def store_subresolution(data_url: str, meta: dict, stamp: bool = True) -> str:
    """Store a crop that is too small to carry a readable plate.

    🚨 THE SIZE IS VERIFIED HERE, NOT TRUSTED FROM THE CLIENT.

    This exists for phone nodes. A phone can run a vehicle detector in a
    browser but not a plate detector on top of it, so it cannot mark a plate
    for redaction - and the rule elsewhere in this file is that an image whose
    plate cannot be located is discarded, because a photograph of a car is a
    photograph of its plate.

    A device can still do something better than locating the plate: destroy it.
    Downscaling the crop below plate legibility on the phone means no readable
    plate ever crosses the network at all, which is a stronger guarantee than
    redacting one after it arrives. The training pipeline degrades crops to
    140-420px anyway, so nothing of value is lost.

    The client claiming it downscaled would be worthless. The image is decoded
    and measured, and anything larger is refused outright rather than quietly
    resized - a submission over the limit means the node is not behaving as it
    claims, and silently fixing it would hide that.
    """
    img = decode_upload(data_url)
    if max(img.size) > SUBRES_MAX_EDGE:
        raise ValueError(
            f"sub-resolution submission is {img.width}x{img.height}; "
            f"longest edge must be <= {SUBRES_MAX_EDGE}px")
    name, _sha = store_crop(img, (0, 0, img.width, img.height), meta,
                            stamp=stamp)
    return name


def subresolution_bytes(data_url: str) -> bytes:
    """The bytes of a sub-resolution (plate-illegible) crop, for the relay inbox.

    Same guarantee as store_subresolution and enforced the same way: the image
    is decoded and MEASURED, and anything a plate could be read from is refused
    rather than quietly shrunk. Returns re-encoded JPEG bytes so a mirror can
    park the crop for the home classifier without storing it as a snapshot.
    """
    import io
    img = decode_upload(data_url)
    if max(img.size) > SUBRES_MAX_EDGE:
        raise ValueError(
            f"sub-resolution submission is {img.width}x{img.height}; "
            f"longest edge must be <= {SUBRES_MAX_EDGE}px")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def downscale_to_subres(data_url: str) -> bytes:
    """Shrink a crop to plate-illegibility for the review pen.

    subresolution_bytes REFUSES an oversized crop, which is right for a phone
    node that must downscale on-device. A CAMERA node sends a full-size crop, so
    the mirror shrinks it here instead: the longest edge is brought down to
    SUBRES_MAX_EDGE, which destroys any readable plate before the crop is ever
    parked in the pen or shown to a reviewer. EXIF is stripped by decode_upload.
    """
    import io
    img = decode_upload(data_url)
    if max(img.size) > SUBRES_MAX_EDGE:
        s = SUBRES_MAX_EDGE / max(img.size)
        img = img.resize((max(1, int(img.width * s)),
                          max(1, int(img.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=82)
    return buf.getvalue()


def store_confirmed(data_url: str, meta: dict, stamp: bool = True,
                    isolate: bool = True) -> str:
    """Store the photograph a REVIEWER has just vouched for.

    🚨 THIS EXISTS BECAUSE `_publish` USED `store_subresolution`, WHICH REFUSES
    ANYTHING OVER 200px. That was invisible while the only thing it was ever
    handed was the 200px pen copy. The moment the reviewer's picture became the
    full-resolution original it would have raised on every confirmation, and
    `_publish` catches the failure and writes the raw bytes out itself - so the
    map would have carried published photographs with no caption, no watermark
    and no background strip, and nothing would have said so.

    The input is already a crop of one vehicle, so the whole image IS the
    subject and (0, 0, w, h) is the honest box - unlike a phone submission,
    where that same call once stored the entire street.

    Redaction does not apply: `meta["tier"]` is "public" here by definition,
    which is the one case the plate is meant to survive. Nothing reaches this
    function without a human having said so.
    """
    img = decode_upload(data_url)
    name, _sha = store_crop(img, (0, 0, img.width, img.height), meta,
                            stamp=stamp, isolate=isolate)
    return name


def crop_full(data_url: str, vehicle_box: tuple) -> bytes:
    """Crop a camera node's frame to its vehicle and keep the resolution.

    The sibling of `crop_to_subres`, and deliberately the same crop: the frame
    is reduced to the vehicle box plus CROP_PAD, so the neighbours' houses and
    everyone on the pavement are gone exactly as they are for the pen copy. The
    ONLY difference is that this one does not then shrink the result to 200px.

    That 200px is not a privacy measure aimed at the vehicle in the picture - it
    is what destroys a PLATE. On a government candidate the plate is the thing
    the public tier is for, and the livery is what the reviewer judges by, so
    applying it here destroyed the evidence and the record in one step.

    ⚠️ THE OUTPUT IS UNREDACTED AND FULL SIZE. It belongs in core.EVIDENCE and
    nowhere else - never SNAPS, never a row's `snap`. See core.EVIDENCE for the
    rails and for the cost of holding it at all.
    """
    import io
    img = decode_upload(data_url)
    x0, y0, x1, y1 = vehicle_box
    pw = (x1 - x0) * CROP_PAD
    box = (max(0, int(x0 - pw)),
           max(0, int(y0 - (y1 - y0) * CROP_PAD_TOP)),
           min(img.width, int(x1 + pw)),
           min(img.height, int(y1 + (y1 - y0) * CROP_PAD_BOTTOM)))
    img = img.crop(box)
    # Same ceiling every stored snapshot gets. Not a degradation of this one in
    # particular: a published public-tier photograph is already capped here, so
    # anything above it could never survive publication anyway.
    if max(img.size) > MAX_EDGE:
        s = MAX_EDGE / max(img.size)
        img = img.resize((max(1, int(img.width * s)),
                          max(1, int(img.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def subres_from_stored(raw: bytes) -> bytes:
    """Shrink an ALREADY-STORED snapshot to review-pen resolution.

    For putting a published sighting back in front of a human - a public flag,
    say. The stored file is a public-tier photograph and may carry a legible
    government plate; the pen is sub-resolution by contract, so it is brought
    down to SUBRES_MAX_EDGE like everything else parked there rather than
    copied across at full size.
    """
    img = Image.open(io.BytesIO(raw))
    if max(img.size) > SUBRES_MAX_EDGE:
        s = SUBRES_MAX_EDGE / max(img.size)
        img = img.resize((max(1, int(img.width * s)),
                          max(1, int(img.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=82)
    return buf.getvalue()


def crop_to_subres(data_url: str, vehicle_box: tuple) -> bytes:
    """Crop a camera node's FRAME to its vehicle, then shrink it for the pen.

    🚨 THE PEN WAS HOLDING WHOLE STREETS.
    `downscale_to_subres` says "a CAMERA node sends a full-size crop" and only
    resizes. That belief is wrong, and `store_submitted` says so directly two
    hundred lines up: run_live.py posts its best FRAME, and the server crops it
    to the vehicle box. So the published snapshot was correctly cropped while
    the review-pen copy of the same sighting was the entire frame, merely made
    small - the neighbours' houses, their porches, other vehicles - shown to
    every reviewer holding a token.

    Two functions held contradictory beliefs about one field, and the pen
    believed the wrong one. Same failure as the 900x506 bug that created
    store_submitted, one surface later: fixing a leak on the path you are
    looking at does not fix the second path that reads the same input.

    Cropping first also makes the downscale do its real job: 200px across a
    whole street leaves a vehicle a few pixels wide, while 200px across the
    vehicle is a picture a human can actually judge - so this improves the
    review it protects.
    """
    import io
    img = decode_upload(data_url)
    x0, y0, x1, y1 = vehicle_box
    # Same asymmetric crop as everywhere else: the reviewer needs to see the
    # roof for the same reason the classifier does.
    pw = (x1 - x0) * CROP_PAD
    img = img.crop((max(0, int(x0 - pw)),
                    max(0, int(y0 - (y1 - y0) * CROP_PAD_TOP)),
                    min(img.width, int(x1 + pw)),
                    min(img.height, int(y1 + (y1 - y0) * CROP_PAD_BOTTOM))))
    if max(img.size) > SUBRES_MAX_EDGE:
        s = SUBRES_MAX_EDGE / max(img.size)
        img = img.resize((max(1, int(img.width * s)),
                          max(1, int(img.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=82)
    return buf.getvalue()


def blur_faces(img: Image.Image) -> Image.Image:
    """Blur faces in a full frame.

    Only reachable when crop-only mode has been switched off. Requires OpenCV;
    if it is missing we refuse rather than silently storing unblurred faces,
    because a privacy control that quietly no-ops is worse than not claiming
    to have one.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "blur_faces needs OpenCV (pip install opencv-python). Refusing to "
            "store a full frame with faces intact.") from exc

    from PIL import ImageFilter
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    for (x, y, w, h) in cascade.detectMultiScale(gray, 1.15, 5, minSize=(24, 24)):
        region = img.crop((x, y, x + w, y + h)).filter(
            ImageFilter.GaussianBlur(radius=max(w, h) / 6.0))
        img.paste(region, (x, y))
    return img


# --------------------------------------------------------------------------
# Simulated frames, for building and demoing with no hardware attached.
# --------------------------------------------------------------------------

def render_simulated(veh: dict, node_name: str, ts: float) -> tuple:
    """Draw a plausible camera frame for a simulated vehicle.

    Returns ``(image, vehicle_box, plate_box)``. The two boxes stand in for
    what a real detector hands back, so the simulator exercises the same
    redaction path that live cameras will.

    Deliberately stylised and watermarked SIMULATED. A synthetic image that
    could be mistaken for a real capture is a liability, so this one cannot be.
    """
    W, H = 640, 400
    img = Image.new("RGB", (W, H), (26, 30, 38))
    d = ImageDraw.Draw(img)

    for i in range(H // 2):                       # sky
        t = i / (H / 2)
        d.line([(0, i), (W, i)], fill=(int(26 + 22 * t), int(30 + 26 * t), int(38 + 34 * t)))
    d.rectangle([0, H // 2, W, H], fill=(38, 38, 42))   # road
    for x in range(0, W, 70):
        d.line([(x, H - 40), (x + 34, H - 40)], fill=(120, 118, 100), width=4)

    col = {"white": (225, 227, 230), "black": (32, 33, 36), "silver": (176, 180, 186),
           "blue": (48, 86, 160), "red": (162, 48, 48), "grey": (110, 113, 120),
           "green": (56, 120, 84)}.get(veh.get("color", "silver"), (176, 180, 186))

    bx0, by0, bx1, by1 = 150, 150, 490, 320
    d.rounded_rectangle([bx0, by0 + 34, bx1, by1], 14, fill=col)          # body
    d.rounded_rectangle([bx0 + 46, by0, bx1 - 46, by0 + 66], 10, fill=col)  # cabin
    d.rounded_rectangle([bx0 + 58, by0 + 10, bx1 - 58, by0 + 58], 8,
                        fill=(18, 22, 30))                                # glass
    for wx in (bx0 + 46, bx1 - 92):                                       # wheels
        d.ellipse([wx, by1 - 30, wx + 46, by1 + 16], fill=(22, 22, 24))
        d.ellipse([wx + 12, by1 - 18, wx + 34, by1 + 4],
                  fill=(190, 190, 195) if veh.get("steelies") else (70, 70, 74))

    if veh.get("light_bar"):
        d.rounded_rectangle([bx0 + 96, by0 - 18, bx1 - 96, by0 - 2], 5, fill=(28, 28, 30))
        d.rectangle([bx0 + 100, by0 - 15, bx0 + 168, by0 - 5], fill=(60, 90, 255))
        d.rectangle([bx1 - 168, by0 - 15, bx1 - 100, by0 - 5], fill=(255, 60, 60))
    if veh.get("pillar_spotlight"):
        d.ellipse([bx0 + 34, by0 + 4, bx0 + 58, by0 + 28], fill=(210, 212, 216))
    if veh.get("push_bumper"):
        d.rectangle([bx1 - 6, by0 + 60, bx1 + 16, by1 - 10], fill=(46, 48, 52))
    if veh.get("livery"):
        d.rectangle([bx0 + 10, by0 + 70, bx1 - 10, by0 + 116], fill=(245, 246, 248))
    if veh.get("agency_decal"):
        d.text((bx0 + 26, by0 + 82), veh.get("agency", "POLICE"),
               font=_font(26), fill=(20, 40, 110))

    px0, py0 = (bx0 + bx1) // 2 - 62, by1 - 44                           # plate
    d.rounded_rectangle([px0, py0, px0 + 124, py0 + 40], 5,
                        fill=(248, 249, 250), outline=(30, 30, 30), width=2)
    txt = veh.get("plate", "")
    f = _font(24)
    tw = d.textlength(txt, font=f)
    d.text((px0 + (124 - tw) / 2, py0 + 8), txt, font=f, fill=(24, 40, 96))

    vehicle_box = (bx0 - 10, by0 - 22, bx1 + 20, by1 + 20)
    plate_box = (px0, py0, px0 + 124, py0 + 40)
    return img, vehicle_box, plate_box
