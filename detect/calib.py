"""Turn a camera's own road markings into a ruler, and a track into a speed.

    marks  = calib.find_dashes(frames)            # the painted dashes, in pixels
    cal    = calib.fit(marks, period_m=12.192)    # image position -> metres
    result = calib.speed(track, cal)              # {mph, lo, hi, ...} or a refusal

🚨 THE POINT OF THIS FILE IS THE ERROR BAR, NOT THE NUMBER.

A speed published against a named officer is an accusation. Every other claim
this project makes sits next to the photograph that proves it, and a speed
cannot - the photograph shows a car, not a velocity. So this returns a RANGE
with the evidence behind it, and refuses outright when the evidence will not
carry a number. A refusal is a good outcome here; a confident wrong answer is
the one thing that would discredit the map.

WHY LANE DASHES. US lane markings are federally standardised (MUTCD 3A.05):
a broken lane line is a 10 ft stripe with a 30 ft gap, a 40 ft = 12.192 m
period, and that ruler is lying in the road in almost every frame the network
already captures. It needs nothing from the contributor, which matters when
the contributors are volunteers.

⚠️ AND IT IS NOT UNIVERSAL, WHICH THE CODE HAS TO SAY OUT LOUD.
  * Residential streets often have NO centre line at all. No dashes, no ruler,
    no speed - and that is most of a volunteer fleet's coverage.
  * The period differs abroad. SparrowMap already has cameras in Finland,
    where the standard is metric and different. `period_m` is a parameter with
    no default guess for that reason.
  * Worn paint, snow, shadow and night all break detection. They make it
    return nothing, which is correct, rather than something wrong.

WHY A 1-D MAP AND NOT A HOMOGRAPHY. Speed along a road needs distance ALONG
THE ROAD, not a full ground plane. For a planar road and a pinhole camera,
distance along the road is a projective function of image position:

    d(u) = (a*u + b) / (c*u + 1)

three unknowns, fitted from the dash edges, whose real separations are known
multiples of the period. Fewer parameters than a homography means fewer ways
to be confidently wrong, and every one of them is checkable against the
spacings it was fitted to.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: MUTCD 3A.05: 10 ft line + 30 ft gap. The ruler, in metres.
US_LANE_PERIOD_M = 12.192

#: Refuse below this many detected dash edges. Three points exactly determine
#: the three unknowns, which means a perfect fit and NO residual - the fit
#: cannot be checked at all. Raised 4 -> 6 on 2026-09-24 after a road with no
#: painted line produced two chance marks that lay on a line and were enough
#: to build a ruler out of noise. Six edges (three dashes) is the first point
#: with real redundancy, and disagreement is the only evidence of accuracy
#: there is.
MIN_EDGES = 6

#: Largest tolerable disagreement between the fitted map and the dash spacings
#: it was built from, as a fraction. 4% of a 40 ft period is about 0.5 m.
MAX_FIT_ERR = 0.04

#: A track has to cross enough of the calibrated stretch to mean anything.
MIN_TRACK_SAMPLES = 6
MIN_TRACK_M = 4.0

#: Widest spread across per-pair speeds before the answer is withheld. A car
#: genuinely accelerating produces a TREND; calibration error and box jitter
#: produce SCATTER, and this file does not pretend to tell them apart - it
#: reports the spread and refuses when it is too wide to publish either way.
MAX_SPREAD = 0.25


@dataclass
class Calibration:
    """image position along the road axis -> metres from the camera."""
    a: float
    b: float
    c: float
    #: unit vector of the road in image space, and the point it passes through
    ux: float
    uy: float
    px: float
    py: float
    fit_err: float = 0.0
    n_edges: int = 0
    period_m: float = US_LANE_PERIOD_M
    notes: list = field(default_factory=list)

    def metres(self, u: float) -> float:
        den = self.c * u + 1.0
        if abs(den) < 1e-9:
            return float("nan")
        return (self.a * u + self.b) / den

    def project(self, x: float, y: float) -> float:
        """Where a point falls ALONG the road axis, in image units."""
        return (x - self.px) * self.ux + (y - self.py) * self.uy

    def usable(self) -> bool:
        return self.n_edges >= MIN_EDGES and self.fit_err <= MAX_FIT_ERR


def _fit_projective(us: list, ds: list) -> tuple:
    """Least squares for d = (a*u + b)/(c*u + 1).

    Linearised as  d = a*u + b - c*u*d, which is linear in (a, b, c). Solved
    with normal equations on a 3x3 - small enough to write out, and writing it
    out avoids making numpy a dependency of a node that already fights for CPU.
    """
    n = len(us)
    rows = [[us[i], 1.0, -us[i] * ds[i]] for i in range(n)]
    ata = [[sum(rows[i][r] * rows[i][cc] for i in range(n)) for cc in range(3)]
           for r in range(3)]
    atb = [sum(rows[i][r] * ds[i] for i in range(n)) for r in range(3)]
    return _solve3(ata, atb)


def _solve3(m: list, v: list) -> tuple:
    """Gaussian elimination with partial pivoting on a 3x3."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return (0.0, 0.0, 0.0)
        a[col], a[piv] = a[piv], a[col]
        for r in range(3):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for cc in range(col, 4):
                a[r][cc] -= f * a[col][cc]
    return tuple(a[i][3] / a[i][i] for i in range(3))


