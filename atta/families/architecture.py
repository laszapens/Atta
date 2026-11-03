# -*- coding: utf-8 -*-
"""Architecture families — recursive growth, no new feature op.

``architecture.tower.growth``
    **Shape grammar / recursion (§6.10)** expressed the way Atta can actually
    do it: a *production rule in the builder*, not an op in the feature graph.

    The claim this family exists to prove is narrow and testable: a growth
    grammar needs a recursive **host language**, not a recursive feature DAG.
    ``registry.Recipe.build`` is a plain Python callable returning a mesh, so a
    production rule with a bounded recursion depth runs today with no kernel
    change and no Spec v3 change.  ``levels`` is genuinely an exponent, so the
    budget is enforced as a *constraint* before any geometry is built
    (``branching ** levels <= max_cells`` and a body-count cap), which is the
    boundary condition a capability like this has to declare.

    Two productions ship, chosen because each has a *provable* topology law:

    ``quadtree``
        Children tile the parent footprint on a ``sqrt(branching) x
        sqrt(branching)`` grid, one storey higher, each scaled by ``scale`` and
        twisted by ``twist_deg`` relative to its parent.  Because every child
        sits *inside* its parent's footprint and overlaps it, and because the
        build is monotone in ``z``, the result is void-free: **genus = 0 at
        every depth** (measured 0 at levels 1-6).
    ``frame``
        Each storey is four corner columns closed by four ties at the top, so
        every storey adds one ring of material around an open void.  The rings
        are chained into a tube, which gives exactly one handle per storey plus
        the corner-column cycles: **genus = 4*levels - 3** (measured
        1, 5, 9, 13, 17, 21, 25, 29 at levels 1-8).  This is the rule that would need a
        per-model ``expected_genus``; the builder publishes the derived value
        through :func:`_publish_genus`, the same convention
        ``pattern.honeycomb_panel`` uses (its genus is ``rows*cols``).

    Both laws are *analytical claims from the construction*, and the report
    prints the measured genus next to the claimed one — the verify gate compares
    them, so a wrong claim fails the build instead of shipping silently.

Corpus evidence (tier A, `alexadam/context-free-art`, Apache-2.0):
``cfdg_tower_growth_recursion`` (e10.cfdg: ``stack_step``, ``taper``,
``branch_probability``, ``levels``, ``slab_aspect``),
``cfdg_tower_growth_forked`` (e11.cfdg: ``fork_probability``, ``fork_scale``,
``fork_angle``, ``twist_probability``) and ``cfdg_tower_growth_nested``
(e12.cfdg: ``trunk_taper``, ``filament_step``, ``switch_probability``,
``slow_twist``).  Those sources are *stochastic* (``branch_probability`` 0.05
with ``levels`` up to 500); Atta needs reproducible geometry, so the
probabilities become deterministic counts here (``branching``) and ``levels``
becomes a budgeted exponent instead of a 500-deep random walk.  That
translation is the interesting part of the family, and the report says what it
costs.

Kernel notes:

* Every body is a closed primitive; the recursion only *places* bodies, and a
  single divide-and-conquer union collapses them into one shell.  Bodies always
  overlap their parent by a real volume (never share a face exactly), because a
  coplanar union is the one thing :mod:`atta.boolean` refuses.
* The union is done as a binary tree rather than left-to-right: merging pairs
  keeps the operand sizes balanced, which is what makes 60+ bodies affordable
  inside the 5 s draft budget.
* ``member`` (strut/plate thickness) is clamped to a floor because the deepest
  storey is what a printer actually has to resolve, not the widest one.
"""

from __future__ import annotations

import math

from .. import primitives as _P
from ..boolean import union
from ..errors import AttaError
from ..kit import finish, quality_level
from ..registry import recipe

__all__ = ["build_tower_growth", "genus_of", "production_bodies"]

_q = quality_level

#: FDM process floor for a printed strut, in metres.
_MEMBER_FLOOR = 1.2e-3


