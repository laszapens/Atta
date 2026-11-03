# -*- coding: utf-8 -*-
"""Surface / organic families built on the sweep-loft shell kernel.

``surface.coons_duct``  square (rounded) inlet -> circular outlet transition
                        duct; a ruled Coons blend skinned by ``surfaces.loft``
                        and shelled by ``surfaces.thicken``.
``organic.seashell``    logarithmic-spiral (growth-grammar) shell; an elliptical
                        section swept along a conical helico-spiral, ribbed and
                        capped at both ends.

Continuity note for the duct
----------------------------
A duct is a *cylinder-topology* body, so a single ``coons()`` quad patch (disk
topology) cannot skin it.  What ``coons`` does in the bilinear case, however, is
exactly what a transition duct needs: with two boundary curves ``C0``/``C1`` and
degenerate cross terms the Coons formula reduces to the **ruled surface**
``S(u, v) = (1-v) C0(u) + v C1(u)``.  That is the section blend used here — both
ends are parametrised by the same polar angle ``u = theta``, so every
intermediate ring is the ruled blend of the inlet and outlet boundary curves and
the skin is G1 across the whole length (the only curvature discontinuity is at
the four rounded-corner tangency lines of the inlet, where a rounded rectangle
is G1 but not G2 by construction).  The blend parameter is smooth-stepped,
``v = 3s^2 - 2s^3``, so ``dv/dz = 0`` at both mouths: the skin leaves the inlet
plane and enters the outlet plane tangentially, which is what makes a duct
butt-weld (G1) to a straight stub instead of leaving a dihedral crease.
"""

from __future__ import annotations

import math

import numpy as np

from .. import surfaces as _S
from ..errors import AttaError
from ..kit import finish, quality_level
from ..mesh import Mesh, oriented, topology
from ..registry import recipe

__all__ = []

_TWO_PI = 2.0 * math.pi


# ---------------------------------------------------------------- 2D helpers

def _poly_area(p):
    return 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1)
                              - p[:, 1] * np.roll(p[:, 0], -1)))


def rounded_rect_radius(theta, a, b, rc):
    """Radial distance of a rounded rectangle from its centre, along ``theta``.

    ``a``/``b`` are half extents and ``rc`` the corner radius.  Closed form:
    the boundary is either a flat side (``x = a`` / ``y = b``) or a corner arc
    centred at ``(a - rc, b - rc)``.  This is the exact boundary of the convex
    shape, so it is C1 in ``theta`` for ``rc > 0``.
    """
    a, b = float(a), float(b)
    rc = float(min(max(rc, 0.0), 0.999 * min(a, b)))
    cs = np.abs(np.cos(theta))
    sn = np.abs(np.sin(theta))
    cx, cy = a - rc, b - rc
    eps = 1e-12
    with np.errstate(divide="ignore", invalid="ignore"):
        rx = np.where(cs > eps, a / np.where(cs > eps, cs, 1.0), np.inf)
        ry = np.where(sn > eps, b / np.where(sn > eps, sn, 1.0), np.inf)
    hit_x = rx * sn <= cy + 1e-12
    hit_y = ry * cs <= cx + 1e-12
    dotc = cx * cs + cy * sn
    disc = np.maximum(dotc * dotc - (cx * cx + cy * cy - rc * rc), 0.0)
    r_arc = dotc + np.sqrt(disc)
    return np.where(hit_x, rx, np.where(hit_y, ry, r_arc))


def _smoothstep(s):
    s = np.clip(np.asarray(s, dtype=np.float64), 0.0, 1.0)
    return s * s * (3.0 - 2.0 * s)


# ---------------------------------------------------------------- coons duct