def fit(edges: list, period_m: float = US_LANE_PERIOD_M,
        stripe_m: float = 3.048) -> Calibration:
    """Fit the ruler from dash edges.

    `edges` is a list of (x, y, kind) in pixels, ordered along the road, where
    kind is 'start' or 'end' of a painted stripe. Their real separations are
    known: within a dash it is the stripe length, and from one dash's start to
    the next dash's start it is the period.
    """
    notes = []
    if len(edges) < MIN_EDGES:
        return Calibration(0, 0, 0, 1, 0, 0, 0, 1.0, len(edges), period_m,
                           [f"only {len(edges)} dash edges; need {MIN_EDGES}"])

    xs = [e[0] for e in edges]
    ys = [e[1] for e in edges]
    # The road axis: the principal direction of the dash edges. Two points
    # would do on a straight road; using all of them means one mis-detected
    # dash cannot swing the axis.
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)

    us = [(xs[i] - mx) * ux + (ys[i] - my) * uy for i in range(len(xs))]
    order = sorted(range(len(us)), key=lambda i: us[i])
    us = [us[i] for i in order]
    kinds = [edges[i][2] for i in order]

    # 🚨 THE IMAGE ORDER IS NOT NECESSARILY THE TRAVEL ORDER, AND GUESSING
    # COSTS 8% ON A PERFECT ROAD.
    #
    # Sorting along the image axis can walk the road far-to-near or
    # near-to-far depending on which way the camera points, and the dash
    # pattern is not symmetric: going one way an edge sequence reads
    # start,end,start,end; going the other it reads end,start,end,start, so
    # every stripe gets charged a gap and every gap a stripe. Measured on the
    # synthetic scene in tools/test_calib.py, that alone produced a 7.8% fit
    # error on geometry that is exactly projective.
    #
    # There is no way to know the direction from the image, and no need to
    # guess: fit BOTH and keep whichever reproduces its own dash spacings
    # better. The residual that decides it is the same number that later
    # decides whether the camera may publish a speed at all.
    def _walk(kseq):
        out, d = [], 0.0
        for i, k in enumerate(kseq):
            if i:
                prev = kseq[i - 1]
                if prev == "start" and k == "end":
                    d += stripe_m
                elif prev == "end" and k == "start":
                    d += period_m - stripe_m
                else:
                    d += period_m
            out.append(d)
        return out

    best = None
    for flip in (False, True):
        kk = list(reversed(kinds)) if flip else kinds
        uu = list(reversed(us)) if flip else us
        dd = _walk(kk)
        aa, bb, cc = _fit_projective(uu, dd)
        trial = Calibration(aa, bb, cc, ux, uy, mx, my, 0.0, len(edges),
                            period_m, list(notes))
        span = max(dd) - min(dd)
        if span <= 0:
            continue
        e = max(abs(trial.metres(uu[i]) - dd[i]) for i in range(len(uu))) / span
        if best is None or e < best[0]:
            best = (e, trial, uu, dd)
    if best is None:
        return Calibration(0, 0, 0, ux, uy, mx, my, 1.0, len(edges), period_m,
                           ["dash edges span no distance"])
    err, cal, us, ds = best
    a, b, c = cal.a, cal.b, cal.c

    # 🚨 THE RESIDUAL IS THE WHOLE POINT. It asks the fitted ruler to
    # reproduce the spacings it was built from. A ruler that cannot measure
    # its own marks cannot measure a car.
    cal.fit_err = err
    if err > MAX_FIT_ERR:
        notes.append(f"fit disagrees with its own dashes by {err:.1%}")
    return cal


