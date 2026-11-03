# -*- coding: utf-8 -*-
"""Recipe registry: typed parameters, resolution, presets, constraints.

Split out of :mod:`atta.recipes` so that family modules (``atta/families/*``)
can register themselves without importing the built-in builders.  Everything
here is re-exported by :mod:`atta.recipes` for backwards compatibility.
"""

from __future__ import annotations

import copy
import math

from .errors import AttaError
from .features import _eval_expr

__all__ = ["ParamSpec", "Recipe", "Resolved", "recipe", "register", "get",
           "all_recipes", "REGISTRY"]

_LENGTH = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}
_UNITLESS = ("none", "ratio", "count", "deg")


# ---------------------------------------------------------------- registry

class ParamSpec:
    """One typed recipe parameter."""

    __slots__ = ("type", "default", "min", "max", "unit", "description", "group",
                 "advanced", "enum")

    def __init__(self, type="float", default=None, min=None, max=None, unit="none",
                 description="", group="", advanced=False, enum=None):
        self.type = str(type)
        self.default = default
        self.min = min
        self.max = max
        self.unit = str(unit)
        self.description = str(description)
        self.group = str(group)
        self.advanced = bool(advanced)
        self.enum = list(enum) if enum is not None else None

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, cls):
            return data
        data = dict(data or {})
        allowed = set(cls.__slots__)
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def to_dict(self):
        out = {"type": self.type, "default": self.default, "unit": self.unit,
               "description": self.description, "group": self.group,
               "advanced": self.advanced}
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.enum is not None:
            out["enum"] = list(self.enum)
        return out

    def coerce(self, value):
        if self.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "int":
            if isinstance(value, bool):
                raise ValueError("expected an integer, got a bool")
            x = float(value)
            if not math.isfinite(x) or abs(x - round(x)) > 1e-9:
                raise ValueError("expected an integer")
            return int(round(x))
        if self.type == "float":
            x = float(value)
            if not math.isfinite(x):
                raise ValueError("expected a finite number")
            return x
        if self.type == "enum":
            if value not in (self.enum or []):
                raise ValueError("expected one of %r" % (self.enum or [],))
            return value
        if self.type == "str":
            return str(value)
        return value


class Resolved:
    """Resolved values: ``display`` in recipe units, ``values`` in metres."""

    __slots__ = ("values", "display", "issues", "applied_patches", "blocked")

    def __init__(self, values, display, issues=None, applied_patches=None, blocked=False):
        self.values = dict(values)
        self.display = dict(display)
        self.issues = list(issues or [])
        self.applied_patches = list(applied_patches or [])
        self.blocked = bool(blocked)

    def to_dict(self):
        return {"values": self.values, "display": self.display, "issues": self.issues,
                "applied_patches": self.applied_patches, "blocked": self.blocked}


