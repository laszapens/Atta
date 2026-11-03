# -*- coding: utf-8 -*-
"""Pure geometry for the mechanical families (metres, radians, no bpy).

Three derivations live here so the builders in :mod:`atta.families.mech` stay
thin and every dimension is *computed* rather than typed in:

``gear_derived`` / ``involute_gear_profile``
    ISO 53 basic rack spur gear: pitch/base/tip/root circles from module and
    tooth count, tooth thickness from the pressure angle, and a real involute
    flank sampled from the base circle to the (possibly truncated) tip circle.

``flange_derived`` / ``flange_profile``
    Welded-neck pipe flange: plate + hub + raised face revolved from one
    closed ``(r, z)`` loop, with the bolt circle, rim width, web width and
    gasket groove all derived from the diameters.

``ladder_layout``
    Print-fit clearance ladder: station pitch, coupon length and the per
    station hole diameters derived from the pin diameter, the clearance
    range and the number of pairs.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "gear_derived", "involute_gear_profile", "gear_point",
    "flange_derived", "flange_profile", "groove_profile",
    "ladder_layout", "ladder_hole_diameters",
    "sane_flank_samples",
]

_TAU = 2.0 * math.pi


# ---------------------------------------------------------------- involute

def _involute_fn(alpha):
    """``inv(alpha) = tan(alpha) - alpha``."""
    return math.tan(alpha) - alpha


def _half_angle(r, rb, half_base):
    """Half angular tooth thickness at radius ``r``.

    Below the base circle the involute does not exist; the flank degenerates to
    a radial line, which is exactly ``half_base`` (the value at ``rb``).
    """
    rr = max(float(r), rb)
    ar = math.acos(min(1.0, rb / rr))
    return half_base - _involute_fn(ar)


def gear_derived(teeth, module, pressure_angle_deg, backlash=0.0,
                 addendum_coeff=1.0, dedendum_coeff=1.25, tip_half_min=None):
    """All gear dimensions from the ISO 53 basic rack.

    Derived (all metres / radians):

    ============  =========================================================
    ``pitch_r``   ``module * teeth / 2``
    ``base_r``    ``pitch_r * cos(pressure_angle)``
    ``addendum``  ``addendum_coeff * module``   (1.0 * m = standard full depth)
    ``dedendum``  ``dedendum_coeff * module``   (1.25 * m leaves 0.25 m clearance)
    ``root_r``    ``pitch_r - dedendum``, floored at ``0.30 * pitch_r``
    ``tip_r``     ``pitch_r + addendum``, *truncated* so the tip keeps a
                  printable chord (``tip_half_min``) instead of coming to a
                  point -- this is the one place where a bad parameter set
                  cannot produce a self-intersecting profile
    ``thickness`` ``pi * module / 2 - backlash`` (arc thickness at the pitch
                  circle; ``backlash`` is removed as an arc length)
    ``half_base`` involute angle at the base circle = ``thickness/(2*pitch)``
                  ``+ inv(pressure_angle)``
    ============
    """
    z = int(teeth)
    m = float(module)
    if z < 4:
        raise ValueError("a gear needs at least 4 teeth, got %r" % (teeth,))
    if m <= 0.0:
        raise ValueError("module must be > 0")
    alpha = math.radians(float(pressure_angle_deg))
    if not (0.0 < alpha < math.radians(45.0)):
        raise ValueError("pressure angle must be within (0, 45) degrees")

    pitch_r = 0.5 * m * z
    base_r = pitch_r * math.cos(alpha)
    addendum = float(addendum_coeff) * m
    dedendum = float(dedendum_coeff) * m
    root_r = max(0.30 * pitch_r, pitch_r - dedendum)
    thickness = 0.5 * math.pi * m - float(backlash)
    half_base = thickness / (2.0 * pitch_r) + _involute_fn(alpha)

    # tip truncation: keep the tooth from closing to a point / hairline
    if tip_half_min is None:
        tip_half_min = max(0.008, 0.05 * math.pi / z)
    tip_half_min = float(tip_half_min)
    tip_r_nom = pitch_r + addendum
    r_lo = max(base_r, root_r)
    tip_r = tip_r_nom
    if _half_angle(tip_r_nom, base_r, half_base) < tip_half_min:
        lo, hi = r_lo, tip_r_nom
        for _ in range(60):  # bisection: half angle decreases with radius
            mid = 0.5 * (lo + hi)
            if _half_angle(mid, base_r, half_base) >= tip_half_min:
                lo = mid
            else:
                hi = mid
        tip_r = lo
    half_tip = _half_angle(tip_r, base_r, half_base)
    half_root = _half_angle(root_r, base_r, half_base)
    return {
        "teeth": z, "module": m, "pressure_angle_deg": float(pressure_angle_deg),
        "pressure_angle": alpha,
        "pitch_r": pitch_r, "base_r": base_r, "tip_r": tip_r,
        "tip_r_nominal": tip_r_nom, "tip_truncated": bool(tip_r < tip_r_nom - 1e-12),
        "root_r": root_r, "addendum": addendum, "dedendum": dedendum,
        "thickness": thickness, "half_base": half_base,
        "half_tip": half_tip, "half_root": half_root,
        "tip_half_min": tip_half_min,
        "pitch_deg": 360.0 / z,
        "tip_chord": 2.0 * half_tip * tip_r,
        "undercut_teeth_min": (2.0 * float(addendum_coeff)
                               / math.sin(alpha) ** 2),
        "contact_ratio_proxy": (tip_r - base_r) / max(1e-12, pitch_r * math.sin(alpha)),
    }


def gear_point(r, angle):
    return (float(r) * math.cos(float(angle)), float(r) * math.sin(float(angle)))


def involute_gear_profile(geom, flank_samples=8, root_samples=3):
    """Closed ``(n, 2)`` tooth loop: involute flanks + tip arc + root arc.

    Walked counter-clockwise tooth by tooth, so the polygon is simple for any
    legal parameter set (every flank angle is strictly monotone in radius).
    """
    z = int(geom["teeth"])
    base_r = float(geom["base_r"])
    root_r = float(geom["root_r"])
    tip_r = float(geom["tip_r"])
    half_base = float(geom["half_base"])
    step = _TAU / z
    fs = max(2, int(flank_samples))
    rs = max(1, int(root_samples))
    r_lo = max(root_r, base_r)

    # one flank, root -> tip, as (radius, half angle) pairs
    flank = []
    if root_r < base_r - 1e-12:
        flank.append((root_r, half_base))  # radial run below the base circle
    for k in range(fs + 1):
        r = r_lo + (tip_r - r_lo) * (float(k) / fs)
        flank.append((r, _half_angle(r, base_r, half_base)))

    pts = []
    for i in range(z):
        a0 = i * step
        for r, h in flank:                     # trailing flank, root -> tip
            pts.append(gear_point(r, a0 - h))
        pts.append(gear_point(tip_r, a0 + flank[-1][1]))   # tip arc chord
        for r, h in reversed(flank[:-1]):      # leading flank, tip -> root
            pts.append(gear_point(r, a0 + h))
        root_half = flank[0][1]
        for k in range(1, rs + 1):             # root arc up to the next tooth
            ang = a0 + root_half + (step - 2.0 * root_half) * (k / (rs + 1.0))
            pts.append(gear_point(root_r, ang))
    return np.asarray(pts, dtype=np.float64)


def sane_flank_samples(teeth, flank_samples, budget=1100):
    """Cap the involute sampling so the profile stays cheap to triangulate."""
    z = max(4, int(teeth))
    keep = int(budget // (2 * z)) - 3
    return max(2, min(int(flank_samples), keep))


# ------------------------------------------------------------------ flange

def flange_derived(outer_d, bore_d, bolt_circle_d, bolt_hole_d, bolt_holes,
                   plate_t, hub_d, hub_h, raised_h, face_d,
                   groove_d, groove_w, groove_depth):
    """Every flange dimension from the diameters; metres in, metrics in mm.

    Derived: bore radius, plate rim (``outer_r - bolt circle outer edge``),
    bolt web (gap between neighbouring bolt holes), hub wall, tangent circle
    chord between bolts, raised-face ring width and the gasket groove limits.
    """
    outer_r = 0.5 * float(outer_d)
    bore_r = 0.5 * float(bore_d)
    hub_r = 0.5 * float(hub_d)
    bc_r = 0.5 * float(bolt_circle_d)
    hole_r = 0.5 * float(bolt_hole_d)
    n = max(1, int(bolt_holes))
    face_r = 0.5 * float(face_d)
    groove_r = 0.5 * float(groove_d)

    rim_mm = (outer_r - (bc_r + hole_r)) * 1e3
    web_mm = (math.pi * bc_r / n - 2.0 * hole_r) * 1e3
    hub_wall_mm = (hub_r - bore_r) * 1e3
    face_ring_mm = (face_r - bore_r) * 1e3
    bolt_to_face_mm = ((bc_r - hole_r) - face_r) * 1e3
    bolt_to_hub_mm = ((bc_r - hole_r) - hub_r) * 1e3
    groove_in_mm = ((groove_r - 0.5 * float(groove_w)) - bore_r) * 1e3
    groove_out_mm = (face_r - (groove_r + 0.5 * float(groove_w))) * 1e3
    return {
        "outer_r": outer_r, "bore_r": bore_r, "hub_r": hub_r, "bc_r": bc_r,
        "hole_r": hole_r, "n_holes": n, "face_r": face_r, "groove_r": groove_r,
        "groove_w": float(groove_w), "groove_depth": float(groove_depth),
        "plate_t": float(plate_t), "hub_h": float(hub_h),
        "raised_h": float(raised_h), "bolt_hole_d": float(bolt_hole_d),
        "bolt_circle_r": bc_r, "hub_wall_mm": hub_wall_mm,
        "rim_mm": rim_mm, "web_mm": web_mm, "face_ring_mm": face_ring_mm,
        "bolt_to_face_mm": bolt_to_face_mm, "bolt_to_hub_mm": bolt_to_hub_mm,
        "groove_in_mm": groove_in_mm, "groove_out_mm": groove_out_mm,
        "has_groove": bool(float(groove_depth) > 0.0),
        "has_raised_face": bool(float(raised_h) > 0.0),
        "genus": 1 + n,
    }


def flange_profile(d):
    """Closed ``(r, z)`` loop: plate + hub (below) + raised face (above).

    ``z = 0`` is the *back* (hub side) face of the plate; the plate occupies
    ``z in [0, plate_t]``, the hub hangs at ``z in [-hub_h, 0]`` and the raised
    face stands on ``z in [plate_t, plate_t + raised_h]``.
    """
    bore_r = float(d["bore_r"])
    outer_r = float(d["outer_r"])
    plate_t = float(d["plate_t"])
    hub_r = float(d["hub_r"])
    hub_h = float(d["hub_h"])
    face_r = float(d["face_r"])
    raised_h = float(d["raised_h"])
    z_top = plate_t + raised_h

    pts = [(bore_r, z_top)]
    if raised_h > 1e-12:
        pts.append((max(face_r, bore_r), z_top))
        pts.append((max(face_r, bore_r), plate_t))
    pts.append((outer_r, plate_t))
    pts.append((outer_r, 0.0))
    if hub_h > 1e-12 and hub_r < outer_r - 1e-12:
        pts.append((min(hub_r, outer_r), 0.0))
        pts.append((min(hub_r, outer_r), -hub_h))
        pts.append((bore_r, -hub_h))
    else:
        pts.append((bore_r, 0.0))
    pts.append((bore_r, z_top))            # close the loop
    return np.asarray(pts, dtype=np.float64)


def groove_profile(d):
    """Closed ``(r, z)`` loop of the gasket-groove cutter (an annular ring)."""
    gr = float(d["groove_r"])
    hw = 0.5 * float(d["groove_w"])
    depth = float(d["groove_depth"])
    z_top = float(d["plate_t"]) + float(d["raised_h"])
    z0 = z_top - depth
    z1 = z_top + 1e-4
    return np.asarray([(gr - hw, z0), (gr + hw, z0), (gr + hw, z1),
                       (gr - hw, z1), (gr - hw, z0)], dtype=np.float64)


# ------------------------------------------------------- ISO 286 fit bands

#: ISO 286-2 deviations in micrometres, keyed by the diameter step
#: ``(lower_exclusive, upper_inclusive)``.  Holes: (ES, EI).  Shafts: (es, ei).
_ISO286 = {
    (3.0, 6.0): {
        "holes": {"H7": (12, 0), "H8": (18, 0), "H9": (30, 0), "H11": (75, 0)},
        "shafts": {"g6": (-4, -12), "f7": (-10, -22), "e9": (-20, -50),
                   "d10": (-30, -78), "h11": (0, -75)},
    },
    (6.0, 10.0): {
        "holes": {"H7": (15, 0), "H8": (22, 0), "H9": (36, 0), "H11": (90, 0)},
        "shafts": {"g6": (-5, -14), "f7": (-13, -28), "e9": (-25, -61),
                   "d10": (-40, -98), "h11": (0, -90)},
    },
}

#: Reference ISO 286 clearance fits, tightest first.
FIT_PAIRS = (("H7", "g6"), ("H7", "f7"), ("H8", "e9"), ("H9", "d10"),
             ("H11", "h11"))


def _iso_step(nominal_d):
    mm = float(nominal_d) * 1e3
    for step in sorted(_ISO286):
        lo, hi = step
        if lo < mm <= hi:
            return _ISO286[step]
    step = min(_ISO286) if mm <= 3.0 else max(_ISO286)
    return _ISO286[step]


def iso286_clearance(nominal_d, hole="H7", shaft="g6"):
    """Diametral clearance (metres, ``min``/``max``) of one ISO 286 fit.

    ``nominal_d`` in metres; the deviation table is the ISO 286-2 3-6 mm and
    6-10 mm step, so the answer is exact for a 3-10 mm nominal shaft.
    """
    step = _iso_step(nominal_d)
    ES, EI = step["holes"][hole]
    es, ei = step["shafts"][shaft]
    return {"min": (EI - es) * 1e-6, "max": (ES - ei) * 1e-6,
            "hole": hole, "shaft": shaft}


def nearest_fit(clearance, nominal_d):
    """Name the ISO 286 fit whose clearance band best matches ``clearance``.

    Returns ``(label, inside, min_m, max_m)``.  Used to label every ladder
    station, so the coupon says which standard fit each step represents.
    """
    c = float(clearance)
    best = None
    for hole, shaft in FIT_PAIRS:
        band = iso286_clearance(nominal_d, hole, shaft)
        lo, hi = band["min"], band["max"]
        inside = lo <= c <= hi
        dist = 0.0 if inside else min(abs(c - lo), abs(c - hi))
        if best is None or (dist, 0 if inside else 1) < best[0]:
            best = ((dist, 0 if inside else 1), "%s/%s" % (hole, shaft),
                    inside, lo, hi)
    return best[1], best[2], best[3], best[4]


# ------------------------------------------------------------------ ladder

def ladder_hole_diameters(pin_d, pairs, clearance_min, clearance_max):
    """Hole diameter per station (metres): the diametral clearance ladder."""
    n = max(1, int(pairs))
    lo, hi = float(clearance_min), float(clearance_max)
    if n == 1:
        steps = [lo]
    else:
        steps = [lo + (hi - lo) * (i / (n - 1.0)) for i in range(n)]
    return [float(pin_d) + c for c in steps], steps


def ladder_layout(pin_d, pairs, clearance_min, clearance_max, depth, gap,
                  spine_w):
    """Coupon layout (metres): station pitch, coupon length, boss dimensions.

    One printed part, two rows: ``-y`` carries a *blind* gauge hole per
    clearance class (boss + hole, floor left on purpose), ``+y`` carries the
    nominal pin that the holes are gauged against.  Blind holes keep the
    coupon at genus 0 for every ``pairs``, so the recipe's single
    ``expected_genus`` stays exact while the ladder stays parametric.

    Derived: ``spine_t`` (plate under the bosses), ``floor_t`` (material left
    below a blind hole), ``boss_rim`` (radial wall around a hole),
    ``boss_d`` / ``boss_h``, station ``pitch`` and the coupon ``length``.

    Process constants are FDM-floor values in metres: 3 mm spine, 1.5 mm
    floor under a blind hole, 1 mm minimum boss rim, 2 mm end margin.
    """
    n = max(1, int(pairs))
    hole_ds, clearances = ladder_hole_diameters(pin_d, pairs, clearance_min,
                                                clearance_max)
    hole_d_max = max(hole_ds)
    pin = float(pin_d)
    spine_t = max(3e-3, 0.5 * pin)
    floor_t = max(1.5e-3, 0.3 * pin)
    boss_rim = max(1.0e-3, 0.15 * pin)
    boss_d = [h + 2.0 * boss_rim for h in hole_ds]
    boss_d_max = max(boss_d)
    boss_h = float(depth) + floor_t
    pitch = boss_d_max + max(0.0, float(gap))
    end_rim = max(2.0e-3, 0.25 * pin)
    usable = (n - 1) * pitch
    length = usable + 2.0 * (0.5 * boss_d_max + end_rim)
    width = float(spine_w)
    y_row = 0.25 * width
    return {
        "pairs": n, "hole_d": hole_ds, "clearance": clearances,
        "hole_d_max": hole_d_max, "clearance_max": float(clearance_max),
        "clearance_min": float(clearance_min),
        "spine_t": spine_t, "floor_t": floor_t, "boss_rim": boss_rim,
        "boss_d": boss_d, "boss_d_max": boss_d_max, "boss_h": boss_h,
        "depth": float(depth), "gap": float(gap), "pitch": pitch,
        "length": length, "width": width, "end_rim": end_rim,
        "y_hole": -y_row, "y_pin": y_row, "x": [-0.5 * usable + i * pitch
                                               for i in range(n)],
        "pin_d": pin, "pin_r": 0.5 * pin,
        "pin_h": max(4e-3, float(depth)),
        "total_h": spine_t + boss_h,
        "genus": 0,
    }
