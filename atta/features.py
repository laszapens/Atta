# -*- coding: utf-8 -*-
"""Feature DAG evaluation.  Feature arguments are **millimetres** (or the
unit given to ``evaluate``); the evaluator converts lengths to metres before
calling the geometry kernel.  Angles stay in degrees.

The graph is validated before geometry runs: unique ids, existing inputs,
acyclic dependencies, known ops and allowed arguments.  Every failure raises
``AttaError`` with a JSON path and an executable fix.
"""

from __future__ import annotations

import ast
import math

import numpy as np

from . import boolean as _B
from . import primitives as _P
from . import surfaces as _S
from .errors import AttaError
from .mesh import Mesh

__all__ = ["FEATURE_OPS", "evaluate", "register_op", "extra_ops"]

_UNIT = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
          "pi": math.pi, "min": min, "max": max, "abs": abs, "round": round}

#: op name -> callable, for ops registered by family modules at import time
_EXTRA_OPS: dict = {}

FEATURE_OPS = {
    "box": {"required": ("size",), "optional": ("center", "align"), "arity": 0,
            "doc": "Axis-aligned box; size=[x,y,z] mm."},
    "cylinder": {"required": ("radius", "height"),
                 "optional": ("center", "axis", "segments", "caps"), "arity": 0,
                 "doc": "Solid cylinder along axis (default z)."},
    "sphere": {"required": ("radius",), "optional": ("center", "segments", "rings"),
               "arity": 0, "doc": "UV sphere."},
    "cone": {"required": ("r_bottom", "r_top", "height"),
             "optional": ("center", "axis", "segments"), "arity": 0,
             "doc": "Cone/frustum; a zero radius becomes an apex."},
    "torus": {"required": ("major_radius", "minor_radius"),
              "optional": ("center", "axis", "seg_major", "seg_minor"), "arity": 0,
              "doc": "Torus."},
    "revolve": {"required": ("profile_rz",), "optional": ("segments", "axis", "caps"),
                "arity": 0, "doc": "Revolve [r,z] profile about the axis."},
    "loft": {"required": ("rings",), "optional": ("closed_ring", "caps"), "arity": 0,
             "doc": "Loft equally sized 3D rings."},
    "sweep": {"required": ("section_xy", "path"),
              "optional": ("closed", "twist_deg", "scale_end"), "arity": 0,
              "doc": "Sweep a 2D section along a 3D path."},
    "coons": {"required": ("edges",), "optional": ("u_samples", "v_samples", "flip"),
              "arity": 0, "doc": "Open Coons patch from (bottom,right,top,left)."},
    "gordon": {"required": ("u_curves", "v_curves"),
               "optional": ("u_samples", "v_samples", "flip"), "arity": 0,
               "doc": "Open Gordon patch from two curve families."},
    "extrude": {"required": ("loop_xy", "height"),
                "optional": ("direction", "caps", "holes_xy"), "arity": 0,
                "doc": "Extrude a closed 2D loop; holes_xy cuts inner loops."},
    "thicken": {"required": ("thickness",), "optional": ("offset",), "arity": 1,
                "doc": "Shell an open surface input."},
    "lobes": {"required": (), "optional": ("count", "amplitude", "phase_deg", "twist_deg"),
              "arity": 1, "doc": "Radial petal modulation of a solid input."},
    "union": {"required": (), "optional": (), "arity": 2,
              "doc": "Union two or more closed inputs."},
    "difference": {"required": (), "optional": (), "arity": 2,
                   "doc": "Subtract later inputs from the first."},
    "intersection": {"required": (), "optional": (), "arity": 2,
                     "doc": "Intersect two or more closed inputs."},
    "mirror": {"required": (), "optional": ("plane",), "arity": 1,
               "doc": "Mirror the input across a principal plane."},
    "pattern_linear": {"required": ("count", "spacing"), "optional": ("axis",), "arity": 1,
                       "doc": "Linear array of the input."},
    "pattern_polar": {"required": ("count",),
                      "optional": ("axis", "angle_deg", "center"), "arity": 1,
                      "doc": "Polar array of the input."},
    "transform": {"required": (),
                  "optional": ("translate", "rotate_deg", "scale", "pivot"), "arity": 1,
                  "doc": "Translate/rotate/scale the input about a pivot."},
}


