# -*- coding: utf-8 -*-
"""Mechanical shape families — real gear geometry, flange, print-fit ladder.

Three families live here, all of them *derived* rather than hardcoded: every
dimension comes from a formula in :mod:`atta.families.mech_geom`, and the
recipe constraints are the design rules that formula assumes.

``mechanical.gear.involute``
    ISO 53 basic-rack involute spur gear.  ``pitch_r = module*teeth/2``,
    ``base_r = pitch_r*cos(alpha)``, ``tip_r = pitch_r + addendum``,
    ``root_r = pitch_r - dedendum``, tooth thickness ``pi*m/2 - backlash``.
    The flank is the *real* involute ``x = rb(cos t + t sin t)`` sampled from
    the base circle outwards; below the base circle it degenerates to the
    radial run that a generated (hobbed) gear actually has.  The tip is
    truncated analytically when the tooth would close to a hairline, which is
    what keeps the polygon simple for any legal parameter set.
    Genus 1: the centre bore is always a through hole (``bore_d >= 2 mm``).

``mechanical.flange``
    Welded-neck pipe flange revolved from one closed ``(r, z)`` loop: plate,
    hub below the plate, raised face above it, optional gasket groove.  The
    bolt circle, plate rim, bolt web and hub wall are all derived, and the
    bolt pattern is a real polar array of through holes.
    Genus ``1 + bolt_holes``: one handle for the bore plus one per bolt hole.
    All three presets keep the same 6-bolt pattern so the registered
    ``expected_genus`` (7) is exact for every preset; changing ``bolt_holes``
    moves the genus to ``1 + bolt_holes`` and can be verified with
    ``atta verify --constraints '{"genus": N}'``.

``tolerance.clearance_ladder``
    FDM print-fit coupon: one spine, a row of *blind* gauge holes with a
    graded diametral clearance and a row of nominal pins to caliper-check the
    printer's actual shaft size.

    Reference ISO 286-2 diametral clearance for a 6 mm nominal shaft, which is
    what :func:`mech_geom.nearest_fit` labels each station with:

    ============  ==========================================
    0.004-0.024   H7/g6   sliding / location fit
    0.010-0.034   H7/f7   close running fit
    0.020-0.068   H8/e9   easy running fit
    0.030-0.108   H9/d10  loose running fit
    0.000-0.150   H11/h11 free fit
    ============  ==========================================

    ISO 286 bands are micrometre-scale, while an FDM printer holds roughly
    +/-0.05 to +/-0.15 mm, so the default ladder (0.02-0.20 mm) deliberately
    sits at the loose end of the standard: it measures the *printed*
    clearance and every station reports the standard fit it is closest to.

    Blind holes (with a derived floor) keep the coupon at genus 0 for every
    ``pairs`` value, so the genus expectation stays exact while the ladder
    stays fully parametric.
"""

from __future__ import annotations

import math

from .. import primitives as _P
from .. import surfaces as _S
from ..boolean import difference, union
from ..kit import finish, quality_level
from ..registry import recipe
from . import mech_geom as _G

__all__ = ["build_gear_involute", "build_flange", "build_clearance_ladder"]

_q = quality_level


def _sit(mesh):
    """Translate a part so its lowest point sits on ``z = 0`` (build plate)."""
    lo = mesh.verts.min(axis=0)
    return mesh.transform(translate=(0.0, 0.0, -float(lo[2])))


# =================================================================== gear

