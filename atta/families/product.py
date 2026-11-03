# -*- coding: utf-8 -*-
"""Product + architecture families.

Two recipes live here:

``product.screw_cap``
    Bottle/jar cap: **one** lofted outer shell (chamfered rim, straight
    skirt, knurled grip band), a **blind** bore (so the cap stays a genus-0
    solid with a closed top), annular thread-relief grooves cut into the bore
    wall, and an optional tamper score line.

    The thread is an **approximation by coaxial annular grooves**, not a true
    helix.  A real helical thread needs a swept profile whose two ends must be
    closed against the bore wall; the kernel has no "thread" op yet, and
    closing a swept helix watertight through the bore is exactly the kind of
    sliver-producing boolean this corpus documents rather than ships.  The
    groove set gives the same *visual* and *axial retention* signature at a
    fraction of the risk; ``rib_rows``/``thread_pitch``/``rib_depth`` are the
    honest knobs.

    Two constructions here are the transferable part:

    1. *The knurl is a polar radius modulation, not N boolean unions.*  The
       grip band is the polar envelope of ``knurl_count`` rib circles, lofted
       together with the skirt rings.  Unioning the same N cylinders fails as
       soon as adjacent ribs overlap (the tool mesh self-intersects): measured,
       the concat-union version raised ``AttaError`` at ``knurl_count=48,
       knurl_depth=0.8``, while the envelope loft is watertight for every
       count/depth in range.  The knurl also *adds* material, so it costs no
       wall thickness — verified by ray probe (wall unchanged with ribs on).
    2. *The wall budget must bound the SUM of the subtractive features.*  The
       bore grooves (``rib_depth``) and the score line (``score_depth``) cut
       the same skirt radially.  Bounding each one alone let every preset
       resolve while the part was unprintable — measured ``min_wall_mm`` =
       0.257 at nominal before the fix.  Now ``depth_stack`` bounds
       ``rib_depth + score_depth + wall`` against the real skirt wall, the
       builder gives every subtractive feature its own Z band, and the
       smallest feature the wall sampler can still find is the thread *land*
       (``thread_land`` constraint, floored at 1.6 mm in the builder).

``architecture.fluted_column``
    Classical column: entasis shaft by ``revolve``, flutes by
    ``pattern_polar`` + ``difference`` (each flute is a lofted tube whose axis
    follows the local entasis radius, so the grooves track the taper), then
    the mouldings are unioned **last** (torus roll at the base, square plinth,
    echinus cone, square abacus) so the flute cut can never nick them.

Feature order (both recipes, and this *is* the transferable knowledge):

    body -> pattern (ribs / flutes) -> subtract features -> mouldings last

Both builders return ``(mesh, finish)`` with lengths in metres.
"""

from __future__ import annotations

import math

import numpy as np

from .. import primitives as _P
from .. import surfaces as _S
from ..boolean import difference, pattern_polar, union
from ..kit import circle_loop, finish, quality_level as _q, ring_at
from ..mesh import concat
from ..registry import recipe

__all__ = ["_build_screw_cap", "_build_fluted_column"]


# ---------------------------------------------------------------- usefulness

def _clamp(x, lo, hi):
    return max(float(lo), min(float(hi), float(x)))