class Recipe:
    """A named parametric family with a deterministic builder."""

    def __init__(self, id, name, description, params, presets=None, constraints=None,
                 expected_genus=0, material="ceramic", tags=None, build=None):
        self.id = str(id)
        self.name = str(name)
        self.description = str(description)
        self.params = {str(k): ParamSpec.from_dict(v) for k, v in dict(params).items()}
        self.presets = copy.deepcopy(dict(presets or {}))
        self.constraints = copy.deepcopy(list(constraints or []))
        self.expected_genus = expected_genus
        self.material = str(material)
        self.tags = list(tags or [])
        self.build = build

    def resolve(self, patch=None, preset="nominal") -> Resolved:
        patch = dict(patch or {})
        raw = {k: spec.default for k, spec in self.params.items()}
        raw.update(self.presets.get("nominal", {}))
        if preset and preset != "nominal":
            if preset not in self.presets:
                raise AttaError("unknown_preset", "unknown preset %r for %s" % (preset, self.id),
                                path="preset",
                                fix="use one of: " + ", ".join(sorted(self.presets)))
            raw.update(self.presets[preset])

        issues, applied, blocked = [], [], False
        for key in sorted(set(patch) - set(self.params)):
            issues.append({"code": "unknown_param", "severity": "warn", "path": key,
                           "message": "%s has no parameter %r; it was ignored" % (self.id, key),
                           "fix": "use one of: " + ", ".join(sorted(self.params))})
        raw.update({k: v for k, v in patch.items() if k in self.params})

        display, values = {}, {}
        for key, spec in self.params.items():
            value = raw.get(key, spec.default)
            try:
                value = spec.coerce(value)
            except (TypeError, ValueError) as exc:
                issues.append({"code": "bad_param", "severity": "error", "path": key,
                               "message": "%s: %s" % (key, exc), "actual": repr(value),
                               "expected": spec.type,
                               "fix": "pass a %s for %s" % (spec.type, key)})
                blocked = True
                try:
                    value = spec.coerce(spec.default)
                except (TypeError, ValueError):
                    continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                display[key] = value
                values[key] = value
                continue
            if spec.min is not None and value < spec.min:
                issues.append({"code": "param_out_of_range", "severity": "warn", "path": key,
                               "message": "%s clamped to minimum %s" % (key, spec.min),
                               "actual": value, "expected": ">= %s" % spec.min,
                               "fix": "%s = %s" % (key, spec.min)})
                value = spec.min
                applied.append({"op": "clamp", "path": key, "value": spec.min})
            if spec.max is not None and value > spec.max:
                issues.append({"code": "param_out_of_range", "severity": "warn", "path": key,
                               "message": "%s clamped to maximum %s" % (key, spec.max),
                               "actual": value, "expected": "<= %s" % spec.max,
                               "fix": "%s = %s" % (key, spec.max)})
                value = spec.max
                applied.append({"op": "clamp", "path": key, "value": spec.max})
            display[key] = value
            values[key] = value * _LENGTH[spec.unit] if spec.unit in _LENGTH else value

        for con in self.constraints:
            expr = con.get("expr")
            if not expr:
                continue
            try:
                ok = bool(_eval_expr(expr, dict(display), "constraint." + str(con.get("id", "?")),
                                     allow_bool=True))
            except AttaError as exc:
                issues.append({"code": "constraint_error", "severity": "error",
                               "path": str(con.get("id", "constraint")),
                               "message": exc.message, "fix": exc.fix})
                blocked = True
                continue
            if not ok:
                sev = str(con.get("severity", "error"))
                issues.append({"code": str(con.get("id", "constraint_failed")),
                               "severity": sev,
                               "path": str(con.get("id", "constraint")),
                               "message": str(con.get("message")
                                              or "constraint failed: %s" % expr),
                               "fix": con.get("fix")})
                if sev == "error":
                    blocked = True
        return Resolved(values, display, issues, applied, blocked)

    def build_shape(self, values, quality="draft"):
        if self.build is None:
            raise AttaError("no_builder", "recipe %s has no builder" % self.id,
                            path="recipe", fix="choose a registered recipe")
        return self.build(dict(values), str(quality))

    def to_dict(self, verbose=False):
        out = {"id": self.id, "name": self.name, "description": self.description,
               "tags": list(self.tags), "presets": list(self.presets),
               "n_params": len(self.params), "expected_genus": self.expected_genus,
               "material": self.material}
        if verbose:
            out["params"] = {k: v.to_dict() for k, v in self.params.items()}
            out["presets"] = copy.deepcopy(self.presets)
            out["constraints"] = copy.deepcopy(self.constraints)
        return out


REGISTRY: dict[str, Recipe] = {}


def register(rec) -> Recipe:
    if not isinstance(rec, Recipe):
        raise AttaError("bad_arg", "register expects a Recipe", path="recipe",
                        fix="construct Recipe(...)")
    if rec.id in REGISTRY:
        raise AttaError("duplicate_recipe", "recipe id %r is already registered" % rec.id,
                        path="recipe.id", fix="choose a unique recipe id")
    REGISTRY[rec.id] = rec
    return rec


def get(recipe_id) -> Recipe:
    key = str(recipe_id)
    if key not in REGISTRY:
        raise AttaError("unknown_recipe", "unknown recipe %r" % key, path="recipe",
                        fix="available recipes: " + ", ".join(sorted(REGISTRY)))
    return REGISTRY[key]


def all_recipes():
    return [REGISTRY[k] for k in sorted(REGISTRY)]


def recipe(recipe_id, name, description, params, presets=None, constraints=None,
           expected_genus=0, material="ceramic", tags=None):
    def deco(fn):
        register(Recipe(id=recipe_id, name=name, description=description, params=params,
                        presets=presets, constraints=constraints,
                        expected_genus=expected_genus, material=material,
                        tags=tags, build=fn))
        return fn
    return deco