@recipe(
    "mechanical.gear.involute", "渐开线直齿齿轮",
    "真渐开线直齿圆柱齿轮（ISO 53 基本齿条）：分度圆 rp=m·z/2、基圆 rb=rp·cosα、"
    "齿顶高 1.0m、齿根高 1.25m、分度圆齿厚 πm/2−backlash；齿廓按渐开线 "
    "x=rb(cos t+t·sin t) 从基圆采样到齿顶圆，齿顶过窄时按几何上限自动截顶。"
    "中心孔贯穿，故 genus=1；可选轮毂（凸出齿宽）。",
    params={
        "teeth": {"type": "int", "default": 20, "min": 6, "max": 100,
                  "unit": "count", "group": "gear",
                  "description": "齿数 z"},
        "module": {"type": "float", "default": 2.5, "min": 0.5, "max": 10.0,
                   "unit": "mm", "group": "gear", "description": "模数 m"},
        "pressure_angle": {"type": "float", "default": 20.0, "min": 14.5,
                           "max": 25.0, "unit": "deg", "group": "gear",
                           "description": "压力角 α（14.5 老标准 / 20 常用 / 25 高扭矩）"},
        "thickness": {"type": "float", "default": 10.0, "min": 2.0, "max": 50.0,
                      "unit": "mm", "group": "gear", "description": "齿宽 b"},
        "bore_d": {"type": "float", "default": 10.0, "min": 2.0, "max": 80.0,
                   "unit": "mm", "group": "mount", "description": "中心孔直径"},
        "hub_enabled": {"type": "bool", "default": True, "unit": "none",
                        "group": "mount"},
        "hub_d": {"type": "float", "default": 30.0, "min": 0.0, "max": 200.0,
                  "unit": "mm", "group": "mount", "description": "轮毂外径"},
        "hub_h": {"type": "float", "default": 4.0, "min": 0.0, "max": 40.0,
                  "unit": "mm", "group": "mount", "description": "轮毂凸出高度"},
        "backlash": {"type": "float", "default": 0.02, "min": 0.0, "max": 0.5,
                     "unit": "mm", "group": "gear",
                     "description": "分度圆齿厚减薄量（总侧隙，弧长）"},
        "flank_samples": {"type": "int", "default": 8, "min": 3, "max": 16,
                          "unit": "count", "group": "quality", "advanced": True,
                          "description": "单侧渐开线采样段数（按齿数自动封顶）"},
    },
    presets={
        "safe": {"teeth": 12, "module": 4.0, "pressure_angle": 20.0,
                 "thickness": 12.0, "bore_d": 8.0, "hub_d": 26.0, "hub_h": 5.0,
                 "backlash": 0.05, "flank_samples": 7},
        "nominal": {"teeth": 20, "module": 2.5, "pressure_angle": 20.0,
                    "thickness": 10.0, "bore_d": 10.0, "hub_d": 30.0,
                    "hub_h": 4.0, "backlash": 0.02, "flank_samples": 8},
        "hero": {"teeth": 32, "module": 2.0, "pressure_angle": 20.0,
                 "thickness": 8.0, "bore_d": 12.0, "hub_d": 40.0, "hub_h": 6.0,
                 "backlash": 0.02, "flank_samples": 12},
    },
    tags=["mechanical", "gear", "involute", "extrude", "boolean", "pattern_polar"],
    expected_genus=1, material="brushed_steel",
    constraints=[
        {"id": "bore_fits_root", "severity": "error",
         "expr": "bore_d <= module * (teeth - 3.5)",
         "fix": "bore_d = module * (teeth - 3.5) * 0.7",
         "message": "中心孔半径必须留在齿根圆以内（齿根圆直径 = m·(z−2.5)）"},
        {"id": "hub_inside_tip", "severity": "error",
         "expr": "(not hub_enabled) or (hub_d > bore_d and hub_d <= module * (teeth + 2) * 0.98)",
         "fix": "hub_d = module * (teeth + 2) * 0.75",
         "message": "轮毂必须大于中心孔且不超过齿顶圆，否则会吃掉轮齿"},
        {"id": "hub_wall", "severity": "error",
         "expr": "(not hub_enabled) or (hub_d - bore_d >= 2 * module)",
         "fix": "hub_d = bore_d + 3 * module",
         "message": "轮毂径向壁厚至少 2 个模数（键槽/紧定螺钉需要材料）"},
        {"id": "backlash_range", "severity": "error",
         "expr": "backlash >= 0 and backlash <= pi * module * 0.2",
         "fix": "backlash = pi * module * 0.02",
         "message": "侧隙不能吃掉分度圆齿厚（πm/2）的 40% 以上"},
        {"id": "face_width", "severity": "warn",
         "expr": "thickness <= module * teeth * 0.4",
         "fix": "thickness = module * teeth * 0.3",
         "message": "齿宽超过分度圆直径的 0.4 倍时齿向载荷分布变差（spur gear b/d <= 1）"},
        {"id": "undercut", "severity": "warn",
         "expr": "teeth >= 2 / sin(pressure_angle * pi / 180) ** 2",
         "fix": "teeth = round(2 / sin(pressure_angle * pi / 180) ** 2) + 2",
         "message": "齿数低于 2·ha*/sin²α 时根切（根切只降强度，打印原型可接受）"},
    ],
)
def build_gear_involute(v, quality="draft"):
    """Involute spur gear: swept profile + hub union + centre bore."""
    m = float(v["module"])
    z = int(v["teeth"])
    th = float(v["thickness"])
    bore_r = 0.5 * float(v["bore_d"])
    geom = _G.gear_derived(z, m, float(v["pressure_angle"]),
                           float(v["backlash"]))

    budget = _q(quality, 1000, 1400, 1800)
    flank = _G.sane_flank_samples(z, int(v["flank_samples"]), budget=budget)
    loop = _G.involute_gear_profile(geom, flank, _q(quality, 2, 3, 4))
    body = _S.extrude(loop, th)
    seg = _q(quality, 64, 96, 144)

    hub_on = bool(v.get("hub_enabled", True))
    hub_r = 0.5 * float(v["hub_d"])
    hub_h = float(v["hub_h"])
    top = th
    if hub_on and hub_h > 1e-9 and hub_r > bore_r + 1e-9:
        hub = _P.cylinder(hub_r, th + hub_h, center=(0.0, 0.0, 0.5 * (th + hub_h)),
                          segments=seg)
        body = union(body, hub)
        top = th + hub_h

    pad = max(5e-4, 0.05 * th)
    bore = _P.cylinder(bore_r, top + 2.0 * pad,
                       center=(0.0, 0.0, 0.5 * top), segments=seg)
    out = difference(body, bore)
    return _sit(out), finish(material="brushed_steel", smooth_angle_deg=32.0)


