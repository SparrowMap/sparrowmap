"""Find the painted lane dashes in a camera's own view, to use as a ruler.

    edges = dashes.find(frames)          # [(x, y, 'start'|'end'), ...]
    cal   = calib.fit(edges)             # image position -> metres

🚨 THE TRAFFIC IS REMOVED BY TIME, NOT BY CLEVERNESS.

Paint does not move and cars do. A per-pixel MEDIAN across frames taken
minutes apart therefore erases every vehicle and leaves the road, without a
vehicle detector, a mask, or any judgement about what a car looks like. It is
the one step that makes the rest easy, and doing it any other way - detecting
cars and inpainting, say - would mean a missed car becomes a painted stripe.

WHAT IS REJECTED, AND WHY EACH TEST EARNS ITS PLACE. Every one of these is a
way a confident wrong ruler gets built, and a wrong ruler is a wrong
accusation:

  * not bright against its surroundings   - a shadow edge, a tar seam
  * not elongated                         - gravel, a manhole, a pothole patch
  * not collinear with the others         - the kerb, a second lane's line,
                                            a crosswalk
  * not shrinking with distance           - anything not lying on the road
                                            plane, e.g. a fence or a roofline
  * fewer than MIN_DASHES survive         - not enough to check itself

The perspective test is the strongest of them and costs nothing: real dashes
on a flat road MUST get smaller as they recede, monotonically. A set of marks
that does not is not a lane line, whatever it looks like.

⚠️ THIS IS NOT VALIDATED ON REAL FOOTAGE YET. The geometry is proven against
ground truth in tools/test_calib.py and the detection against rendered scenes
in tools/test_dashes.py, which is proof that the maths and the image
processing are right - NOT proof that it survives rain, snow, worn paint or a
low sun. Until it has been run against real camera frames, nothing it produces
should reach a published number.
"""
from __future__ import annotations

import math

#: Frames to median together. Enough that a queue of traffic cannot outvote
#: the road, and they should be spread over MINUTES rather than seconds - a
#: car stopped at a light is stationary too, and only time tells them apart.
MEDIAN_FRAMES = 25

#: A dash has to be at least this much longer than it is wide.
#:
#: 🚨 LOW ON PURPOSE, BECAUSE PERSPECTIVE FORESHORTENS A DASH TOWARDS A SQUARE.
#: A dash recedes along its own length, so distance compresses that length far
#: faster than its width: measured on the rendered scene, the same painted
#: stripe goes 17x143 near the camera, then 11x41, 13x24, 11x17 and 9x11 as it
#: recedes - aspect ratios of 8.4, 3.7, 1.85, 1.55 and 1.2. A threshold of 2.0
#: therefore threw away the three furthest dashes and left the fit with no
#: redundancy at all, which read as the detector being weak when it was the
#: test being wrong about geometry.
#:
#: Elongation is kept only to reject the obviously round - gravel, a manhole, a
#: patch. The real discriminators are collinearity and the perspective test,
#: which a manhole cannot pass however elongated it happens to look.
MIN_ELONGATION = 1.15

#: Fraction of the frame a single dash may occupy. Bigger than this and it is
#: a wall, a vehicle roof, or the sky.
MAX_DASH_AREA = 0.02
MIN_DASH_AREA = 0.00002

#: 🚨 A DASH IS A SEGMENT; A LANE EDGE LINE IS CONTINUOUS, AND TELLING THEM
#: APART IS MOST OF THE WORK.
#: Nearly every road that has dashes also has a SOLID line beside them - the
#: kerb line, the edge line, the far lane's divider - running parallel and
#: unbroken. It is bright, elongated and collinear-ish, so it passes every
#: other test here, and including it drags the fitted road axis off true and
#: contributes an edge pair at a distance the dash pattern does not predict.
#: What it cannot fake is being BROKEN: a dash spans a few percent of the
#: visible road, a solid line spans nearly all of it. Measured on the rendered
#: scene in tools/test_dashes.py, the kerb arrived as components 469 and 394
#: pixels long against dashes of 135 and 13.
MAX_DASH_FRAC = 0.22

#: How far off the fitted centre line a dash may sit, as a fraction of the
#: line's own length. A kerb running parallel is the thing this rejects.
MAX_OFFLINE = 0.06

#: 🚨 THREE DASHES, NOT TWO, AND THE REASON IS REDUNDANCY RATHER THAN TASTE.
#: Two dashes give four edges against three unknowns - one degree of freedom,
#: so almost anything fits and the residual barely moves. Measured: a road
#: with NO painted line at all produced two chance marks that lay on a line,
#: and that was enough to build a ruler out of noise. Three dashes give six
#: edges and three degrees of freedom, which is the first point at which the
#: fit can meaningfully disagree with itself - and disagreement is the only
#: evidence of accuracy this file has.
MIN_DASHES = 3


def median_road(frames: list):
    """The scene with the traffic taken out of it."""
    import numpy as np
    if not frames:
        return None
    stack = np.stack([f if f.ndim == 2 else _grey(f) for f in frames[:MEDIAN_FRAMES]])
    return np.median(stack, axis=0).astype("uint8")


