# -*- coding: utf-8 -*-
"""Shared builder helpers for recipes and family modules.

Kernel-facing conventions
-------------------------
* Lengths are **metres** inside builders (``resolve()`` already converted the
  display millimetre values), so ``wall=3`` mm arrives as ``0.003``.
* A builder returns ``(mesh, finish)``; ``finish`` comes from :func:`finish`.
* Prefer composing primitives/``surfaces`` and the boolean kernel over
  hand-built vertex arrays — family code should stay readable.
"""

from __future__ import annotations

import math

import numpy as np

from . import primitives as _P
from . import surfaces as _S

__all__ = ["quality_level", "finish", "circle_loop", "ring_at", "dedupe_profile",
           "wall_profile", "closed_wall", "mug_radius", "rounded_rect",
           "gear_loop"]


def quality_level(quality, draft, review=None, hero=None):
    q = str(quality or "draft").lower()
    if q in ("hero", "high", "final", "deliver"):
        return int(hero if hero is not None else (review if review is not None else draft))
    if q in ("review", "standard", "medium", "med"):
        return int(review if review is not None else draft)
    return int(draft)


def finish(bevel_width=None, bevel_segments=2, bevel_angle_deg=40.0,
            material=None, smooth_angle_deg=35.0):
    out = {"smooth": True, "smooth_angle_deg": float(smooth_angle_deg)}
    if bevel_width is not None:
        out["bevel"] = {"width": float(bevel_width), "segments": int(bevel_segments),
                        "angle_deg": float(bevel_angle_deg)}
    if material:
        out["material"] = str(material)
    return out


def circle_loop(radius, n):
    n = max(8, int(n))
    a = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
    return np.column_stack([float(radius) * np.cos(a), float(radius) * np.sin(a)])


def ring_at(loop_xy, z):
    p = np.asarray(loop_xy, dtype=np.float64)
    return np.column_stack([p[:, 0], p[:, 1], np.full(len(p), float(z))])


def dedupe_profile(p, tol=1e-9):
    p = np.asarray(p, dtype=np.float64)
    keep = [0]
    for i in range(1, len(p)):
        if np.linalg.norm(p[i] - p[keep[-1]]) > tol:
            keep.append(i)
    return p[keep]


def wall_profile(outer, wall, base_t):
    outer = np.asarray(outer, dtype=np.float64)
    src = outer[outer[:, 1] >= float(base_t) - 1e-12]
    if len(src) < 2:
        src = outer[-1:]
    inner = np.column_stack([np.maximum(1e-4, src[:, 0] - float(wall)), src[:, 1]])
    return inner[::-1]


def closed_wall(outer, inner, base_t):
    return np.vstack([np.asarray(outer, dtype=np.float64),
                      np.asarray(inner, dtype=np.float64),
                      [[0.0, float(base_t)], [0.0, 0.0]]])


def mug_radius(z, height, base, belly, top):
    t = np.clip(np.asarray(z, dtype=np.float64) / float(height), 0.0, 1.0)
    return base + (belly - base) * np.sin(np.pi * t) ** 0.9 + (top - belly) * t ** 3


def rounded_rect(length, width, radius, per_corner=8):
    L, W, r = float(length), float(width), float(radius)
    r = max(0.0, min(r, 0.49 * min(L, W)))
    if r <= 1e-12:
        return np.array([[-L / 2, -W / 2], [L / 2, -W / 2],
                         [L / 2, W / 2], [-L / 2, W / 2]], dtype=np.float64)
    corners = [(L / 2 - r, -W / 2 + r, -90.0, 0.0),
               (L / 2 - r, W / 2 - r, 0.0, 90.0),
               (-L / 2 + r, W / 2 - r, 90.0, 180.0),
               (-L / 2 + r, -W / 2 + r, 180.0, 270.0)]
    pts = []
    k = max(2, int(per_corner))
    for cx, cy, a0, a1 in corners:
        for a in np.linspace(math.radians(a0), math.radians(a1), k + 1):
            pts.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    return np.asarray(pts, dtype=np.float64)


def gear_loop(teeth, pitch_radius, module, tip_ratio=0.32, root_ratio=0.54):
    teeth = int(teeth)
    if teeth < 4:
        raise AttaError("bad_arg", "gear needs >= 4 teeth", path="teeth",
                        fix="use teeth >= 4")
    rp = float(pitch_radius)
    add = float(module)
    ded = 1.25 * float(module)
    ro, rr = rp + add, max(rp * 0.2, rp - ded)
    step = 2.0 * math.pi / teeth
    tip_half = step * float(tip_ratio) * 0.5
    root_half = step * float(root_ratio) * 0.5
    pts = []
    for i in range(teeth):
        a = i * step
        pts.append([rr * math.cos(a - root_half), rr * math.sin(a - root_half)])
        pts.append([ro * math.cos(a - tip_half), ro * math.sin(a - tip_half)])
        pts.append([ro * math.cos(a + tip_half), ro * math.sin(a + tip_half)])
        pts.append([rr * math.cos(a + root_half), rr * math.sin(a + root_half)])
    return np.asarray(pts, dtype=np.float64)