# ================================================================= flange

@recipe(
    "mechanical.flange", "管法兰（凸面 + 可选密封槽）",
    "焊接颈管法兰：一条闭合 (r,z) 剖面回转出「板 + 下侧轮毂 + 上侧凸面」，"
    "抗剪螺栓孔为真实极坐标阵列贯穿板厚，可选环形密封垫槽。"
    "螺栓圆、板边余量、螺栓间腹板、轮毂壁厚全部由直径派生。"
    "genus = 1 + bolt_holes（中心孔 1 个 + 每个贯穿螺栓孔 1 个）；"
    "三套 preset 固定 6 螺栓，故 expected_genus=7 对每套 preset 都精确；"
    "改 bolt_holes 后请用 verify --constraints '{\"genus\": 1+bolt_holes}' 复核。",
    params={
        "outer_d": {"type": "float", "default": 160.0, "min": 40.0, "max": 600.0,
                    "unit": "mm", "group": "plate", "description": "法兰外径"},
        "bore_d": {"type": "float", "default": 80.0, "min": 10.0, "max": 400.0,
                   "unit": "mm", "group": "plate", "description": "通径（内孔）"},
        "bolt_circle_d": {"type": "float", "default": 130.0, "min": 30.0,
                          "max": 560.0, "unit": "mm", "group": "bolts",
                          "description": "螺栓圆直径"},
        "bolt_holes": {"type": "int", "default": 6, "min": 4, "max": 24,
                       "unit": "count", "group": "bolts"},
        "bolt_hole_d": {"type": "float", "default": 18.0, "min": 4.0, "max": 60.0,
                        "unit": "mm", "group": "bolts"},
        "plate_t": {"type": "float", "default": 16.0, "min": 4.0, "max": 80.0,
                    "unit": "mm", "group": "plate", "description": "板厚 t"},
        "hub_d": {"type": "float", "default": 100.0, "min": 0.0, "max": 500.0,
                  "unit": "mm", "group": "hub", "description": "轮毂外径"},
        "hub_h": {"type": "float", "default": 25.0, "min": 0.0, "max": 150.0,
                  "unit": "mm", "group": "hub", "description": "轮毂高（0 = 平板法兰）"},
        "raised_h": {"type": "float", "default": 2.0, "min": 0.0, "max": 10.0,
                     "unit": "mm", "group": "face", "description": "凸面高（0 = 全平面）"},
        "face_d": {"type": "float", "default": 100.0, "min": 10.0, "max": 500.0,
                   "unit": "mm", "group": "face", "description": "凸面外径"},
        "groove_d": {"type": "float", "default": 0.0, "min": 0.0, "max": 500.0,
                     "unit": "mm", "group": "face", "description": "密封槽中心直径（0 = 无槽）"},
        "groove_w": {"type": "float", "default": 0.0, "min": 0.0, "max": 40.0,
                     "unit": "mm", "group": "face", "description": "密封槽宽"},
        "groove_depth": {"type": "float", "default": 0.0, "min": 0.0, "max": 8.0,
                         "unit": "mm", "group": "face", "description": "密封槽深（0 = 无槽）"},
    },
    presets={
        "safe": {"outer_d": 110.0, "bore_d": 50.0, "bolt_circle_d": 88.0,
                 "bolt_holes": 6, "bolt_hole_d": 14.0, "plate_t": 12.0,
                 "hub_d": 68.0, "hub_h": 18.0, "raised_h": 2.0, "face_d": 66.0,
                 "groove_depth": 0.0, "groove_d": 0.0, "groove_w": 0.0},
        "nominal": {"outer_d": 160.0, "bore_d": 80.0, "bolt_circle_d": 130.0,
                    "bolt_holes": 6, "bolt_hole_d": 18.0, "plate_t": 16.0,
                    "hub_d": 100.0, "hub_h": 25.0, "raised_h": 2.0,
                    "face_d": 100.0, "groove_depth": 0.0, "groove_d": 0.0,
                    "groove_w": 0.0},
        "hero": {"outer_d": 240.0, "bore_d": 120.0, "bolt_circle_d": 200.0,
                 "bolt_holes": 6, "bolt_hole_d": 22.0, "plate_t": 20.0,
                 "hub_d": 150.0, "hub_h": 35.0, "raised_h": 3.0,
                 "face_d": 166.0, "groove_d": 142.0, "groove_w": 10.0,
                 "groove_depth": 1.5},
    },
    tags=["mechanical", "flange", "revolve", "pattern_polar", "boolean"],
    expected_genus=7, material="steel",
    constraints=[
        {"id": "bolt_rim", "severity": "error",
         "expr": "outer_d - bolt_circle_d - bolt_hole_d >= 0.06 * outer_d",
         "fix": "bolt_circle_d = outer_d - bolt_hole_d - 0.08 * outer_d",
         "message": "螺栓孔外缘到法兰外圆的板边余量至少 6% 外径"},
        {"id": "bolt_web", "severity": "error",
         "expr": "pi * bolt_circle_d / bolt_holes - bolt_hole_d >= 2.5",
         "fix": "bolt_holes = max(4, floor(pi * bolt_circle_d / (bolt_hole_d + 3)))",
         "message": "相邻螺栓孔之间的腹板宽度至少 2.5 mm（极坐标阵列不能自身相交）"},
        {"id": "bolt_clear_hub", "severity": "error",
         "expr": "(hub_h <= 0) or (bolt_circle_d - bolt_hole_d >= hub_d + 4)",
         "fix": "bolt_circle_d = hub_d + bolt_hole_d + 6",
         "message": "螺栓孔内缘必须让开轮毂外径 4 mm"},
        {"id": "raised_face", "severity": "error",
         "expr": "(raised_h <= 0) or (face_d >= bore_d + 8 and face_d <= bolt_circle_d - bolt_hole_d - 4)",
         "fix": "face_d = clamp(bolt_circle_d - bolt_hole_d - 8, bore_d + 10, ...)",
         "message": "凸面环宽要够（>=4 mm）且不得伸进螺栓圆"},
        {"id": "raised_depth", "severity": "error",
         "expr": "raised_h <= plate_t * 0.5",
         "fix": "raised_h = plate_t * 0.15",
         "message": "凸面高度不得超过板厚的一半"},
        {"id": "groove_fits", "severity": "error",
         "expr": "(groove_depth <= 0) or (raised_h > 0 and groove_d - groove_w >= bore_d + 4 and groove_d + groove_w <= face_d - 4)",
         "fix": "groove_d = (bore_d + face_d) / 2, groove_w = (face_d - bore_d) / 3",
         "message": "密封槽必须整圈落在凸面环内（距内孔/外缘各留 2 mm）"},
        {"id": "groove_depth_ok", "severity": "error",
         "expr": "groove_depth <= raised_h * 0.8",
         "fix": "groove_depth = raised_h * 0.5",
         "message": "槽深不得超过凸面高度的 80%，否则切穿凸面"},
        {"id": "hub_wall", "severity": "error",
         "expr": "(hub_h <= 0) or (hub_d - bore_d >= 4)",
         "fix": "hub_d = bore_d + 6",
         "message": "轮毂单边壁厚至少 2 mm（焊接坡口需要材料）"},
        {"id": "hub_fits_plate", "severity": "error",
         "expr": "hub_d <= outer_d * 0.85 and hub_h <= outer_d * 0.6",
         "fix": "hub_d = outer_d * 0.7, hub_h = outer_d * 0.2",
         "message": "轮毂必须明显小于法兰外径"},
        {"id": "bore_fits_plate", "severity": "error",
         "expr": "bore_d <= outer_d * 0.75",
         "fix": "bore_d = outer_d * 0.5",
         "message": "内孔超过 0.75 外径时板环太薄"},
        {"id": "plate_slender", "severity": "warn",
         "expr": "plate_t <= outer_d * 0.15",
         "fix": "plate_t = outer_d * 0.1",
         "message": "板厚超过 0.15 外径属厚板法兰，刚度有余而重量偏大"},
    ],
)
def build_flange(v, quality="draft"):
    """Revolve plate/hub/raised face, cut the gasket groove and bolt circle."""
    d = _G.flange_derived(float(v["outer_d"]), float(v["bore_d"]),
                          float(v["bolt_circle_d"]), float(v["bolt_hole_d"]),
                          int(v["bolt_holes"]), float(v["plate_t"]),
                          float(v["hub_d"]), float(v["hub_h"]),
                          float(v["raised_h"]), float(v["face_d"]),
                          float(v["groove_d"]), float(v["groove_w"]),
                          float(v["groove_depth"]))
    seg = _q(quality, 96, 144, 192)
    body = _S.revolve(_G.flange_profile(d), segments=seg)

    if d["has_groove"]:
        body = difference(body, _S.revolve(_G.groove_profile(d),
                                           segments=seg))

    pad = max(2e-4, 0.02 * d["plate_t"])
    z_hi = d["plate_t"] + d["raised_h"] + pad
    z_lo = -pad
    half = 0.5 * (z_hi + z_lo)
    height = z_hi - z_lo
    hole_seg = _q(quality, 32, 48, 64)
    for i in range(d["n_holes"]):
        ang = 2.0 * math.pi * i / d["n_holes"]
        tool = _P.cylinder(d["hole_r"], height,
                           center=(d["bc_r"] * math.cos(ang),
                                   d["bc_r"] * math.sin(ang), half),
                           segments=hole_seg)
        body = difference(body, tool)
    return _sit(body), finish(material="steel", smooth_angle_deg=32.0)