def _polar_envelope(r_base, rib_r, axis_r, count, n):
    """Polar radius of ``disc(r_base) ∪ rib circles``, sampled at ``n`` angles.

    Each rib is a circle of radius ``rib_r`` centred at ``axis_r`` every
    ``2*pi/count``; the outer boundary of the union is a single-valued polar
    function (the ribs never contain the axis while ``3*rib_r < r_base``), so
    the resulting loop is a valid star-shaped polygon and the loft of such
    rings is watertight **by construction** — no boolean, no sliver risk.
    """
    ang = np.linspace(0.0, 2.0 * math.pi, int(n), endpoint=False)
    u = np.column_stack([np.cos(ang), np.sin(ang)])
    out = np.full(len(ang), float(r_base))
    if count > 0 and rib_r > 0.0:
        step = 2.0 * math.pi / int(count)
        k = np.arange(int(count))
        centres = np.column_stack([axis_r * np.cos(k * step),
                                   axis_r * np.sin(k * step)])
        d = u @ centres.T                              # (n, count)
        rad = rib_r * rib_r - axis_r * axis_r + d * d
        t = np.where(rad >= 0.0, d + np.sqrt(np.maximum(rad, 0.0)), 0.0)
        out = np.maximum(out, t.max(axis=1))
    return np.column_stack([out * np.cos(ang), out * np.sin(ang)])


# ---------------------------------------------------------------- screw cap

