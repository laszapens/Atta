# -*- coding: utf-8 -*-
"""Spec v3 -- the single declarative input to Atta.

A spec says *what* to build, never *how*: either a curated ``recipe`` family or
a general feature ``graph``.  This module owns four verbs:

    validate_spec(spec)  -> {ok, spec, errors}
    propose_spec(spec)   -> {ok, spec, assumption_ledger, ...}
    compile_spec(spec)   -> {ok, compiled, errors}
    spec_hash(spec)      -> sha256 of canonical JSON

Units are millimetres and degrees at this boundary.  Recipe parameters are
resolved by ``Recipe.resolve`` (values in metres); feature graph args are not
evaluated here.

Assumptions are **design defaults**, never measurements.  Every inferred value
lands in an explicit ledger and is also written as a human-readable string in
``spec.assumptions``.  ``validate`` never invents assumptions.

``atta.recipes`` and ``atta.features`` are unfinished neighbours.  They are
imported lazily, inside functions, and a missing module becomes an actionable
``AttaError`` (surfaced as an ordinary validation error by public verbs).
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re
import sys
from collections.abc import Mapping
from typing import Any

from .errors import AttaError
from .version import SCHEMA_SPEC

SCHEMA = SCHEMA_SPEC  # "atta.spec/v3"

KIND_RECIPE = "recipe"
KIND_GRAPH = "graph"
KIND_ASSEMBLY = "assembly"

MATE_TYPES = (
    "coincident", "concentric", "axis", "plane", "distance", "angle",
    "tangent", "gear", "insert", "parallel", "perpendicular", "lock",
    "point_on_plane",
)

QUALITY = ("draft", "review", "deliver")

#: The single vocabulary is the material preset registry; spec validation must
#: not drift from what the Blender worker can actually resolve.
try:
    from . import materials as _materials
    MATERIALS = frozenset(_materials.names())
except Exception:  # pragma: no cover - materials is a sibling module
    MATERIALS = frozenset({
        "ceramic", "porcelain", "quartz", "glass", "glass_frosted",
        "steel", "brushed_steel", "stainless", "aluminum", "aluminium",
        "brass", "copper", "gold", "plastic", "abs", "nylon", "wood",
        "powder_blue", "engine_outer", "dark",
    })

CONSTRAINT_DOCS: dict[str, str] = {
    "bbox": "{x:[min,max], y:[min,max], z:[min,max]} or a target value per axis (mm)",
    "volume_mm3": "[min,max] or a target volume (mm^3)",
    "min_wall_mm": "number; ray-sampled wall thickness (mm)",
    "genus": "integer; expected topology genus",
    "closed": "bool; mesh must be watertight",
    "n_components": "integer; expected number of connected components",
    "dihedral_max_deg": "number; maximum face-angle jump (degrees)",
    "curvature_p95_deg": "number; 95th percentile curvature (degrees)",
    "clearance": "[{a,b,min}] minimum distance between parts (mm)",
    "interference": "[{a,b,max}] allowed overlap volume (mm^3)",
    "interfaces": "[{a,b,max_mm3}] designed press-fit/overlap allowance",
    "envelope": "{min:[x,y,z], max:[x,y,z]} world envelope",
    "holes": "[{diameter, tolerance, count}] expected cylindrical holes",
    "render": "{res:[w,h], samples:n, require_ndc:true} render contract",
}

#: Human-facing finish keys.  ``*_mm`` is millimetres, ``*_deg`` degrees.
FINISH_DOCS: dict[str, str] = {
    "smooth": "bool; enable smooth shading",
    "smooth_deg": "number 0..180; smooth shading angle",
    "smooth_angle_deg": "number 0..180; alias of smooth_deg",
    "bevel_mm": "number >= 0; bevel width in mm",
    "bevel_segments": "integer >= 1; bevel segments",
    "bevel_angle_deg": "number 0..180; bevel angle limit",
    "material": "optional material override; prefer spec.material",
}

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_MAX_LIST_ROWS = {"clearance", "interference", "interfaces", "holes"}

#: Keys that may sit either in a feature's ``args`` or at its top level.
_TOP_LEVEL_FEATURE_KEYS = {"id", "op", "args", "inputs"}


# --------------------------------------------------------------------- helpers

def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dataclass-like object or a dict."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _err(errors: list[dict], code: str, path: str, message: str, fix: str) -> None:
    errors.append({"code": str(code), "path": str(path),
                   "message": str(message), "fix": str(fix)})


def _err_from_atta(exc: AttaError, *, path: str | None = None) -> dict:
    return {"code": exc.code,
            "path": path or exc.path or "$",
            "message": exc.message,
            "fix": exc.fix or "Fix the underlying module or input and retry."}


def _load_recipes():
    """Lazy import so a missing ``atta.recipes`` never breaks import time."""
    mod = sys.modules.get("atta.recipes")
    if mod is not None:
        return mod
    try:
        return importlib.import_module("atta.recipes")
    except Exception as exc:  # noqa: BLE001 - any import failure is the message
        raise AttaError(
            "recipes_unavailable",
            "atta.recipes is not available: %s" % exc,
            path="$.model.id",
            fix="Install/implement atta.recipes (get(recipe_id)), "
                "or use model.kind='graph'.",
        ) from exc


def _get_recipe(recipe_id: str):
    mod = _load_recipes()
    getter = getattr(mod, "get", None)
    if not callable(getter):
        raise AttaError(
            "recipes_unavailable",
            "atta.recipes has no callable get(recipe_id)",
            path="$.model.id",
            fix="Expose get(recipe_id) -> Recipe from atta.recipes.",
        )
    try:
        return getter(recipe_id)
    except Exception as exc:  # noqa: BLE001 - registry may raise its own error
        raise AttaError(
            "unknown_recipe",
            "unknown recipe %r: %s" % (recipe_id, exc),
            path="$.model.id",
            fix="Call atta_catalog for recipe ids, or fix model.id.",
        ) from exc


def _load_feature_ops() -> dict:
    """Lazy import of the feature registry."""
    mod = sys.modules.get("atta.features")
    if mod is None:
        try:
            mod = importlib.import_module("atta.features")
        except Exception as exc:  # noqa: BLE001
            raise AttaError(
                "features_unavailable",
                "atta.features is not available: %s" % exc,
                path="$.model.features",
                fix="Install/implement atta.features.FEATURE_OPS, "
                    "or use model.kind='recipe'.",
            ) from exc
    ops = getattr(mod, "FEATURE_OPS", None)
    if not isinstance(ops, Mapping):
        raise AttaError(
            "features_unavailable",
            "atta.features.FEATURE_OPS is missing or not a mapping",
            path="$.model.features",
            fix="Expose FEATURE_OPS: {op: {required, optional, doc}}.",
        )
    return dict(ops)


# ---------------------------------------------------------------- JSON / hash

def canonical_json(value: Any) -> str:
    """Sorted keys, no insignificant whitespace, no NaN/Infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def spec_hash(spec: Any) -> str:
    """sha256 of canonical JSON.  ``assumptions`` are part of the document."""
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- validation