# ================================================================= ladder

@recipe(
    "tolerance.clearance_ladder", "打印配合间隙阶梯量块",
    "FDM 间隙标定件：一条脊梁上并排 N 级盲孔量规 + 一行名义销。"
    "孔直径 = pin_d + clearance_i，间隙按 clearance_min..clearance_max 等分，"
    "每级用 mech_geom.nearest_fit 标注最近的 ISO 286-2 配合（6 mm 名义直径的直径间隙："
    "H7/g6 0.004–0.024、H7/f7 0.010–0.034、H8/e9 0.020–0.068、H9/d10 0.030–0.108、"
    "H11/h11 0.000–0.150 mm）。ISO 286 是微米级标准，而 FDM 实际公差约 ±0.05–0.15 mm，"
    "故默认梯度刻意落在标准松端及其以外——测的是「打印出来的」间隙。"
    "盲孔留底（floor_t 派生）使 genus 恒为 0，pairs 可自由改。",
    params={
        "pin_d": {"type": "float", "default": 6.0, "min": 2.0, "max": 20.0,
                  "unit": "mm", "group": "coupon", "description": "名义轴径"},
        "pairs": {"type": "int", "default": 5, "min": 2, "max": 10,
                  "unit": "count", "group": "coupon", "description": "量规级数 N"},
        "clearance_min": {"type": "float", "default": 0.02, "min": 0.0,
                          "max": 1.0, "unit": "mm", "group": "fits",
                          "description": "最小直径间隙（最紧配合）"},
        "clearance_max": {"type": "float", "default": 0.2, "min": 0.02,
                          "max": 1.5, "unit": "mm", "group": "fits",
                          "description": "最大直径间隙（最松配合）"},
        "depth": {"type": "float", "default": 10.0, "min": 2.0, "max": 40.0,
                  "unit": "mm", "group": "coupon", "description": "量规孔深（盲孔）"},
        "gap": {"type": "float", "default": 6.0, "min": 0.5, "max": 30.0,
                "unit": "mm", "group": "coupon", "description": "相邻量规之间的间距"},
        "spine_w": {"type": "float", "default": 30.0, "min": 8.0, "max": 80.0,
                    "unit": "mm", "group": "coupon", "description": "脊梁宽度"},
    },
    presets={
        "safe": {"pin_d": 4.0, "pairs": 4, "clearance_min": 0.08,
                 "clearance_max": 0.26, "depth": 8.0, "gap": 5.0,
                 "spine_w": 24.0},
        "nominal": {"pin_d": 6.0, "pairs": 5, "clearance_min": 0.02,
                    "clearance_max": 0.20, "depth": 10.0, "gap": 6.0,
                    "spine_w": 30.0},
        "hero": {"pin_d": 8.0, "pairs": 7, "clearance_min": 0.02,
                 "clearance_max": 0.60, "depth": 14.0, "gap": 8.0,
                 "spine_w": 40.0},
    },
    tags=["tolerance", "fit", "clearance", "coupon", "gauge", "boolean"],
    expected_genus=0, material="abs_white",
    constraints=[
        {"id": "ladder_monotonic", "severity": "error",
         "expr": "clearance_max >= clearance_min + 0.02",
         "fix": "clearance_max = clearance_min + 0.3",
         "message": "阶梯必须真的分级：最大间隙要比最小间隙大 0.02 mm 以上"},
        {"id": "clearance_range", "severity": "error",
         "expr": "clearance_max <= pin_d * 0.25",
         "fix": "clearance_max = pin_d * 0.1",
         "message": "直径间隙超过名义轴径 25% 就不再算配合（ISO 286 最大间隙档约 2~8%）"},
        {"id": "gauge_depth", "severity": "error",
         "expr": "depth >= 0.8 * pin_d",
         "fix": "depth = pin_d",
         "message": "量规孔深至少 0.8 倍轴径，否则试插时无法判断轴线偏摆"},
        {"id": "rows_clear", "severity": "error",
         "expr": "spine_w >= 2 * (pin_d + clearance_max) + 4 * max(0.15 * pin_d, 1.0) + 8",
         "fix": "spine_w = 2 * (pin_d + clearance_max) + 4 * max(0.15 * pin_d, 1.0) + 12",
         "message": "孔座与销分列 ±脊宽/4，脊梁太窄会让两行连在一起"},
        {"id": "station_gap", "severity": "error",
         "expr": "gap >= clearance_max",
         "fix": "gap = clearance_max * 2",
         "message": "相邻量规的间距必须大于最大间隙，否则测不出是哪一级配合"},
        {"id": "pin_slender", "severity": "warn",
         "expr": "depth <= 6 * pin_d",
         "fix": "depth = 4 * pin_d",
         "message": "孔深超过 6 倍轴径时 FDM 孔轴线误差已经主导配合判断"},
    ],
)
def build_clearance_ladder(v, quality="draft"):
    """Spine + blind gauge-hole bosses (-y) + nominal pins (+y)."""
    lay = _G.ladder_layout(float(v["pin_d"]), int(v["pairs"]),
                           float(v["clearance_min"]),
                           float(v["clearance_max"]), float(v["depth"]),
                           float(v["gap"]), float(v["spine_w"]))
    seg = _q(quality, 32, 48, 64)
    spine_t = lay["spine_t"]
    body = _P.box([lay["length"], lay["width"], spine_t],
                  center=(0.0, 0.0, 0.5 * spine_t))
    embed = 0.45 * spine_t                    # sink parts -> no coplanar faces

    # gauge-hole bosses on the -y row
    for x, boss_d in zip(lay["x"], lay["boss_d"]):
        h = lay["boss_h"] + embed
        body = union(body, _P.cylinder(0.5 * boss_d, h,
                                       center=(x, lay["y_hole"],
                                               spine_t - embed + 0.5 * h),
                                       segments=seg))

    # nominal pins on the +y row: cone root + shaft
    cone_tot = max(0.8e-3, 0.5 * lay["pin_d"]) + embed
    for x in lay["x"]:
        pin_r = lay["pin_r"]
        ym = lay["y_pin"]
        body = union(body, _P.cone(1.7 * pin_r, pin_r, cone_tot,
                                   center=(x, ym, spine_t - embed
                                           + 0.5 * cone_tot), segments=seg))
        shaft_h = (lay["spine_t"] + lay["pin_h"]) - (spine_t - embed + cone_tot)
        if shaft_h > 1e-4:
            body = union(body, _P.cylinder(pin_r, shaft_h,
                                           center=(x, ym, spine_t - embed
                                                   + cone_tot + 0.5 * shaft_h),
                                           segments=seg))

    # blind gauge holes: bored from the boss top down to the derived floor
    pad = max(2e-4, 0.02 * lay["depth"])
    z_top = lay["spine_t"] + lay["boss_h"]
    z_floor = lay["spine_t"] + lay["floor_t"]
    for x, hole_d in zip(lay["x"], lay["hole_d"]):
        tool = _P.cylinder(0.5 * hole_d, z_top - z_floor + pad,
                           center=(x, lay["y_hole"],
                                   0.5 * (z_top + pad + z_floor)), segments=seg)
        body = difference(body, tool)
    return _sit(body), finish(material="abs_white", smooth_angle_deg=32.0)