@recipe(
    "product.screw_cap", "螺纹瓶盖",
    "回转外壳（倒角裙壁 + 闭合顶盖）+ 极坐标肋条抓握带 + 盲孔内腔 + "
    "内壁环形退刀槽（近似螺纹）+ 可选防盗环刻线。螺纹为环形槽近似，非真螺旋。"
    "所有减料特征在 Z 向分区，且每条都用「壁厚预算」约束守住最小壁厚。",
    params={
        "inner_d": {"type": "float", "default": 28.0, "min": 8.0, "max": 120.0,
                    "unit": "mm", "group": "fit",
                    "description": "内腔直径（匹配瓶颈外径 + 配合间隙）"},
        "outer_d": {"type": "float", "default": 35.0, "min": 12.0, "max": 140.0,
                    "unit": "mm", "group": "body", "description": "裙壁外径（不含肋条）"},
        "height": {"type": "float", "default": 18.0, "min": 6.0, "max": 60.0,
                   "unit": "mm", "group": "body"},
        "top_t": {"type": "float", "default": 2.0, "min": 0.8, "max": 12.0,
                  "unit": "mm", "group": "shell", "description": "顶盖厚度（盲孔顶板）"},
        "wall": {"type": "float", "default": 2.0, "min": 1.5, "max": 10.0,
                 "unit": "mm", "group": "shell",
                 "description": "设计最小壁厚：每个减料特征之后必须剩余的壁厚 "
                                "（下限 1.5 mm = 产品类验收规则）"},
        "knurl_count": {"type": "int", "default": 24, "min": 0, "max": 96,
                        "unit": "count", "group": "grip", "description": "竖向肋条数，0=光面"},
        "knurl_depth": {"type": "float", "default": 0.8, "min": 0.0, "max": 3.0,
                        "unit": "mm", "group": "grip",
                        "description": "肋条径向凸出量（向外加料，不减薄壁厚）"},
        "rib_depth": {"type": "float", "default": 0.5, "min": 0.0, "max": 3.0,
                      "unit": "mm", "group": "thread",
                      "description": "内壁环形槽径向切深（显式参数，不隐含）"},
        "rib_rows": {"type": "int", "default": 4, "min": 0, "max": 24,
                     "unit": "count", "group": "thread",
                     "description": "内壁环形槽数量（近似螺纹），0=无"},
        "thread_pitch": {"type": "float", "default": 3.0, "min": 1.0, "max": 10.0,
                         "unit": "mm", "group": "thread", "description": "环形槽轴距"},
        "score_depth": {"type": "float", "default": 0.7, "min": 0.0, "max": 3.0,
                        "unit": "mm", "group": "thread",
                        "description": "防盗环刻线径向切深（显式参数，不隐含）"},
        "band_gap": {"type": "float", "default": 3.0, "min": 0.0, "max": 20.0,
                     "unit": "mm", "group": "thread",
                     "description": "防盗环刻线离底面的高度，0=关闭"},
        "segments": {"type": "int", "default": 128, "min": 32, "max": 384,
                     "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"wall": 2.4, "outer_d": 36.0, "top_t": 2.6, "knurl_count": 20,
                 "knurl_depth": 0.6, "rib_depth": 0.4, "score_depth": 0.6,
                 "rib_rows": 3, "thread_pitch": 3.4, "band_gap": 3.5,
                 "segments": 96},
        "nominal": {"wall": 2.0, "outer_d": 35.0, "top_t": 2.0, "knurl_count": 24,
                    "knurl_depth": 0.8, "rib_depth": 0.5, "score_depth": 0.7,
                    "rib_rows": 4, "thread_pitch": 3.0, "band_gap": 3.0,
                    "segments": 128},
        "hero": {"wall": 1.8, "outer_d": 35.0, "height": 22.0, "top_t": 2.0,
                 "knurl_count": 36, "knurl_depth": 0.45, "rib_depth": 0.55,
                 "score_depth": 0.75, "rib_rows": 6, "thread_pitch": 3.0,
                 "band_gap": 2.8, "segments": 256},
    },
    tags=["product", "revolve", "pattern_polar", "boolean", "knurl", "cap",
          "thread_approx", "wall_budget"],
    expected_genus=0, material="abs_white",
    constraints=[
        # ---- wall budget: the *sum* rule is the one that keeps the part
        #      printable when two subtractive features share a cross-section
        {"id": "skirt_wall", "expr": "outer_d - inner_d >= 2 * wall",
         "severity": "error", "message": "裙壁过薄：实际壁厚 (outer_d-inner_d)/2 小于设计壁厚 wall",
         "fix": "outer_d = inner_d + 2 * wall"},
        {"id": "rib_depth_fits", "expr": "outer_d - inner_d >= 2 * (rib_depth + wall)",
         "severity": "error",
         "message": "内壁环形槽切深后剩余壁厚不足 wall",
         "fix": "rib_depth = (outer_d - inner_d) / 2 - wall"},
        {"id": "score_depth_fits", "expr": "outer_d - inner_d >= 2 * (score_depth + wall)",
         "severity": "error",
         "message": "防盗刻线切深后剩余壁厚不足 wall",
         "fix": "score_depth = (outer_d - inner_d) / 2 - wall"},
        {"id": "depth_stack", "expr": "outer_d - inner_d >= 2 * (rib_depth + score_depth + wall)",
         "severity": "error",
         "message": "环形槽 + 刻线切深叠加后剩余壁厚不足 wall（约束必须约束『和』而不是各自）",
         "fix": "rib_depth + score_depth = (outer_d - inner_d) / 2 - wall"},
        {"id": "thread_land", "expr": "(rib_rows <= 1) or thread_pitch >= 2 * rib_depth + 1.6",
         "severity": "error",
         "message": "环形槽轴距太小，两条槽之间的『槽间台』(land) 不到 1.6 mm",
         "fix": "thread_pitch = 2 * rib_depth + 2.0"},
        {"id": "knurl_embeds", "expr": "3 * knurl_depth <= (outer_d - inner_d) / 2",
         "severity": "error",
         "message": "肋条内伸超过裙壁，会切进内腔、与裙壁只剩刀口接触",
         "fix": "knurl_depth = (outer_d - inner_d) / 6"},
        # ---- thickness / proportions
        {"id": "top_thick", "expr": "top_t >= wall",
         "severity": "error", "message": "顶盖比裙壁薄，盲孔顶板会成为最薄处",
         "fix": "top_t = wall * 1.2"},
        {"id": "top_fits", "expr": "top_t <= height * 0.5",
         "severity": "error", "message": "顶盖过厚，内腔会消失",
         "fix": "top_t = height * 0.25"},
        # ---- feature packing
        {"id": "knurl_pitch", "expr": "knurl_count <= 0 or knurl_count * 4 * knurl_depth"
                                     " <= pi * (outer_d - 2 * knurl_depth)",
         "severity": "warn",
         "message": "肋条间距小于肋宽，相邻肋会合并成波浪带（几何仍闭合，视觉可接受）",
         "fix": "knurl_count = pi * (outer_d - 2*knurl_depth) / (4*knurl_depth)"},
        {"id": "knurl_size", "expr": "knurl_depth <= outer_d * 0.05",
         "severity": "error", "message": "肋条凸出量过大，超过外径的 5%",
         "fix": "knurl_depth = outer_d * 0.03"},
        {"id": "thread_fits", "expr": "(rib_rows <= 1) or "
                                     "((rib_rows - 1) * thread_pitch + band_gap + 1.2"
                                     " <= height - top_t)",
         "severity": "error",
         "message": "环形槽总轴距 + 刻线位置超过盲孔深度，顶部槽会切穿顶板",
         "fix": "rib_rows = ((height - top_t - band_gap - 1.2) / thread_pitch) + 1"},
        {"id": "band_fits", "expr": "(band_gap <= 0) or (band_gap <= height * 0.45)",
         "severity": "error", "message": "防盗刻线位置过高，刻线会切进顶盖",
         "fix": "band_gap = height * 0.2"},
    ],
)
def _build_screw_cap(v, quality="draft"):
    inner_d, outer_d = float(v["inner_d"]), float(v["outer_d"])
    h, top_t = float(v["height"]), float(v["top_t"])
    knurls, kdepth = int(v["knurl_count"]), float(v["knurl_depth"])
    groove, score_d = float(v["rib_depth"]), float(v["score_depth"])
    rows, pitch = int(v["rib_rows"]), float(v["thread_pitch"])
    band = float(v["band_gap"])
    seg = max(24, int(v["segments"]))

    ro, ri = 0.5 * outer_d, 0.5 * inner_d
    if ro <= ri:  # defensive: a constraint already blocks this
        ro = ri + max(4e-4, 0.06 * ri)
    rwall = ro - ri
    MIN = 1e-4                     # 0.1 mm: a feature floor, in metres
    #: rim chamfer, tied to the real wall so a thin cap stays legal
    ch = max(MIN, min(0.10 * h, 0.35 * rwall))

    # ---- z layout.  Every *subtractive* feature owns its own band so that no
    #      two of them ever share a cross-section.  This is the geometric half
    #      of the wall budget; the constraint half is `depth_stack`.
    zb1 = h - top_t                                   # bore ceiling
    score_w = max(2.5e-4, 0.35 * band) if band > 0.0 else 0.0
    score_top = band + 0.5 * score_w
    score_bot = band - 0.5 * score_w
    groove = min(groove, 0.45 * rwall, 0.55 * ri)
    # first groove: keep >= 1.8 mm of land between it and the skirt mouth (the
    # land is a real feature, and a ray sampler treats it as wall thickness),
    # and keep out of the rim-chamfer zone where the wall is already thinner
    z_first = ch + groove + 1.8e-3
    if band > 0.0:
        z_first = max(z_first, score_top + groove + max(3e-4, 0.4 * groove))
    last_groove = zb1 - max(1.6 * groove, 0.03 * zb1)
    band_lo = max(ch + 0.10 * h, score_top + 0.02 * h)
    band_hi = h - ch - 0.05 * h

    # ---- 1. outer shell as ONE loft: chamfered rim, straight skirt, and the
    #         knurled grip band as a *polar radius modulation* of the skirt
    #         rings.  Building the band analytically (instead of unioning
    #         `knurl_count` rib cylinders) is what keeps high rib counts
    #         watertight: with N overlapping cylinders the tool mesh becomes
    #         self-intersecting and the boolean fails, while the polar
    #         envelope of the same union is a star-shaped loop that cannot.
    n_ang = max(seg, 8 * knurls if knurls > 0 else 0)
    has_band = knurls > 0 and kdepth > 1e-6
    ramp = 0.0
    if has_band:
        ramp = max(2e-5, min(0.15 * kdepth, 0.45 * (band_lo - ch)))
        if band_hi - band_lo <= 2.0 * ramp:           # no room: plain skirt
            has_band = False
    if has_band:
        rib_r = 2.0 * kdepth                   # crest protrudes exactly knurl_depth
        axis_r = ro - kdepth                   # embedded one depth into the skirt
        env = _polar_envelope(ro, rib_r, axis_r, knurls, n_ang)
        ring_c = circle_loop(ro, n_ang)
        ring_r = circle_loop(max(MIN, ro - ch), n_ang)
        body = _S.loft([ring_at(ring_r, 0.0),
                        ring_at(ring_c, ch),
                        ring_at(ring_c, band_lo - ramp),
                        ring_at(env, band_lo),
                        ring_at(env, band_hi),
                        ring_at(ring_c, band_hi + ramp),
                        ring_at(ring_c, h - ch),
                        ring_at(ring_r, h)], caps=True)
    elif ch > 1.5 * MIN:
        body = _S.revolve(np.array([[ro - ch, 0.0], [ro, ch],
                                    [ro, h - ch], [ro - ch, h]]), seg)
    else:
        body = _S.revolve(np.array([[ro, 0.0], [ro, h]]), seg)

    # ---- 2. blind bore (keeps the cap genus 0: closed top disc)
    over = max(1.2e-4, 0.5 * top_t)
    if zb1 + over > 1e-3:
        bore = _P.cylinder(ri, zb1 + over, center=(0.0, 0.0, 0.5 * (zb1 - over)),
                           segments=seg)
        body = difference(body, bore)

    # ---- 3. thread relief: annular grooves in the bore wall (thread approximation)
    if rows > 0 and groove > 5e-5 and zb1 > 3.0 * groove:
        # two independent floors: grooves must not touch each other
        # (2.2*groove) and the land between them must stay a printable wall
        # (2*groove + 1.6 mm) — the land is the thinnest feature the wall
        # sampler can find inside the bore
        pitch_eff = max(pitch, 2.2 * groove, 2.0 * groove + 1.6e-3)
        tools = []
        for i in range(rows):
            z = z_first + i * pitch_eff
            if z > last_groove:
                break
            tools.append(_P.torus(ri, groove, center=(0.0, 0.0, z),
                                  seg_major=seg,
                                  seg_minor=max(8, int(_q(quality, 10, 12, 16)))))
        if tools:
            body = difference(body, concat(tools))

    # ---- 4. tamper score line: outer notch that must NOT sever the part and
    #         must not touch the groove band (Z-separated above)
    if band > 1e-6 and score_d > 1e-6:
        nd = min(score_d, 0.70 * rwall)
        r_in = ro - nd
        r_out = ro + (kdepth if has_band else 0.0) + max(4e-4, score_w)
        if (score_bot > MIN and r_in > ri + MIN and r_out > r_in
                and score_top < band_lo - MIN):
            loop = np.array([[r_in, score_bot], [r_out, score_bot],
                             [r_out, score_top], [r_in, score_top],
                             [r_in, score_bot]])
            body = difference(body, _S.revolve(loop, seg))

    return body, finish(bevel_width=min(0.0006, 0.30 * rwall),
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=45.0, material="abs_white")