@recipe(
    "surface.coons_duct", "方转圆过渡风管",
    "方（圆角）进口到圆出口的直纹 Coons 过渡蒙皮 + 法向加厚成等壁厚壳体；"
    "两端沿切向离开进口平面、进入出口平面（G1），可对接直管段。",
    params={
        "length": {"type": "float", "default": 240.0, "min": 40.0, "max": 600.0,
                   "unit": "mm", "group": "body", "description": "进口到出口的轴向长度"},
        "inlet_w": {"type": "float", "default": 160.0, "min": 20.0, "max": 420.0,
                    "unit": "mm", "group": "inlet"},
        "inlet_h": {"type": "float", "default": 120.0, "min": 20.0, "max": 420.0,
                    "unit": "mm", "group": "inlet"},
        "corner_radius": {"type": "float", "default": 22.0, "min": 0.0, "max": 80.0,
                          "unit": "mm", "group": "inlet",
                          "description": "进口方口圆角半径"},
        "outlet_d": {"type": "float", "default": 100.0, "min": 20.0, "max": 420.0,
                     "unit": "mm", "group": "outlet"},
        "wall": {"type": "float", "default": 3.0, "min": 1.0, "max": 14.0,
                 "unit": "mm", "group": "shell"},
        "twist_deg": {"type": "float", "default": 0.0, "min": -180.0, "max": 180.0,
                      "unit": "deg", "group": "body",
                      "description": "出口截面相对进口的扭转角"},
        "segments": {"type": "int", "default": 64, "min": 16, "max": 192,
                     "unit": "count", "group": "quality",
                     "description": "环向分段数"},
        "rounds": {"type": "int", "default": 32, "min": 12, "max": 96,
                   "unit": "count", "group": "quality",
                   "description": "轴向放样截面数（>=12 保证过渡平滑）"},
    },
    presets={
        "safe": {"wall": 4.0, "segments": 48, "rounds": 20, "twist_deg": 0.0},
        "nominal": {"wall": 3.0, "segments": 64, "rounds": 32, "twist_deg": 0.0},
        "hero": {"wall": 2.0, "segments": 112, "rounds": 56, "twist_deg": 15.0},
    },
    tags=["surface", "loft", "coons", "thicken", "duct", "transition"],
    expected_genus=1, material="aluminum",
    constraints=[
        {"id": "wall_fits", "expr": "wall <= 0.2 * min(inlet_w, inlet_h, outlet_d)",
         "severity": "error", "message": "壁厚过大，无法在过渡段内做等壁厚加厚",
         "fix": "wall = 0.1 * min(inlet_w, inlet_h, outlet_d)"},
        {"id": "corner_ok", "expr": "corner_radius <= 0.45 * min(inlet_w, inlet_h)",
         "severity": "error", "message": "进口圆角超过半宽，圆角互相吃掉",
         "fix": "corner_radius = 0.3 * min(inlet_w, inlet_h)"},
        {"id": "wall_vs_corner",
         "expr": "corner_radius <= 0 or wall <= 0.45 * corner_radius",
         "severity": "error",
         "message": "壁厚接近进口圆角半径，内壁在圆角处会自交（尖点）",
         "fix": "corner_radius = 3 * wall"},
        {"id": "outlet_fits", "expr": "outlet_d <= 1.4 * min(inlet_w, inlet_h)",
         "severity": "warn", "message": "出口比进口大得多，过渡会外扩并变薄",
         "fix": "outlet_d = min(inlet_w, inlet_h)"},
        {"id": "slope_ok",
         "expr": "length >= 0.5 * max(inlet_w, inlet_h, outlet_d)",
         "severity": "warn", "message": "过渡太陡，法向壁厚沿轴向会被拉斜",
         "fix": "length = max(inlet_w, inlet_h, outlet_d)"},
    ],
)
def _build_coons_duct(v, quality="draft"):
    L = float(v["length"])
    iw, ih = float(v["inlet_w"]), float(v["inlet_h"])
    od, wall = float(v["outlet_d"]), float(v["wall"])
    rc = float(v["corner_radius"])
    twist = math.radians(float(v["twist_deg"]))
    stations = quality_level(quality, int(v["rounds"]), int(v["rounds"]) + 8,
                             int(v["rounds"]) + 16)
    stations = max(12, min(128, stations))
    seg = quality_level(quality, int(v["segments"]), int(v["segments"]) * 3 // 2,
                        int(v["segments"]) * 2)
    n = 4 * max(3, int(seg) // 4)          # keep 4-fold symmetry of the inlet
    theta = np.linspace(0.0, _TWO_PI, n, endpoint=False)

    a_in, b_in, r_out = 0.5 * iw, 0.5 * ih, 0.5 * od
    rc = min(max(rc, 0.0), 0.999 * min(a_in, b_in))
    r_in = rounded_rect_radius(theta, a_in, b_in, rc)
    if float(np.min(r_in)) <= wall:
        raise AttaError("wall_too_thick", "wall does not fit inside the inlet",
                        path="wall", fix="reduce wall or enlarge the inlet")
    if r_out <= wall:
        raise AttaError("wall_too_thick", "wall does not fit inside the outlet",
                        path="wall", fix="reduce wall or enlarge outlet_d")

    rings = []
    for i in range(stations):
        s = i / float(stations - 1)
        b = float(_smoothstep(s))            # ruled-Coons blend parameter
        psi = twist * b                      # frozen at the inlet, full at outlet
        rr = (1.0 - b) * r_in + b * r_out
        ang = theta + psi
        rings.append(np.column_stack([rr * np.cos(ang), rr * np.sin(ang),
                                      np.full(n, s * L)]))
    skin = oriented(_S.loft(rings, closed_ring=True, caps=False))
    if topology(skin)["closed"]:
        raise AttaError("bad_surface", "duct skin unexpectedly closed", path="rings",
                        fix="this is an engine bug; report it")
    shell = _S.thicken(skin, wall, offset=-1.0)
    shell.meta.update({"kind": "coons_duct", "wall": wall, "stations": stations,
                       "segments": n, "twist_deg": float(v["twist_deg"])})
    return shell, finish(smooth_angle_deg=30.0, material="aluminum")


# ---------------------------------------------------------------- seashell

@recipe(
    "organic.seashell", "对数螺线海螺",
    "生长语法族：中心线为圆锥对数螺线 r = R exp(b(phi - phi_end))，"
    "椭圆截面随 r 指数放大，沿螺线 rib_count 条肋纹；两端封口成闭合实体。",
    params={
        "turns": {"type": "float", "default": 3.2, "min": 1.0, "max": 7.0,
                  "unit": "none", "group": "grammar",
                  "description": "螺线圈数（生长长度）"},
        "growth_rate": {"type": "float", "default": 1.55, "min": 1.10, "max": 2.60,
                        "unit": "none", "group": "grammar",
                        "description": "每圈半径放大倍率（生长率）"},
        "shell_radius": {"type": "float", "default": 78.0, "min": 25.0, "max": 220.0,
                         "unit": "mm", "group": "body",
                         "description": "壳体末圈（壳口）中心线半径"},
        "section_w": {"type": "float", "default": 30.0, "min": 6.0, "max": 120.0,
                      "unit": "mm", "group": "section",
                      "description": "壳口截面径向全宽"},
        "section_h": {"type": "float", "default": 34.0, "min": 6.0, "max": 120.0,
                      "unit": "mm", "group": "section",
                      "description": "壳口截面轴向全高"},
        "rib_count": {"type": "int", "default": 14, "min": 0, "max": 48,
                      "unit": "count", "group": "ribs",
                      "description": "每圈肋纹数（0 = 光壳）"},
        "rib_depth": {"type": "float", "default": 0.10, "min": 0.0, "max": 0.45,
                      "unit": "ratio", "group": "ribs"},
        "height_scale": {"type": "float", "default": 0.85, "min": 0.10, "max": 2.00,
                         "unit": "none", "group": "body",
                         "description": "轴向抬升相对半径的比例"},
        "samples": {"type": "int", "default": 160, "min": 40, "max": 400,
                    "unit": "count", "group": "quality",
                    "description": "螺线方向截面数"},
        "segments": {"type": "int", "default": 24, "min": 12, "max": 96,
                     "unit": "count", "group": "quality",
                     "description": "截面环向分段数"},
    },
    presets={
        "safe": {"turns": 2.6, "growth_rate": 1.60, "section_w": 34.0,
                 "section_h": 34.0, "rib_depth": 0.08, "samples": 110,
                 "segments": 20},
        "nominal": {"turns": 3.2, "growth_rate": 1.55, "shell_radius": 78.0,
                    "section_w": 30.0, "section_h": 34.0, "rib_count": 14,
                    "rib_depth": 0.10, "samples": 160, "segments": 24},
        "hero": {"turns": 3.6, "growth_rate": 1.50, "shell_radius": 92.0,
                 "section_w": 34.0, "section_h": 40.0, "rib_count": 18,
                 "rib_depth": 0.12, "samples": 260, "segments": 36},
    },
    tags=["organic", "sweep", "loft", "growth_grammar", "logarithmic_spiral"],
    expected_genus=0, material="porcelain",
    constraints=[
        {"id": "whorl_clear",
         "expr": "max(section_w, section_h) * (1 + rib_depth) <= 0.85 * (growth_rate - 1)"
                 " * shell_radius * sqrt(1 + height_scale * height_scale)",
         "severity": "error",
         "message": "截面大于螺距，相邻螺层会互相穿插（不是单层实体）",
         "fix": "section_h = 0.6 * (growth_rate - 1) * shell_radius"},
        {"id": "section_fits", "expr": "max(section_w, section_h) <= 1.1 * shell_radius",
         "severity": "error", "message": "截面比壳口半径还大，壳体无法闭合",
         "fix": "section_w = 0.6 * shell_radius"},
        {"id": "growth_grows", "expr": "growth_rate > 1.0",
         "severity": "error", "message": "生长率必须 > 1，否则螺线不生长",
         "fix": "growth_rate = 1.3"},
        {"id": "rib_sane", "expr": "rib_count * rib_depth <= 6",
         "severity": "warn", "message": "肋纹过密过深，接近截面自交",
         "fix": "rib_depth = 6.0 / max(1, rib_count)"},
        {"id": "apex_visible",
         "expr": "growth_rate ** turns <= 60",
         "severity": "warn", "message": "圈数×生长率过大，顶部退化成一个点",
         "fix": "turns = 60 ** (1.0 / growth_rate)"},
    ],
)
def _build_seashell(v, quality="draft"):
    turns = float(v["turns"])
    g = float(v["growth_rate"])
    R = float(v["shell_radius"])
    sw, sh = float(v["section_w"]), float(v["section_h"])
    rib_n = int(v["rib_count"])
    rib_d = float(v["rib_depth"])
    hs = float(v["height_scale"])
    ns = max(40, quality_level(quality, int(v["samples"]),
                               int(v["samples"]) * 3 // 2, int(v["samples"]) * 2))
    ns = min(500, ns)
    seg = max(12, quality_level(quality, int(v["segments"]),
                                int(v["segments"]) * 3 // 2, int(v["segments"]) * 2))

    phi_end = _TWO_PI * turns
    b = math.log(g) / _TWO_PI
    phi = np.linspace(0.0, phi_end, ns)
    r = R * np.exp(b * (phi - phi_end))
    z = hs * (r - r[0])
    centre = np.column_stack([r * np.cos(phi), r * np.sin(phi), z])

    # transport-free frame: radial direction projected off the tangent, so the
    # section's wide axis always points away from the shell axis
    tan = np.gradient(centre, axis=0)
    tan /= np.maximum(np.linalg.norm(tan, axis=1, keepdims=True), 1e-15)
    radial = np.column_stack([np.cos(phi), np.sin(phi), np.zeros_like(phi)])
    u = radial - tan * np.einsum("ij,ij->i", radial, tan)[:, None]
    u /= np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-15)
    w = np.cross(tan, u)
    w /= np.maximum(np.linalg.norm(w, axis=1, keepdims=True), 1e-15)

    grow = r / R
    if rib_n > 0 and rib_d > 0.0:
        grow = grow * (1.0 + rib_d * np.cos(rib_n * phi))
    semi_u = 0.5 * sw * grow
    semi_w = 0.5 * sh * grow
    theta = np.linspace(0.0, _TWO_PI, seg, endpoint=False)
    cu = np.cos(theta)[None, :, None]
    su = np.sin(theta)[None, :, None]
    rings = (centre[:, None, :]
             + (semi_u[:, None, None] * cu) * u[:, None, :]
             + (semi_w[:, None, None] * su) * w[:, None, :])
    if not np.isfinite(rings).all():
        raise AttaError("bad_geometry", "seashell ring contains NaN", path="turns",
                        fix="check growth_rate and height_scale")
    mesh = _S.loft(rings, closed_ring=True, caps=True)
    mesh.meta.update({"kind": "seashell", "turns": turns, "growth_rate": g,
                      "ribs": rib_n, "rings": int(ns), "segments": int(seg)})
    return mesh, finish(smooth_angle_deg=40.0, material="porcelain")
