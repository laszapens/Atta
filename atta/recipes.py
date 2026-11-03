# -*- coding: utf-8 -*-
"""Curated parametric families.

Display units are **millimetres**; ``resolve()`` returns both ``display``
(recipe units) and ``values`` (metres for the kernel).  Angles stay in
degrees, ratios/counts are unitless.  Every recipe exposes safe/nominal/hero
presets, parameter bounds, constraints, an expected genus and a material.

The registry machinery lives in :mod:`atta.registry`; family modules live in
``atta/families/`` and register themselves on import (see
``atta/families/__init__.py``).
"""

from __future__ import annotations

import copy
import math

import numpy as np

from . import curves as _C
from . import primitives as _P
from . import surfaces as _S
from .boolean import difference, union
from .errors import AttaError
from .features import _eval_expr
from .mesh import Mesh
from .registry import (ParamSpec, Recipe, Resolved, all_recipes, get, recipe,
                       register, REGISTRY)

__all__ = ["ParamSpec", "Recipe", "Resolved", "recipe", "register", "get",
           "all_recipes"]

_LENGTH = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254}
_UNITLESS = ("none", "ratio", "count", "deg")


# ---------------------------------------------------------------- helpers
# Shared builder helpers live in :mod:`atta.kit`; the underscore aliases are
# kept so the built-in builders below read unchanged.
from .kit import (circle_loop as _circle, closed_wall as _closed_wall,
                  dedupe_profile as _dedupe_profile, finish as _finish,
                  gear_loop as _gear_loop, mug_radius as _mug_radius,
                  quality_level as _q, ring_at as _ring_at,
                  rounded_rect as _rounded_rect, wall_profile as _wall_profile)

# ---------------------------------------------------------------- products