# ---------------------------------------------------------------- fluted column

@recipe(
    "architecture.fluted_column", "古典凹槽柱",
    "收分 + 卷杀（entasis）回转柱身，极坐标阵列的凹槽沿收分半径逐环放样后差集，"
    "柱础/柱头线脚最后并集。特征顺序：柱身 -> 凹槽 -> 线脚。",
    params={
        "height": {"type": "float", "default": 2400.0, "min": 300.0, "max": 12000.0,
                   "unit": "mm", "group": "body", "description": "柱总高（含柱础与柱头）"},
        "bottom_d": {"type": "float", "default": 320.0, "min": 60.0, "max": 2400.0,
                     "unit": "mm", "group": "shaft", "description": "柱身底部直径"},
        "top_d": {"type": "float", "default": 260.0, "min": 40.0, "max": 2400.0,
                  "unit": "mm", "group": "shaft", "description": "柱身顶部直径"},
        "flute_count": {"type": "int", "default": 20, "min": 0, "max": 64,
                        "unit": "count", "group": "flute", "description": "凹槽数，0=光柱"},
        "flute_depth": {"type": "float", "default": 6.0, "min": 0.0, "max": 60.0,
                        "unit": "mm", "group": "flute"},
        "flute_frac": {"type": "float", "default": 0.86, "min": 0.10, "max": 0.98,
                       "unit": "ratio", "group": "flute",
                       "description": "柱身被开槽的比例（其余为柱颈光面）"},
        "entasis": {"type": "float", "default": 0.02, "min": 0.0, "max": 0.10,
                    "unit": "ratio", "group": "shaft",
                    "description": "卷杀鼓凸量（相对底半径），0=直线收分"},
        "base_h": {"type": "float", "default": 160.0, "min": 20.0, "max": 1600.0,
                   "unit": "mm", "group": "base"},
        "base_w": {"type": "float", "default": 440.0, "min": 80.0, "max": 4000.0,
                   "unit": "mm", "group": "base", "description": "方形柱础边长"},
        "cap_h": {"type": "float", "default": 200.0, "min": 20.0, "max": 2000.0,
                  "unit": "mm", "group": "capital"},
        "segments": {"type": "int", "default": 240, "min": 32, "max": 640,
                     "unit": "count", "group": "quality", "advanced": True,
                     "description": "柱身圆周分段；建议 >= 12 x flute_count"},
        "profile_samples": {"type": "int", "default": 40, "min": 8, "max": 160,
                            "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"flute_count": 16, "flute_depth": 5.0, "entasis": 0.015,
                 "base_h": 170.0, "cap_h": 210.0, "segments": 192,
                 "profile_samples": 32},
        "nominal": {"flute_count": 20, "flute_depth": 6.0, "entasis": 0.02,
                    "base_h": 160.0, "cap_h": 200.0, "segments": 240,
                    "profile_samples": 40},
        "hero": {"flute_count": 24, "flute_depth": 7.0, "entasis": 0.025,
                 "base_h": 160.0, "cap_h": 200.0, "base_w": 460.0,
                 "segments": 384, "profile_samples": 64},
    },
    tags=["architecture", "revolve", "pattern_polar", "boolean", "flute",
          "column", "entasis"],
    expected_genus=0, material="ceramic",
    constraints=[
        {"id": "taper_ok", "expr": "top_d <= bottom_d",
         "severity": "error", "message": "柱顶直径大于柱底，收分方向反了",
         "fix": "top_d = bottom_d * 0.82"},
        {"id": "plinth_wider", "expr": "base_w >= bottom_d * 1.15",
         "severity": "error", "message": "柱础未比柱身宽，柱身会露出础边",
         "fix": "base_w = bottom_d * 1.4"},
        {"id": "abacus_covers", "expr": "base_w >= top_d * 1.55",
         "severity": "error", "message": "顶板（abacus）盖不住柱头 echinus 的外缘",
         "fix": "base_w = top_d * 1.7"},
        {"id": "cap_ok", "expr": "cap_h <= height * 0.2",
         "severity": "error", "message": "柱头过高，柱身被压扁",
         "fix": "cap_h = height * 0.12"},
        {"id": "base_ok", "expr": "base_h <= height * 0.25",
         "severity": "error", "message": "柱础过高",
         "fix": "base_h = height * 0.1"},
        {"id": "stack_ok", "expr": "base_h + cap_h <= height * 0.45",
         "severity": "error", "message": "柱础 + 柱头占掉总高太多，柱身不足一半",
         "fix": "base_h + cap_h = height * 0.3"},
        {"id": "flute_tool_fits",
         "expr": "flute_count <= 0 or flute_depth * flute_count <= 0.41 * pi * top_d",
         "severity": "error",
         "message": "凹槽过深/过密：槽工具半径会小于槽深，形成倒扣（undercut）",
         "fix": "flute_depth = 0.35 * pi * top_d / flute_count"},
        {"id": "flute_shallow",
         "expr": "flute_depth <= min(bottom_d, top_d) * 0.15",
         "severity": "warn", "message": "凹槽深度超过柱径的 15%，柱身会明显变细",
         "fix": "flute_depth = min(bottom_d, top_d) * 0.08"},
        {"id": "segments_per_flute",
         "expr": "flute_count <= 0 or segments >= flute_count * 8",
         "severity": "warn",
         "message": "每个凹槽的圆周分段不足 8，布尔会出现细长三角形（sliver）",
         "fix": "segments = flute_count * 12"},
    ],
)
def _build_fluted_column(v, quality="draft"):
    h = float(v["height"])
    rb, rt = 0.5 * float(v["bottom_d"]), 0.5 * float(v["top_d"])
    n_fl = int(v["flute_count"])
    f_depth = float(v["flute_depth"])
    f_frac = float(v["flute_frac"])
    entasis = float(v["entasis"])
    base_h, base_w = float(v["base_h"]), float(v["base_w"])
    cap_h = float(v["cap_h"])
    seg = max(32, int(v["segments"]))
    prof_n = max(8, int(v["profile_samples"]))

    # ---- z layout (all bands overlap their neighbours by construction so no
    #      two boolean inputs share a coplanar face)
    z_shaft_lo = base_h                      # visible shaft foot
    z_shaft_hi = h - cap_h                   # visible shaft head
    if z_shaft_hi - z_shaft_lo < 0.05 * h:   # defensive (constraints prevent it)
        z_shaft_lo, z_shaft_hi = 0.25 * h, 0.8 * h
    span = z_shaft_hi - z_shaft_lo
    z_low = z_shaft_lo - 0.45 * base_h        # buried in the plinth
    z_high = z_shaft_hi + 0.45 * cap_h        # buried in the echinus

    # ---- 1. entasis radius function  r(t) = taper * (1 + entasis * sin(pi t))
    def r_at(z):
        t = _clamp((float(z) - z_shaft_lo) / span, 0.0, 1.0)
        return (rb + (rt - rb) * t) * (1.0 + entasis * math.sin(math.pi * t))

    # The base roll (torus moulding) lives *in the profile*, as a smooth bump,
    # NOT as a unioned torus.  A unioned torus meets the shaft at a shallow
    # crossing; atta_verify evaluates the finish (Blender bevel) before it
    # samples wall thickness, and the bevel across that crossing carved a
    # 0.118 mm "wall" at z=171.6 mm (measured, flute-free build).  A C-infinity
    # bump has no edge at all, so there is nothing for the bevel to carve, and
    # it also removes one boolean.
    m_t = max(2e-4, 0.06 * rb)               # roll tube radius (moulding scale)
    z_t = z_shaft_lo + 2.4 * m_t             # roll centre, above the plinth
    roll_s = 0.80 * m_t                      # bump width
    roll_p = 0.45 * m_t                      # bump protrusion

    def r_prof(z):
        r = r_at(z)
        if roll_s > 0.0:
            u = (float(z) - z_t) / roll_s
            if abs(u) < 3.0:
                r = r + roll_p * math.exp(-u * u)
        return r

    # sample coarsely along the shaft but finely across the roll, otherwise the
    # 3-sigma bump (~5 % of the height) falls between profile samples
    z_lo_roll = max(z_shaft_lo, z_t - 3.2 * roll_s)
    z_hi_roll = min(z_shaft_hi, z_t + 3.2 * roll_s)
    z_head = np.linspace(z_shaft_lo, z_lo_roll, max(3, prof_n // 4), endpoint=False)
    z_roll = np.linspace(z_lo_roll, z_hi_roll, 14)
    z_tail = np.linspace(z_hi_roll, z_shaft_hi, max(4, prof_n - len(z_head)), endpoint=False)
    zs = np.concatenate([z_head, z_roll, z_tail])
    prof = np.vstack([[r_prof(z_low), z_low],
                      np.column_stack([np.array([r_prof(z) for z in zs]), zs]),
                      [r_prof(z_high), z_high]])
    body = _S.revolve(prof, seg)

    # ---- 2. flutes AFTER the entasis: one lofted tube per flute, axis riding
    #      the local shaft radius, then a single polar-pattern difference.
    if n_fl > 0 and f_depth > 1e-6:
        pitch_top = math.pi * 2.0 * rt / n_fl
        # target groove width ~ classical 0.78 of the pitch, but never wider
        # than 0.92 * pitch so neighbouring tools stay disjoint
        w_target = 0.78 * pitch_top
        r_tool = (0.25 * w_target * w_target + f_depth * f_depth) / (2.0 * f_depth)
        r_tool = min(max(r_tool, 1.12 * f_depth), 0.46 * pitch_top)
        d_eff = min(f_depth, 0.88 * r_tool)      # never undercut the surface
        if d_eff > 1e-6:
            z0 = z_shaft_lo + 0.10 * span
            z1 = z0 + _clamp(f_frac, 0.05, 0.98) * (z_shaft_hi - z0)
            n_ring = max(6, int(_q(quality, 12, 20, 32)))
            z_rings = np.linspace(z0 - 1.2 * d_eff, z1 + 1.2 * d_eff, n_ring)
            tool_seg = max(12, int(_q(quality, 16, 24, 32)))
            rings = []
            for z in z_rings:
                loop = circle_loop(r_tool, tool_seg)
                axis = r_at(z) + r_tool - d_eff
                rings.append(ring_at(loop + np.array([axis, 0.0]), float(z)))
            flutes = pattern_polar(_S.loft(rings, caps=True), n_fl)
            body = difference(body, flutes)

    # ---- 3. mouldings LAST: plinth -> capital (echinus + abacus).  The base
    #      roll is already part of the shaft profile (see r_prof), so only
    #      genuinely separate solids are unioned here.
    if base_h > 1e-3 and base_w > 1e-3:
        body = union(body, _P.box([base_w, base_w, base_h],
                                  center=(0.0, 0.0, 0.5 * base_h)))

    if cap_h > 1e-3:
        ech_h = 0.55 * cap_h
        ech = _P.cone(r_bottom=min(rt * 1.02, 0.45 * base_w),
                      r_top=min(rt * 1.40, 0.47 * base_w),
                      height=ech_h,
                      center=(0.0, 0.0, h - cap_h + 0.5 * ech_h),
                      segments=seg)
        ab_w = 0.94 * base_w
        ab_h = 0.55 * cap_h
        abacus = _P.box([ab_w, ab_w, ab_h],
                        center=(0.0, 0.0, h - 0.5 * ab_h))
        body = union(body, union(ech, abacus))

    # The bevel is *part of the wall budget*: ``atta_verify`` evaluates the
    # finish (Blender bevel) before it samples wall thickness, so a bevel
    # wider than the sharpest junction carves a sliver that the min-wall gate
    # sees as a wall.  Measured: a 1.5 mm bevel on the flute-free shaft made a
    # 0.2355 mm "wall" at the torus roll; scaled to the moulding radius it is
    # harmless.  Tie the width to the smallest moulding (the torus tube radius).
    bevel_m = min(5e-4, 0.05 * m_t)
    return body, finish(bevel_width=bevel_m,
                        bevel_segments=_q(quality, 1, 2, 3),
                        bevel_angle_deg=38.0, material="ceramic")