def _validate_finish(raw: Any, errors: list[dict]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _err(errors, "bad_finish", "$.finish", "finish must be an object",
             "Use {} or {bevel_mm: 0.8, smooth_deg: 35}.")
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        path = "$.finish.%s" % key
        if key not in FINISH_DOCS:
            _err(errors, "unknown_finish_key", path,
                 "unknown finish key %r" % (key,),
                 "Allowed keys: %s." % ", ".join(sorted(FINISH_DOCS)))
            continue
        if key == "smooth":
            if not isinstance(value, bool):
                _err(errors, "bad_finish", path, "smooth must be a bool",
                     "Use true or false.")
                continue
        elif key == "bevel_segments":
            if not _is_num(value) or not float(value).is_integer() or value < 1:
                _err(errors, "bad_finish", path,
                     "bevel_segments must be an integer >= 1",
                     "Use e.g. bevel_segments: 2.")
                continue
            value = int(value)
        elif key == "bevel_mm":
            if not _is_num(value) or not math.isfinite(float(value)) or value < 0:
                _err(errors, "bad_finish", path,
                     "bevel_mm must be a finite number >= 0",
                     "Use e.g. bevel_mm: 0.8.")
                continue
            value = float(value)
        elif key == "material":
            if not isinstance(value, str) or not value.strip():
                _err(errors, "bad_finish", path,
                     "finish.material must be a non-empty string",
                     "Prefer spec.material; otherwise give a material name.")
                continue
        else:  # smooth_deg / smooth_angle_deg / bevel_angle_deg
            if not _is_num(value) or not math.isfinite(float(value)) or not 0 <= value <= 180:
                _err(errors, "bad_finish", path,
                     "%s must be a number in 0..180" % key,
                     "Use e.g. %s: 35." % key)
                continue
            value = float(value)
        out[key] = value
    return out


def _validate_constraint_value(key: str, value: Any, errors: list[dict]) -> None:
    path = "$.constraints.%s" % key
    if key == "bbox":
        if not isinstance(value, Mapping):
            _err(errors, "bad_constraint", path, "bbox must be an object",
                 "Use {x:[min,max], y:[min,max], z:[min,max]}.")
        return
    if key in _MAX_LIST_ROWS:
        if not isinstance(value, (list, tuple)):
            _err(errors, "bad_constraint", path, "%s must be an array" % key,
                 "Wrap rows in a list.")
            return
        for i, row in enumerate(value):
            if not isinstance(row, Mapping):
                _err(errors, "bad_constraint", "%s[%d]" % (path, i),
                     "%s row must be an object" % key,
                     "Use {a:..., b:..., min|max|max_mm3:...}.")
                continue
            if key != "holes" and ("a" not in row or "b" not in row):
                _err(errors, "bad_constraint", "%s[%d]" % (path, i),
                     "%s row needs a and b" % key,
                     "Name the two parts as a and b.")
        return
    if key == "envelope":
        if not isinstance(value, Mapping):
            _err(errors, "bad_constraint", path, "envelope must be an object",
                 "Use {min:[x,y,z], max:[x,y,z]}.")
        return
    if key in {"genus", "n_components"}:
        if not _is_num(value) or not float(value).is_integer() or value < 0:
            _err(errors, "bad_constraint", path,
                 "%s must be a non-negative integer" % key,
                 "Use e.g. %s: 0." % key)
        return
    if key == "closed":
        if not isinstance(value, bool):
            _err(errors, "bad_constraint", path, "closed must be a bool",
                 "Use true or false.")
        return
    if key == "volume_mm3":
        if _is_num(value):
            return
        if not isinstance(value, (list, tuple)) or len(value) != 2 or \
                not all(_is_num(v) for v in value):
            _err(errors, "bad_constraint", path,
                 "volume_mm3 must be a number or [min,max]",
                 "Use e.g. [100000, 900000].")
        return
    if key == "render":
        if not isinstance(value, Mapping):
            _err(errors, "bad_constraint", path, "render must be an object",
                 "Use {res:[w,h], samples:n, require_ndc:true}.")
        return
    if key in {"min_wall_mm", "dihedral_max_deg", "curvature_p95_deg"}:
        if not _is_num(value) or not math.isfinite(float(value)):
            _err(errors, "bad_constraint", path, "%s must be a finite number" % key,
                 "Use a number; angles are degrees.")


def _validate_constraints(raw: Any, errors: list[dict]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _err(errors, "bad_constraints", "$.constraints",
             "constraints must be an object",
             "Use {} or a map of constraint keys.")
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CONSTRAINT_DOCS:
            _err(errors, "unknown_constraint", "$.constraints.%s" % key,
                 "unknown constraint %r" % (key,),
                 "Allowed keys: %s." % ", ".join(sorted(CONSTRAINT_DOCS)))
            continue
        _validate_constraint_value(str(key), value, errors)
        out[str(key)] = value
    return out


def _validate_assumptions(raw: Any, errors: list[dict]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        _err(errors, "bad_assumptions", "$.assumptions",
             "assumptions must be an array of strings",
             'Use [] or ["one human-readable statement"].')
        return []
    out: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            _err(errors, "bad_assumption", "$.assumptions[%d]" % i,
                 "assumption must be a non-empty string",
                 "Assumptions are human-readable strings; provenance lives "
                 "in the propose ledger.")
            continue
        out.append(item)
    return out


def _validate_recipe_params(model: dict, recipe: Any, errors: list[dict]) -> None:
    raw = model.get("params")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        _err(errors, "bad_params", "$.model.params",
             "model.params must be an object",
             "Use {} or {param_name: value}.")
        model["params"] = {}
        return
    specs = _attr(recipe, "params", {})
    if not isinstance(specs, Mapping):
        _err(errors, "bad_recipe", "$.model.id",
             "recipe.params must be a mapping of ParamSpec",
             "Fix the recipe definition.")
        model["params"] = dict(raw)
        return

    norm: dict[str, Any] = {}
    for name, value in raw.items():
        path = "$.model.params.%s" % name
        if not isinstance(name, str) or not _ID_RE.match(name):
            _err(errors, "bad_param_name", path,
                 "invalid parameter name %r" % (name,),
                 "Use letters/digits/underscore, not starting with a digit.")
            continue
        ps = specs.get(name)
        if ps is None:
            _err(errors, "unknown_param", path,
                 "unknown parameter %r for recipe %r"
                 % (name, _attr(recipe, "id", "?")),
                 "Known params: %s." % (", ".join(sorted(specs)) or "(none)"))
            continue
        typ = str(_attr(ps, "type", "float") or "float")
        ok, norm_value, why = _coerce_param(value, ps, typ)
        if not ok:
            _err(errors, "bad_param", path, why,
                 "Give a %s value; see atta_catalog for the recipe schema." % typ)
            continue
        lo, hi = _attr(ps, "min", None), _attr(ps, "max", None)
        if _is_num(norm_value):
            if _is_num(lo) and norm_value < lo:
                _err(errors, "param_out_of_range", path,
                     "%s=%s is below minimum %s" % (name, norm_value, lo),
                     "Use %s >= %s." % (name, lo))
                continue
            if _is_num(hi) and norm_value > hi:
                _err(errors, "param_out_of_range", path,
                     "%s=%s is above maximum %s" % (name, norm_value, hi),
                     "Use %s <= %s." % (name, hi))
                continue
        norm[name] = norm_value
    model["params"] = norm


def _coerce_param(value: Any, ps: Any, typ: str) -> tuple[bool, Any, str]:
    if typ == "bool":
        if not isinstance(value, bool):
            return False, None, "expected a bool, got %r" % (value,)
        return True, value, ""
    if typ == "int":
        if not _is_num(value) or not float(value).is_integer():
            return False, None, "expected an integer, got %r" % (value,)
        return True, int(value), ""
    if typ == "enum":
        allowed = list(_attr(ps, "enum", []) or [])
        if value not in allowed:
            return False, None, "expected one of %r, got %r" % (allowed, value)
        return True, value, ""
    if typ == "str":
        if not isinstance(value, str):
            return False, None, "expected a string, got %r" % (value,)
        return True, value, ""
    if not _is_num(value) or not math.isfinite(float(value)):
        return False, None, "expected a finite number, got %r" % (value,)
    return True, float(value), ""


def _validate_recipe(model: dict, errors: list[dict]) -> None:
    rid = model.get("id")
    if not isinstance(rid, str) or not rid.strip():
        _err(errors, "bad_recipe_id", "$.model.id",
             "model.id must be a non-empty string",
             "Use a recipe id from atta_catalog, e.g. product.mug.")
        return

    preset = model.get("preset", "nominal")
    if preset is None:
        preset = "nominal"
    model["preset"] = preset
    if not isinstance(preset, str) or not preset.strip():
        _err(errors, "bad_preset", "$.model.preset",
             "model.preset must be a non-empty string",
             "Use 'nominal' or a preset listed by atta_catalog.")
        return

    try:
        recipe = _get_recipe(rid)
    except AttaError as exc:
        errors.append(_err_from_atta(exc))
        return

    _validate_recipe_params(model, recipe, errors)

    presets = _attr(recipe, "presets", {}) or {}
    if preset != "nominal" and preset not in presets:
        _err(errors, "unknown_preset", "$.model.preset",
             "unknown preset %r for recipe %r" % (preset, rid),
             "Known presets: %s." % (", ".join(sorted(presets)) or "(none)"))


def _cycle_nodes(ids: list[str], deps: dict[str, list[str]]) -> set[str]:
    state: dict[str, int] = {}
    found: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for dep in deps.get(node, []):
            if dep not in deps:
                continue
            if state.get(dep) == 1:
                found.update(stack[stack.index(dep):])
            elif state.get(dep) is None:
                visit(dep, stack)
        stack.pop()
        state[node] = 2

    for node in ids:
        if state.get(node) is None:
            visit(node, [])
    return found


def _validate_graph(model: dict, errors: list[dict]) -> None:
    raw_params = model.get("params", {})
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, Mapping):
        _err(errors, "bad_params", "$.model.params",
             "graph.params must be an object of numeric values",
             "Use {} or {w: 40, h: 20}; feature args can use {\"$param\":\"w\"}.")
        raw_params = {}
    params = {}
    for name, value in raw_params.items():
        if not isinstance(name, str) or not _ID_RE.match(name):
            _err(errors, "bad_param_name", "$.model.params",
                 "invalid param name %r" % (name,),
                 "Use letters/digits/underscore, not starting with a digit.")
            continue
        if not _is_num(value) or not math.isfinite(float(value)):
            _err(errors, "bad_param", "$.model.params.%s" % name,
                 "param must be a finite number", "Use a number in mm/degrees.")
            continue
        params[name] = value
    model["params"] = params

    units = model.get("units", "mm")
    if units != "mm":
        _err(errors, "bad_units", "$.model.units",
             "graph units must be 'mm', got %r" % (units,),
             "Convert feature arguments to mm and set units='mm'.")
    model["units"] = "mm"

    segments = model.get("segments", 64)
    if not _is_num(segments) or not float(segments).is_integer() or segments < 3:
        _err(errors, "bad_segments", "$.model.segments",
             "segments must be an integer >= 3",
             "Use e.g. segments: 64.")
    else:
        model["segments"] = int(segments)

    raw_feats = model.get("features")
    if not isinstance(raw_feats, (list, tuple)) or not raw_feats:
        _err(errors, "bad_features", "$.model.features",
             "graph.features must be a non-empty array",
             "Add at least one primitive feature, e.g. box.")
        raw_feats = []

    ops: dict | None = None
    try:
        ops = _load_feature_ops()
    except AttaError as exc:
        errors.append(_err_from_atta(exc, path="$.model.features"))

    features: list[dict] = []
    ids: list[str] = []
    known: set[str] = set()
    for i, raw in enumerate(raw_feats):
        base = "$.model.features[%d]" % i
        if not isinstance(raw, Mapping):
            _err(errors, "bad_feature", base, "feature must be an object",
                 'Use {"id":..., "op":..., "args":{...}}.')
            continue
        feat = copy.deepcopy(dict(raw))
        fid = feat.get("id")
        if not isinstance(fid, str) or not _ID_RE.match(fid):
            _err(errors, "bad_id", base + ".id",
                 "feature id is missing or invalid: %r" % (fid,),
                 "Use a unique id like 'plate' or 'cut_01'.")
        elif fid in known:
            _err(errors, "duplicate_id", base + ".id",
                 "duplicate feature id %r" % (fid,),
                 "Every feature id must be unique.")
        else:
            known.add(fid)
            ids.append(fid)

        op = feat.get("op")
        if not isinstance(op, str) or not op:
            _err(errors, "unknown_feature", base + ".op",
                 "feature op is missing",
                 "Use one of: %s." % (", ".join(sorted(ops or {})) or "(unavailable)"))
        elif ops is not None and op not in ops:
            _err(errors, "unknown_feature", base + ".op",
                 "unknown feature op %r" % (op,),
                 "Use one of: %s." % ", ".join(sorted(ops)))
        elif ops is not None:
            docs = ops.get(op) or {}
            required = docs.get("required", []) if isinstance(docs, Mapping) else []
            args = feat.get("args")
            arg_keys = set(args) if isinstance(args, Mapping) else set()
            for key in required:
                if key in {"id", "op"}:
                    continue
                if key in _TOP_LEVEL_FEATURE_KEYS:
                    if key not in feat:
                        _err(errors, "bad_arg", base + "." + key,
                             "%s requires %r" % (op, key),
                             "Add %r; see atta_catalog for %s." % (key, op))
                elif key not in arg_keys:
                    _err(errors, "bad_arg", base + ".args.%s" % key,
                         "%s requires arg %r" % (op, key),
                         "Add %r to args; see atta_catalog for %s." % (key, op))

        args = feat.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            _err(errors, "bad_arg", base + ".args",
                 "feature args must be an object",
                 "Use {} or {key: value}.")
            args = {}
        feat["args"] = copy.deepcopy(dict(args))

        inputs = feat.get("inputs", [])
        if inputs is None:
            inputs = []
        if not isinstance(inputs, (list, tuple)):
            _err(errors, "bad_arg", base + ".inputs",
                 "feature inputs must be an array of ids",
                 "Use [] or [\"earlier_feature_id\"].")
            inputs = []
        feat["inputs"] = list(inputs)
        features.append(feat)

    # References (checked after all ids are known, so forward refs can cycle).
    for i, feat in enumerate(features):
        base = "$.model.features[%d]" % i
        for j, ref in enumerate(feat.get("inputs", [])):
            if not isinstance(ref, str):
                _err(errors, "bad_arg", "%s.inputs[%d]" % (base, j),
                     "input reference must be a feature id string",
                     "Use an existing feature id.")
            elif ref not in known:
                _err(errors, "unknown_input", "%s.inputs[%d]" % (base, j),
                     "input refers to unknown feature %r" % (ref,),
                     "Known ids: %s." % (", ".join(sorted(known)) or "(none)"))

    deps = {feat["id"]: [r for r in feat.get("inputs", []) if isinstance(r, str)]
            for feat in features
            if isinstance(feat.get("id"), str) and feat["id"] in known}
    cycle = _cycle_nodes(ids, deps)
    if cycle:
        _err(errors, "cycle", "$.model.features",
             "feature graph has a cycle through: %s" % ", ".join(sorted(cycle)),
             "Break the cycle; a feature graph must be a DAG.")

    output = model.get("output")
    if output is None:
        if ids:
            model["output"] = ids[-1]
    elif not isinstance(output, str) or output not in known:
        _err(errors, "unknown_output_ref", "$.model.output",
             "output refers to unknown feature %r" % (output,),
             "Known ids: %s." % (", ".join(sorted(known)) or "(none)"))
    model["features"] = features


def _prefix_errors(errors: list[dict], prefix: str) -> list[dict]:
    out = []
    for err in errors:
        row = dict(err)
        path = str(row.get("path") or "$")
        row["path"] = prefix + path[1:] if path.startswith("$") else prefix + "." + path
        out.append(row)
    return out


def _vec3(raw) -> bool:
    return (isinstance(raw, (list, tuple)) and len(raw) == 3
            and all(_is_num(x) and math.isfinite(float(x)) for x in raw))


def _validate_transform(raw: Any, path: str, errors: list[dict]) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        _err(errors, "bad_transform", path, "transform must be an object",
             'Use {"translate_mm":[0,0,0], "rotate_deg":[0,0,0]}.')
        return
    for key in ("translate_mm", "translate"):
        if key in raw and not _vec3(raw[key]):
            _err(errors, "bad_transform", path + "." + key,
                 "%s must be three finite numbers (mm)" % key,
                 "Use [x, y, z] in millimetres.")
    for key in ("rotate_deg", "rotate"):
        if key in raw and not _vec3(raw[key]):
            _err(errors, "bad_transform", path + "." + key,
                 "%s must be three finite numbers (degrees)" % key,
                 "Use [rx, ry, rz] in degrees.")
    if "matrix" in raw:
        m = raw["matrix"]
        ok = (isinstance(m, (list, tuple)) and len(m) == 4
              and all(isinstance(r, (list, tuple)) and len(r) == 4 for r in m)
              and all(_is_num(v) and math.isfinite(float(v)) for row in m for v in row))
        if not ok:
            _err(errors, "bad_transform", path + ".matrix",
                 "matrix must be a 4x4 finite array (translation in mm)",
                 "Provide a row-major 4x4 matrix, or use translate_mm/rotate_deg.")


def _validate_ports(raw: Any, path: str, errors: list[dict]) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        _err(errors, "bad_ports", path, "ports must be an array",
             "Use [] or [{name, origin_mm, axis, radius_mm}].")
        return []
    out = []
    seen = set()
    for i, item in enumerate(raw):
        base = "%s[%d]" % (path, i)
        if not isinstance(item, Mapping):
            _err(errors, "bad_port", base, "port must be an object",
                 'Use {"name":"axis_z","origin_mm":[0,0,0],"axis":[0,0,1]}.')
            continue
        port = copy.deepcopy(dict(item))
        name = port.get("name")
        if not isinstance(name, str) or not _ID_RE.match(name):
            _err(errors, "bad_port_name", base + ".name",
                 "port name is missing or invalid: %r" % (name,),
                 "Use letters/digits/underscore, e.g. 'axis_z'.")
        elif name in seen:
            _err(errors, "duplicate_port", base + ".name",
                 "duplicate port name %r" % name, "Use a unique port name.")
        else:
            seen.add(name)
        if "origin_mm" in port and not _vec3(port["origin_mm"]):
            _err(errors, "bad_port", base + ".origin_mm",
                 "origin_mm must be three finite numbers (mm)",
                 "Use [x, y, z] in millimetres.")
        for key in ("axis", "normal"):
            if key in port:
                v = port[key]
                if not _vec3(v) or float(sum(float(x) ** 2 for x in v)) <= 1e-18:
                    _err(errors, "bad_port", base + "." + key,
                         "%s must be a non-zero 3-vector" % key,
                         "Use a direction such as [0,0,1].")
        if "radius_mm" in port and (not _is_num(port["radius_mm"])
                                    or float(port["radius_mm"]) < 0):
            _err(errors, "bad_port", base + ".radius_mm",
                 "radius_mm must be a number >= 0", "Use e.g. 5.0.")
        out.append(port)
    return out


def _validate_part_source(part: dict, base: str, errors: list[dict]) -> None:
    sources = [k for k in ("recipe", "graph", "spec", "model_id")
               if part.get(k) is not None]
    if len(sources) != 1:
        _err(errors, "bad_part_source", base,
             "part needs exactly one of recipe/graph/spec/model_id; got %s"
             % (", ".join(sources) or "none"),
             "Use recipe+params, an inline graph, an inline spec, or model_id.")
        return
    src = sources[0]
    if src == "recipe":
        rid = part.get("recipe")
        if not isinstance(rid, str) or not rid.strip():
            _err(errors, "bad_recipe_id", base + ".recipe",
                 "recipe must be a non-empty recipe id",
                 "Use an id from atta_catalog.")
            return
        try:
            recipe = _get_recipe(rid)
        except AttaError as exc:
            errors.append(_err_from_atta(exc, path=base + ".recipe"))
            return
        sub = {"params": dict(part.get("params") or {}),
               "preset": part.get("preset", "nominal")}
        before = len(errors)
        _validate_recipe_params(sub, recipe, errors)
        for err in errors[before:]:
            err["path"] = base + str(err.get("path") or "").replace("$.model", "")
        part["params"] = sub["params"]
        presets = _attr(recipe, "presets", {}) or {}
        preset = sub.get("preset")
        if not isinstance(preset, str) or not preset.strip():
            _err(errors, "bad_preset", base + ".preset",
                 "preset must be a non-empty string", "Use 'nominal'.")
        elif preset != "nominal" and preset not in presets:
            _err(errors, "unknown_preset", base + ".preset",
                 "unknown preset %r for recipe %r" % (preset, rid),
                 "Known presets: %s." % (", ".join(sorted(presets)) or "(none)"))
    elif src == "graph":
        graph = part.get("graph")
        if not isinstance(graph, Mapping):
            _err(errors, "bad_graph", base + ".graph",
                 "graph must be an object with features",
                 'Use {"features":[{"id":"b","op":"box","args":{"size":[1,1,1]}}]}.')
            return
        sub = {"kind": "graph", "features": copy.deepcopy(graph.get("features")),
               "units": graph.get("units", "mm"),
               "segments": graph.get("segments", 64),
               "output": graph.get("output")}
        before = len(errors)
        _validate_graph(sub, errors)
        for err in errors[before:]:
            err["path"] = base + ".graph" + str(err.get("path") or "").replace(
                "$.model", "")
        part["graph"] = {
            "features": sub.get("features"),
            "units": sub.get("units"),
            "segments": sub.get("segments"),
            "output": sub.get("output"),
        }
    elif src == "spec":
        nested = part.get("spec")
        if not isinstance(nested, Mapping):
            _err(errors, "bad_part_spec", base + ".spec",
                 "spec must be an object", "Inline a model spec or use a graph.")
            return
        if (nested.get("model") or {}).get("kind") == KIND_ASSEMBLY \
                or nested.get("kind") == KIND_ASSEMBLY:
            _err(errors, "nested_assembly", base + ".spec",
                 "an assembly part cannot itself be an assembly",
                 "Flatten the parts into this assembly.")
            return
        v = validate_spec(nested)
        errors.extend(_prefix_errors(v.get("errors") or [], base + ".spec"))
        part["spec"] = v.get("spec") or nested
    else:
        mid = part.get("model_id")
        if not isinstance(mid, str) or not mid.strip():
            _err(errors, "bad_model_id", base + ".model_id",
                 "model_id must be a non-empty string",
                 "Use a model_id built in this workspace.")


def _validate_assembly(model: dict, errors: list[dict]) -> None:
    parts = model.get("parts")
    if not isinstance(parts, (list, tuple)) or not parts:
        _err(errors, "bad_parts", "$.model.parts",
             "assembly.parts must be a non-empty array",
             "Add at least one part with an id and a recipe/graph.")
        parts = []
    if len(parts) > 256:
        _err(errors, "too_many_parts", "$.model.parts",
             "assembly has %d parts; the limit is 256" % len(parts),
             "Split the assembly into subassemblies.")
    ids: set[str] = set()
    norm_parts = []
    for i, raw in enumerate(parts):
        base = "$.model.parts[%d]" % i
        if not isinstance(raw, Mapping):
            _err(errors, "bad_part", base, "part must be an object",
                 'Use {"id":"base","recipe":"part.bracket","params":{...}}.')
            continue
        part = copy.deepcopy(dict(raw))
        pid = part.get("id")
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            _err(errors, "bad_part_id", base + ".id",
                 "part id is missing or invalid: %r" % (pid,),
                 "Use a unique id like 'base' or 'shaft_01'.")
        elif pid in ids:
            _err(errors, "duplicate_part_id", base + ".id",
                 "duplicate part id %r" % pid, "Every part id must be unique.")
        else:
            ids.add(pid)
        _validate_part_source(part, base, errors)
        _validate_transform(part.get("transform"), base + ".transform", errors)
        if "fixed" in part and not isinstance(part["fixed"], bool):
            _err(errors, "bad_fixed", base + ".fixed", "fixed must be a bool",
                 "Use true or false.")
        if "material" in part:
            m = part["material"]
            if not isinstance(m, str) or m.lower() not in MATERIALS:
                _err(errors, "bad_material", base + ".material",
                     "unknown material %r" % (m,),
                     "Use one of: %s." % ", ".join(sorted(MATERIALS)))
        if "quality" in part:
            q = part["quality"]
            if not isinstance(q, str) or q.lower() not in QUALITY:
                _err(errors, "bad_quality", base + ".quality",
                     "quality must be one of %s" % ", ".join(QUALITY),
                     "Use draft.")
        part["ports"] = _validate_ports(part.get("ports"), base + ".ports", errors)
        norm_parts.append(part)
    model["parts"] = norm_parts

    mates = model.get("mates", [])
    if mates is None:
        mates = []
    if not isinstance(mates, (list, tuple)):
        _err(errors, "bad_mates", "$.model.mates", "mates must be an array",
             "Use [] or [{id,type,a,b}].")
        mates = []
    seen = set()
    norm_mates = []
    for i, raw in enumerate(mates):
        base = "$.model.mates[%d]" % i
        if not isinstance(raw, Mapping):
            _err(errors, "bad_mate", base, "mate must be an object",
                 'Use {"id":"m1","type":"concentric","a":"base","b":"shaft"}.')
            continue
        mate = copy.deepcopy(dict(raw))
        mid = mate.get("id")
        if not isinstance(mid, str) or not _ID_RE.match(mid):
            _err(errors, "bad_mate_id", base + ".id",
                 "mate id is missing or invalid: %r" % (mid,),
                 "Use a unique id like 'm1'.")
        elif mid in seen:
            _err(errors, "duplicate_mate_id", base + ".id",
                 "duplicate mate id %r" % mid, "Every mate id must be unique.")
        else:
            seen.add(mid)
        typ = mate.get("type")
        if not isinstance(typ, str) or typ.lower() not in MATE_TYPES:
            _err(errors, "bad_mate_type", base + ".type",
                 "unknown mate type %r" % (typ,),
                 "Use one of: %s." % ", ".join(MATE_TYPES))
        else:
            mate["type"] = typ.lower()
        for key in ("a", "b"):
            ref = mate.get(key)
            if not isinstance(ref, str) or ref not in ids:
                _err(errors, "unknown_part", base + "." + key,
                     "mate references unknown part %r" % (ref,),
                     "Known parts: %s." % (", ".join(sorted(ids)) or "(none)"))
        if mate.get("a") == mate.get("b") and mate.get("a") is not None:
            _err(errors, "self_mate", base, "a mate cannot reference one part twice",
                 "Use two distinct parts; use a lock mate for a rigid pair.")
        for key in ("port_a", "port_b"):
            if key in mate and not isinstance(mate[key], str):
                _err(errors, "bad_port_ref", base + "." + key,
                     "%s must be a port name string" % key,
                     "Use e.g. 'axis_z' or omit it for the default port.")
        for key in ("offset_mm", "value_mm", "distance_mm", "angle_deg",
                    "weight"):
            if key in mate and not _is_num(mate[key]):
                _err(errors, "bad_mate_value", base + "." + key,
                     "%s must be a number" % key, "Use a number.")
        if "align" in mate and not isinstance(mate["align"], str):
            _err(errors, "bad_align", base + ".align", "align must be a string",
                 "Use auto | parallel | antiparallel.")
        if "allow_interference" in mate and not isinstance(
                mate["allow_interference"], bool):
            _err(errors, "bad_allow_interference", base + ".allow_interference",
                 "allow_interference must be a bool", "Use true or false.")
        norm_mates.append(mate)
    model["mates"] = norm_mates

    solver = model.get("solver")
    if solver is not None:
        if not isinstance(solver, Mapping):
            _err(errors, "bad_solver", "$.model.solver",
                 "solver must be an object", "Use {} or {max_nfev, tolerance_mm}.")
        else:
            for key in ("max_nfev", "tolerance_mm"):
                if key in solver and not _is_num(solver[key]):
                    _err(errors, "bad_solver", "$.model.solver." + key,
                         "%s must be a number" % key, "Use a number.")
    clearance = model.get("clearance")
    if clearance is not None and not isinstance(clearance, Mapping):
        _err(errors, "bad_clearance", "$.model.clearance",
             "clearance options must be an object",
             "Use {\"threshold_mm\": 5}.")
    allow = model.get("allow_interference")
    if allow is not None:
        if not isinstance(allow, (list, tuple)):
            _err(errors, "bad_allow", "$.model.allow_interference",
                 "allow_interference must be an array",
                 'Use [{"a":"base","b":"shaft"}] or omit it.')
        else:
            for i, row in enumerate(allow):
                if not isinstance(row, Mapping) or row.get("a") not in ids \
                        or row.get("b") not in ids:
                    _err(errors, "bad_allow", "$.model.allow_interference[%d]" % i,
                         "allow_interference row needs known a/b parts",
                         "Use {\"a\":\"part1\",\"b\":\"part2\"}.")


def validate_spec(spec: Any) -> dict:
    """Validate and normalise a spec.  Accumulates errors; never raises."""
    errors: list[dict] = []
    if not isinstance(spec, Mapping):
        _err(errors, "bad_spec", "$", "spec must be a JSON object",
             "Pass {schema, name, model, ...}.")
        return {"ok": False, "spec": None, "errors": errors}

    out = copy.deepcopy(dict(spec))

    schema = out.get("schema")
    if schema is None:
        out["schema"] = SCHEMA
    elif schema != SCHEMA:
        _err(errors, "bad_schema", "$.schema",
             "unsupported schema %r" % (schema,),
             "Set schema to %r." % SCHEMA)

    name = out.get("name")
    if name is None:
        out["name"] = "part"
    elif not isinstance(name, str) or not name.strip():
        _err(errors, "bad_name", "$.name", "name must be a non-empty string",
             "Use e.g. name: 'mug-01'.")

    quality = out.get("quality")
    if quality is None:
        out["quality"] = "draft"
    elif not isinstance(quality, str) or quality.lower() not in QUALITY:
        _err(errors, "bad_quality", "$.quality",
             "quality must be one of %s" % (", ".join(QUALITY),),
             "Use quality: 'draft'.")
    else:
        out["quality"] = quality.lower()

    material = out.get("material")
    if material is None:
        out["material"] = "ceramic"
    elif not isinstance(material, str) or material.lower() not in MATERIALS:
        _err(errors, "bad_material", "$.material",
             "unknown material %r" % (material,),
             "Use one of: %s." % ", ".join(sorted(MATERIALS)))
    else:
        out["material"] = material.lower()

    model = out.get("model")
    if not isinstance(model, Mapping):
        _err(errors, "bad_model", "$.model", "model must be an object",
             "Use {kind:'recipe', id:...} or {kind:'graph', features:[...]}.")
        model = {}
    else:
        model = copy.deepcopy(dict(model))
    out["model"] = model

    kind = model.get("kind")
    if kind is None:
        _err(errors, "bad_model_kind", "$.model.kind",
             "model.kind is required",
             "Use 'recipe', 'graph' or 'assembly'.")
    elif kind == KIND_RECIPE:
        model["kind"] = KIND_RECIPE
        _validate_recipe(model, errors)
    elif kind == KIND_GRAPH:
        model["kind"] = KIND_GRAPH
        _validate_graph(model, errors)
    elif kind == KIND_ASSEMBLY:
        model["kind"] = KIND_ASSEMBLY
        _validate_assembly(model, errors)
    else:
        _err(errors, "bad_model_kind", "$.model.kind",
             "unknown model.kind %r" % (kind,),
             "Use 'recipe', 'graph' or 'assembly'.")

    out["finish"] = _validate_finish(out.get("finish"), errors)
    out["constraints"] = _validate_constraints(out.get("constraints"), errors)
    out["assumptions"] = _validate_assumptions(out.get("assumptions"), errors)

    return {"ok": not errors, "spec": out, "errors": errors}


# --------------------------------------------------------------------- propose

def _assume(ledger: list[dict], path: str, value: Any, unit: str, why: str) -> None:
    ledger.append({
        "id": "A%d" % (len(ledger) + 1),
        "path": path,
        "value": value,
        "unit": unit,
        "basis": "design_default",
        "status": "assumed",
        "requires_confirmation": True,
        "why": why,
    })


def _statement(entry: dict) -> str:
    unit = (" " + entry["unit"]) if entry.get("unit") else ""
    return ("%s was not supplied; assumed %s%s (%s; design default, "
            "requires confirmation)."
            % (entry["path"], entry["value"], unit, entry["why"]))


def _unit_of(ps: Any) -> str:
    unit = _attr(ps, "unit", "none")
    return "" if unit in (None, "none", "") else str(unit)


def _propose_recipe(s: dict, model: dict, ledger: list[dict],
                    unresolved: list[dict], errors: list[dict]) -> None:
    rid = model.get("id")
    if not isinstance(rid, str) or not rid:
        return
    try:
        recipe = _get_recipe(rid)
    except AttaError as exc:
        errors.append(_err_from_atta(exc))
        return

    if model.get("preset") is None:
        model["preset"] = "nominal"
        _assume(ledger, "$.model.preset", "nominal", "",
                "recipe %r default preset" % rid)

    if s.get("quality") is None:
        s["quality"] = "draft"
        _assume(ledger, "$.quality", "draft", "", "default build quality")
    if s.get("material") is None:
        material = _attr(recipe, "material", "ceramic") or "ceramic"
        s["material"] = material
        _assume(ledger, "$.material", material, "",
                "recipe %r declared material" % rid)

    params = model.get("params")
    if params is None:
        params = {}
        model["params"] = params
    if not isinstance(params, dict):
        return  # validate reports bad_params
    specs = _attr(recipe, "params", {}) or {}
    if not isinstance(specs, Mapping):
        return
    for name, ps in specs.items():
        if name in params:
            continue
        default = _attr(ps, "default", None)
        if default is None:
            unresolved.append({
                "code": "missing_param",
                "path": "$.model.params.%s" % name,
                "message": "param %r has no default and was not supplied" % name,
                "fix": "Give an explicit %s value; no design default exists." % name,
            })
            continue
        params[name] = default
        _assume(ledger, "$.model.params.%s" % name, default,
                _unit_of(ps),
                "recipe ParamSpec.default for %r" % name)


def _feature_has(feat: Mapping, args: Mapping, key: str) -> bool:
    return bool(args.get(key)) or bool(feat.get(key))


def _propose_graph(s: dict, model: dict, ledger: list[dict],
                   unresolved: list[dict]) -> None:
    if model.get("units") is None:
        model["units"] = "mm"
        _assume(ledger, "$.model.units", "mm", "",
                "public spec units are millimetres")
    if model.get("segments") is None:
        model["segments"] = 64
        _assume(ledger, "$.model.segments", 64, "",
                "general default tessellation for graph primitives")
    if s.get("quality") is None:
        s["quality"] = "draft"
        _assume(ledger, "$.quality", "draft", "", "default build quality")
    if s.get("material") is None:
        s["material"] = "ceramic"
        _assume(ledger, "$.material", "ceramic", "", "default material")

    feats = model.get("features")
    if not isinstance(feats, list):
        return

    last_id = None
    for i, feat in enumerate(feats):
        if not isinstance(feat, dict):
            continue
        base = "$.model.features[%d]" % i
        if feat.get("id") is None:
            fid = "f%d" % i
            feat["id"] = fid
            _assume(ledger, base + ".id", fid, "",
                    "graph feature id derived from its position")
        if isinstance(feat.get("id"), str):
            last_id = feat["id"]

        op = feat.get("op")
        args = feat.get("args") if isinstance(feat.get("args"), Mapping) else {}
        if op == "hole" and not _feature_has(feat, args, "at"):
            unresolved.append({
                "code": "missing_at",
                "path": base + ".args.at",
                "message": "hole has no 'at' points; hole positions are design intent",
                "fix": "Give at least one [x,y] or [x,y,z] in args.at.",
            })
        if op == "revolve" and not _feature_has(feat, args, "profile"):
            unresolved.append({
                "code": "missing_profile",
                "path": base + ".args.profile",
                "message": "revolve has no profile; the axis profile is design intent",
                "fix": "Give args.profile=[[r,z],...] or a polygon profile.",
            })
        if op == "loft":
            sections = args.get("sections", feat.get("sections"))
            if not isinstance(sections, list) or len(sections) < 2:
                unresolved.append({
                    "code": "missing_sections",
                    "path": base + ".args.sections",
                    "message": "loft needs at least two sections",
                    "fix": "Give args.sections=[{profile:..., z:...}, ...].",
                })
        if op == "sweep" and not _feature_has(feat, args, "path"):
            unresolved.append({
                "code": "missing_path",
                "path": base + ".args.path",
                "message": "sweep has no path; the sweep path is design intent",
                "fix": "Give args.path as a point list.",
            })

    if model.get("output") is None and last_id is not None:
        model["output"] = last_id
        _assume(ledger, "$.model.output", last_id, "",
                "output defaults to the last feature in the graph")


def _propose_assembly(s: dict, model: dict, ledger: list[dict],
                      unresolved: list[dict], errors: list[dict]) -> None:
    if s.get("quality") is None:
        s["quality"] = "draft"
        _assume(ledger, "$.quality", "draft", "", "default build quality")
    if s.get("material") is None:
        s["material"] = "steel"
        _assume(ledger, "$.material", "steel", "",
                "default material for a mechanical assembly")

    parts = model.get("parts")
    if not isinstance(parts, list):
        return
    any_auto_ports = False
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        base = "$.model.parts[%d]" % i
        if part.get("transform") is None:
            part["transform"] = {"translate_mm": [0, 0, 0],
                                 "rotate_deg": [0, 0, 0]}
            _assume(ledger, base + ".transform",
                    {"translate_mm": [0, 0, 0], "rotate_deg": [0, 0, 0]}, "mm/deg",
                    "identity initial pose; mates may move a free part")
        if part.get("fixed") is None:
            fixed = (i == 0)
            part["fixed"] = fixed
            _assume(ledger, base + ".fixed", fixed, "",
                    "anchor the first part so the assembly has a world frame"
                    if fixed else "leave this part free for the mate solver")
        ports = part.get("ports")
        if not ports:
            any_auto_ports = True
            _assume(ledger, base + ".ports", "auto: bbox center/axes/faces", "",
                    "no ports declared; the bbox frame is a design default and "
                    "must be confirmed for a real mate")
    if model.get("mates") is None:
        model["mates"] = []
        _assume(ledger, "$.model.mates", [], "",
                "no mates supplied; the assembly keeps its initial transforms")
    if any_auto_ports:
        unresolved.append({
            "code": "auto_ports",
            "path": "$.model.parts",
            "message": "one or more parts use auto-derived bbox ports",
            "fix": "Declare ports (origin_mm, axis, normal, radius_mm) for every "
                   "mating feature; bbox ports are only a starting frame.",
        })
    if model.get("solver") is None:
        model["solver"] = {"max_nfev": 200, "tolerance_mm": 0.01}
        _assume(ledger, "$.model.solver",
                {"max_nfev": 200, "tolerance_mm": 0.01}, "",
                "numerical solver defaults; not a measurement")


def _dedupe_errors(*groups: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for group in groups:
        for err in group:
            key = (err.get("code"), err.get("path"), err.get("message"))
            if key in seen:
                continue
            seen.add(key)
            out.append(err)
    return out


def propose_spec(spec: Any) -> dict:
    """Fill explainable design defaults; never invent measurements.

    Recipe parameters come from ``ParamSpec.default``.  Graph specs get
    ``units``/``segments``/``quality``/``material`` and a default ``output``.
    Critical positioning (hole ``at``, ``revolve`` profile, ``loft`` sections,
    ``sweep`` path) is left in ``unresolved`` for the caller to supply.
    """
    if not isinstance(spec, Mapping):
        errors = [{"code": "bad_spec", "path": "$",
                   "message": "spec must be a JSON object",
                   "fix": "Pass {schema, name, model, ...}."}]
        return {"ok": False, "spec": None, "assumption_ledger": [],
                "assumption_summary": {"n": 0, "assumed": 0, "unresolved": 0,
                                       "total": 0, "requires_confirmation": 0},
                "unresolved": [], "errors": errors}

    s = copy.deepcopy(dict(spec))
    ledger: list[dict] = []
    unresolved: list[dict] = []
    errors: list[dict] = []

    if s.get("schema") is None:
        s["schema"] = SCHEMA
    if s.get("name") is None:
        s["name"] = "part"

    model = s.get("model")
    if isinstance(model, dict):
        kind = model.get("kind")
        if kind == KIND_RECIPE:
            _propose_recipe(s, model, ledger, unresolved, errors)
        elif kind == KIND_GRAPH:
            _propose_graph(s, model, ledger, unresolved)
        elif kind == KIND_ASSEMBLY:
            _propose_assembly(s, model, ledger, unresolved, errors)

    # Make prototype assumptions visible in the human-readable field.
    existing = s.get("assumptions")
    if not isinstance(existing, list):
        existing = []
    statements = [x for x in existing if isinstance(x, str)]
    for entry in ledger:
        text = _statement(entry)
        if text not in statements:
            statements.append(text)
    if statements:
        s["assumptions"] = statements

    v = validate_spec(s)
    errors = _dedupe_errors(errors, v["errors"])
    proposed = v["spec"] if v["spec"] is not None else s
    summary = {
        "n": len(ledger),
        "assumed": sum(1 for e in ledger if e["status"] == "assumed"),
        "unresolved": len(unresolved),
        "total": len(ledger),
        "requires_confirmation": sum(1 for e in ledger
                                     if e.get("requires_confirmation")),
    }
    return {
        "ok": not errors and not unresolved,
        "spec": proposed,
        "assumption_ledger": ledger,
        "assumption_summary": summary,
        "unresolved": unresolved,
        "errors": errors,
    }


# --------------------------------------------------------------------- compile

def _json_copy(value: Any) -> Any:
    """Deep copy through canonical JSON so compiled output is deterministic."""
    return json.loads(canonical_json(value))


def compile_spec(spec: Any) -> dict:
    """Validate then compile to the geometry-facing contract.

    Recipe parameters are resolved through ``Recipe.resolve``: ``params_display``
    stays in mm, ``params_m`` is in metres.  A graph is **not** evaluated here;
    its features, output, units and segments are carried through untouched.
    """
    v = validate_spec(spec)
    if not v["ok"]:
        return {"ok": False, "compiled": None, "errors": v["errors"]}

    s = v["spec"]
    model = s["model"]
    base = {
        "name": s["name"],
        "kind": model["kind"],
        "quality": s["quality"],
        "material": s["material"],
        "finish": s["finish"],
        "constraints": s["constraints"],
        "assumptions": s["assumptions"],
        "spec_hash": spec_hash(s),
    }

    errors: list[dict] = []
    if model["kind"] == KIND_ASSEMBLY:
        compiled = dict(base)
        parts_out = []
        for part in model.get("parts") or []:
            row = copy.deepcopy(dict(part))
            if part.get("recipe"):
                try:
                    recipe = _get_recipe(str(part["recipe"]))
                    resolved = recipe.resolve(
                        patch=dict(part.get("params") or {}),
                        preset=str(part.get("preset") or "nominal"))
                except Exception as exc:  # noqa: BLE001 - compile returns values
                    _err(errors, "assembly_part_resolve", "$.model.parts",
                         "part %r could not resolve: %s" % (part.get("id"), exc),
                         "Fix the part parameters; see atta_catalog.")
                    continue
                if bool(_attr(resolved, "blocked", False)):
                    _err(errors, "assembly_part_blocked", "$.model.parts",
                         "part %r recipe constraints block this parameter set"
                         % (part.get("id"),),
                         "Fix the part issues: %s"
                         % json.dumps(_attr(resolved, "issues", []) or [],
                                      ensure_ascii=False))
                    continue
                row["params_display"] = dict(_attr(resolved, "display", {}) or {})
                row["params_m"] = dict(_attr(resolved, "values", {}) or {})
                row["preset"] = str(part.get("preset") or "nominal")
            parts_out.append(row)
        compiled.update({
            "parts": parts_out,
            "mates": model.get("mates") or [],
            "solver": model.get("solver") or {},
            "clearance": model.get("clearance") or {},
            "allow_interference": model.get("allow_interference") or [],
            "params_display": {},
            "params_m": {},
            "preset": None,
        })
    elif model["kind"] == KIND_GRAPH:
        compiled = dict(base)
        compiled.update({
            "features": model["features"],
            "output": model["output"],
            "units": model["units"],
            "segments": model["segments"],
            # Graph params feed $param/$expr in feature args; they are already
            # in public units, so they are passed through unchanged.
            "params_display": dict(model.get("params") or {}),
            "params_m": dict(model.get("params") or {}),
            "preset": None,
        })
    else:
        try:
            recipe = _get_recipe(model["id"])
        except AttaError as exc:
            return {"ok": False, "compiled": None,
                    "errors": [_err_from_atta(exc)]}
        try:
            resolved = recipe.resolve(patch=dict(model["params"]),
                                      preset=model["preset"])
        except Exception as exc:  # noqa: BLE001 - recipe errors are values
            _err(errors, "resolve_failed", "$.model.params",
                 "Recipe.resolve failed: %s" % exc,
                 "Check parameter values and preset against atta_catalog.")
            return {"ok": False, "compiled": None, "errors": errors}

        if bool(_attr(resolved, "blocked", False)):
            issues = _attr(resolved, "issues", None) or []
            _err(errors, "recipe_blocked", "$.model.params",
                 "recipe constraints block this parameter set",
                 "Resolve the recipe issues: %s"
                 % (json.dumps(issues, ensure_ascii=False) if issues else "see issues"))
            return {"ok": False, "compiled": None, "errors": errors}

        display = dict(_attr(resolved, "display", None) or {})
        values = dict(_attr(resolved, "values", None) or {})
        compiled = dict(base)
        compiled.update({
            "recipe": model["id"],
            "preset": model["preset"],
            "params_display": display,
            "params_m": values,
        })

    try:
        compiled = _json_copy(compiled)
    except Exception as exc:  # noqa: BLE001 - non-JSON recipe values are a value
        _err(errors, "compile_failed", "$",
             "compiled spec is not JSON-serialisable: %s" % exc,
             "Use plain JSON numbers/strings in params, finish and constraints.")
        return {"ok": False, "compiled": None, "errors": errors}
    return {"ok": True, "compiled": compiled, "errors": errors}


# ---------------------------------------------------------------------- schema

_RECIPE_EXAMPLE = {
    "schema": SCHEMA,
    "name": "mug-01",
    "model": {
        "kind": "recipe",
        "id": "product.mug",
        "preset": "nominal",
        "params": {"height": 95},
    },
    "quality": "draft",
    "material": "ceramic",
    "finish": {"bevel_mm": 0.8, "smooth_deg": 35},
    "constraints": {
        "bbox": {"x": [60, 140]},
        "min_wall_mm": 2.5,
        "volume_mm3": [100000, 900000],
    },
    "assumptions": [],
}

_GRAPH_EXAMPLE = {
    "schema": SCHEMA,
    "name": "bracket",
    "model": {
        "kind": "graph",
        "units": "mm",
        "segments": 64,
        "output": "cut",
        "features": [
            {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
            {"id": "holes", "op": "cylinder",
             "args": {"radius": 4, "height": 20, "center": [15, 25, 3]}},
            {"id": "cut", "op": "difference",
             "args": {}, "inputs": ["plate", "holes"]},
        ],
    },
    "quality": "draft",
    "material": "steel",
    "finish": {"bevel_mm": 0.5, "smooth_deg": 30},
    "constraints": {},
    "assumptions": [],
}


_ASSEMBLY_EXAMPLE = {
    "schema": SCHEMA,
    "name": "pin_on_plate",
    "model": {
        "kind": "assembly",
        "parts": [
            {"id": "plate", "fixed": True,
             "graph": {"features": [
                 {"id": "plate", "op": "box",
                  "args": {"size": [80, 60, 10], "center": [40, 30, 5]}}]}},
            {"id": "pin",
             "graph": {"features": [
                 {"id": "pin", "op": "cylinder",
                  "args": {"radius": 5, "height": 30,
                           "center": [40, 30, 25]}}]}},
        ],
        "mates": [
            {"id": "m1", "type": "concentric", "a": "plate", "b": "pin",
             "port_a": "axis_z", "port_b": "axis_z", "offset_mm": 20},
        ],
        "solver": {"max_nfev": 200, "tolerance_mm": 0.01},
    },
    "quality": "draft",
    "material": "steel",
    "constraints": {},
    "assumptions": [],
}


def schema() -> dict:
    """Field documentation for agents; three runnable examples."""
    return {
        "schema": SCHEMA,
        "model_kinds": [KIND_RECIPE, KIND_GRAPH, KIND_ASSEMBLY],
        "fields": {
            "schema": "string; must be %r" % SCHEMA,
            "name": "non-empty string",
            "model": "object with kind='recipe', 'graph' or 'assembly'",
            "quality": "one of %s" % ", ".join(QUALITY),
            "material": "one of %s" % ", ".join(sorted(MATERIALS)),
            "finish": dict(FINISH_DOCS),
            "constraints": dict(CONSTRAINT_DOCS),
            "assumptions": "array of human-readable strings (provenance text)",
        },
        "recipe_model": {
            "kind": "recipe",
            "id": "recipe id from atta_catalog",
            "preset": "preset name; defaults to 'nominal'",
            "params": "known recipe params in mm/degrees/counts",
        },
        "graph_model": {
            "kind": "graph",
            "units": "'mm' (fixed)",
            "segments": "integer >= 3 tessellation default",
            "features": "array of {id, op, args, inputs}; a DAG in mm",
            "output": "feature id to build; defaults to the last feature",
        },
        "assembly_model": {
            "kind": "assembly",
            "parts": "non-empty array of {id, recipe|graph|spec|model_id, "
                     "transform:{translate_mm,rotate_deg,matrix}, fixed, ports, "
                     "material, quality, preset, params}",
            "mates": "array of {id, type, a, b, port_a, port_b, offset_mm, "
                     "value_mm, angle_deg, align, weight, allow_interference}",
            "mate_types": list(MATE_TYPES),
            "ports": "declared ports override auto bbox ports: "
                     "{name, origin_mm, axis, normal, radius_mm, kind}; "
                     "auto ports are center, axis_x/y/z, face_x-/x+/...",
            "solver": "{max_nfev, tolerance_mm}; least-squares mate solve",
            "clearance": "{threshold_mm} for sampled clearance checks",
            "allow_interference": "array of {a,b} pairs allowed to overlap "
                                  "(press fits, threads)",
        },
        "constraints": dict(CONSTRAINT_DOCS),
        "finish": dict(FINISH_DOCS),
        "quality": list(QUALITY),
        "material": sorted(MATERIALS),
        "examples": {
            "recipe": copy.deepcopy(_RECIPE_EXAMPLE),
            "graph": copy.deepcopy(_GRAPH_EXAMPLE),
            "assembly": copy.deepcopy(_ASSEMBLY_EXAMPLE),
        },
        "assumption_ledger_entry": {
            "id": "A1",
            "path": "$.model.params.height",
            "value": 95,
            "unit": "mm",
            "basis": "design_default",
            "status": "assumed",
            "requires_confirmation": True,
            "why": "recipe ParamSpec.default for 'height'",
        },
        "assumption_rule": (
            "propose may fill design defaults, never measurements. Critical "
            "positioning (hole at, revolve profile, loft sections, sweep path) "
            "is returned in unresolved and must be supplied explicitly."
        ),
        "units": "mm and degrees at this boundary; graph args are mm",
    }


__all__ = [
    "SCHEMA",
    "KIND_RECIPE",
    "KIND_GRAPH",
    "KIND_ASSEMBLY",
    "MATE_TYPES",
    "QUALITY",
    "MATERIALS",
    "CONSTRAINT_DOCS",
    "FINISH_DOCS",
    "canonical_json",
    "spec_hash",
    "validate_spec",
    "propose_spec",
    "compile_spec",
    "schema",
]
