# -*- coding: utf-8 -*-
"""Unit conventions.

Public API is **millimetres** and **degrees**; the geometry kernel is
**metres** and **radians**.  Conversion happens at exactly two boundaries:
spec/feature argument parsing and Blender export.  Nothing in between guesses.
"""

from __future__ import annotations

import math

MM = 0.001
CM = 0.01
M = 1.0
INCH = 0.0254

_SCALE = {"mm": MM, "cm": CM, "m": M, "in": INCH, "inch": INCH}


def length_scale(unit: str | None) -> float:
    key = str(unit or "mm").strip().lower()
    if key not in _SCALE:
        raise ValueError("unsupported length unit %r (mm/cm/m/in)" % (unit,))
    return _SCALE[key]


def to_m(value: float, unit: str = "mm") -> float:
    return float(value) * length_scale(unit)


def to_mm(value_m: float) -> float:
    return float(value_m) * 1000.0


def to_mm3(value_m3: float) -> float:
    return float(value_m3) * 1e9


def to_mm2(value_m2: float) -> float:
    return float(value_m2) * 1e6


def mm_list(values, unit: str = "mm") -> list[float]:
    return [to_m(v, unit) for v in values]


def deg2rad(value: float) -> float:
    return float(value) * math.pi / 180.0


def rad2deg(value: float) -> float:
    return float(value) * 180.0 / math.pi


def fmt_length(value_m: float, digits: int = 3) -> str:
    return ("%." + str(int(digits)) + "f mm") % to_mm(value_m)


__all__ = ["MM", "CM", "M", "INCH", "length_scale", "to_m", "to_mm",
           "to_mm2", "to_mm3", "mm_list", "deg2rad", "rad2deg", "fmt_length"]
