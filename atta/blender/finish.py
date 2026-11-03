# -*- coding: utf-8 -*-
"""Finish normalisation: one canonical dict, metres and degrees.

A finish is the *only* thing the Blender backend adds on top of the numpy
geometry: bevel, smooth-by-angle, material.  Callers may write it in the
public millimetre vocabulary (``bevel_mm``, ``smooth_deg``) or in the internal
metre vocabulary (``bevel.width``); :func:`normalize_finish` folds both into::

    {
      "smooth": True,
      "smooth_angle_deg": 35.0,
      "bevel": {"width": 0.0008, "segments": 2, "angle_deg": 40.0},
      "material": "ceramic",
      "extra": {},
    }

``bevel.width`` is **metres**; every other length the user sees is mm.  A
zero width disables the bevel entirely rather than creating a no-op modifier.
"""

from __future__ import annotations

import copy

from .. import materials

__all__ = [
    "FINISH_DEFAULTS", "normalize_finish", "material_spec", "finish_key",
    "VIEWS",
]

VIEWS = ("iso", "front", "top", "left", "right", "back",
         "iso_l", "iso_r", "hero", "hero_l", "hero_r")

FINISH_DEFAULTS: dict = {
    "smooth": True,
    "smooth_angle_deg": 35.0,
    "bevel": {"width": 0.0, "segments": 2, "angle_deg": 40.0},
    "material": materials.DEFAULT,
    "extra": {},
}

_ALIASES = {
    "smooth_deg": "smooth_angle_deg",
    "smooth_angle": "smooth_angle_deg",
    "bevel_mm": "bevel_mm",
    "bevel_width_mm": "bevel_width_mm",
    "bevel_segments": "bevel_segments",
    "smooth": "smooth",
}


def _num(value, key: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError("finish.%s must be a number, got %r" % (key, value)) from None
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError("finish.%s must be finite, got %r" % (key, value))
    return out


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def normalize_finish(finish: dict | None = None) -> dict:
    """Return the canonical finish dict, validating material and clamping.

    Accepted spellings::

        {"bevel_mm": 0.8, "smooth_deg": 35, "material": "ceramic"}
        {"bevel": {"width_mm": 0.8, "segments": 3, "angle_deg": 40}}
        {"bevel": {"width": 0.0008, "segments": 2}}

    A bare number for ``bevel`` is metres (internal vocabulary).  Unknown
    keys are preserved under ``extra`` so a caller can extend the pipeline
    without losing data.  Raises ``ValueError`` for a bad type or an unknown
    material name.
    """
    if finish is None:
        finish = {}
    if not isinstance(finish, dict):
        raise ValueError("finish must be a dict or None, got %r" % (type(finish).__name__,))

    out = copy.deepcopy(FINISH_DEFAULTS)
    extra: dict = {}

    for key, value in finish.items():
        if key == "bevel":
            continue
        if key == "material":
            out["material"] = value
            continue
        if key in _ALIASES:
            target = _ALIASES[key]
            if target in ("smooth_angle_deg",):
                out["smooth_angle_deg"] = _clamp(_num(value, key), 0.0, 180.0)
            elif target == "bevel_mm":
                out["bevel"]["width"] = _clamp(_num(value, key), 0.0, 1e9) / 1000.0
            elif target == "bevel_width_mm":
                out["bevel"]["width"] = _clamp(_num(value, key), 0.0, 1e9) / 1000.0
            elif target == "bevel_segments":
                out["bevel"]["segments"] = int(_clamp(_num(value, key), 0, 32))
            elif target == "smooth":
                out["smooth"] = bool(value)
            else:
                extra[key] = value
            continue
        if key == "smooth_angle_deg":
            out["smooth_angle_deg"] = _clamp(_num(value, key), 0.0, 180.0)
            continue
        extra[key] = value

    bevel = finish.get("bevel")
    if bevel is not None:
        if isinstance(bevel, (int, float)) and not isinstance(bevel, bool):
            out["bevel"]["width"] = _clamp(_num(bevel, "bevel"), 0.0, 1e9)
        elif isinstance(bevel, dict):
            resolved = {}
            for key, value in bevel.items():
                if key == "width":
                    resolved["width"] = _clamp(_num(value, "bevel.width"), 0.0, 1e9)
                elif key == "width_mm":
                    resolved["width"] = _clamp(_num(value, "bevel.width_mm"), 0.0, 1e9) / 1000.0
                elif key == "segments":
                    resolved["segments"] = int(_clamp(_num(value, "bevel.segments"), 0, 32))
                elif key in ("angle", "angle_deg"):
                    resolved["angle_deg"] = _clamp(_num(value, "bevel.angle_deg"), 0.0, 180.0)
                else:
                    extra["bevel.%s" % key] = value
            out["bevel"].update(resolved)
        else:
            raise ValueError("finish.bevel must be a dict or a number, got %r"
                             % (type(bevel).__name__,))

    if not isinstance(out["material"], (str, dict)):
        raise ValueError("finish.material must be a preset name, got %r"
                         % (out["material"],))
    ok, errors = materials.validate(out["material"])
    if not ok:
        raise ValueError(errors[0]["message"] + " — " + errors[0]["fix"])

    if not out["smooth"]:
        # A disabled smooth pass makes the angle meaningless; keep 0 for honesty.
        out["smooth_angle_deg"] = 0.0
    out["bevel"]["segments"] = int(out["bevel"]["segments"])
    if out["bevel"]["width"] <= 0.0:
        out["bevel"]["segments"] = 0
    out["extra"] = extra
    return out


def material_spec(finish: dict | None = None) -> dict:
    """Resolve the finish's material into a flat BSDF parameter dict."""
    norm = normalize_finish(finish)
    return materials.resolve(norm["material"])


def finish_key(finish: dict | None = None) -> str:
    """Stable JSON key for render caching."""
    import json

    norm = normalize_finish(finish)
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), default=list)