def speed(track: list, cal: Calibration, w: int = 0, h: int = 0) -> dict:
    """Speed from a banked track, as a RANGE with its working shown.

    `track` is what VehiclePass.motion(full=True) banks: rows of
    [t_seconds, cx, cy, bw, bh] with the centres normalised 0-1.

    Returns either {"ok": False, "why": ...} or a result carrying the per-pair
    speeds it averaged, so a reader can see the arithmetic rather than be
    handed a number.
    """
    if not cal.usable():
        return {"ok": False, "why": "this camera is not calibrated: "
                                    + ("; ".join(cal.notes) or "no ruler")}
    if not track or len(track) < MIN_TRACK_SAMPLES:
        return {"ok": False, "why": f"only {len(track or [])} samples; "
                                    f"need {MIN_TRACK_SAMPLES}"}

    pts = []
    for row in track:
        t, cx, cy = float(row[0]), float(row[1]), float(row[2])
        x, y = cx * (w or 1), cy * (h or 1)
        pts.append((t, cal.metres(cal.project(x, y))))
    pts = [(t, d) for t, d in pts if d == d]          # drop NaN
    if len(pts) < MIN_TRACK_SAMPLES:
        return {"ok": False, "why": "track leaves the calibrated stretch"}

    travelled = abs(pts[-1][1] - pts[0][1])
    if travelled < MIN_TRACK_M:
        return {"ok": False, "why": f"only {travelled:.1f} m of travel; "
                                    f"need {MIN_TRACK_M}"}

    # 🚨 EVERY CONSECUTIVE PAIR, NOT JUST THE ENDPOINTS. Endpoints give one
    # average and no way to check it. Pairs give a distribution, and a
    # distribution is the only evidence that the calibration and the tracking
    # are behaving: a constant-speed car through a correct ruler produces
    # pairs that agree.
    pairs = []
    for i in range(1, len(pts)):
        dt = pts[i][0] - pts[i - 1][0]
        if dt <= 0:
            continue
        pairs.append(abs(pts[i][1] - pts[i - 1][1]) / dt)
    if len(pairs) < 3:
        return {"ok": False, "why": "not enough timed pairs"}

    pairs_sorted = sorted(pairs)
    mid = len(pairs_sorted) // 2
    median = (pairs_sorted[mid] if len(pairs_sorted) % 2
              else (pairs_sorted[mid - 1] + pairs_sorted[mid]) / 2)
    lo, hi = pairs_sorted[0], pairs_sorted[-1]
    spread = (hi - lo) / median if median > 0 else 1.0

    # The overall average is what gets reported: it uses the full baseline, so
    # it is the least sensitive to any single jittery box.
    overall = travelled / (pts[-1][0] - pts[0][0])
    mph = overall * 2.236936

    out = {
        "ok": spread <= MAX_SPREAD,
        "mph": round(mph, 1),
        "mps": round(overall, 2),
        "metres": round(travelled, 1),
        "seconds": round(pts[-1][0] - pts[0][0], 2),
        "samples": len(pts),
        "pair_mph": [round(p * 2.236936, 1) for p in pairs],
        "spread": round(spread, 3),
        "fit_err": round(cal.fit_err, 4),
        "dash_edges": cal.n_edges,
        # The honest interval: the slowest and fastest the pairs support,
        # never the bare average on its own.
        "lo_mph": round(lo * 2.236936, 1),
        "hi_mph": round(hi * 2.236936, 1),
    }
    if not out["ok"]:
        out["why"] = (f"pair speeds disagree by {spread:.0%} - either the car "
                      f"changed speed or the ruler is wrong, and this cannot "
                      f"tell which")
    return out