def _publish_genus(recipe_id, genus):
    """Publish a per-model ``expected_genus`` for a derived genus.

    ``registry.Recipe.expected_genus`` is one static int, but a growth grammar's
    genus is a function of ``rule`` and ``levels``.  ``engine`` reads
    ``recipe.expected_genus`` *after* the builder runs, so updating the registry
    entry here makes ``verify`` compare against the real value for every
    parameter set instead of only the nominal one.  (Same convention as
    ``pattern.honeycomb_panel``.)  Never let bookkeeping break geometry.
    """
    try:
        from ..registry import REGISTRY
        rec = REGISTRY.get(str(recipe_id))
        if rec is not None:
            rec.expected_genus = int(genus)
    except Exception:  # noqa: BLE001
        pass


def genus_of(rule, levels):
    """The topological claim of each production, as a function of depth.

    ``quadtree`` never encloses a void (children live inside the parent
    footprint and the build is monotone in z), so it is 0 at every depth.
    ``frame`` chains one ring of material per storey; each storey contributes
    four corner-column cycles plus its tie ring, and the chain shares one ring,
    giving ``4*levels - 3``.
    """
    levels = int(levels)
    if str(rule) == "frame":
        return max(1, 4 * levels - 3)
    return 0


def production_bodies(rule, levels, branching):
    """How many primitives a parameter set will union (the cost driver)."""
    levels, branching = int(levels), int(branching)
    if str(rule) == "frame":
        return 8 * levels
    if branching < 2:
        return levels
    return (branching ** levels - 1) // (branching - 1)


def _place(size_xyz, center, rot_z_deg=0.0, twist_deg=0.0):
    """Box at ``center`` rotated about z, optionally pre-rotated by a tower twist.

    ``mesh.transform`` applies X then Y then Z (``mz @ my @ mx``), so a pure z
    rotation is the world twist and ``rot_z_deg`` is the body's own heading.
    """
    sx, sy, sz = (float(v) for v in size_xyz)
    if sx <= 0.0 or sy <= 0.0 or sz <= 0.0:
        raise AttaError("bad_geometry", "growth produced a non-positive body size",
                        path="member", fix="raise member or reduce levels")
    m = _P.box([sx, sy, sz], center=(0.0, 0.0, 0.0))
    rz = float(rot_z_deg) + float(twist_deg)
    if abs(rz) > 1e-9:
        m = m.transform(rotate_deg=(0.0, 0.0, rz))
    return m.transform(translate=(float(center[0]), float(center[1]),
                                  float(center[2])))


def _ring_offsets(branching, radius):
    """Child centres on a ring — used when ``branching`` is not a square grid."""
    out = []
    for k in range(branching):
        a = 2.0 * math.pi * k / branching
        out.append((radius * math.cos(a), radius * math.sin(a)))
    return out


def _grid_offsets(branching, extent):
    """Child centres on a ``sqrt(branching)`` grid, or ``None`` if not square.

    A quadtree is only a quadtree when the branching factor is a perfect square;
    for 2/3/5/6 children a ring is the honest fallback and the docstring says so.
    """
    n = int(round(math.sqrt(branching)))
    if n * n != branching or n < 2:
        return None
    span = float(extent)
    half = (n - 1) / 2.0
    return [((i - half) * span / max(1, n), (j - half) * span / max(1, n))
            for i in range(n) for j in range(n)]


def _unify(meshes):
    """Collapse bodies into one shell with a balanced (binary-tree) union."""
    if not meshes:
        raise AttaError("bad_geometry", "growth produced no bodies", path="levels",
                        fix="use levels >= 1")
    cur = list(meshes)
    while len(cur) > 1:
        nxt = [union(cur[i], cur[i + 1]) for i in range(0, len(cur) - 1, 2)]
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    return cur[0]


# ------------------------------------------------------------- productions