@recipe(
    "product.mug", "带把手的马克杯",
    "旋转体薄壁杯身 + 平行传输扫掠把手 + 布尔并集。handle_enabled=False 时 genus 为 0。",
    params={
        "height": {"type": "float", "default": 95.0, "min": 60.0, "max": 140.0,
                   "unit": "mm", "group": "body", "description": "杯身总高"},
        "outer_radius": {"type": "float", "default": 42.0, "min": 28.0, "max": 60.0,
                        "unit": "mm", "group": "body", "description": "腹部最大外半径"},
        "base_radius": {"type": "float", "default": 30.0, "min": 20.0, "max": 45.0,
                        "unit": "mm", "group": "body"},
        "top_radius": {"type": "float", "default": 38.0, "min": 24.0, "max": 55.0,
                       "unit": "mm", "group": "body"},
        "wall": {"type": "float", "default": 3.5, "min": 2.0, "max": 6.0,
                 "unit": "mm", "group": "shell", "description": "杯壁厚度"},
        "base_thickness": {"type": "float", "default": 5.0, "min": 2.5, "max": 10.0,
                           "unit": "mm", "group": "body"},
        "handle_enabled": {"type": "bool", "default": True, "unit": "none", "group": "handle"},
        "handle_gap": {"type": "float", "default": 28.0, "min": 16.0, "max": 45.0,
                       "unit": "mm", "group": "handle"},
        "handle_tube_r": {"type": "float", "default": 5.0, "min": 3.0, "max": 8.0,
                          "unit": "mm", "group": "handle"},
        "handle_z_top": {"type": "float", "default": 0.72, "min": 0.55, "max": 0.86,
                         "unit": "ratio", "group": "handle"},
        "handle_z_bottom": {"type": "float", "default": 0.30, "min": 0.14, "max": 0.45,
                            "unit": "ratio", "group": "handle"},
        "segments": {"type": "int", "default": 96, "min": 32, "max": 256,
                     "unit": "count", "group": "quality", "advanced": True},
        "profile_samples": {"type": "int", "default": 64, "min": 24, "max": 192,
                            "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"wall": 4.0, "handle_tube_r": 5.5, "handle_gap": 26.0,
                 "segments": 64, "profile_samples": 48},
        "nominal": {"wall": 3.5, "handle_tube_r": 5.0, "handle_gap": 28.0,
                    "segments": 96, "profile_samples": 64},
        "hero": {"wall": 3.2, "handle_tube_r": 4.8, "handle_gap": 30.0,
                 "segments": 160, "profile_samples": 112},
    },
    tags=["product", "revolve", "sweep", "boolean", "shell", "handle"],
    expected_genus=1, material="ceramic",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= min(base_radius, top_radius) * 0.28",
         "severity": "error", "message": "杯壁过厚，内腔会消失",
         "fix": "wall = min(base_radius, top_radius) * 0.22"},
        {"id": "bottom_fits", "expr": "base_thickness <= height * 0.28",
         "severity": "error", "fix": "base_thickness = height * 0.15"},
        {"id": "handle_fits", "expr": "handle_tube_r <= outer_radius * 0.28",
         "severity": "warn", "fix": "handle_tube_r = outer_radius * 0.22"},
        {"id": "handle_height", "expr": "handle_gap <= height * 0.65",
         "severity": "error", "fix": "handle_gap = height * 0.5"},
    ],
)
def _build_mug(v, quality="draft"):
    H, rb = float(v["height"]), float(v["base_radius"])
    belly, top = float(v["outer_radius"]), float(v["top_radius"])
    wall, base_t = float(v["wall"]), float(v["base_thickness"])
    n, seg = max(12, int(v["profile_samples"])), max(16, int(v["segments"]))

    z_out = np.linspace(0.0, H, n)
    outer = np.column_stack([_mug_radius(z_out, H, rb, belly, top), z_out])
    z_in = np.linspace(H, base_t, n)
    inner = np.column_stack([np.maximum(1e-4, _mug_radius(z_in, H, rb, belly, top) - wall),
                             z_in])
    body = _S.revolve(_closed_wall(outer, inner, base_t), seg)

    if bool(v.get("handle_enabled", True)):
        tube_r, gap = float(v["handle_tube_r"]), float(v["handle_gap"])
        z1 = H * float(v["handle_z_top"])
        z2 = H * float(v["handle_z_bottom"])
        r1 = float(_mug_radius(np.array([z1]), H, rb, belly, top)[0])
        r2 = float(_mug_radius(np.array([z2]), H, rb, belly, top)[0])
        depth = max(3e-4, 0.55 * wall)
        reach = max(r1, r2) + gap
        ctrl = np.array([
            [r1 - depth, 0.0, z1],
            [reach, 0.0, z1 + 0.05 * H],
            [reach, 0.0, 0.5 * (z1 + z2)],
            [reach, 0.0, z2 - 0.05 * H],
            [r2 - depth, 0.0, z2],
        ])
        path = _C.catmull_rom(ctrl, _q(quality, 8, 14, 24))
        section = _circle(tube_r, _q(quality, 12, 16, 24))
        body = union(body, _S.sweep(section, path))
    finish = _finish(bevel_width=min(0.0012, wall * 0.28),
                     bevel_segments=_q(quality, 1, 2, 3),
                     bevel_angle_deg=45.0, material="ceramic")
    return body, finish