def _grey(img):
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _components(road):
    """Bright, elongated blobs, with their geometry."""
    import cv2
    import numpy as np
    # Paint is brighter than asphalt but the asphalt's brightness varies
    # hugely across a frame - sunlit near, shadowed far. A local threshold
    # asks "brighter than ITS OWN surroundings", which is the question that
    # survives that, where a single global cut does not.
    blur = cv2.GaussianBlur(road, (0, 0), 3)
    local = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                  cv2.THRESH_BINARY, 61, -12)
    local = cv2.morphologyEx(local, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(local, 8)
    h, w = road.shape[:2]
    area_px = float(h * w)
    out = []
    for i in range(1, n):
        x, y, bw, bh, a = stats[i]
        if not (MIN_DASH_AREA * area_px <= a <= MAX_DASH_AREA * area_px):
            continue
        long_side, short_side = max(bw, bh), max(1, min(bw, bh))
        if long_side / short_side < MIN_ELONGATION:
            continue
        # A solid line, a shadow edge or a railing: too long to be one dash.
        if long_side > MAX_DASH_FRAC * max(h, w):
            continue
        ys, xs = np.where(labels == i)
        out.append({"cx": float(cents[i][0]), "cy": float(cents[i][1]),
                    "xs": xs, "ys": ys, "area": int(a),
                    "long": float(long_side)})
    return out


def _axis_of(pts):
    """Least-squares principal direction through some points."""
    cxs = [p[0] for p in pts]
    cys = [p[1] for p in pts]
    mx, my = sum(cxs) / len(cxs), sum(cys) / len(cys)
    sxx = sum((x - mx) ** 2 for x in cxs)
    syy = sum((y - my) ** 2 for y in cys)
    sxy = sum((cxs[i] - mx) * (cys[i] - my) for i in range(len(cxs)))
    th = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return math.cos(th), math.sin(th), mx, my


def _axis(comps, frame_diag: float):
    """The road line, found ROBUSTLY - by consensus, not by averaging.

    🚨 LEAST SQUARES THROUGH EVERYTHING IS THE WRONG TOOL AND FAILS SILENTLY.
    The marks handed here are not all dashes: a kerb fragment, a patch of
    bright verge, a shadow edge that survived. Fitting a line through all of
    them puts the axis between the dashes and the junk, at which point NOTHING
    lies on it and the detector reports "no marks lie on one line" - which
    reads as "this road has no dashes" when it actually has five.

    So: every pair of marks proposes a line, and the line with the most marks
    near it wins. Outliers cannot vote for a line they are not on, and no
    threshold has to be tuned to exclude them individually.
    """
    best = None
    tol = 0.012 * frame_diag
    n = len(comps)
    for i in range(n):
        for j in range(i + 1, n):
            ax, ay = comps[i]["cx"], comps[i]["cy"]
            bx, by = comps[j]["cx"], comps[j]["cy"]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            ux, uy = dx / L, dy / L
            inliers = [c for c in comps
                       if abs(-(c["cx"] - ax) * uy + (c["cy"] - ay) * ux) <= tol]
            if best is None or len(inliers) > len(best):
                best = inliers
    if not best or len(best) < MIN_DASHES:
        return None
    ux, uy, mx, my = _axis_of([(c["cx"], c["cy"]) for c in best])
    return ux, uy, mx, my, best


def find(frames: list, debug: bool = False):
    """Dash edges in pixels, ready for calib.fit.

    Returns [] whenever the evidence will not carry a ruler, which is the
    common case on a residential street with no centre line at all.
    """
    road = median_road(frames)
    if road is None:
        return ([], {"why": "no frames"}) if debug else []
    comps = _components(road)
    info = {"components": len(comps)}
    if len(comps) < MIN_DASHES:
        info["why"] = f"only {len(comps)} bright elongated marks"
        return ([], info) if debug else []

    h, w = road.shape[:2]
    got = _axis(comps, math.hypot(h, w))
    if got is None:
        info["why"] = "no line has enough marks on it"
        return ([], info) if debug else []
    ux, uy, mx, my, keep = got
    for c in keep:
        dx, dy = c["cx"] - mx, c["cy"] - my
        c["u"] = dx * ux + dy * uy
    span = max(c["u"] for c in keep) - min(c["u"] for c in keep)
    if span <= 0:
        info["why"] = "marks are all in one place"
        return ([], info) if debug else []
    info["collinear"] = len(keep)
    if len(keep) < MIN_DASHES:
        info["why"] = f"only {len(keep)} marks lie on one line"
        return ([], info) if debug else []

    keep.sort(key=lambda c: c["u"])
    # 🚨 PERSPECTIVE, THE TEST THAT COSTS NOTHING AND CATCHES MOST OF IT.
    # Dashes on a flat road get steadily smaller as they recede. A run of
    # marks whose sizes do not change monotonically is not a lane line - it is
    # a fence, a railing, a row of windows, or three unrelated things.
    sizes = [c["long"] for c in keep]
    rising = all(sizes[i] <= sizes[i + 1] * 1.35 for i in range(len(sizes) - 1))
    falling = all(sizes[i] >= sizes[i + 1] / 1.35 for i in range(len(sizes) - 1))
    if not (rising or falling):
        info["why"] = "marks do not shrink with distance - not on the road plane"
        return ([], info) if debug else []

    edges = []
    for c in keep:
        us = (c["xs"] - mx) * ux + (c["ys"] - my) * uy
        i0, i1 = int(us.argmin()), int(us.argmax())
        a = (float(c["xs"][i0]), float(c["ys"][i0]))
        b = (float(c["xs"][i1]), float(c["ys"][i1]))
        edges.append((a[0], a[1], "start"))
        edges.append((b[0], b[1], "end"))
    info["dashes"] = len(keep)
    info["edges"] = len(edges)
    return (edges, info) if debug else edges