def extra_ops() -> dict:
    """Ops contributed by family modules (name -> callable)."""
    return dict(_EXTRA_OPS)


#: Declared argument *kinds*, so spec validation can reject a bad arg before
#: geometry runs.  ``evaluate`` has always enforced these (``_num``/``_mat``/
#: ``vec``/``integer``/``flag``); the point of this table is that
#: ``spec.validate_spec`` can apply the same rules without evaluating.
#:
#: Kinds: number (finite scalar), int (whole, optionally bounded), length
#: (finite scalar in model units), vec3 (3 finite numbers), vec3_free (3 finite
#: numbers, not unit-scaled), mat2 (n>=2 x 2), mat3 (n>=3 x 3), rings (>=2
#: mat3), rings2 (>=2 x 2), loops (>=1 mat2), curves (>=2 mat3), curves4
#: (exactly 4 mat3), bool, enum (any string), enum:<a|b|...>, strings (list of
#: strings), points (>=1 vec3-or-vec2), anything (defer to the op).
_ARG_TYPES = {
    "box": {"size": "vec3", "center": "vec3", "align": "anything"},
    "cylinder": {"radius": "length", "height": "length", "center": "vec3",
                 "axis": "enum:x|y|z", "segments": ("int", 3), "caps": "bool"},
    "sphere": {"radius": "length", "center": "vec3", "segments": ("int", 3),
               "rings": ("int", 2)},
    "cone": {"r_bottom": "length", "r_top": "length", "height": "length",
             "center": "vec3", "axis": "enum:x|y|z", "segments": ("int", 3)},
    "torus": {"major_radius": "length", "minor_radius": "length",
              "center": "vec3", "axis": "enum:x|y|z",
              "seg_major": ("int", 3), "seg_minor": ("int", 3)},
    "revolve": {"profile_rz": "mat2", "segments": ("int", 3),
                "axis": "enum:x|y|z", "caps": "bool"},
    "loft": {"rings": "rings", "closed_ring": "bool", "caps": "bool"},
    "sweep": {"section_xy": "mat2", "path": "mat3", "closed": "bool",
              "twist_deg": "number", "scale_end": "number"},
    "coons": {"edges": "curves4", "u_samples": ("int", 2),
              "v_samples": ("int", 2), "flip": "bool"},
    "gordon": {"u_curves": "curves", "v_curves": "curves",
               "u_samples": ("int", 2), "v_samples": ("int", 2),
               "flip": "bool"},
    "extrude": {"loop_xy": "mat2", "height": "length",
                "direction": "vec3_free", "caps": "bool", "holes_xy": "loops"},
    "thicken": {"thickness": "length", "offset": "number"},
    "lobes": {"count": ("int", 0), "amplitude": "number",
              "phase_deg": "number", "twist_deg": "number"},
    "union": {}, "difference": {}, "intersection": {},
    "mirror": {"plane": "enum:x|y|z"},
    "pattern_linear": {"count": ("int", 1), "spacing": "length",
                       "axis": "enum:x|y|z"},
    "pattern_polar": {"count": ("int", 1), "axis": "enum:x|y|z",
                      "angle_deg": "number", "center": "vec3"},
    "transform": {"translate": "vec3", "rotate_deg": "vec3_free",
                  "scale": "anything", "pivot": "vec3"},
}


def arg_kinds(op: str) -> dict:
    """Declared arg kinds for ``op`` (empty when nothing is declared).

    Family ops declare theirs via ``register_op(..., args={...})``; built-ins
    come from :data:`_ARG_TYPES`.  A missing entry means "not declared", and
    validation then only checks that the key is allowed.
    """
    entry = FEATURE_OPS.get(str(op)) or {}
    declared = entry.get("args")
    if isinstance(declared, dict):
        return dict(declared)
    return dict(_ARG_TYPES.get(str(op)) or {})