@recipe(
    "product.bowl", "花瓣薄壁碗",
    "旋转体薄壁碗 + 径向花瓣/扭转；由 Catmull-Rom 母线生成内外壁。",
    params={
        "height": {"type": "float", "default": 65.0, "min": 35.0, "max": 120.0,
                   "unit": "mm", "group": "body"},
        "base_radius": {"type": "float", "default": 30.0, "min": 16.0, "max": 50.0,
                        "unit": "mm", "group": "body"},
        "belly_radius": {"type": "float", "default": 66.0, "min": 35.0, "max": 100.0,
                         "unit": "mm", "group": "body"},
        "top_radius": {"type": "float", "default": 74.0, "min": 40.0, "max": 110.0,
                       "unit": "mm", "group": "body"},
        "wall": {"type": "float", "default": 3.6, "min": 1.8, "max": 5.5,
                 "unit": "mm", "group": "shell",
                 "description": "nominal wall; lobe modulation thins the sampled "
                                "minimum to about 0.46x, so 3.6 mm samples ~1.66 mm"},
        "base_thickness": {"type": "float", "default": 4.5, "min": 2.0, "max": 9.0,
                           "unit": "mm", "group": "body"},
        "lobes": {"type": "int", "default": 12, "min": 0, "max": 32,
                  "unit": "count", "group": "style"},
        "lobe_amp": {"type": "float", "default": 0.035, "min": 0.0, "max": 0.12,
                     "unit": "ratio", "group": "style"},
        "twist_deg": {"type": "float", "default": 0.0, "min": -180.0, "max": 180.0,
                      "unit": "deg", "group": "style"},
        "segments": {"type": "int", "default": 96, "min": 32, "max": 256,
                     "unit": "count", "group": "quality", "advanced": True},
        "profile_samples": {"type": "int", "default": 56, "min": 20, "max": 160,
                            "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"wall": 4.2, "lobes": 8, "segments": 64, "profile_samples": 40},
        "nominal": {"wall": 3.6, "lobes": 12, "segments": 96, "profile_samples": 56},
        "hero": {"wall": 3.4, "lobes": 16, "segments": 160, "profile_samples": 96},
    },
    tags=["product", "revolve", "lobes", "shell"],
    expected_genus=0, material="ceramic",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= min(base_radius, top_radius) * 0.26",
         "severity": "error", "fix": "wall = min(base_radius, top_radius) * 0.2"},
        {"id": "base_fits", "expr": "base_thickness <= height * 0.3",
         "severity": "error", "fix": "base_thickness = height * 0.15"},
    ],
)
def _build_bowl(v, quality="draft"):
    H, rb = float(v["height"]), float(v["base_radius"])
    belly, top = float(v["belly_radius"]), float(v["top_radius"])
    wall, base_t = float(v["wall"]), float(v["base_thickness"])
    n, seg = max(12, int(v["profile_samples"])), max(16, int(v["segments"]))
    ctrl = np.array([[rb, 0.0],
                     [rb + (belly - rb) * 0.55, 0.18 * H],
                     [belly, 0.48 * H],
                     [belly * 0.94, 0.72 * H],
                     [top, H]])
    outer = _dedupe_profile(_C.catmull_rom(ctrl, max(4, n // 4)))
    inner = _wall_profile(outer, wall, base_t)
    out = _S.revolve(_closed_wall(outer, inner, base_t), seg)
    k = int(v.get("lobes", 0))
    if k > 0 and float(v.get("lobe_amp", 0.0)) > 1e-6:
        out = _S.lobes(out, k, float(v["lobe_amp"]), 0.0, float(v.get("twist_deg", 0.0)))
    return out, _finish(bevel_width=min(0.0008, wall * 0.22),
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=42.0, material="ceramic")


@recipe(
    "product.vase", "扭转花瓣花瓶",
    "Catmull-Rom 母线旋转体 + 颈口收缩 + 径向花瓣/沿轴扭转。",
    params={
        "height": {"type": "float", "default": 165.0, "min": 80.0, "max": 300.0,
                   "unit": "mm", "group": "body"},
        "base_radius": {"type": "float", "default": 34.0, "min": 18.0, "max": 60.0,
                        "unit": "mm", "group": "body"},
        "belly_radius": {"type": "float", "default": 52.0, "min": 28.0, "max": 90.0,
                         "unit": "mm", "group": "body"},
        "neck_radius": {"type": "float", "default": 22.0, "min": 10.0, "max": 45.0,
                        "unit": "mm", "group": "body"},
        "lip_radius": {"type": "float", "default": 26.0, "min": 12.0, "max": 50.0,
                       "unit": "mm", "group": "body"},
        "wall": {"type": "float", "default": 3.0, "min": 1.8, "max": 5.5,
                 "unit": "mm", "group": "shell"},
        "base_thickness": {"type": "float", "default": 5.0, "min": 2.0, "max": 10.0,
                           "unit": "mm", "group": "body"},
        "lobes": {"type": "int", "default": 10, "min": 0, "max": 32,
                  "unit": "count", "group": "style"},
        "lobe_amp": {"type": "float", "default": 0.03, "min": 0.0, "max": 0.12,
                     "unit": "ratio", "group": "style"},
        "twist_deg": {"type": "float", "default": 75.0, "min": -240.0, "max": 240.0,
                      "unit": "deg", "group": "style"},
        "segments": {"type": "int", "default": 112, "min": 32, "max": 320,
                     "unit": "count", "group": "quality", "advanced": True},
        "profile_samples": {"type": "int", "default": 72, "min": 24, "max": 200,
                            "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"wall": 3.5, "lobes": 8, "segments": 72, "profile_samples": 48},
        "nominal": {"wall": 3.0, "lobes": 10, "segments": 112, "profile_samples": 72},
        "hero": {"wall": 2.5, "lobes": 14, "segments": 192, "profile_samples": 120},
    },
    tags=["product", "revolve", "catmull", "lobes", "twist", "shell"],
    expected_genus=0, material="quartz",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= neck_radius * 0.32",
         "severity": "error", "fix": "wall = neck_radius * 0.25"},
        {"id": "base_fits", "expr": "base_thickness <= height * 0.2",
         "severity": "error", "fix": "base_thickness = height * 0.1"},
        {"id": "neck_below_lip", "expr": "neck_radius <= lip_radius * 1.2",
         "severity": "warn"},
    ],
)
def _build_vase(v, quality="draft"):
    H, rb = float(v["height"]), float(v["base_radius"])
    belly, neck, lip = float(v["belly_radius"]), float(v["neck_radius"]), float(v["lip_radius"])
    wall, base_t = float(v["wall"]), float(v["base_thickness"])
    n, seg = max(12, int(v["profile_samples"])), max(16, int(v["segments"]))
    ctrl = np.array([[rb, 0.0],
                     [rb + (belly - rb) * 0.72, 0.16 * H],
                     [belly, 0.42 * H],
                     [belly * 0.86, 0.64 * H],
                     [neck, 0.86 * H],
                     [lip, H]])
    outer = _dedupe_profile(_C.catmull_rom(ctrl, max(5, n // 5)))
    inner = _wall_profile(outer, wall, base_t)
    out = _S.revolve(_closed_wall(outer, inner, base_t), seg)
    k = int(v.get("lobes", 0))
    if k > 0 and float(v.get("lobe_amp", 0.0)) > 1e-6:
        out = _S.lobes(out, k, float(v["lobe_amp"]), 0.0, float(v.get("twist_deg", 0.0)))
    return out, _finish(bevel_width=min(0.0009, wall * 0.24),
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=42.0, material="quartz")


@recipe(
    "product.bottle", "放样中空瓶",
    "多截面放样外形 + 内腔放样 + 布尔差集；内腔抬高开口。",
    params={
        "height": {"type": "float", "default": 220.0, "min": 100.0, "max": 360.0,
                   "unit": "mm", "group": "body"},
        "body_radius": {"type": "float", "default": 42.0, "min": 24.0, "max": 75.0,
                        "unit": "mm", "group": "body"},
        "shoulder_radius": {"type": "float", "default": 30.0, "min": 16.0, "max": 60.0,
                            "unit": "mm", "group": "body"},
        "neck_radius": {"type": "float", "default": 15.0, "min": 7.0, "max": 35.0,
                        "unit": "mm", "group": "body"},
        "lip_radius": {"type": "float", "default": 18.0, "min": 8.0, "max": 40.0,
                       "unit": "mm", "group": "body"},
        "wall": {"type": "float", "default": 3.0, "min": 1.8, "max": 5.5,
                 "unit": "mm", "group": "shell"},
        "base_thickness": {"type": "float", "default": 6.0, "min": 2.5, "max": 14.0,
                           "unit": "mm", "group": "body"},
        "segments": {"type": "int", "default": 96, "min": 32, "max": 256,
                     "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"wall": 3.5, "segments": 64},
        "nominal": {"wall": 3.0, "segments": 96},
        "hero": {"wall": 2.5, "segments": 160},
    },
    tags=["product", "loft", "boolean", "shell"],
    expected_genus=0, material="glass_frosted",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= neck_radius * 0.34",
         "severity": "error", "fix": "wall = neck_radius * 0.25"},
        {"id": "base_fits", "expr": "base_thickness <= height * 0.16",
         "severity": "error", "fix": "base_thickness = height * 0.08"},
        {"id": "neck_order", "expr": "neck_radius <= shoulder_radius <= body_radius",
         "severity": "warn"},
    ],
)
def _build_bottle(v, quality="draft"):
    H, rbody = float(v["height"]), float(v["body_radius"])
    rsh, rneck, rlip = (float(v["shoulder_radius"]), float(v["neck_radius"]),
                        float(v["lip_radius"]))
    wall, base_t = float(v["wall"]), float(v["base_thickness"])
    seg = max(16, int(v["segments"]))
    sections = [(0.00, rbody * 0.94), (0.10, rbody), (0.42, rbody),
                (0.68, rbody * 0.92), (0.84, rsh), (0.95, rneck), (1.00, rlip)]
    outer_rings, inner_rings = [], []
    lip_lift = max(0.01, 2.0 * wall)
    for i, (t, r) in enumerate(sections):
        z = t * H
        outer_rings.append(_ring_at(_circle(r, seg), z))
        zi = base_t if i == 0 else z
        if i == len(sections) - 1:
            zi = H + lip_lift
        inner_rings.append(_ring_at(_circle(max(1e-4, r - wall), seg), zi))
    outer = _S.loft(outer_rings, caps=True)
    inner = _S.loft(inner_rings, caps=True)
    return difference(outer, inner), _finish(
        bevel_width=min(0.0010, wall * 0.25), bevel_segments=_q(quality, 1, 2, 3),
        bevel_angle_deg=45.0, material="glass_frosted")


# ---------------------------------------------------------------- mechanical

@recipe(
    "mechanism.gear", "直齿齿轮",
    "简化直齿轮廓 + 拉伸 + 轮毂并集 + 中心孔差集（非渐开线精确齿廓）。",
    params={
        "teeth": {"type": "int", "default": 20, "min": 6, "max": 80,
                  "unit": "count", "group": "gear"},
        "module": {"type": "float", "default": 3.0, "min": 0.8, "max": 10.0,
                   "unit": "mm", "group": "gear", "description": "模数"},
        "thickness": {"type": "float", "default": 10.0, "min": 2.0, "max": 40.0,
                      "unit": "mm", "group": "gear"},
        "bore_radius": {"type": "float", "default": 8.0, "min": 2.0, "max": 30.0,
                        "unit": "mm", "group": "gear"},
        "hub_radius": {"type": "float", "default": 15.0, "min": 0.0, "max": 45.0,
                       "unit": "mm", "group": "gear"},
        "hub_enabled": {"type": "bool", "default": True, "unit": "none", "group": "gear"},
        "segments": {"type": "int", "default": 128, "min": 32, "max": 320,
                     "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"teeth": 16, "module": 4.0, "thickness": 12.0, "segments": 96},
        "nominal": {"teeth": 20, "module": 3.0, "thickness": 10.0, "segments": 128},
        "hero": {"teeth": 24, "module": 2.5, "thickness": 10.0, "segments": 192},
    },
    tags=["mechanism", "gear", "extrude", "boolean"],
    expected_genus=1, material="brushed_steel",
    constraints=[
        {"id": "bore_fits", "expr": "bore_radius < module * teeth / 2 * 0.55",
         "severity": "error", "fix": "bore_radius = module * teeth / 2 * 0.35"},
        {"id": "hub_fits", "expr": "(not hub_enabled) or hub_radius > bore_radius",
         "severity": "warn", "fix": "hub_radius = bore_radius * 1.6"},
        {"id": "thickness_ok", "expr": "thickness <= module * teeth * 0.35",
         "severity": "warn"},
    ],
)
def _build_gear(v, quality="draft"):
    teeth, module = int(v["teeth"]), float(v["module"])
    th, bore_r = float(v["thickness"]), float(v["bore_radius"])
    hub_r, seg = float(v["hub_radius"]), max(32, int(v["segments"]))
    pitch_r = module * teeth / 2.0
    body = _S.extrude(_gear_loop(teeth, pitch_r, module), th)
    if bool(v.get("hub_enabled", True)) and hub_r > bore_r:
        hub = _S.extrude(_circle(hub_r, seg), th * 1.6)
        body = union(body, hub.transform(translate=(0.0, 0.0, -0.3 * th)))
    bore = _P.cylinder(bore_r, th * 2.2, center=(0.0, 0.0, 0.5 * th), segments=seg)
    out = difference(body, bore)
    return out, _finish(bevel_width=0.0003, bevel_segments=_q(quality, 1, 1, 2),
                        bevel_angle_deg=30.0, material="brushed_steel")


# ---------------------------------------------------------------- surfaces

@recipe(
    "surface.panel", "四边 Coons 曲面面板",
    "四条边界曲线 -> Coons 曲面 -> 双面偏移成闭合薄壳。",
    params={
        "width": {"type": "float", "default": 300.0, "min": 60.0, "max": 1200.0,
                  "unit": "mm", "group": "panel"},
        "depth": {"type": "float", "default": 200.0, "min": 40.0, "max": 900.0,
                  "unit": "mm", "group": "panel"},
        "height": {"type": "float", "default": 80.0, "min": 0.0, "max": 600.0,
                   "unit": "mm", "group": "panel"},
        "thickness": {"type": "float", "default": 4.0, "min": 0.8, "max": 30.0,
                      "unit": "mm", "group": "shell"},
        "bulge": {"type": "float", "default": 25.0, "min": -200.0, "max": 300.0,
                  "unit": "mm", "group": "panel", "description": "面内鼓包幅度，沿 +Z"},
        "u_samples": {"type": "int", "default": 32, "min": 4, "max": 160,
                      "unit": "count", "group": "quality", "advanced": True},
        "v_samples": {"type": "int", "default": 24, "min": 4, "max": 160,
                      "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"thickness": 5.0, "u_samples": 20, "v_samples": 16},
        "nominal": {"thickness": 4.0, "u_samples": 32, "v_samples": 24},
        "hero": {"thickness": 4.0, "u_samples": 64, "v_samples": 48},
    },
    tags=["surface", "coons", "quad_patch", "thicken", "shell"],
    expected_genus=0, material="powder_blue",
    constraints=[
        {"id": "thickness_vs_size", "expr": "thickness <= min(width, depth) * 0.08",
         "severity": "error", "fix": "thickness = min(width, depth) * 0.04"},
        {"id": "bulge_ok", "expr": "abs(bulge) <= max(width, depth) * 0.6",
         "severity": "warn"},
    ],
)
def _build_panel(v, quality="draft"):
    W, D, H = float(v["width"]), float(v["depth"]), float(v["height"])
    T, bulge = float(v["thickness"]), float(v["bulge"])
    us, vs = max(4, int(v["u_samples"])), max(4, int(v["v_samples"]))
    n = max(8, us, vs)
    x0, x1 = -W / 2.0, W / 2.0
    y0, y1 = -D / 2.0, D / 2.0
    bottom = np.column_stack([np.linspace(x0, x1, n), np.full(n, y0), np.zeros(n)])
    right = np.column_stack([np.full(n, x1), np.linspace(y0, y1, n),
                             np.linspace(0.0, H, n)])
    top = np.column_stack([np.linspace(x1, x0, n), np.full(n, y1), np.full(n, H)])
    left = np.column_stack([np.full(n, x0), np.linspace(y1, y0, n),
                            np.linspace(H, 0.0, n)])
    surface = _S.coons([bottom, right, top, left], us, vs)
    if abs(bulge) > 0.0:
        w = (np.sin(np.pi * np.linspace(0.0, 1.0, us + 1))[:, None]
             * np.sin(np.pi * np.linspace(0.0, 1.0, vs + 1))[None, :])
        verts = surface.verts.copy()
        verts[:, 2] += bulge * w.reshape(-1)
        surface = Mesh(verts, surface.faces, dict(surface.meta))
    out = _S.thicken(surface, T, 0.0)
    return out, _finish(bevel_width=min(0.0008, T * 0.2),
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=35.0, material="powder_blue")


@recipe(
    "surface.dome", "曲线网络穹顶薄壳",
    "u/v 曲线网络 -> Gordon 曲面 -> 双面偏移薄壳。",
    params={
        "width": {"type": "float", "default": 240.0, "min": 60.0, "max": 900.0,
                  "unit": "mm", "group": "dome"},
        "depth": {"type": "float", "default": 200.0, "min": 50.0, "max": 800.0,
                  "unit": "mm", "group": "dome"},
        "height": {"type": "float", "default": 120.0, "min": 10.0, "max": 500.0,
                   "unit": "mm", "group": "dome"},
        "thickness": {"type": "float", "default": 4.0, "min": 0.8, "max": 30.0,
                      "unit": "mm", "group": "shell"},
        "u_count": {"type": "int", "default": 5, "min": 3, "max": 15,
                    "unit": "count", "group": "network"},
        "v_count": {"type": "int", "default": 5, "min": 3, "max": 15,
                    "unit": "count", "group": "network"},
        "u_samples": {"type": "int", "default": 32, "min": 6, "max": 160,
                      "unit": "count", "group": "quality", "advanced": True},
        "v_samples": {"type": "int", "default": 24, "min": 6, "max": 160,
                      "unit": "count", "group": "quality", "advanced": True},
        "curve_samples": {"type": "int", "default": 24, "min": 8, "max": 120,
                          "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"thickness": 5.0, "u_count": 4, "v_count": 4,
                 "u_samples": 20, "v_samples": 16, "curve_samples": 16},
        "nominal": {"thickness": 4.0, "u_count": 5, "v_count": 5,
                    "u_samples": 32, "v_samples": 24, "curve_samples": 24},
        "hero": {"thickness": 4.0, "u_count": 7, "v_count": 7,
                 "u_samples": 64, "v_samples": 48, "curve_samples": 40},
    },
    tags=["surface", "gordon", "network", "thicken", "shell", "organic"],
    expected_genus=0, material="engine_outer",
    constraints=[
        {"id": "thickness_vs_size", "expr": "thickness <= min(width, depth) * 0.08",
         "severity": "error", "fix": "thickness = min(width, depth) * 0.04"},
        {"id": "height_ok", "expr": "height <= max(width, depth) * 0.8",
         "severity": "warn"},
    ],
)
def _build_dome(v, quality="draft"):
    W, D, H = float(v["width"]), float(v["depth"]), float(v["height"])
    T = float(v["thickness"])
    m, n = max(3, int(v["u_count"])), max(3, int(v["v_count"]))
    us, vs = max(6, int(v["u_samples"])), max(6, int(v["v_samples"]))
    cs = max(8, int(v["curve_samples"]))
    ax, ay = W / 2.0, D / 2.0

    def z_at(x, y):
        return H * math.sqrt(max(0.0, 1.0 - (x / ax) ** 2 - (y / ay) ** 2))

    xs = np.linspace(-ax, ax, cs)
    ys = np.linspace(-ay, ay, cs)
    u_curves = [np.array([[x, y, z_at(x, y)] for x in xs])
                for y in np.linspace(-ay, ay, n)]
    v_curves = [np.array([[x, y, z_at(x, y)] for y in ys])
                for x in np.linspace(-ax, ax, m)]
    return _S.thicken(_S.gordon(u_curves, v_curves, us, vs), T, 0.0), _finish(
        bevel_width=min(0.0008, T * 0.2), bevel_segments=_q(quality, 1, 2, 3),
        bevel_angle_deg=35.0, material="engine_outer")


# ---------------------------------------------------------------- parts

@recipe(
    "part.bracket", "带孔安装支架",
    "圆角矩形板拉伸 + 两个通孔差集；genus = 孔数。",
    params={
        "length": {"type": "float", "default": 100.0, "min": 40.0, "max": 400.0,
                   "unit": "mm", "group": "plate"},
        "width": {"type": "float", "default": 60.0, "min": 20.0, "max": 240.0,
                  "unit": "mm", "group": "plate"},
        "thickness": {"type": "float", "default": 6.0, "min": 2.0, "max": 20.0,
                      "unit": "mm", "group": "plate"},
        "hole_radius": {"type": "float", "default": 5.0, "min": 1.5, "max": 20.0,
                        "unit": "mm", "group": "holes"},
        "hole_spacing": {"type": "float", "default": 60.0, "min": 10.0, "max": 300.0,
                         "unit": "mm", "group": "holes"},
        "corner_radius": {"type": "float", "default": 6.0, "min": 0.0, "max": 20.0,
                          "unit": "mm", "group": "plate"},
        "segments": {"type": "int", "default": 64, "min": 16, "max": 256,
                     "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"thickness": 7.0, "hole_radius": 5.0, "corner_radius": 8.0,
                 "segments": 48},
        "nominal": {"thickness": 6.0, "hole_radius": 5.0, "corner_radius": 6.0,
                    "segments": 64},
        "hero": {"thickness": 5.0, "hole_radius": 4.5, "corner_radius": 8.0,
                 "segments": 96},
    },
    tags=["part", "extrude", "boolean", "bracket"],
    expected_genus=2, material="powder_steel",
    constraints=[
        {"id": "hole_fits", "expr": "hole_radius <= width * 0.28",
         "severity": "error", "fix": "hole_radius = width * 0.2"},
        {"id": "holes_inside", "expr": "hole_spacing / 2 + hole_radius <= length / 2 - 1",
         "severity": "error", "fix": "hole_spacing = length - 2 * hole_radius - 4"},
        {"id": "thickness_ok", "expr": "thickness <= width * 0.4",
         "severity": "error", "fix": "thickness = width * 0.2"},
        {"id": "corner_ok", "expr": "corner_radius <= min(length, width) * 0.2",
         "severity": "warn", "fix": "corner_radius = min(length, width) * 0.1"},
    ],
)
def _build_bracket(v, quality="draft"):
    L, W, T = float(v["length"]), float(v["width"]), float(v["thickness"])
    rh, spacing = float(v["hole_radius"]), float(v["hole_spacing"])
    corner, seg = float(v["corner_radius"]), max(16, int(v["segments"]))
    loop = _rounded_rect(L, W, corner, max(3, seg // 12))
    plate = _S.extrude(loop, T)
    for sign in (-1.0, 1.0):
        tool = _P.cylinder(rh, T + 0.002, center=(sign * spacing / 2.0, 0.0, T / 2.0),
                           segments=seg)
        plate = difference(plate, tool)
    return plate, _finish(bevel_width=min(0.0006, T * 0.15),
                          bevel_segments=_q(quality, 1, 2, 2),
                          bevel_angle_deg=45.0, material="powder_steel")


@recipe(
    "part.enclosure", "开口电子外壳",
    "长方体倒角壳 + 内腔差集 + 可选前壁线缆缺口（从口沿向下，保持 genus 0）。",
    params={
        "length": {"type": "float", "default": 120.0, "min": 40.0, "max": 400.0,
                   "unit": "mm", "group": "body"},
        "width": {"type": "float", "default": 80.0, "min": 30.0, "max": 300.0,
                  "unit": "mm", "group": "body"},
        "height": {"type": "float", "default": 40.0, "min": 15.0, "max": 200.0,
                   "unit": "mm", "group": "body"},
        "wall": {"type": "float", "default": 3.0, "min": 1.2, "max": 8.0,
                 "unit": "mm", "group": "shell"},
        "base": {"type": "float", "default": 3.0, "min": 1.2, "max": 10.0,
                 "unit": "mm", "group": "shell"},
        "cable_slot": {"type": "bool", "default": True, "unit": "none", "group": "cable"},
        "cable_width": {"type": "float", "default": 20.0, "min": 5.0, "max": 120.0,
                        "unit": "mm", "group": "cable"},
        "cable_height": {"type": "float", "default": 12.0, "min": 4.0, "max": 80.0,
                         "unit": "mm", "group": "cable"},
    },
    presets={
        "safe": {"wall": 3.5, "base": 3.5, "cable_width": 18.0, "cable_height": 10.0},
        "nominal": {"wall": 3.0, "base": 3.0, "cable_width": 20.0, "cable_height": 12.0},
        "hero": {"wall": 2.5, "base": 2.5, "cable_width": 22.0, "cable_height": 14.0},
    },
    tags=["part", "box", "boolean", "enclosure", "shell"],
    expected_genus=0, material="abs_white",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= min(length, width) * 0.12",
         "severity": "error", "fix": "wall = min(length, width) * 0.08"},
        {"id": "base_fits", "expr": "base <= height * 0.5",
         "severity": "error", "fix": "base = height * 0.25"},
        {"id": "slot_fits", "expr": "(not cable_slot) or cable_width <= width * 0.5",
         "severity": "warn", "fix": "cable_width = width * 0.3"},
        {"id": "slot_height_ok", "expr": "(not cable_slot) or cable_height <= height * 0.5",
         "severity": "warn", "fix": "cable_height = height * 0.3"},
    ],
)
def _build_enclosure(v, quality="draft"):
    L, W, H = float(v["length"]), float(v["width"]), float(v["height"])
    wall, base = float(v["wall"]), float(v["base"])
    outer = _P.box([L, W, H], center=(0.0, 0.0, 0.0), align="min")
    cavity = _P.box([max(1e-4, L - 2.0 * wall), max(1e-4, W - 2.0 * wall),
                     max(1e-4, H - base + 0.001)],
                    center=(wall, wall, base), align="min")
    out = difference(outer, cavity)
    if bool(v.get("cable_slot", True)):
        cw, ch = float(v["cable_width"]), float(v["cable_height"])
        slot = _P.box([cw, 2.0 * wall + 0.002, ch + 0.001],
                      center=(0.5 * (L - cw), -wall - 0.001, H - ch), align="min")
        out = difference(out, slot)
    return out, _finish(bevel_width=min(0.0008, wall * 0.2),
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=45.0, material="abs_white")


# ------------------------------------------------------- extended families
# Family modules register themselves on import.  The loader is tolerant by
# default so that a broken third-party family cannot take the whole engine
# down; ``ATTA_FAMILIES_STRICT=1`` turns import failures into hard errors
# (tests use it to prove every shipped family loads).
from . import families as _families  # noqa: E402

_families.load(strict=False)