def _grow_quadtree(levels, branching, base_w, height, member, scale, taper,
                   twist_deg, overlap):
    """Footprint-subdivision tower: children tile the parent body, one storey up.

    Two shrink ratios, both live: ``taper`` decays the storey footprint and
    ``scale`` sets the body inside its footprint.  A child's footprint is
    ``base_w*(scale*taper)**level`` (level 0 is exactly ``base_w``) and it is laid
    on a ``sqrt(branching)`` grid
    *inside* the parent body, so a child is always a real volume overlap of its
    parent and the union can never fall apart.
    """
    bodies = []
    storey_h = height / levels
    body_h = storey_h * (1.0 + overlap)

    def rec(level, body_w, cx, cy, z0, heading):
        bodies.append(_place((body_w, body_w, body_h),
                             (cx, cy, z0 - 0.5 * overlap * storey_h),
                             rot_z_deg=heading, twist_deg=twist_deg * level))
        if level + 1 >= levels:
            return
        child_w = max(member, body_w * scale * taper)
        child_w = min(child_w, body_w * 0.98)     # never wider than its parent
        spread = body_w - child_w                 # centres stay inside parent
        grid = _grid_offsets(branching, spread)
        if grid is None:
            grid = _ring_offsets(branching, 0.5 * spread)
        for dx, dy in grid:
            rec(level + 1, child_w, cx + dx, cy + dy, z0 + storey_h,
                heading + twist_deg)

    rec(0, base_w, 0.0, 0.0, 0.0, 0.0)
    return bodies


def _grow_frame(levels, base_w, height, member, taper, twist_deg, overlap):
    """Ring-per-storey frame: four columns closed by four ties, chained in z."""
    bodies = []
    storey_h = height / levels
    tie_t = member * 0.90                        # differs from member -> no coplanar faces
    col_h = storey_h + member * 0.20              # overshoot into the storey above

    for i in range(levels):
        w = base_w * (taper ** i)
        if w <= 2.0 * member:
            raise AttaError(
                "bad_geometry",
                "storey %d narrowed to %.2f mm, below 2x member (%.2f mm)"
                % (i, w * 1e3, 2.0 * member * 1e3),
                path="taper",
                fix="raise base_w or taper, or reduce levels")
        z0 = i * storey_h
        c = 0.5 * w - 0.5 * member                # corner-column centre
        tw = twist_deg * i
        ca, sa = math.cos(math.radians(tw)), math.sin(math.radians(tw))

        def rot(px, py):
            return (px * ca - py * sa, px * sa + py * ca)

        # four corner columns spanning the storey (and into the next tie ring)
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = rot(sx * c, sy * c)
            bodies.append(_place((member, member, col_h),
                                 (x, y, z0 + 0.5 * storey_h), rot_z_deg=tw))
        # four ties at the top of the storey: length ends at the column centres
        tie_len = w - member
        for dx, dy, heading in ((0, -1, 0.0), (0, 1, 0.0),
                                (1, 0, 90.0), (-1, 0, 90.0)):
            x, y = rot(dx * c, dy * c)
            bodies.append(_place((tie_len, tie_t, tie_t),
                                 (x, y, z0 + storey_h), rot_z_deg=heading + tw))
    return bodies


# ------------------------------------------------------------------ recipe