def register_op(name, spec, fn):
    """Register a geometry op contributed by a family module.

    ``spec`` uses the same shape as :data:`FEATURE_OPS` entries
    (``required``/``optional``/``arity``/``doc``).  Argument validation,
    unit conversion and graph wiring are handled by the core, so a new op
    only has to map validated keywords to geometry.
    """
    name = str(name)
    if not name or not name.replace("_", "").isalnum():
        raise AttaError("bad_arg", "op name must be an identifier", path="op",
                        fix="use snake_case, e.g. text_engrave")
    if name in FEATURE_OPS and name not in _EXTRA_OPS:
        raise AttaError("duplicate_op", "op %r is already a built-in" % name,
                        path="op", fix="pick a new name")
    spec = dict(spec or {})
    if "required" not in spec:
        raise AttaError("bad_arg", "op %r needs a 'required' tuple" % name,
                        path="op.spec", fix="pass required=() for a no-arg op")
    if spec.get("arity", 0) not in (0, 1, 2):
        raise AttaError("bad_arg", "op %r has bad arity" % name, path="op.spec",
                        fix="arity must be 0, 1 or 2")
    entry = {"required": tuple(spec["required"]),
             "optional": tuple(spec.get("optional", ())),
             "arity": int(spec.get("arity", 0)),
             "doc": str(spec.get("doc", ""))}
    for key in ("lengths", "vectors", "ints"):
        if spec.get(key):
            entry[key] = tuple(spec[key])
    # Declared arg kinds let spec.validate_spec type-check args without
    # building geometry.  An op may pass ``args={name: kind}`` verbatim, or
    # let the kinds be derived from lengths/vectors/ints.
    kinds = dict(spec.get("args") or {})
    for key in spec.get("lengths") or ():
        kinds.setdefault(key, "length")
    for key in spec.get("vectors") or ():
        kinds.setdefault(key, "vec3")
    for key in spec.get("ints") or ():
        kinds.setdefault(key, ("int", 0))
    if kinds:
        entry["args"] = kinds
    FEATURE_OPS[name] = entry
    _EXTRA_OPS[name] = fn
    return fn


# ---------------------------------------------------------------- safe eval

