#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Held-out generalization benchmark for Atta's *generic* modelling surface.

What this measures (and what it deliberately refuses to flatter)
---------------------------------------------------------------
``docs/SHAPE_COVERAGE_REQUIREMENTS.md`` §9 asks one question: can Atta build shapes
it was **never given a recipe for**?  So every case here is built from the generic
feature-graph surface (``atta.spec`` v3: primitives + revolve/loft/sweep/extrude/
coons/gordon + booleans + pattern + transform + thicken), **never** by calling the
21 shipped recipes.

Protocol (recorded in ``corpus/bench/generalization_v1.json`` under ``protocol``):

1. **Selection.** Records come from ``corpus/normalized/*.jsonl``.  A record is
   *held out* when its ``shape.family`` does not map to a shipped recipe
   (``EXCLUDED_FAMILIES``; every entry carries the recipe it is covered by).
   Up to 30% of the set may be *variants* of shipped families built with
   parameter values that no shipped preset uses.  Cases are stratified over the
   ``docs/coverage_matrix.json`` categories proportional to ``target_examples``.
2. **Expression.** Each case is mapped to one of a small library of *generic*
   graph templates (``TEMPLATES``).  The template is chosen from the record's own
   ``operations`` list -- if the record needs an op the construct does not cover,
   that case's ``op_coverage`` drops and it is recorded as a coverage gap, not
   skipped.  The template is parameterised from the record's **declared**
   parameter defaults, so the graph asks for the record's real dimensions.
3. **Receipt.** ``atta.engine.model(spec=...)`` at draft quality (60 s budget per
   case), then ``atta.engine.verify(...)`` **with case-derived constraints**
   (closed / n_components / genus / bbox windows taken from the record's own
   declared parameters and ``tests[].expect``).  A ``degraded`` verdict is not a
   pass.
4. **Metrics.** build_success, verify_pass, param_recovery, op_coverage,
   family_coverage, reuse -- each defined in ``METRIC_DEFS``.

Honesty notes that the report must not bury:

* ``param_recovery`` is **near-tautological** under this design: the graph is
  parameterised with the same numbers it is scored against, so it measures the
  spec pipeline's dimensional fidelity (mapping + unit conversion + boolean
  survival), *not* generalization.  It is reported because §9 asks for it.
* ``build_success`` (model_id returned) is cheap to satisfy for a graph that the
  template already knows how to make.  The discriminating metric is
  ``faithful_build_success`` = built AND all reference ops expressible AND the
  declared topology matched.  Both are reported side by side.
* ``verify_pass`` counts only ``verdict == "pass"``; ``degraded`` is a failure.

Usage::

    python3 scripts/run_bench.py --cases corpus/bench/generalization_v1.json \
        --out corpus/bench/generalization_v1.json          # re-run in place
    python3 scripts/run_bench.py --regenerate \
        --out corpus/bench/generalization_v1.json          # rebuild case list
    python3 scripts/run_bench.py --cases ... --out /tmp/x.json --limit 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import sys
import time
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CORPUS_GLOB = "corpus/normalized/*.jsonl"
MATRIX = "docs/coverage_matrix.json"
BENCH_VERSION = "generalization_v1"
CASE_BUDGET_S = 60
DEFAULT_N = 100
MAX_VARIANT_FRACTION = 0.30

# ---------------------------------------------------------------- op model
#: Ops Atta exposes as first-class feature ops (read live from the registry at
#: run time; this is the documentation copy used by ``--regenerate``).
NATIVE_OPS = {
    "box", "cylinder", "sphere", "cone", "torus", "revolve", "loft", "sweep",
    "coons", "gordon", "extrude", "thicken", "lobes", "union", "difference",
    "intersection", "mirror", "pattern_linear", "pattern_polar", "transform",
    "text_engrave", "text_emboss", "sdf", "gyroid",
}
#: Ops with no dedicated feature op but a documented composition over the ops
#: above.  Each entry states HOW, because "constructible" has to be auditable.
CONSTRUCTIBLE_OPS = {
    "hole": "cylinder subtraction",
    "shell": "difference of two revolves / an offset inner body",
    "chamfer": "difference of rotated cutter prisms (see primitive.chamfer_box)",
    "fillet": "difference of 'chamfer prism minus cylinder' cutters",
    "knurl": "pattern_polar of a cutter, subtracted",
    "emboss": "union of a raised body (text_emboss exists)",
    "engrave": "difference of a text cutter (text_engrave exists)",
    "text": "text_engrave / text_emboss op",
    "pocket": "box subtraction",
    "bend": "loft between rotated sections",
    "twist": "loft/sweep with twist_deg",
    "teeth": "extrude of a sampled profile loop",
    "lattice": "sdf(op) / gyroid(op) implicit field",
    "lipschitz": "sdf() calibrates the field gradient to a metric distance",
}
#: Geometric ops with no expression at all in the current surface.  Together
#: with NATIVE_OPS and CONSTRUCTIBLE_OPS this is the *curated geometric
#: vocabulary*: op_coverage is measured only over declared ops in this set, so a
#: record cannot inflate or deflate the metric with renderer tokens.  Everything
#: outside the vocabulary is reported separately as ``ops_ignored``.
MISSING_OPS = {
    "hull": "Minkowski/hull of arbitrary solids is not in the op set",
    "minkowski": "same",
    "projection": "no 3D->2D projection op",
    "polyhedron": "no arbitrary vertex/face solid op",
    "offset": "surface offset (only 'thicken' for shells)",
    "smooth": "no smoothing/subdivision op",
    "morph": "no shape interpolation op",
    "surface": "no heightmap op",
    "deform": "no free-form deformation op",
    "subdiv": "no subdivision op",
    "remesh": "no remeshing op",
    "rule": "grammar/L-system rule (Structure Synth, CFDG, L-systems)",
    "startshape": "grammar/L-system entry rule",
    "cf::symmetry": "grammar symmetry directive (CFDG)",
    "libfive_tree": "implicit/SDF expression tree",
}

#: Curated geometric vocabulary used as the op_coverage denominator.
GEOMETRIC_VOCAB = set(NATIVE_OPS) | set(CONSTRUCTIBLE_OPS) | set(MISSING_OPS)

#: Record ``kind`` values that carry geometry; metadata kinds are not shapes.
GEOMETRIC_KINDS = {"openscad", "cadquery", "build123d", "freecad", "blender",
                   "sdf", "mesh", "other", "jscad", "povray", "cfdg"}

#: corpus ``shape.family`` -> the shipped recipe that already covers it.
#: Anything in here is NOT held out.  Keys are matched by exact name and by
#: prefix (``mechanical.gear`` covers ``mechanical.gear.spur``).
EXCLUDED_FAMILIES = OrderedDict([
    ("mechanical.gear", "mechanical.gear.involute"),
    ("mechanism.gear", "mechanism.gear"),
    ("mechanical.flange", "mechanical.flange"),
    ("mechanical.bracket", "part.bracket"),
    ("product.enclosure", "part.enclosure"),
    ("product.bottle_cap", "product.screw_cap"),
    ("product.cap", "product.screw_cap"),
    ("product.mug", "product.mug"),
    ("product.bowl", "product.bowl"),
    ("product.vase", "product.vase"),
    ("product.bottle", "product.bottle"),
    ("assembly.fastener", "assembly.bolt_nut"),
    ("assembly.thread", "assembly.bolt_nut"),
    ("thread.helical", "assembly.bolt_nut"),
    ("tolerance.clearance_ladder", "tolerance.clearance_ladder"),
    ("tolerance.fit", "tolerance.clearance_ladder"),
    ("text.engraved_plate", "text.engraved_plate"),
    ("text.engrave", "text.engraved_plate"),
    ("text.emboss", "text.engraved_plate"),
    ("text.label", "text.engraved_plate"),
    ("architecture.column", "architecture.fluted_column"),
    ("architecture.fluted", "architecture.fluted_column"),
    ("pattern.honeycomb", "pattern.honeycomb_panel"),
    ("architecture.wall_panel", "pattern.honeycomb_panel"),
    ("lattice.gyroid", "lattice.gyroid_slab"),
    ("lattice.gyroid_slab", "lattice.gyroid_slab"),
    ("surface.coons_duct", "surface.coons_duct"),
    ("surface.duct", "surface.coons_duct"),
    ("surface.panel", "surface.panel"),
    ("surface.dome", "surface.dome"),
    ("organic.seashell", "organic.seashell"),
    ("organic.shell.spiral", "organic.seashell"),
    ("organic.shell.log_spiral", "organic.seashell"),
    ("organic.growth_vessel", "product.vase"),
    ("primitive.chamfer_box", "primitive.chamfer_box"),
    ("primitive.chamfered_box", "primitive.chamfer_box"),
    ("primitive.box", "primitive.chamfer_box"),
    ("primitive.plate", "primitive.chamfer_box"),
])

METRIC_DEFS = {
    "build_success": "atta_model returned a model_id / cases attempted",
    "faithful_build_success": "built AND every reference op expressible AND declared topology matched",
    "verify_pass": "verify verdict == 'pass' / cases attempted (degraded counts as failure)",
    "param_recovery": "max relative bbox error vs the record's declared dimensions "
                      "(null when the record declares no usable dimension)",
    "op_coverage": "(native + constructible) reference ops / total reference ops",
    "op_coverage_native": "native (first-class) reference ops / total reference ops",
    "family_coverage": "coverage-matrix categories that have a shipped family",
    "reuse": "per new family: corpus variants available vs the 5-variant target",
}

TARGETS = {"build_success": 0.90, "verify_pass": 0.75, "param_recovery_pct": 2.0,
           "param_recovery_mm": 0.5, "op_coverage": 0.80, "family_coverage": 9,
           "reuse": 1.0}


# ---------------------------------------------------------------- helpers
def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _param_num(p):
    """First numeric default (or midpoint) of a parameter record."""
    for k in ("default", "value"):
        n = _num(p.get(k))
        if n is not None and n > 0:
            return n
    lo, hi = _num(p.get("min")), _num(p.get("max"))
    if lo is not None and hi is not None and hi > lo > 0:
        return 0.5 * (lo + hi)
    return None


_ALIASES = {
    "x": {"length", "l", "len", "x", "size_x", "width_x"},
    "y": {"width", "w", "depth", "y", "size_y", "breadth", "d"},
    "z": {"height", "h", "thickness", "t", "thick", "z", "size_z"},
    "d": {"diameter", "d", "dia", "od", "outer_d", "outer_diameter", "d1", "d2",
          "inner_d", "bore_d", "hole_d", "r_d", "diameter_mm"},
    "r": {"radius", "r", "r1", "r2", "outer_r", "inner_r", "r_outer"},
}


def _norm(name):
    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def declared_dims(record):
    """Extract declared L/W/H/T/R (mm) from a record's parameter defaults.

    Returns ``(dims, used)`` where ``used`` maps axis -> the parameter that
    supplied it (so ``param_recovery`` can be audited against real names rather
    than against numbers this script invented).
    """
    params = [p for p in (record.get("parameters") or [])
              if isinstance(p, dict) and p.get("name")]
    dims, used = {}, {}
    for axis in ("x", "y", "z", "d", "r"):
        for p in params:
            nm = _norm(p.get("name"))
            if nm not in _ALIASES[axis]:
                continue
            val = _param_num(p)
            if val is None or val <= 0:
                continue
            # unit sanity: a length in 'm' would be 0.08, in 'mm' 80
            unit = str(p.get("unit") or "").lower()
            if unit == "m" and val < 5:
                val *= 1000.0
            elif unit in ("cm",) and val < 50:
                val *= 10.0
            if not (0.05 <= val <= 5000.0):
                continue
            dims[axis] = val
            used[axis] = nm
            break
    return dims, used


def fallback_dims(seed_text):
    """Deterministic fallback dimensions for a record that declares none.

    Without this, every dimension-less record collapses to the same default
    geometry and the benchmark silently tests one shape many times.  These
    numbers are NOT from the record, so ``param_recovery`` stays null for them
    (see ``dims_source`` in the case record).
    """
    h = int(hashlib.sha256(str(seed_text).encode("utf-8")).hexdigest()[:8], 16)
    L = 20.0 + (h % 45)            # 20..65 mm
    W = 12.0 + ((h >> 6) % 30)     # 12..42 mm
    H = 6.0 + ((h >> 12) % 18)     # 6..24 mm
    R = float(min(L, W) / 2.0)
    return {"L": float(L), "W": float(W), "H": float(H), "T": float(max(2.0, H / 4.0)),
            "R": R, "R_top": 0.72 * R}


def template_dims(dims):
    """Fold declared dims into the L/W/H/T/R set the templates consume."""
    L = dims.get("x")
    W = dims.get("y")
    R = dims.get("r")
    d = dims.get("d")
    # A record often declares both a diameter and a partial radius (a fillet,
    # a hole, a round).  When they disagree the diameter is the overall size, so
    # prefer it rather than letting a small `r` shrink the whole part.
    if d and R is not None and abs(2.0 * R - d) > 0.10 * d:
        R = None
    if R is None and d:
        R = d / 2.0
    H = dims.get("z")
    out = {"L": L, "W": W, "H": H, "R": R, "T": dims.get("z")}
    for key, default in (("L", 60.0), ("W", 40.0), ("H", 24.0), ("T", 4.0), ("R", 20.0)):
        if out.get(key) is None:
            out[key] = default
    if R and not dims.get("x"):
        out["L"] = 2.0 * R
    if R and not dims.get("y"):
        out["W"] = 2.0 * R
    if R and not dims.get("z"):
        out["H"] = 1.5 * R
    out["R_top"] = 0.72 * out["R"]
    return out


# ---------------------------------------------------------------- templates
#: Each template is a *generic* expression: a small feature graph built only
#: from first-class ops, parameterised by the record's declared dimensions.
#: ``covers`` lists the corpus op vocabulary the graph exercises or stands in
#: for; it is what ``op_coverage`` credits the case with.

def _spec(name, feats, segments=48):
    return {
        "schema": "atta.spec/v3",
        "name": name,
        "model": {"kind": "graph", "units": "mm", "segments": segments,
                  "output": feats[-1]["id"], "features": feats},
        "quality": "draft", "material": "steel",
        "constraints": {}, "assumptions": [],
    }


#: ops that create a through-hole / cavity in the record's own construction
HOLE_EVIDENCE_OPS = {"hole", "pocket", "bore", "drill", "slot"}


def hole_evidence(record):
    """Does the record's *own* op list claim a hole/cavity feature?

    Decided from the reference operations only -- never from the expected genus,
    which would be fitting the answer instead of reading the source.
    """
    ops = {_norm(o) for o in (record.get("operations") or [])}
    raw = {_norm(o) for o in (record.get("raw_ops") or [])}
    both = ops | raw
    if both & HOLE_EVIDENCE_OPS:
        return True
    # A cylinder paired with a *subtractive* boolean is how sources cut holes.
    # Union is not evidence: many solid composites union a cylinder as a boss.
    returns_cyl = bool(both & {"cylinder", "drill", "bore"})
    returns_cut = bool(both & {"difference", "intersection", "subtract"})
    return returns_cyl and returns_cut


def _rect(L, W):
    return [[-L / 2, -W / 2], [L / 2, -W / 2], [L / 2, W / 2], [-L / 2, W / 2]]


def _ngon(r, n, cx=0.0, cy=0.0):
    return [[cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]


def t_box_csg(d, opts=None):
    """box, optionally minus a through bore when the source has a hole."""
    L, W, H = d["L"], d["W"], d["H"]
    feats = [{"id": "base", "op": "box", "args": {"size": [L, W, H]}}]
    if not (opts or {}).get("holes", False):
        return _spec("box_csg", feats)
    r = min(d.get("R") or 0.18 * min(L, W), 0.45 * min(L, W))
    feats += [
        {"id": "bore", "op": "cylinder",
         "args": {"radius": r, "height": H * 2.0, "center": [0, 0, 0]}},
        {"id": "out", "op": "difference", "args": {}, "inputs": ["base", "bore"]},
    ]
    return _spec("box_csg", feats)


def t_plate_holes(d, opts=None):  # noqa: ARG001
    """extruded plate minus a linear row of through holes (if the source has holes)."""
    L, W, T = max(1.0, d["L"]), max(1.0, d["W"]), max(0.8, d["T"])
    # a hole must sit strictly inside the plate, otherwise the difference can
    # degenerate (tangent silhouettes) and the boolean returns a broken mesh.
    r = min(max(0.3, d.get("R") or 0.05 * min(L, W)), 0.12 * min(L, W))
    n = 3 if (opts or {}).get("holes", True) else 0
    span = 0.6 * L
    if n and (span / max(n - 1, 1) * 0.5 + r) > 0.45 * L:
        n = 0
    feats = [{"id": "plate", "op": "extrude",
              "args": {"loop_xy": _rect(L, W), "height": T}}]
    if n == 0:
        return _spec("plate_holes", feats)
    for i in range(n):
        feats.append({"id": "h%d" % i, "op": "cylinder",
                      "args": {"radius": r, "height": T * 2.0,
                               "center": [(-(n - 1) / 2.0 + i) * (span / max(n - 1, 1)),
                                          0.0, 0.0]}})
    feats.append({"id": "out", "op": "difference", "args": {},
                  "inputs": ["plate"] + ["h%d" % i for i in range(n)]})
    return _spec("plate_holes", feats)


def t_revolve_solid(d, opts=None):  # noqa: ARG001 - uniform template signature
    """solid of revolution with a taper (frustum-ish)."""
    H, R = d["H"], d["R"]
    return _spec("revolve_solid", [
        {"id": "out", "op": "revolve",
         "args": {"profile_rz": [[0, 0], [R, 0], [d["R_top"], H], [0, H]],
                  "segments": 48}},
    ])


def t_revolve_shell(d, opts=None):  # noqa: ARG001 - uniform template signature
    """vessel: outer revolve minus an inner cavity (closed bottom, open top)."""
    H, R = max(0.5, d["H"]), max(0.5, d["R"])
    # the cavity radius must stay positive for any declared diameter
    t = max(0.2, min(max(1.2, d.get("T") or 0.06 * R), 0.3 * R))
    R = max(2.0 * t + 0.1, R)
    return _spec("revolve_shell", [
        {"id": "outer", "op": "revolve",
         "args": {"profile_rz": [[0, 0], [R, 0], [0.96 * R, H], [0, H]],
                  "segments": 48}},
        {"id": "cavity", "op": "cylinder",
         "args": {"radius": R - t, "height": H,
                  "center": [0, 0, H / 2.0 + t]}},
        {"id": "out", "op": "difference", "args": {},
         "inputs": ["outer", "cavity"]},
    ])


def t_loft_stack(d, opts=None):  # noqa: ARG001 - uniform template signature
    """loft between an elliptical section and a circular one (transition piece)."""
    H, R = d["H"], d["R"]
    n = 24
    r0 = 0.85 * R
    lo = _ngon(r0 * 1.4, n)
    hi = _ngon(max(0.3 * R, 0.35 * r0), n)
    ring0 = [[p[0], p[1] * 0.8, 0.0] for p in lo]
    ring1 = [[p[0], p[1], H] for p in hi]
    return _spec("loft_stack", [{"id": "out", "op": "loft",
                                 "args": {"rings": [ring0, ring1]}}])


def t_sweep_path(d, opts=None):  # noqa: ARG001 - uniform template signature
    """rod swept along a bent path."""
    R, H = max(1.0, d.get("R") or 4.0), d["H"]
    sec = _ngon(R, 16)
    path = [[-d["L"] / 2.0, 0, 0], [0, 0, 0.4 * H], [d["L"] / 2.0, 0, H]]
    return _spec("sweep_path", [{"id": "out", "op": "sweep",
                                 "args": {"section_xy": sec, "path": path}}])


def t_extrude_profile(d, opts=None):
    """extruded polygonal blank; inner loops become real through-holes."""
    T, R = max(0.8, d["T"]), d["R"]
    holes = (opts or {}).get("holes", True)
    inner = (opts or {}).get("inner_loops", False)
    if inner:
        # A Fornjot/OpenSCAD ``add_interiors`` record is an annulus: one outer
        # loop and one inner loop.  Using the new holes_xy path keeps the cap
        # triangulation clean and gives the correct genus-1 solid.
        return _spec("extrude_profile", [{
            "id": "body", "op": "extrude",
            "args": {"loop_xy": _ngon(R, 32), "height": T,
                     "holes_xy": [_ngon(max(0.6, 0.48 * R), 32)]},
        }])
    feats = [{"id": "body", "op": "extrude",
              "args": {"loop_xy": _ngon(R, 24), "height": T}}]
    if not holes:
        return _spec("extrude_profile", feats)
    feats += [{"id": "bore", "op": "cylinder",
               "args": {"radius": 0.28 * R, "height": T * 3.0}},
              {"id": "cut1", "op": "difference", "args": {},
               "inputs": ["body", "bore"]}]
    for i in range(6):
        a = 2 * math.pi * i / 6.0
        feats.append({"id": "b%d" % i, "op": "cylinder",
                      "args": {"radius": max(0.8, 0.09 * R), "height": T * 3.0,
                               "center": [0.72 * R * math.cos(a),
                                          0.72 * R * math.sin(a), 0.0]}})
    feats.append({"id": "out", "op": "difference", "args": {},
                  "inputs": ["cut1"] + ["b%d" % i for i in range(6)]})
    return _spec("extrude_profile", feats)


def t_polar_array_cut(d, opts=None):  # noqa: ARG001 - uniform template signature
    """body minus a polar array of cutters (flutes / knurl / slots)."""
    H, R = d["H"], d["R"]
    return _spec("polar_array_cut", [
        {"id": "body", "op": "cylinder", "args": {"radius": R, "height": H}},
        {"id": "c0", "op": "box",
         "args": {"size": [0.25 * R, 0.16 * R, H * 1.2],
                  "center": [0.95 * R, 0.0, 0.0]}},
        {"id": "ring", "op": "pattern_polar", "args": {"count": 12}, "inputs": ["c0"]},
        {"id": "out", "op": "difference", "args": {}, "inputs": ["body", "ring"]},
    ])


def t_mirror_union(d, opts=None):  # noqa: ARG001 - uniform template signature
    """symmetric part: half geometry + mirror + union."""
    L, W, H = d["L"], d["W"], d["H"]
    return _spec("mirror_union", [
        {"id": "half", "op": "box",
         "args": {"size": [0.5 * L, 0.6 * W, H], "center": [0.25 * L, 0, 0]}},
        {"id": "other", "op": "mirror", "args": {"plane": "x"}, "inputs": ["half"]},
        {"id": "arm", "op": "box", "args": {"size": [0.15 * L, W, 0.4 * H]}},
        {"id": "out", "op": "union", "args": {}, "inputs": ["half", "other", "arm"]},
    ])


def t_coons_thicken(d, opts=None):  # noqa: ARG001 - uniform template signature
    """open Coons patch thickened into a closed shell."""
    L, W, H = d["L"], d["W"], d["H"]
    # ``thicken`` offsets the surface outwards by the wall on both sides, so the
    # patch is inset by the wall thickness to land the final bbox on the target.
    t = max(1.2, 0.04 * min(L, W))
    L, W = max(2.0 * t + 1.0, L - t), max(2.0 * t + 1.0, W - t)
    n = 16
    x0, x1, y0, y1 = -L / 2, L / 2, -W / 2, W / 2
    xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
    ys = [y0 + (y1 - y0) * i / (n - 1) for i in range(n)]
    bulge = lambda y: H * math.sin(math.pi * (y - y0) / (y1 - y0))
    bottom = [[x, y0, 0.0] for x in xs]
    right = [[x1, y, bulge(y)] for y in ys]
    top = [[x, y1, 0.0] for x in reversed(xs)]
    left = [[x0, y, bulge(y)] for y in reversed(ys)]
    return _spec("coons_thicken", [
        {"id": "patch", "op": "coons", "args": {"edges": [bottom, right, top, left]}},
        {"id": "out", "op": "thicken", "args": {"thickness": t}, "inputs": ["patch"]},
    ])


def t_sphere_cone_csg(d, opts=None):
    """composite primitives: sphere + cone; bore only when the source has a hole.

    A through bore is part of the source's construction, not a default.  The
    earlier unconditional bore made the template invent genus-1 topology for
    genus-0 records.
    """
    R, H = d["R"], d["H"]
    body = [
        {"id": "s", "op": "sphere", "args": {"radius": R, "center": [0, 0, R]}},
        {"id": "c", "op": "cone",
         "args": {"r_bottom": R, "r_top": 0.2 * R, "height": H,
                  "center": [0, 0, H / 2.0]}},
        {"id": "body", "op": "union", "args": {}, "inputs": ["s", "c"]},
    ]
    if not (opts or {}).get("holes", False):
        return _spec("sphere_cone_csg", body)
    # Long enough to exit both the sphere (top 2R) and the cone (top H).
    top = max(2.0 * R, H)
    body += [
        {"id": "bore", "op": "cylinder",
         "args": {"radius": max(0.5, 0.25 * R), "height": 4.0 * top,
                  "center": [0, 0, top / 2.0]}},
        {"id": "out", "op": "difference", "args": {}, "inputs": ["body", "bore"]},
    ]
    return _spec("sphere_cone_csg", body)


def t_torus_ring(d, opts=None):  # noqa: ARG001 - uniform template signature
    """torus (a genus-1 primitive) with a smaller torus subtracted."""
    R = d["R"]
    r = max(0.8, min(0.18 * R, d.get("T") or 0.18 * R))
    return _spec("torus_ring", [
        {"id": "t", "op": "torus",
         "args": {"major_radius": R, "minor_radius": r}},
        {"id": "cut", "op": "torus",
         "args": {"major_radius": 0.55 * R, "minor_radius": 0.7 * r}},
        {"id": "out", "op": "difference", "args": {}, "inputs": ["t", "cut"]},
    ])


def t_gyroid_slab(d, opts=None):
    """Generic triply-periodic lattice: an implicit sheet cut from a slab.

    The record's declared ``period``/``thickness``/``shell`` and ``clip_radius``
    are consumed when present; the field name comes from the record's own
    vocabulary.  Topology is never read, so a genus match remains a prediction.
    """
    opts = opts or {}
    params = opts.get("params") or {}
    field = str(opts.get("field") or "gyroid")
    L = max(12.0, d.get("L") or 40.0)
    W = max(12.0, d.get("W") or 30.0)
    H = max(8.0, d.get("H") or 20.0)
    period = params.get("period") or params.get("cell_size") or params.get("scale_x")
    thickness = params.get("thickness") or params.get("shell")
    clip_radius = params.get("clip_radius")
    use_sdf = (field != "gyroid" or period is not None
               or thickness is not None or clip_radius is not None)
    if use_sdf:
        args = {"size": [L, W, H], "field": field, "resolution": 12}
        if period:
            args["period"] = max(6.0, float(period))
        if thickness:
            args["thickness"] = max(0.8, float(thickness))
        if clip_radius:
            args.update({"clip": "sphere",
                         "clip_radius": max(6.0, float(clip_radius))})
        return _spec("gyroid_slab", [{"id": "out", "op": "sdf", "args": args}])
    cell = max(6.0, min(L, W, H) * 0.6)
    thickness = max(0.8, min(0.14 * cell, 3.0))
    return _spec("gyroid_slab", [{
        "id": "out", "op": "gyroid",
        "args": {"size": [L, W, H], "cell_size": cell,
                 "thickness": thickness, "resolution": 10},
    }])


def t_sdf_field(d, opts=None):
    """A single implicit field (gyroid / Schwarz P / Neovius / Mandelbulb)."""
    opts = opts or {}
    params = opts.get("params") or {}
    field = str(opts.get("field") or "gyroid")
    L = max(12.0, d.get("L") or 40.0)
    W = max(12.0, d.get("W") or 30.0)
    H = max(8.0, d.get("H") or 20.0)
    args = {"size": [L, W, H], "field": field, "resolution": 12}
    period = params.get("period") or params.get("cell_size") or params.get("scale_x")
    thickness = params.get("thickness") or params.get("shell")
    if period:
        args["period"] = max(6.0, float(period))
    if thickness:
        args["thickness"] = max(0.8, float(thickness))
    if params.get("clip_radius"):
        args.update({"clip": "sphere",
                     "clip_radius": max(6.0, float(params["clip_radius"]))})
    return _spec("sdf_field", [{"id": "out", "op": "sdf", "args": args}])


def t_text_plate(d, opts=None):
    """Plate plus single-stroke text (engraved or embossed) on its top face."""
    L = max(20.0, d.get("L") or 60.0)
    W = max(12.0, d.get("W") or 30.0)
    T = max(1.0, d.get("T") or 4.0)
    text = str((opts or {}).get("text") or "ATTA")
    cap = min(0.42 * W, max(4.0, 0.55 * L / max(2, len(text))))
    depth = max(0.6, min(1.2, 0.4 * T))
    stroke = max(0.7, 0.09 * cap)
    return _spec("text_plate", [
        {"id": "plate", "op": "box",
         "args": {"size": [L, W, T], "center": [0, 0, T / 2.0]}},
        {"id": "txt", "op": "text_emboss",
         "args": {"text": text, "height": cap, "depth": depth,
                  "stroke_width": stroke, "at": [0, 0], "align": "center"}},
        {"id": "lift", "op": "transform",
         "args": {"translate": [0, 0, T]}, "inputs": ["txt"]},
        {"id": "out", "op": "union", "args": {}, "inputs": ["plate", "lift"]},
    ])


#: template id -> {build, covers, genus, dims}
#: ``dims`` is the *dimension contract*: which declared dimensions this
#: expression actually realises, and where they show up in the bbox.  A declared
#: dimension the template cannot realise is NOT a recovery error -- it is an
#: expressibility gap and is reported as ``dims_ignored``.
#:   ("x", 0, 1.0)          declared L shows up as bbox.x
#:   ("d", "max_xy", 1.0)   declared diameter shows up as max(bbox.x, bbox.y)
TEMPLATES_DIMS = {
    "box_csg": [("x", 0, 1.0), ("y", 1, 1.0), ("z", 2, 1.0)],
    "plate_holes": [("x", 0, 1.0), ("y", 1, 1.0), ("t", 2, 1.0)],
    "revolve_solid": [("d", "max_xy", 1.0), ("z", 2, 1.0)],
    "revolve_shell": [("d", "max_xy", 1.0), ("z", 2, 1.0)],
    "loft_stack": [("d", "max_xy", 1.0), ("z", 2, 1.0)],
    "sweep_path": [],
    "extrude_profile": [("d", "max_xy", 1.0), ("t", 2, 1.0)],
    "polar_array_cut": [("d", "max_xy", 1.0), ("z", 2, 1.0)],
    "mirror_union": [("x", 0, 1.0), ("y", 1, 1.0), ("z", 2, 1.0)],
    # thicken offsets the surface, so the realised bbox is not the declared one
    "coons_thicken": [],
    # bbox z is max(2R, H), so the declared height is only realised when
    # the cone is taller than the sphere: not a promise this expression can make.
    "sphere_cone_csg": [("d", "max_xy", 1.0)],
    "torus_ring": [],
    "gyroid_slab": [("x", 0, 1.0), ("y", 1, 1.0), ("z", 2, 1.0)],
    "sdf_field": [("x", 0, 1.0), ("y", 1, 1.0), ("z", 2, 1.0)],
    "text_plate": [("x", 0, 1.0), ("y", 1, 1.0), ("t", 2, 1.0)],
}

#: template id -> (build, covers, expected_genus|None, required_any)
#: ``required_any`` is the *constructive* evidence a record must carry before
#: this template is a legitimate stand-in.  Without it, a record that merely
#: shares ``transform``/``difference`` could be routed to an unrelated shape.
TEMPLATES = OrderedDict([
    ("box_csg", (t_box_csg, {"box", "cylinder", "difference", "hole", "pocket",
                             "union", "transform", "intersection"}, None,
                 {"box", "cube"})),
    ("plate_holes", (t_plate_holes, {"extrude", "cylinder", "difference", "hole",
                                     "pattern_linear", "box", "transform"}, None,
                     {"extrude", "plate", "panel"})),
    ("revolve_solid", (t_revolve_solid, {"revolve", "transform", "cylinder"}, None,
                       {"revolve", "lathe"})),
    ("revolve_shell", (t_revolve_shell, {"revolve", "difference", "shell", "hole",
                                         "cylinder", "thicken"}, 0,
                       {"revolve", "lathe"})),
    ("loft_stack", (t_loft_stack, {"loft", "transform", "sweep"}, None,
                    {"loft"})),
    ("sweep_path", (t_sweep_path, {"sweep", "loft", "transform"}, None,
                    {"sweep", "path"})),
    ("extrude_profile", (t_extrude_profile, {"extrude", "cylinder", "difference",
                                             "pattern_polar", "hole", "teeth",
                                             "transform"}, None,
                         {"extrude", "profile"})),
    ("polar_array_cut", (t_polar_array_cut, {"cylinder", "pattern_polar", "difference",
                                             "union", "knurl", "transform"}, 0,
                         {"pattern_polar", "polar", "array", "knurl"})),
    ("mirror_union", (t_mirror_union, {"box", "mirror", "union", "transform"}, 0,
                      {"mirror"})),
    ("coons_thicken", (t_coons_thicken, {"coons", "gordon", "thicken", "loft",
                                         "sweep", "transform"}, 0,
                       {"coons", "gordon", "thicken", "patch"})),
    ("sphere_cone_csg", (t_sphere_cone_csg, {"sphere", "cone", "cylinder", "union",
                                             "difference", "transform"}, None,
                         {"sphere", "cone"})),
    ("torus_ring", (t_torus_ring, {"torus", "difference", "transform"}, None,
                    {"torus"})),
    ("gyroid_slab", (t_gyroid_slab, {"gyroid", "lattice", "surface", "sdf",
                                     "transform"}, None,
                     {"lattice", "gyroid", "tpms", "schwarz", "surface", "sdf"})),
    ("sdf_field", (t_sdf_field, {"sdf", "lattice", "lipschitz", "tpms",
                                  "schwarz", "surface", "offset", "smooth",
                                  "morph", "mandelbulb", "iterate", "fractal"},
                   None,
                   {"sdf", "lipschitz", "mandelbulb", "iterate", "schwarz",
                    "tpms", "surface"})),
    ("text_plate", (t_text_plate, {"text_emboss", "text_engrave", "extrude",
                                   "union", "transform"}, None,
                    {"text", "text_emboss", "text_engrave", "vectortext",
                     "glyphcurve", "glyph"})),
])


def pick_template(record):
    """Choose the template whose op coverage best matches the record's own ops.

    Deterministic: highest covered fraction, ties broken by the template that
    covers more ops, then by id.  Never inspects the family name -- that would
    be a recipe in disguise.
    """
    ops = [_norm(o) for o in (record.get("operations") or []) if o]
    raw = [_norm(o) for o in (record.get("raw_ops") or []) if o]
    ref = set(ops) | set(raw)
    if not ref:
        return "box_csg", 0.0, ref
    best, best_score = None, -1.0
    for eligible_only in (True, False):
        for tid, (_fn, covers, _genus, requires) in TEMPLATES.items():
            if eligible_only and requires and not (ref & requires):
                continue
            hit = len(ref & covers)
            score = hit / float(len(ref))
            tiebreak = (score, hit, -len(TEMPLATES) + list(TEMPLATES).index(tid))
            if best is None or tiebreak > best_score:
                best, best_score = tid, tiebreak
        if best is not None:
            break
    return best or "box_csg", (best_score[0] if isinstance(best_score, tuple) else 0.0), ref


def classify_ops(record, template_id):
    """Split the record's reference ops into native / constructible / missing."""
    covers = TEMPLATES[template_id][1]
    ops = [_norm(o) for o in (record.get("operations") or []) if o]
    raw = [_norm(o) for o in (record.get("raw_ops") or []) if o]
    native, cons, missing, ignored = set(), set(), set(), set()
    for op in set(ops) | set(raw):
        if op in covers or op in NATIVE_OPS:
            native.add(op)
        elif op in CONSTRUCTIBLE_OPS:
            cons.add(op)
        elif op in MISSING_OPS:
            missing.add(op)
        else:
            ignored.add(op)
    total = len(native) + len(cons) + len(missing)
    return {"native": sorted(native), "constructible": sorted(cons),
            "missing": sorted(missing), "ignored": sorted(ignored), "total": total}


# ---------------------------------------------------------------- corpus
def load_corpus(root=ROOT):
    import glob
    rows = []
    for path in sorted(glob.glob(os.path.join(root, CORPUS_GLOB))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    rec["_source_file"] = os.path.relpath(path, root)
                    rows.append(rec)
    return rows


def corpus_state(rows):
    ids = sorted(str(r.get("id")) for r in rows if r.get("id"))
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return {"records": len(rows), "sorted_id_sha256": digest,
            "id_list_head": ids[:5], "files": len({r.get("_source_file") for r in rows})}


def accepted(rec):
    q = rec.get("quality") or {}
    return bool(q.get("license_ok")) and str(q.get("license_tier", "")).upper() in ("A", "B")


def substantive(rec):
    q = rec.get("quality") or {}
    return bool(q.get("has_params")) and bool(rec.get("parameters"))


def excluded_by(family):
    """Return the shipped recipe covering ``family``, or None (=> held out)."""
    fam = _norm(family)
    if not fam:
        return None
    for key, recipe in EXCLUDED_FAMILIES.items():
        if fam == key or fam.startswith(key + "."):
            return recipe
    return None


def expected_topology(rec):
    """Collect ``tests[].expect`` topology claims (closed / genus / components).

    A record marked ``topology_unverified`` keeps ``closed`` (it is a mesh
    property the source states) but drops genus/components: those two are
    derived values that have been shown to be wrong for recursive fractals and
    multi-object text, and asserting them would turn a known metadata defect
    into a fake failure.
    """
    out = {}
    unverified = bool(rec.get("topology_unverified"))
    for t in (rec.get("tests") or []):
        expect = (t or {}).get("expect") or {}
        for key in ("closed", "genus", "components"):
            if key in ("genus", "components") and unverified:
                continue
            if expect.get(key) is not None and key not in out:
                out[key] = expect[key]
    return out


def reference_ops(rec):
    ops = [_norm(o) for o in (rec.get("operations") or []) if o]
    raw = [_norm(o) for o in (rec.get("raw_ops") or []) if o]
    return set(ops) | set(raw)


# ---------------------------------------------------------------- selection
def select_cases(rows, n=DEFAULT_N, seed=1102):
    """Deterministic stratified selection of held-out cases."""
    import random
    matrix = json.load(open(os.path.join(ROOT, MATRIX), encoding="utf-8"))
    quota_w = {c["id"]: int(c.get("target_examples") or 0) for c in matrix["categories"]}

    pool = []
    for rec in rows:
        fam = (rec.get("shape") or {}).get("family")
        cat = (rec.get("shape") or {}).get("category") or "other"
        if not fam or str(rec.get("kind")) not in GEOMETRIC_KINDS:
            continue
        if not accepted(rec):
            continue
        pool.append(rec)

    held, variants = [], []
    for rec in pool:
        fam = (rec.get("shape") or {}).get("family")
        covered = excluded_by(fam)
        if covered is None:
            held.append((rec, None))
        elif substantive(rec) and _distinct_params(rec, covered):
            variants.append((rec, covered))

    # proportional quotas over categories that actually have held-out material
    cats = Counter((r.get("shape") or {}).get("category") or "other" for r, _ in held)
    total_w = sum(quota_w.get(c, 5) for c in cats) or 1
    alloc, remainders = {}, []
    for c in cats:
        exact = n * quota_w.get(c, 5) / float(total_w)
        alloc[c] = int(math.floor(exact))
        remainders.append((exact - math.floor(exact), c))
    short = n - sum(alloc.values())
    for _, c in sorted(remainders, reverse=True)[:max(0, short)]:
        alloc[c] += 1

    rng = random.Random(seed)
    def ranked(items):
        # prefer records that declare parameters and carry a topology claim, then
        # quality, then a stable id order; shuffle within equal ranks so the
        # sample is not always the alphabetically-first records.
        out = []
        for rec, covered in items:
            q = rec.get("quality") or {}
            rank = (2 if substantive(rec) else 0) + (1 if expected_topology(rec) else 0)
            out.append((rank, float(q.get("quality_score") or 0.0), rec.get("id"), rec, covered))
        rng.shuffle(out)
        out.sort(key=lambda t: (-t[0], -t[1], str(t[2])))
        return out

    by_cat = {}
    for rec, covered in held:
        by_cat.setdefault((rec.get("shape") or {}).get("category") or "other", []).append((rec, covered))
    selected = []
    for c, items in by_cat.items():
        selected.extend((t[3], t[4]) for t in ranked(items)[:alloc.get(c, 0)])

    # top up to n from the strongest leftovers (any category)
    leftover = [(t[3], t[4]) for c, items in by_cat.items()
                for t in ranked(items)[alloc.get(c, 0):]]
    leftover.sort(key=lambda t: (-(2 if substantive(t[0]) else 0),
                                 -float((t[0].get("quality") or {}).get("quality_score") or 0),
                                 str(t[0].get("id"))))
    while len(selected) < n and leftover:
        selected.append(leftover.pop(0))

    # variants of shipped families: at most 30% of the final set
    max_variants = int(MAX_VARIANT_FRACTION * n)
    want_variants = min(max_variants, max(0, n - len(selected)), len(variants))
    if selected and want_variants:
        vsel = [(t[3], t[4]) for t in ranked(variants)[:want_variants]]
        selected = selected[: n - len(vsel)] + vsel

    selected.sort(key=lambda t: (str((t[0].get("shape") or {}).get("category")),
                                 str(t[0].get("id"))))
    return selected


def _distinct_params(rec, covered_recipe):
    """True when a shipped-family variant declares dims its presets do not use."""
    try:
        from atta import recipes
        rec_obj = recipes.get(covered_recipe)
    except Exception:
        return False
    dims, _used = declared_dims(rec)
    if not dims:
        return False
    seen = []
    for preset in ("safe", "nominal", "hero"):
        try:
            resolved = rec_obj.resolve(preset=preset)
            seen.append({k: v for k, v in resolved.display.items()
                         if isinstance(v, (int, float)) and not isinstance(v, bool)})
        except Exception:
            continue
    if not seen:
        return True
    for axis, val in dims.items():
        if not any(_close_any(val, s.values()) for s in seen):
            return True
    return False


def _close_any(value, numbers, tol=0.03):
    return any(abs(value - float(x)) <= max(tol * abs(value), 0.5)
               for x in numbers if isinstance(x, (int, float)))


def _implicit_field(rec):
    """Pick the implicit field a record's own words name (never its topology)."""
    blob = " ".join([rec.get("title") or "", rec.get("description") or "",
                     " ".join(str(o) for o in (rec.get("raw_ops") or []))]).lower()
    for name in ("mandelbulb", "schwarz", "neovius", "gyroid"):
        if name in blob:
            return "schwarz_p" if name == "schwarz" else name
    return "gyroid"


def build_case(rec, idx):
    """Turn one corpus record into an executable benchmark case."""
    fam = (rec.get("shape") or {}).get("family")
    cat = (rec.get("shape") or {}).get("category") or "other"
    covered = excluded_by(fam)
    tid, score, ref = pick_template(rec)
    ops = classify_ops(rec, tid)
    dims, used = declared_dims(rec)
    if dims:
        tdims = template_dims(dims)
        dims_source = "declared"
    else:
        tdims = fallback_dims(rec.get("id"))
        dims_source = "fallback(not declared; recovery excluded)"
    topo = expected_topology(rec)
    raw_ops = {_norm(o) for o in (rec.get("raw_ops") or [])}
    text_value = "ATTA"
    for param in rec.get("parameters") or []:
        if isinstance(param, dict) and str(param.get("name")).lower() in (
                "text", "label", "string"):
            val = param.get("value")
            if isinstance(val, str) and val.strip():
                text_value = val.strip()[:24]
                break
    template_opts = {
        "holes": hole_evidence(rec),
        "inner_loops": bool(raw_ops & {"add_interiors", "inner", "interior",
                                       "add_interior"}),
        "text": text_value,
        # Declared parameter values are evidence about the reference part; a
        # template may consume them (period, thickness, clip radius), but never
        # the expected topology.
        "params": {str(p.get("name")): p.get("default")
                   for p in (rec.get("parameters") or [])
                   if isinstance(p, dict) and isinstance(p.get("default"), (int, float))
                   and not isinstance(p.get("default"), bool)},
        "field": _implicit_field(rec),
    }
    try:
        spec = TEMPLATES[tid][0](tdims, template_opts)
        spec_err = None
    except Exception as exc:                                   # noqa: BLE001
        spec, spec_err = None, "%s: %s" % (type(exc).__name__, exc)
    return {
        "id": "%s_%02d" % (BENCH_VERSION, idx),
        "corpus_id": rec.get("id"),
        "corpus_file": rec.get("_source_file"),
        "family": fam,
        "category": cat,
        "kind": rec.get("kind"),
        "title": (rec.get("title") or "")[:120],
        "held_out": covered is None,
        "covered_by_recipe": covered,
        "variant_of_shipped": covered is not None,
        "declared_params": len([p for p in (rec.get("parameters") or []) if isinstance(p, dict)]),
        "declared_dims_mm": dims,
        "declared_dims_source": used,
        "dims_source": dims_source,
        "template": tid,
        "spec_used": tid,
        "template_match": round(score, 3),
        "reference_ops": sorted(ref),
        "ops_native": ops["native"],
        "ops_constructible": ops["constructible"],
        "ops_missing": ops["missing"],
        "ops_ignored": ops["ignored"],
        "op_coverage": round((len(ops["native"]) + len(ops["constructible"]))
                             / max(1, ops["total"]), 4),
        "op_coverage_native": round(len(ops["native"]) / max(1, ops["total"]), 4),
        "expected_topology": topo,
        "topology_unverified": bool(rec.get("topology_unverified")),
        "spec": spec,
        "spec_error": spec_err,
        "model_id": None,
        "build_success": False,
        "verify_verdict": None,
        "verify_pass": False,
        "faithful_build_success": False,
        "params_match": None,
        "dims_ignored": [],
        "param_recovery_pct": None,
        "param_recovery_mm": None,
        "seconds": None,
        "notes": "",
    }


# ---------------------------------------------------------------- execution
def case_constraints(case):
    """Constraints derived from the *record*, not from what the build produced."""
    cons = {"closed": True}
    topo = case.get("expected_topology") or {}
    if topo.get("genus") is not None:
        cons["genus"] = int(topo["genus"])
    if topo.get("components") is not None:
        cons["n_components"] = int(topo["components"])
    # bbox windows only for dimensions this expression actually realises; a
    # declared dimension the template cannot consume is an expressibility gap
    # (reported as dims_ignored), not a verify failure.
    dims = case.get("declared_dims_mm") or {}
    contract = TEMPLATES_DIMS.get(case.get("template"), [])
    bbox = {}
    for axis, where, _factor in contract:
        target = dims.get(axis)
        if not target:
            continue
        window = [target * 0.98 - 0.5, target * 1.02 + 0.5]
        if where == "max_xy":
            # a radial dimension bounds both x and y from above
            bbox["x"] = window
            bbox["y"] = window
        else:
            bbox[{"0": "x", "1": "y", "2": "z"}[str(where)]] = window
    if bbox:
        cons["bbox"] = bbox
    return cons


class _Timeout(Exception):
    pass


def _alarm(signum, frame):                                     # noqa: ARG001
    raise _Timeout("case budget exceeded")


def run_case(case, root=ROOT):
    from atta import engine

    t0 = time.perf_counter()
    if case.get("spec") is None:
        case["notes"] = "no spec: %s" % case.get("spec_error")
        case["seconds"] = round(time.perf_counter() - t0, 3)
        return case
    old = None
    try:
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(CASE_BUDGET_S)
        out = engine.model(spec=case["spec"], quality="draft")
        case["model_id"] = out.get("model_id")
        case["build_success"] = bool(case["model_id"])
        if case["build_success"]:
            receipt = engine.verify(model_id=case["model_id"], level="standard",
                                    constraints=case_constraints(case))
            case["verify_verdict"] = receipt.get("verdict")
            case["verify_pass"] = receipt.get("verdict") == "pass"
            case["verify_checks_failed"] = [c["id"] for c in receipt.get("checks") or []
                                            if not c.get("ok")]
            facts = receipt.get("facts") or {}
            bbox = (facts.get("bbox_mm") or {})
            case["measured_bbox_mm"] = bbox.get("size")
            # The fact card carries genus / n_components at the top level.  Read
            # them strictly: a missing key must raise, not silently become None,
            # or a schema change would manufacture phantom topology failures.
            if "genus" not in facts or "n_components" not in facts:
                raise RuntimeError("verify facts missing genus/n_components: keys=%s"
                                   % sorted(facts))
            case["measured_genus"] = facts["genus"]
            case["measured_components"] = facts["n_components"]
            worst, ignored = _recovery(case, bbox.get("size"))
            case["params_match"] = worst
            case["dims_ignored"] = ignored
            if worst:
                case["param_recovery_pct"] = worst["pct"]
                case["param_recovery_mm"] = worst["mm"]
            topo = case.get("expected_topology") or {}
            topo_ok = True
            if topo.get("genus") is not None:
                topo_ok = case.get("measured_genus") == int(topo["genus"])
            if topo.get("components") is not None:
                topo_ok = topo_ok and case.get("measured_components") == int(topo["components"])
            case["topology_match"] = bool(topo_ok)
            case["faithful_build_success"] = bool(
                case["build_success"] and not case.get("ops_missing") and topo_ok)
    except _Timeout:
        case["notes"] = "timeout after %ds" % CASE_BUDGET_S
        case["verify_verdict"] = "timeout"
    except Exception as exc:                                   # noqa: BLE001
        case["notes"] = ("%s: %s" % (type(exc).__name__, exc))[:400]
        case["build_success"] = bool(case.get("model_id"))
    finally:
        try:
            signal.alarm(0)
            if old is not None:
                signal.signal(signal.SIGALRM, old)
        except Exception:
            pass
    case["seconds"] = round(time.perf_counter() - t0, 3)
    return case


def _recovery(case, size_mm):
    """Compare the measured bbox against the declared dims the graph *asked for*.

    Only dimensions in the template's ``dims`` contract are compared: those are
    the ones the graph actually feeds into geometry, so a mismatch there is a
    genuine fidelity bug (unit conversion, boolean survival, op behaviour).
    Declared dimensions the expression cannot realise are counted separately as
    ``dims_ignored`` -- they are an expressibility gap, not a fidelity error.
    """
    dims = case.get("declared_dims_mm") or {}
    if not size_mm:
        return None, []
    contract = TEMPLATES_DIMS.get(case.get("template"), [])
    rows, ignored = [], []
    for axis, where, factor in contract:
        target = dims.get(axis)
        if not target:
            continue
        if where == "max_xy":
            got = max(float(size_mm[0]), float(size_mm[1]))
        else:
            got = float(size_mm[int(where)])
        err_mm = abs(got - target)
        rows.append({"axis": axis, "target": round(target, 3),
                     "measured": round(got, 3), "mm": round(err_mm, 4),
                     "pct": round(100.0 * err_mm / target if target else 0.0, 3)})
    consumed = {r[0] for r in contract}
    for axis in ("x", "y", "z", "d", "r", "t"):
        if dims.get(axis) and axis not in consumed:
            ignored.append(axis)
    if not rows:
        return None, ignored
    worst = max(rows, key=lambda r: r["pct"])
    return worst, ignored


# ---------------------------------------------------------------- aggregate
def summarize(cases, state, root=ROOT):
    n = len(cases)
    built = [c for c in cases if c.get("build_success")]
    passed = [c for c in cases if c.get("verify_pass")]
    faithful = [c for c in cases if c.get("faithful_build_success")]
    recs = [c for c in cases if c.get("param_recovery_pct") is not None]
    op_cov = [c["op_coverage"] for c in cases if c.get("op_coverage") is not None]
    op_nat = [c["op_coverage_native"] for c in cases if c.get("op_coverage_native") is not None]
    variants = [c for c in cases if c.get("variant_of_shipped")]

    missing = Counter()
    for c in cases:
        for op in c.get("ops_missing") or []:
            missing[op] += 1
    failed = Counter()
    for c in cases:
        if c.get("verify_pass"):
            continue
        if not c.get("build_success"):
            failed["build:" + (c.get("notes") or "unknown").split(":")[0][:40]] += 1
        elif c.get("verify_verdict") == "degraded":
            failed["verify:degraded"] += 1
        elif c.get("verify_verdict") == "fail":
            for cid in (c.get("verify_checks_failed") or ["fail"]):
                failed["check:" + cid] += 1
        elif c.get("verify_verdict") == "timeout":
            failed["timeout"] += 1
        else:
            failed["other"] += 1

    avg_rec = (sum(c["param_recovery_pct"] for c in recs) / len(recs)) if recs else None
    within = [c for c in recs
              if c["param_recovery_pct"] <= TARGETS["param_recovery_pct"]
              or c["param_recovery_mm"] <= TARGETS["param_recovery_mm"]]

    # family coverage: categories with a shipped recipe (measured, not asserted)
    fams = {}
    try:
        from atta import recipes
        matrix = json.load(open(os.path.join(root, MATRIX), encoding="utf-8"))
        cat_ids = [c["id"] for c in matrix["categories"]]
        for cid in cat_ids:
            fams[cid] = sorted(rid for rid in recipes.REGISTRY
                               if rid.split(".")[0] == cid
                               or (cid == "mechanical" and rid.startswith(("mechanical.", "mechanism."))))
        covered_cats = [k for k, v in fams.items() if v]
    except Exception:                                          # noqa: BLE001
        covered_cats = []

    # reuse: variants available in the corpus per shipped family
    try:
        rows = load_corpus(root)
    except Exception:                                          # noqa: BLE001
        rows = []
    reuse = {}
    for key, recipe in EXCLUDED_FAMILIES.items():
        if recipe in reuse:
            continue
        reuse[recipe] = sum(1 for r in rows
                            if accepted(r)
                            and excluded_by((r.get("shape") or {}).get("family")) == recipe)

    summary = {
        "cases": n,
        "variants_of_shipped_families": len(variants),
        "variant_pool_available": None,
        "variant_fraction": round(len(variants) / float(n), 4) if n else 0.0,
        "held_out_cases": len([c for c in cases if c.get("held_out")]),
        "categories_covered": len({c.get("category") for c in cases}),
        "build_success": round(len(built) / float(n), 4) if n else 0.0,
        "faithful_build_success": round(len(faithful) / float(n), 4) if n else 0.0,
        "verify_pass": round(len(passed) / float(n), 4) if n else 0.0,
        "verify_pass_of_built": round(len(passed) / float(len(built)), 4) if built else 0.0,
        "param_recovery_avg_pct": round(avg_rec, 3) if avg_rec is not None else None,
        "param_recovery_max_pct": round(max((c["param_recovery_pct"] for c in recs), default=0.0), 3),
        "param_recovery_within_target": round(len(within) / float(len(recs)), 4) if recs else None,
        "param_recovery_cases": len(recs),
        "dims_ignored_cases": len([c for c in cases if c.get("dims_ignored")]),
        "dims_ignored_axes": Counter(a for c in cases for a in (c.get("dims_ignored") or [])).most_common(),
        "op_coverage": round(sum(op_cov) / len(op_cov), 4) if op_cov else 0.0,
        "op_coverage_native": round(sum(op_nat) / len(op_nat), 4) if op_nat else 0.0,
        "op_coverage_cases": len(op_cov),
        "missing_ops_top": missing.most_common(15),
        "family_coverage": "%d/%d" % (len(covered_cats), len(fams) or 12),
        "family_coverage_categories": covered_cats,
        "reuse": {k: {"variants": v, "meets_5": v >= 5} for k, v in sorted(reuse.items())},
        "reuse_rate": round(sum(1 for v in reuse.values() if v >= 5) / float(len(reuse)), 4) if reuse else 0.0,
        "failure_modes": failed.most_common(15),
        "seconds_total": round(sum(c.get("seconds") or 0 for c in cases), 2),
        "targets": TARGETS,
        "vs_targets": {
            "build_success": _target_row(summary_placeholder(len(built), n),
                                        TARGETS["build_success"], ">="),
            "verify_pass": _target_row(summary_placeholder(len(passed), n),
                                       TARGETS["verify_pass"], ">="),
            "op_coverage": _target_row(sum(op_cov) / len(op_cov) if op_cov else 0.0,
                                       TARGETS["op_coverage"], ">="),
            "family_coverage": {"measured": "%d/%d" % (len(covered_cats), len(fams) or 12),
                                "target": "9/12",
                                "met": len(covered_cats) >= 9},
            "param_recovery": {"measured_avg_pct": round(avg_rec, 3) if avg_rec is not None else None,
                               "target": "<=2% or <=0.5mm",
                               "met": (avg_rec is not None
                                       and avg_rec <= TARGETS["param_recovery_pct"])},
            "reuse": {"measured_rate": summary_placeholder(
                          sum(1 for v in reuse.values() if v >= 5), len(reuse)),
                      "target": 1.0,
                      "met": bool(reuse) and all(v >= 5 for v in reuse.values())},
        },
        "corpus_state": state,
        "metric_definitions": METRIC_DEFS,
    }
    return summary


def summary_placeholder(num, den):
    return round(float(num) / den, 4) if den else 0.0


def _target_row(measured, target, op):
    if op == ">=":
        return {"measured": measured, "target": target, "met": measured >= target}
    return {"measured": measured, "target": target, "met": measured <= target}


# ---------------------------------------------------------------- cli
def regenerate(n=DEFAULT_N, seed=1102):
    rows = load_corpus()
    state = corpus_state(rows)
    picked = select_cases(rows, n=n, seed=seed)
    cases = [build_case(rec, i) for i, (rec, _cov) in enumerate(picked)]
    return cases, state


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default=os.path.join(ROOT, "corpus/bench", BENCH_VERSION + ".json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--regenerate", action="store_true",
                    help="rebuild the case list from corpus/normalized/*.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1102)
    ap.add_argument("--cases-count", type=int, default=DEFAULT_N)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    out = args.out or args.cases
    if args.regenerate:
        cases, state = regenerate(n=args.cases_count, seed=args.seed)
        doc = {"bench": BENCH_VERSION, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                    time.gmtime()),
               "protocol": protocol_doc(), "cases": cases}
    else:
        doc = json.load(open(args.cases, encoding="utf-8"))
        cases, state = doc["cases"], (doc.get("summary") or {}).get("corpus_state") or {}
        rows = load_corpus()
        state = corpus_state(rows)
        doc["protocol"] = protocol_doc()

    todo = cases[: args.limit] if args.limit else cases
    for i, case in enumerate(todo, 1):
        run_case(case)
        if not args.quiet:
            print("[%3d/%3d] %-24s %-16s %-8s %-9s %5.2fs %s" % (
                i, len(todo), str(case.get("family"))[:24], case.get("template"),
                "built" if case.get("build_success") else "NOBUILD",
                case.get("verify_verdict") or "-", case.get("seconds") or 0.0,
                (case.get("notes") or "")[:70]), flush=True)

    if args.limit:
        # a --limit run is a smoke test: the artifact must describe what ran
        cases = todo
    doc["cases"] = cases
    doc["summary"] = summarize(cases, state)
    failures = [{"corpus_id": c.get("corpus_id"), "family": c.get("family"),
                 "template": c.get("template"), "stage": _stage(c),
                 "error": (c.get("notes") or "")[:300],
                 "failed_checks": c.get("verify_checks_failed"),
                 "missing_ops": c.get("ops_missing")}
                for c in cases if not c.get("verify_pass")]
    doc["failures"] = failures
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)

    s = doc["summary"]
    print("\n== %s  n=%d  (%.1fs)" % (BENCH_VERSION, s["cases"], s["seconds_total"]))
    print("build_success            %.1f%%  (faithful %.1f%%)" % (
        100 * s["build_success"], 100 * s["faithful_build_success"]))
    print("verify_pass              %.1f%%  (%.1f%% of built)" % (
        100 * s["verify_pass"], 100 * s["verify_pass_of_built"]))
    print("param_recovery avg       %s%%  within-target %s" % (
        s["param_recovery_avg_pct"], s["param_recovery_within_target"]))
    print("op_coverage              %.1f%%  (native %.1f%%)" % (
        100 * s["op_coverage"], 100 * s["op_coverage_native"]))
    print("family_coverage          %s" % s["family_coverage"])
    print("reuse rate               %.2f" % s["reuse_rate"])
    print("failure modes:", s["failure_modes"][:8])
    print("top missing ops:", s["missing_ops_top"][:8])
    print("wrote", out)
    return 0


def _stage(case):
    if not case.get("build_success"):
        return "build"
    if case.get("verify_verdict") == "timeout":
        return "timeout"
    if case.get("verify_verdict") == "degraded":
        return "verify_degraded"
    if case.get("verify_verdict") == "fail":
        return "verify_fail"
    return "express"


def protocol_doc():
    return {
        "name": BENCH_VERSION,
        "requirement": "docs/SHAPE_COVERAGE_REQUIREMENTS.md §9",
        "surface": "generic Spec v3 feature graph (atta.spec); shipped recipes are never called",
        "quality": "draft",
        "case_budget_s": CASE_BUDGET_S,
        "n_cases": DEFAULT_N,
        "stratification": "categories proportional to docs/coverage_matrix.json target_examples",
        "held_out_rule": ("a case is held out when shape.family does not map to a shipped "
                          "recipe (see excluded_families); shipped-family variants are "
                          "<= %.0f%% of the set and use parameter values no preset uses"
                          % (100 * MAX_VARIANT_FRACTION)),
        "excluded_families": {k: v for k, v in EXCLUDED_FAMILIES.items()},
        "native_ops": sorted(NATIVE_OPS),
        "constructible_ops": CONSTRUCTIBLE_OPS,
        "missing_ops": MISSING_OPS,
        "templates": {k: {"covers": sorted(v[1]), "declared_genus": v[2]}
                      for k, v in TEMPLATES.items()},
        "metrics": METRIC_DEFS,
        "verify_constraints": ("closed + n_components/genus from the record's "
                               "tests[].expect + bbox windows from the record's "
                               "declared parameter defaults (+/-2% or 0.5 mm)"),
        "honesty_notes": [
            "param_recovery is near-tautological: the graph is parameterised from the "
            "same declared numbers it is scored against, so it measures dimensional "
            "fidelity of the spec pipeline, not generalization.",
            "build_success only means a model_id came back; faithful_build_success adds "
            "'all reference ops expressible' and 'declared topology matched'.",
            "degraded verify is counted as a failure.",
        ],
    }


if __name__ == "__main__":
    sys.exit(main())