@recipe(
    "architecture.tower.growth", "递归生长塔（形状语法）",
    "形状语法 / 递归生产式：builder 里的递归生成，不是 feature 图里的 op。"
    "两条生产式各有一个可证明的拓扑律：quadtree 子体铺满父体足迹并逐层升高，"
    "单调于 z 且不围合空腔 → genus 恒为 0；frame 每层四根角柱 + 四根顶梁围成一环，"
    "环串联成管 → genus = 4*levels−3（实测 levels=1..5 → 1,5,9,13,17）。"
    "levels 是指数，故先以约束封顶：branching**levels ≤ max_cells 且体数 ≤ max_bodies，"
    "超限直接拒绝而不是把 kernel 拖死。语料为 CFDG 随机文法（e10/e11/e12.cfdg），"
    "此处把概率换成确定的分支数，把 levels 从 500 深随机游走换成受预算约束的指数。",
    params={
        "levels": {"type": "int", "default": 3, "min": 1, "max": 8,
                   "unit": "count", "group": "growth",
                   "description": "递归深度（指数）：层数 = levels"},
        "branching": {"type": "int", "default": 4, "min": 2, "max": 6,
                      "unit": "count", "group": "growth",
                      "description": "每个父体的子体数（4 = 2×2 网格细分）"},
        "rule": {"type": "enum", "default": "quadtree",
                 "enum": ["quadtree", "frame"], "unit": "none", "group": "growth",
                 "description": "生产式：quadtree(genus 0) / frame(genus 4L−3)"},
        "height": {"type": "float", "default": 180.0, "min": 20.0, "max": 1200.0,
                   "unit": "mm", "group": "tower", "description": "总高"},
        "base_w": {"type": "float", "default": 60.0, "min": 10.0, "max": 400.0,
                   "unit": "mm", "group": "tower", "description": "底层足迹宽度"},
        "member": {"type": "float", "default": 6.0, "min": 1.2, "max": 40.0,
                   "unit": "mm", "group": "tower",
                   "description": "杆件/板厚（FDM 下限 1.2 mm）"},
        "scale": {"type": "float", "default": 0.5, "min": 0.3, "max": 0.95,
                  "unit": "ratio", "group": "growth",
                  "description": "子体相对父体的尺寸比"},
        "taper": {"type": "float", "default": 0.94, "min": 0.5, "max": 1.05,
                  "unit": "ratio", "group": "tower",
                  "description": "逐层足迹收缩（frame 的柱位收缩）"},
        "twist_deg": {"type": "float", "default": 0.0, "min": -30.0, "max": 30.0,
                      "unit": "deg", "group": "growth",
                      "description": "逐层扭转（CFDG slow_twist）"},
        "max_cells": {"type": "int", "default": 512, "min": 4, "max": 8192,
                      "unit": "count", "group": "budget",
                      "description": "深度预算：允许的最大叶体数 branching**levels"},
        "max_bodies": {"type": "int", "default": 96, "min": 4, "max": 1024,
                       "unit": "count", "group": "budget",
                       "description": "体数预算（union 成本驱动）"},
    },
    presets={
        "safe": {"levels": 2, "branching": 3, "rule": "quadtree", "height": 140.0,
                 "base_w": 70.0, "member": 8.0, "scale": 0.55, "taper": 0.95,
                 "twist_deg": 0.0, "max_cells": 512, "max_bodies": 96},
        "nominal": {"levels": 3, "branching": 4, "rule": "quadtree",
                    "height": 180.0, "base_w": 60.0, "member": 6.0,
                    "scale": 0.5, "taper": 0.94, "twist_deg": 0.0,
                    "max_cells": 512, "max_bodies": 96},
        "hero": {"levels": 4, "branching": 3, "rule": "frame", "height": 240.0,
                 "base_w": 80.0, "member": 5.0, "scale": 0.6, "taper": 0.96,
                 "twist_deg": 6.0, "max_cells": 512, "max_bodies": 96},
    },
    tags=["architecture", "grammar", "recursion", "growth", "boolean"],
    expected_genus=0, material="powder_blue",
    constraints=[
        {"id": "depth_budget", "severity": "error",
         "expr": "branching ** levels <= max_cells",
         "fix": "levels = 3, branching = 4  (or raise max_cells deliberately)",
         "message": "levels 是指数：branching**levels 不得超过 max_cells，"
                    "否则体数爆炸、boolean 成本不可控"},
        {"id": "body_budget", "severity": "error",
         "expr": "(branching ** levels - 1) / (branching - 1) <= max_bodies",
         "fix": "levels = 3  (or raise max_bodies deliberately)",
         "message": "union 成本由体数决定：等比级数体数不得超过 max_bodies"},
        {"id": "member_floor", "severity": "error",
         "expr": "member >= 1.2",
         "fix": "member = 1.6",
         "message": "杆件厚度低于 1.2 mm 时 FDM 打不出连续实体（最小特征尺寸）"},
        {"id": "member_fits_base", "severity": "error",
         "expr": "member <= base_w * 0.25",
         "fix": "member = base_w * 0.12",
         "message": "杆件厚度不得超过底层足迹的 1/4，否则塔退化为实心柱"},
        {"id": "top_not_collapsed", "severity": "error",
         "expr": "base_w * (scale * taper) ** (levels - 1) >= member * 1.6",
         "fix": "taper = 0.97, scale = 0.6  (or reduce levels)",
         "message": "最深一层的足迹必须仍大于 1.6 倍杆件厚度，"
                    "否则子体相互吞并、拓扑律失效"},
        {"id": "storey_inset_ok", "severity": "error",
         "expr": "base_w * (1 - taper) <= 1.8 * member",
         "fix": "taper = 0.97  (每层内缩不得超过杆厚，否则层与层断开)",
         "message": "逐层内缩量 base_w*(1−taper) 不得超过 1.8 倍杆厚："
                    "frame 的上一层次落不进下一层顶梁，塔会散成多层"},
        {"id": "twist_corner_aligned", "severity": "error",
         "expr": "twist_deg * pi / 180 * base_w / 2 <= 1.2 * member",
         "fix": "twist_deg = 0  (or raise member / lower base_w)",
         "message": "逐层扭转把上一层角柱沿环切向挪开 base_w/2*θ；一旦超过杆厚，"
                    "上一层角柱就落不到下一层顶梁上，塔散成 levels 段（实测 8° 即断开）"},
        {"id": "storey_tall_enough", "severity": "warn",
         "expr": "height >= levels * member * 3",
         "fix": "height = levels * member * 5",
         "message": "每层高度不足 3 倍杆件厚度时，层与层无法区分（退化为叠饼）"},
    ],
)
def build_tower_growth(v, quality="draft"):
    """Recursive production in the builder; returns one watertight shell."""
    rule = str(v.get("rule") or "quadtree")
    levels = int(v["levels"])
    branching = int(v["branching"])
    height = float(v["height"])
    base_w = float(v["base_w"])
    member = float(v["member"])
    scale = float(v["scale"])
    taper = float(v["taper"])
    twist = float(v["twist_deg"])

    # Belt-and-braces: the constraints above are the contract, this is the
    # refusal of last resort for anything built outside the registry.
    cells = branching ** levels
    bodies = production_bodies(rule, levels, branching)
    max_cells = int(v.get("max_cells", 512))
    max_bodies = int(v.get("max_bodies", 96))
    if cells > max_cells or bodies > max_bodies:
        raise AttaError(
            "budget_exceeded",
            "growth rule %r at levels=%d, branching=%d needs %d leaf cells / %d "
            "bodies, over the budget (%d / %d)"
            % (rule, levels, branching, cells, bodies, max_cells, max_bodies),
            path="levels",
            fix="reduce levels, reduce branching, or raise max_cells/max_bodies "
                "deliberately (cost is ~linear in bodies)")
    if member < _MEMBER_FLOOR:
        raise AttaError("bad_member", "member %.3f mm is below the 1.2 mm floor"
                        % (member * 1e3), path="member", fix="member = 1.6")

    overlap = _q(quality, 0.22, 0.28, 0.34)
    if rule == "frame":
        meshes = _grow_frame(levels, base_w, height, member, taper, twist, overlap)
    else:
        meshes = _grow_quadtree(levels, branching, base_w, height, member, scale,
                                taper, twist, overlap)

    out = _unify(meshes)
    # sit on the build plate and centre in x/y
    lo = out.verts.min(axis=0)
    hi = out.verts.max(axis=0)
    out = out.transform(translate=(-0.5 * (lo[0] + hi[0]),
                                   -0.5 * (lo[1] + hi[1]), -float(lo[2])))
    _publish_genus("architecture.tower.growth", genus_of(rule, levels))
    return out, finish(material="powder_blue", smooth_angle_deg=32.0)