def _eval_expr(expr, scope, path, allow_bool=False):
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        raise AttaError("bad_arg", "expression %r is not valid arithmetic: %s" % (expr, exc),
                        path=path, fix="use + - * / ** % and sqrt/sin/cos/tan/min/max/abs/round")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                raise AttaError("bad_arg", "expression has a boolean constant", path=path,
                                fix="use numeric or string literals")
            if isinstance(node.value, str):
                # enum params are first-class, so constraints must be able to
                # compare them: mode != "mitre"
                return node.value
            if not isinstance(node.value, (int, float)):
                raise AttaError("bad_arg", "expression has a non-numeric constant", path=path,
                                fix="use numeric or string literals")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in scope:
                return scope[node.id]
            if node.id in _FUNCS:
                return _FUNCS[node.id]
            raise AttaError("bad_arg", "expression uses unknown name %r" % node.id, path=path,
                            fix="use the feature params or pi")
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -ev(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +ev(node.operand)
            if allow_bool and isinstance(node.op, ast.Not):
                return not ev(node.operand)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            try:
                if isinstance(node.op, ast.Add):
                    return a + b
                if isinstance(node.op, ast.Sub):
                    return a - b
                if isinstance(node.op, ast.Mult):
                    return a * b
                if isinstance(node.op, ast.Div):
                    return a / b
                if isinstance(node.op, ast.Pow):
                    return a ** b
                if isinstance(node.op, ast.Mod):
                    return a % b
            except (ArithmeticError, ValueError) as exc:
                raise AttaError("bad_arg", "expression arithmetic failed: %s" % exc,
                                path=path, fix="check divisors and exponents") from exc
        if allow_bool and isinstance(node, ast.BoolOp):
            vals = [bool(ev(v)) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if allow_bool and isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                try:
                    if isinstance(op, ast.Lt):
                        ok = left < right
                    elif isinstance(op, ast.LtE):
                        ok = left <= right
                    elif isinstance(op, ast.Gt):
                        ok = left > right
                    elif isinstance(op, ast.GtE):
                        ok = left >= right
                    elif isinstance(op, ast.Eq):
                        ok = left == right
                    elif isinstance(op, ast.NotEq):
                        ok = left != right
                    else:
                        ok = False
                except TypeError as exc:
                    raise AttaError("bad_arg", "expression comparison failed: %s" % exc,
                                    path=path, fix="compare numbers only") from exc
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise AttaError("bad_arg", "expression calls a disallowed function", path=path,
                                fix="only sqrt/sin/cos/tan/min/max/abs/round are allowed")
            if node.keywords:
                raise AttaError("bad_arg", "expression calls cannot use keyword arguments",
                                path=path, fix="pass positional arguments")
            fn = _FUNCS[node.func.id]
            if not callable(fn):
                raise AttaError("bad_arg", "%s is a constant" % node.func.id, path=path,
                                fix="write pi directly")
            try:
                return fn(*[ev(a) for a in node.args])
            except (ArithmeticError, ValueError, TypeError) as exc:
                raise AttaError("bad_arg", "expression function failed: %s" % exc,
                                path=path, fix="check the function domain") from exc
        raise AttaError("bad_arg", "expression uses an unsupported operation", path=path,
                        fix="use + - * / ** % and sqrt/sin/cos/tan/min/max/abs/round")

    return ev(tree)


def _resolve(value, params, path):
    if isinstance(value, dict):
        if "$param" in value:
            key = str(value["$param"])
            if key not in params:
                raise AttaError("bad_arg", "unknown parameter %r" % key, path=path,
                                fix="available params: " + ", ".join(sorted(params)))
            return params[key]
        if "$expr" in value:
            return _eval_expr(value["$expr"], dict(params), path + ".$expr")
        return {str(k): _resolve(v, params, path + "." + str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve(v, params, path + "[%d]" % i) for i, v in enumerate(value)]
    return value


# ---------------------------------------------------------------- arguments

def _num(v, name, path):
    if isinstance(v, bool):
        raise AttaError("bad_arg", "%s must be a number" % name, path=path,
                        fix="remove the boolean value")
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise AttaError("bad_arg", "%s must be a number" % name, path=path,
                        fix="pass a numeric value") from exc
    if not math.isfinite(x):
        raise AttaError("bad_arg", "%s must be finite" % name, path=path,
                        fix="pass a finite value")
    return x


def _mat(v, name, cols, path, min_rows=2, scale=1.0):
    arr = np.asarray(v, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != cols or len(arr) < min_rows:
        raise AttaError("bad_arg", "%s must be (n>=%d,%d)" % (name, min_rows, cols),
                        path=path, fix="pass a rectangular array of coordinates")
    if not np.isfinite(arr).all():
        raise AttaError("bad_arg", "%s contains NaN or Inf" % name, path=path,
                        fix="remove non-finite coordinates")
    return arr * float(scale)


def _prepare(op, args, meshes, scale, path):
    spec = FEATURE_OPS[op]
    allowed = set(spec["required"]) | set(spec["optional"])
    for key in args:
        if key not in allowed:
            raise AttaError("bad_arg", "unexpected arg %r for %s" % (key, op),
                            path=path + ".args." + str(key),
                            fix="allowed args: " + (", ".join(sorted(allowed)) or "none"))

    def req(name):
        if name not in args:
            raise AttaError("bad_arg", "missing required arg %r for %s" % (name, op),
                            path=path + ".args", fix="add args.%s" % name)
        return args[name]

    def opt(name, default=None):
        return args.get(name, default)

    def leng(v, name):
        return _num(v, name, path + ".args." + name) * scale

    def integer(v, name, minimum=None):
        x = _num(v, name, path + ".args." + name)
        if abs(x - round(x)) > 1e-9:
            raise AttaError("bad_arg", "%s must be an integer" % name,
                            path=path + ".args." + name, fix="pass a whole number")
        k = int(round(x))
        if minimum is not None and k < minimum:
            raise AttaError("bad_arg", "%s must be >= %d" % (name, minimum),
                            path=path + ".args." + name, fix="use %s >= %d" % (name, minimum))
        return k

    def vec(v, name, n=3, scaled=True):
        arr = np.asarray(v, dtype=np.float64).reshape(-1)
        if arr.shape != (n,) or not np.isfinite(arr).all():
            raise AttaError("bad_arg", "%s must be %d finite numbers" % (name, n),
                            path=path + ".args." + name, fix="pass a %d-vector" % n)
        return arr * (scale if scaled else 1.0)

    def flag(v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    if op == "box":
        return {"size": vec(req("size"), "size"), "center": vec(opt("center", (0, 0, 0)), "center"),
                "align": str(opt("align", "center"))}
    if op == "cylinder":
        return {"radius": leng(req("radius"), "radius"), "height": leng(req("height"), "height"),
                "center": vec(opt("center", (0, 0, 0)), "center"), "axis": str(opt("axis", "z")),
                "segments": integer(opt("segments", 64), "segments", 3),
                "caps": flag(opt("caps", True))}
    if op == "sphere":
        return {"radius": leng(req("radius"), "radius"),
                "center": vec(opt("center", (0, 0, 0)), "center"),
                "segments": integer(opt("segments", 64), "segments", 3),
                "rings": integer(opt("rings", 32), "rings", 2)}
    if op == "cone":
        return {"r_bottom": leng(req("r_bottom"), "r_bottom"),
                "r_top": leng(req("r_top"), "r_top"), "height": leng(req("height"), "height"),
                "center": vec(opt("center", (0, 0, 0)), "center"), "axis": str(opt("axis", "z")),
                "segments": integer(opt("segments", 64), "segments", 3)}
    if op == "torus":
        return {"major_radius": leng(req("major_radius"), "major_radius"),
                "minor_radius": leng(req("minor_radius"), "minor_radius"),
                "center": vec(opt("center", (0, 0, 0)), "center"), "axis": str(opt("axis", "z")),
                "seg_major": integer(opt("seg_major", 96), "seg_major", 3),
                "seg_minor": integer(opt("seg_minor", 32), "seg_minor", 3)}
    if op == "revolve":
        return {"profile_rz": _mat(req("profile_rz"), "profile_rz", 2,
                                   path + ".args.profile_rz", min_rows=2, scale=scale),
                "segments": integer(opt("segments", 96), "segments", 3),
                "axis": str(opt("axis", "z")), "caps": flag(opt("caps", True))}
    if op == "loft":
        rings = req("rings")
        if not isinstance(rings, (list, tuple)) or len(rings) < 2:
            raise AttaError("bad_arg", "rings must be a list of >= 2 rings",
                            path=path + ".args.rings", fix="pass at least two rings")
        return {"rings": [_mat(r, "rings[%d]" % i, 3, path + ".args.rings[%d]" % i,
                               min_rows=3, scale=scale) for i, r in enumerate(rings)],
                "closed_ring": flag(opt("closed_ring", True)),
                "caps": flag(opt("caps", True))}
    if op == "sweep":
        return {"section_xy": _mat(req("section_xy"), "section_xy", 2,
                                   path + ".args.section_xy", min_rows=3, scale=scale),
                "path": _mat(req("path"), "path", 3, path + ".args.path",
                             min_rows=2, scale=scale),
                "closed": flag(opt("closed", False)),
                "twist_deg": _num(opt("twist_deg", 0.0), "twist_deg", path + ".args.twist_deg"),
                "scale_end": _num(opt("scale_end", 1.0), "scale_end", path + ".args.scale_end")}
    if op == "coons":
        edges = req("edges")
        if not isinstance(edges, (list, tuple)) or len(edges) != 4:
            raise AttaError("bad_arg", "edges must contain 4 curves",
                            path=path + ".args.edges", fix="pass (bottom,right,top,left)")
        return {"edges": [_mat(c, "edges[%d]" % i, 3, path + ".args.edges[%d]" % i,
                               min_rows=2, scale=scale) for i, c in enumerate(edges)],
                "u_samples": integer(opt("u_samples", 32), "u_samples", 2),
                "v_samples": integer(opt("v_samples", 32), "v_samples", 2),
                "flip": flag(opt("flip", False))}
    if op == "gordon":
        def family(name, minimum=2):
            v = req(name) if name in args else opt(name, [])
            if not isinstance(v, (list, tuple)) or len(v) < minimum:
                raise AttaError("bad_arg", "%s must be a list of >= %d curves" % (name, minimum),
                                path=path + ".args." + name, fix="pass at least two curves")
            return [_mat(c, "%s[%d]" % (name, i), 3, path + ".args.%s[%d]" % (name, i),
                         min_rows=2, scale=scale) for i, c in enumerate(v)]
        return {"u_curves": family("u_curves"), "v_curves": family("v_curves"),
                "u_samples": integer(opt("u_samples", 32), "u_samples", 2),
                "v_samples": integer(opt("v_samples", 32), "v_samples", 2),
                "flip": flag(opt("flip", False))}
    if op == "extrude":
        raw_holes = opt("holes_xy", [])
        if raw_holes is None:
            raw_holes = []
        if not isinstance(raw_holes, (list, tuple)):
            raise AttaError("bad_arg", "holes_xy must be a list of 2D loops",
                            path=path + ".args.holes_xy",
                            fix="pass [[[x,y],...], ...]")
        holes = [_mat(hole, "holes_xy[%d]" % i, 2,
                      path + ".args.holes_xy[%d]" % i,
                      min_rows=3, scale=scale)
                 for i, hole in enumerate(raw_holes)]
        return {"loop_xy": _mat(req("loop_xy"), "loop_xy", 2, path + ".args.loop_xy",
                                min_rows=3, scale=scale),
                "height": leng(req("height"), "height"),
                "direction": vec(opt("direction", (0, 0, 1)), "direction", scaled=False),
                "caps": flag(opt("caps", True)),
                "holes_xy": holes}
    if op == "thicken":
        return {"surface": meshes[0], "thickness": leng(req("thickness"), "thickness"),
                "offset": _num(opt("offset", 0.0), "offset", path + ".args.offset")}
    if op == "lobes":
        return {"mesh": meshes[0], "count": integer(opt("count", 12), "count", 0),
                "amplitude": _num(opt("amplitude", 0.03), "amplitude", path + ".args.amplitude"),
                "phase_deg": _num(opt("phase_deg", 0.0), "phase_deg", path + ".args.phase_deg"),
                "twist_deg": _num(opt("twist_deg", 0.0), "twist_deg", path + ".args.twist_deg")}
    if op in ("union", "difference", "intersection"):
        return {"meshes": meshes}
    if op == "mirror":
        return {"mesh": meshes[0], "plane": str(opt("plane", "x"))}
    if op == "pattern_linear":
        return {"mesh": meshes[0], "count": integer(req("count"), "count", 1),
                "spacing": leng(req("spacing"), "spacing"), "axis": str(opt("axis", "x"))}
    if op == "pattern_polar":
        return {"mesh": meshes[0], "count": integer(req("count"), "count", 1),
                "axis": str(opt("axis", "z")),
                "angle_deg": _num(opt("angle_deg", 360.0), "angle_deg", path + ".args.angle_deg"),
                "center": vec(opt("center", (0, 0, 0)), "center")}
    if op == "transform":
        try:
            raw_scale = np.asarray(opt("scale", 1.0), dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise AttaError("bad_arg", "scale must be a number or a 3-vector",
                            path=path + ".args.scale", fix="use scale=1.0") from exc
        if raw_scale.ndim == 0:
            if not np.isfinite(raw_scale) or float(raw_scale) == 0.0:
                raise AttaError("bad_arg", "scale must be finite and non-zero",
                                path=path + ".args.scale", fix="use scale=1.0")
            sc = float(raw_scale)
        elif raw_scale.shape == (3,) and np.isfinite(raw_scale).all() \
                and np.all(raw_scale != 0.0):
            sc = raw_scale
        else:
            raise AttaError("bad_arg", "scale must be a non-zero number or 3-vector",
                            path=path + ".args.scale", fix="use scale=1.0")
        return {"mesh": meshes[0],
                "translate": vec(opt("translate", (0, 0, 0)), "translate"),
                "rotate_deg": vec(opt("rotate_deg", (0, 0, 0)), "rotate_deg", scaled=False),
                "scale": sc, "pivot": vec(opt("pivot", (0, 0, 0)), "pivot")}
    extra_prepare = _EXTRA_OPS.get(op)
    if extra_prepare is not None:
        return _prepare_extra(op, args, path, scale, meshes)
    raise AttaError("unknown_feature", "unknown feature op %r" % op, path=path + ".op",
                    fix="use one of: " + ", ".join(sorted(FEATURE_OPS)))


def _prepare_extra(op, args, path, scale, meshes=()):
    """Validate/coerce arguments for an op contributed by a family module.

    An op may declare ``lengths``/``vectors``/``ints`` in its spec to get the
    same unit scaling and validation as the built-ins; anything else is passed
    through as-is.  A callable attached as ``fn.prepare`` takes full control
    (it receives ``(args, scale, path)`` and must return the keyword dict).
    """
    spec = FEATURE_OPS[op]
    fn = _EXTRA_OPS[op]
    allowed = set(spec["required"]) | set(spec["optional"])
    for key in args:
        if key not in allowed:
            raise AttaError("bad_arg", "unexpected arg %r for %s" % (key, op),
                            path=path + ".args." + str(key),
                            fix="allowed args: " + (", ".join(sorted(allowed)) or "none"))
    for name in spec["required"]:
        if name not in args:
            raise AttaError("bad_arg", "missing required arg %r for %s" % (name, op),
                            path=path + ".args", fix="add args.%s" % name)
    custom = getattr(fn, "prepare", None)
    if custom is not None:
        return custom(dict(args), scale, path)

    lengths = set(spec.get("lengths") or ())
    vectors = set(spec.get("vectors") or ())
    ints = set(spec.get("ints") or ())
    out = {}
    for key, value in args.items():
        where = path + ".args." + str(key)
        if key in lengths:
            out[key] = _num(value, key, where) * scale
        elif key in vectors:
            arr = np.asarray(value, dtype=np.float64).reshape(-1)
            if not np.isfinite(arr).all():
                raise AttaError("bad_arg", "%s contains NaN or Inf" % key, path=where,
                                fix="pass finite coordinates")
            out[key] = arr * scale
        elif key in ints:
            x = _num(value, key, where)
            if abs(x - round(x)) > 1e-9:
                raise AttaError("bad_arg", "%s must be an integer" % key, path=where,
                                fix="pass a whole number")
            out[key] = int(round(x))
        else:
            out[key] = value
    # arity>=1 ops always receive their wired input, exactly as the built-ins
    # do by hand.  Without this an extra op silently ignores its input mesh.
    if int(FEATURE_OPS[op]["arity"]) >= 1 and meshes:
        out["mesh"] = meshes[0]
    elif int(FEATURE_OPS[op]["arity"]) >= 1:
        raise AttaError("bad_arg", "%s needs an input mesh" % op, path=path + ".inputs",
                        fix="pass inputs=[\"<id>\"]")
    return out


def _apply(op, p):
    if op == "box":
        return _P.box(**p)
    if op == "cylinder":
        return _P.cylinder(**p)
    if op == "sphere":
        return _P.sphere(**p)
    if op == "cone":
        return _P.cone(**p)
    if op == "torus":
        return _P.torus(**p)
    if op == "revolve":
        return _S.revolve(**p)
    if op == "loft":
        return _S.loft(**p)
    if op == "sweep":
        return _S.sweep(**p)
    if op == "coons":
        return _S.coons(**p)
    if op == "gordon":
        return _S.gordon(**p)
    if op == "extrude":
        return _S.extrude(**p)
    if op == "thicken":
        return _S.thicken(**p)
    if op == "lobes":
        return _S.lobes(**p)
    if op in ("union", "difference", "intersection"):
        meshes = p["meshes"]
        out = meshes[0]
        for other in meshes[1:]:
            out = _B.boolean(out, other, op)
        return out
    if op == "mirror":
        return _B.mirror(**p)
    if op == "pattern_linear":
        return _B.pattern_linear(**p)
    if op == "pattern_polar":
        return _B.pattern_polar(**p)
    if op == "transform":
        return p["mesh"].transform(translate=p["translate"], rotate_deg=p["rotate_deg"],
                                   scale=p["scale"], pivot=p["pivot"])
    fn = _EXTRA_OPS.get(op)
    if fn is not None:
        return fn(**p)
    raise AttaError("unknown_feature", "unknown feature op %r" % op, path="features.op",
                    fix="use one of: " + ", ".join(sorted(FEATURE_OPS)))


# ---------------------------------------------------------------- evaluate

def evaluate(features, params=None, units="mm", output=None) -> Mesh:
    """Evaluate a feature graph and return the output mesh in metres.

    Rows are ``{"id", "op", "args", "inputs"}``; ids must be unique and inputs
    may only reference other rows.  ``args`` values may be numbers, nested
    lists, ``{"$param": "name"}`` or ``{"$expr": "w/2 + 4"}`` (safe arithmetic
    over ``params``).  Lengths are converted from ``units`` to metres.
    """
    scale = _UNIT.get(str(units).lower())
    if scale is None:
        raise AttaError("bad_arg", "units must be mm, cm, m or in", path="units",
                        fix="use units='mm'")
    if not isinstance(features, (list, tuple)) or len(features) == 0:
        raise AttaError("bad_arg", "features must be a non-empty list", path="features",
                        fix="add at least one feature row")
    rows, index = [], {}
    for i, row in enumerate(features):
        if not isinstance(row, dict):
            raise AttaError("bad_arg", "feature row must be an object", path="features[%d]" % i,
                            fix='use {"id": "...", "op": "..."}')
        fid = row.get("id")
        if not isinstance(fid, str) or not fid:
            raise AttaError("bad_arg", "feature id must be a non-empty string",
                            path="features[%d].id" % i, fix="give the row a unique id")
        if fid in index:
            raise AttaError("duplicate_id", "feature id %r is used twice" % fid,
                            path="features[%d].id" % i, fix="rename one of the duplicate ids")
        index[fid] = i
        rows.append(row)
    params = dict(params or {})
    memo, state = {}, {}

    def ev(fid, stack):
        if fid in memo:
            return memo[fid]
        if state.get(fid) == 1:
            cycle = stack[stack.index(fid):] + [fid]
            raise AttaError("cycle", "feature graph has a cycle: " + " -> ".join(cycle),
                            path="features[%d].inputs" % index[fid],
                            fix="remove one dependency so the graph is acyclic")
        i = index[fid]
        state[fid] = 1
        row = rows[i]
        op = str(row.get("op", "")).lower()
        spec = FEATURE_OPS.get(op)
        if spec is None:
            raise AttaError("unknown_feature", "unknown feature op %r" % op,
                            path="features[%d].op" % i,
                            fix="use one of: " + ", ".join(sorted(FEATURE_OPS)))
        inputs = row.get("inputs", [])
        if not isinstance(inputs, (list, tuple)):
            raise AttaError("bad_arg", "inputs must be a list of feature ids",
                            path="features[%d].inputs" % i, fix="pass a list of ids")
        arity = int(spec["arity"])
        if arity == 0 and len(inputs) != 0:
            raise AttaError("bad_arg", "%s takes no inputs" % op, path="features[%d].inputs" % i,
                            fix="remove inputs")
        if arity == 1 and len(inputs) != 1:
            raise AttaError("bad_arg", "%s needs exactly one input" % op,
                            path="features[%d].inputs" % i, fix="pass inputs=[\"<id>\"]")
        if arity == 2 and len(inputs) < 2:
            raise AttaError("bad_arg", "%s needs at least two inputs" % op,
                            path="features[%d].inputs" % i,
                            fix="pass inputs=[\"<id>\", \"<id>\"]")
        for j, name in enumerate(inputs):
            if not isinstance(name, str) or name not in index:
                raise AttaError("unknown_input", "feature %r references unknown input %r"
                                % (fid, name), path="features[%d].inputs[%d]" % (i, j),
                                fix="available ids: " + ", ".join(index))
        meshes = [ev(str(name), stack + [fid]) for name in inputs]
        args = row.get("args", {})
        if not isinstance(args, dict):
            raise AttaError("bad_arg", "args must be an object",
                            path="features[%d].args" % i, fix="pass a dict of arguments")
        resolved = _resolve(args, params, "features[%d].args" % i)
        try:
            prepared = _prepare(op, resolved, meshes, scale, "features[%d]" % i)
            out = _apply(op, prepared)
        except AttaError as exc:
            if exc.path and exc.path.startswith("features["):
                raise
            raise AttaError(exc.code, exc.message,
                            path="features[%d]" % i + ("." + exc.path if exc.path else ""),
                            fix=exc.fix, details=exc.details, severity=exc.severity)
        state[fid] = 2
        memo[fid] = out
        return out

    if output is None:
        out_id = rows[-1]["id"]
    else:
        out_id = str(output)
        if out_id not in index:
            raise AttaError("unknown_input", "output %r is not a feature id" % out_id,
                            path="output", fix="use one of: " + ", ".join(index))
    mesh = ev(out_id, [])
    mesh = Mesh(mesh.verts.copy(), mesh.faces.copy(), dict(mesh.meta))
    mesh.meta["features"] = list(index)
    mesh.meta["output"] = out_id
    return mesh
