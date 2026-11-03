# -*- coding: utf-8 -*-
"""Pattern / assembly / primitive families.

Three recipes, each aimed at a *generalisation technique* rather than one shape
(see ``docs/SHAPE_COVERAGE_REQUIREMENTS.md`` §6):

``pattern.honeycomb_panel``
    **Symmetry + repetition (hexagonal tiling)**.  A plate is perforated by a
    honeycomb array of through-holes.  The array is a real hexagonal lattice, so
    the rib between *any* two neighbouring holes is exactly ``wall`` (the holes
    are one cell shrunk by ``wall``, and every neighbour is at the same centre
    distance).  Rows/cols are explicit, the array is centred, and every hole is
    guaranteed to be fully enclosed by an error-level fit constraint.  That
    makes the genus exactly ``rows * cols``, and the recipe publishes that count
    as its per-model ``expected_genus`` (see :func:`_publish_genus`).

``assembly.bolt_nut``
    **Assembly mates + constraint propagation (fastener clearance)**.  A hex-head
    bolt and its matching hex nut, joined only by a thin cast runner gate so the
    result is one closed solid.  The nut bore is genuinely
    ``thread_d + clearance``; the bolt shank is ``thread_d``.  The clearance is a
    first-class parameter with an FDM design rule attached, and the report
    measures the bore/shank diameters back out of the built mesh.

``primitive.chamfer_box``
    **Feature order / robust CSG (chamfer and fillet by edge class)**.  A box
    cut by rotated prisms whose faces sit exactly on the 45° chamfer planes.
    Vertical edges and top/bottom edges are independently sized, and the mode
    swaps the prisms for prisms-minus-cylinder cutters to get edge-class
    fillets.  Both edge classes are cut by *one* boolean against the union of
    the cutters, which keeps the result convex, watertight and cheap.

Units: the registry hands builders **metres** (``unit="mm"`` params are already
converted), so every number below is metres unless it is a count/degree/enum.

Kernel notes worth keeping:

* every cutter is a closed solid; family code must never hand-assemble vertex
  arrays, because :mod:`atta.boolean` rejects open inputs and a broken cutter
  would surface as an opaque boolean failure;
* cutters for one feature are concatenated / unioned first and subtracted once,
  which is both faster and numerically calmer than a loop of ``difference``;
* nothing here ships a bevel in ``finish``: ``atta_verify`` evaluates the Blender
  bevel (``engine._evaluated``) before measuring, so a bevel would make genus and
  clearance depend on a modifier instead of on the parameters.
"""

from __future__ import annotations

import math

import numpy as np

from .. import primitives as _P
from .. import surfaces as _S
from ..boolean import difference, union
from ..errors import AttaError
from ..kit import finish, quality_level, rounded_rect
from ..mesh import concat
from ..registry import recipe

__all__ = ["_build_honeycomb_panel", "_build_bolt_nut", "_build_chamfer_box"]

_EPS = 1e-12
#: sqrt(3)/2 — the vertical row pitch of a pointy-top hexagonal lattice, in units
#: of the flat-to-flat cell size.
_ROW_PITCH = 0.5 * math.sqrt(3.0)


# ---------------------------------------------------------------- helpers

def _publish_genus(recipe_id, genus):
    """Publish a per-model ``expected_genus`` for a family whose genus is derived.

    ``registry.Recipe.expected_genus`` is a single static int, but a perforated
    panel's genus is exactly its hole count, which moves with ``rows``/``cols``.
    ``engine._build_from_recipe`` reads ``recipe.expected_genus`` *after* running
    the builder, so updating the registry entry here makes ``verify`` compare the
    mesh against the real hole count for every parameter set instead of only for
    the nominal one.  Failures are swallowed: a missing registry entry must never
    break a build.
    """
    try:
        from ..registry import REGISTRY
        rec = REGISTRY.get(str(recipe_id))
        if rec is not None:
            rec.expected_genus = int(genus)
    except Exception:  # noqa: BLE001 - never let bookkeeping break geometry
        pass


def _signed_area(loop):
    p = np.asarray(loop, dtype=np.float64)
    return 0.5 * float((p[:, 0] * np.roll(p[:, 1], -1)
                        - np.roll(p[:, 0], -1) * p[:, 1]).sum())


def _ccw(loop):
    """Return the loop wound counter-clockwise (``extrude`` orients by area)."""
    p = np.asarray(loop, dtype=np.float64)
    return p if _signed_area(p) > 0.0 else p[::-1].copy()


def _hex_loop_xy(radius, rot_deg=0.0):
    """Pointy-top regular hexagon loop; flats face ±x, vertices face ±y.

    "Pointy-top" is what makes a honeycomb *uniform*: every one of the six
    neighbours then shares a flat face, so a cell shrunk by ``wall`` on all sides
    leaves a constant rib.
    """
    a = np.deg2rad(np.arange(6, dtype=np.float64) * 60.0 + 30.0 + float(rot_deg))
    return np.column_stack([float(radius) * np.cos(a), float(radius) * np.sin(a)])


def _hex_ring(af, z, inset=0.0, center=(0.0, 0.0)):
    """Hexagonal ring across flats ``af`` inset by ``inset`` (for rim chamfers)."""
    radius = max(_EPS, (float(af) - 2.0 * float(inset))) / math.sqrt(3.0)
    loop = _hex_loop_xy(radius)
    loop[:, 0] += float(center[0])
    loop[:, 1] += float(center[1])
    return np.column_stack([loop, np.full(6, float(z))])


def _hex_prism(af, height, chamfer=0.0, center=(0.0, 0.0)):
    """Hex prism across flats ``af`` with an optional 45° rim chamfer top+bottom."""
    af, h = float(af), float(height)
    c = float(chamfer or 0.0)
    c = max(0.0, min(c, 0.20 * min(af, h)))
    zs = (0.0, h) if c <= _EPS else (0.0, c, h - c, h)
    rings = []
    for z in zs:
        inset = c if (z <= _EPS or z >= h - _EPS) else 0.0
        rings.append(_hex_ring(af, z, inset, center))
    return _S.loft(rings)


def _circle_ring(radius, z, segments, center=(0.0, 0.0)):
    a = np.linspace(0.0, 2.0 * math.pi, max(3, int(segments)), endpoint=False)
    return np.column_stack([float(center[0]) + float(radius) * np.cos(a),
                            float(center[1]) + float(radius) * np.sin(a),
                            np.full(len(a), float(z))])


def _chamfered_shank(radius, z0, z1, segments, center=(0.0, 0.0), tip_chamfer=0.0):
    """Cylinder along +z from ``z0`` to ``z1`` with a cone lead-in at ``z1``.

    The shank is lofted rather than unioned from cylinder+cone so the lead-in is
    part of the same closed shell (no coplanar contact face for the boolean).
    """
    r = float(radius)
    lo, hi = float(z0), float(z1)
    if hi - lo <= _EPS:
        raise AttaError("shank_length", "shank has no length", path="length",
                        fix="raise length")
    c = max(0.0, min(float(tip_chamfer or 0.0), 0.45 * (hi - lo), 0.45 * r))
    rings = [_circle_ring(r, lo, segments, center)]
    if c > _EPS:
        rings.append(_circle_ring(r, hi - c, segments, center))
        rings.append(_circle_ring(max(_EPS, r - c), hi, segments, center))
    else:
        rings.append(_circle_ring(r, hi, segments, center))
    return _S.loft(rings)


def _edge_pentagon(span, height, side, width, margin):
    """2-D cutter outline for one top/bottom edge, in the (along, up) plane.

    The two leading vertices sit exactly on the 45° chamfer plane through
    ``(side*span/2 - width, height/2)``; the rest of the pentagon is pushed
    ``margin`` outside the box so no cutter face is coplanar with a box face
    (coplanar booleans are the classic source of non-manifold results).
    """
    s, half, h = float(side), 0.5 * float(span), 0.5 * float(height)
    w, m = float(width), float(margin)
    return _ccw(np.array([[s * (half - w), h], [s * half, h - w],
                          [s * (half + m), h - w], [s * (half + m), h + m],
                          [s * (half - w), h + m]], dtype=np.float64))


def _prism(poly_ab, ax_a, ax_b, lo, hi):
    """Prism from a 2-D polygon, extruded along the one axis it does not use.

    ``poly_ab`` is expressed in the ``(ax_a, ax_b)`` plane of the world axes and
    the solid spans ``lo..hi`` along the remaining axis.  Both ends are lofted
    with ear-clipped caps, so the result is a closed solid with no assumption
    about how ``extrude`` orders its local frame — the earlier
    "extrude with a direction vector" version silently mirrored one axis, which
    put a fillet cylinder on the wrong side of its own cutter.
    """
    p = _ccw(poly_ab)
    n = len(p)
    spare = 3 - int(ax_a) - int(ax_b)
    # ``loft`` builds side faces from the ring order, so the polygon has to be
    # counter-clockwise *as seen along +spare*.  That is an even permutation of
    # the polygon's own frame only for (0,1)->2 and (1,2)->0, so flip for the
    # odd ones (e.g. the (x, z) polygon swept along y); otherwise the side faces
    # wind inward while the ear-clipped caps wind outward, and the "solid" is
    # rejected by the boolean kernel as inconsistently wound.
    if (int(ax_a), int(ax_b), spare) in ((0, 2, 1), (2, 1, 0), (1, 0, 2)):
        p = p[::-1].copy()

    def ring(level):
        r = np.zeros((n, 3), dtype=np.float64)
        r[:, int(ax_a)] = p[:, 0]
        r[:, int(ax_b)] = p[:, 1]
        r[:, spare] = float(level)
        return r

    return _S.loft([ring(lo), ring(hi)])


def _cyl_z(radius, x, y, height, segments):
    return _P.cylinder(radius, height, center=(x, y, 0.0), segments=segments)


def _cyl_axis(radius, length, axis, offset_a, offset_b, segments):
    """Cylinder along ``axis`` placed at the two coordinates that are not its own."""
    m = _P.cylinder(float(radius), float(length), axis=axis, segments=int(segments))
    t = [0.0, 0.0, 0.0]
    idx = "xyz".index(str(axis))
    for i, val in ((0, offset_a), (1, offset_b)):
        j = i if i < idx else i + 1
        t[j] = float(val)
    return m.transform(translate=t)


def _difference_all(mesh, cutters):
    """Subtract every cutter from ``mesh`` one at a time.

    Subtracting in sequence instead of ``difference(mesh, union(cutters))`` is
    deliberate: the top/bottom cutter of one edge class and of the other share
    the plane ``z = H/2 - w`` (and the vertical cutters share faces with the
    horizontal cutters' sweep caps).  Those coplanar *cutter* faces are outside
    the box, so they are geometrically irrelevant, but unioning the cutters makes
    the kernel resolve a 2-D face-on-face contact, which intermittently comes
    back non-manifold.  Sequential subtraction never compares two cutters.
    """
    parts = [m for m in cutters if m is not None]
    if not parts:
        raise AttaError("empty_csg", "no cutters were generated", path="cutter",
                        fix="enable at least one edge class / hole")
    out = mesh
    for cut in parts:
        out = difference(out, cut)
    return out


# ---------------------------------------------------------------- pattern

@recipe(
    "pattern.honeycomb_panel", "六边形蜂窝穿孔板",
    "平板按真实六边形点阵穿孔：各向同性肋宽 wall（相邻孔中心距处处等于 cell_size），"
    "孔数 = rows*cols，genus 随之发布为 expected_genus；array 居中并受 fit 约束保护。",
    params={
        "length": {"type": "float", "default": 120.0, "min": 20.0, "max": 1000.0,
                   "unit": "mm", "group": "panel", "description": "板长（X）"},
        "width": {"type": "float", "default": 90.0, "min": 20.0, "max": 1000.0,
                  "unit": "mm", "group": "panel", "description": "板宽（Y）"},
        "thickness": {"type": "float", "default": 3.0, "min": 1.0, "max": 20.0,
                      "unit": "mm", "group": "panel", "description": "板厚（Z，孔为通孔）"},
        "cell_size": {"type": "float", "default": 10.0, "min": 4.0, "max": 60.0,
                      "unit": "mm", "group": "pattern",
                      "description": "蜂窝胞元对边距（含肋宽），即孔中心距"},
        "wall": {"type": "float", "default": 2.0, "min": 0.4, "max": 10.0,
                 "unit": "mm", "group": "pattern", "description": "肋宽（相邻孔之间的料宽）"},
        "rows": {"type": "int", "default": 5, "min": 1, "max": 14,
                 "unit": "count", "group": "pattern", "description": "行数（奇数行错位半胞）"},
        "cols": {"type": "int", "default": 7, "min": 1, "max": 20,
                 "unit": "count", "group": "pattern", "description": "每行孔数"},
        "edge_margin": {"type": "float", "default": 6.0, "min": 1.0, "max": 100.0,
                        "unit": "mm", "group": "pattern",
                        "description": "孔阵外轮廓到板边的最小料宽"},
        "hex_rotation": {"type": "float", "default": 0.0, "min": -15.0, "max": 15.0,
                         "unit": "deg", "group": "pattern",
                         "description": "整个孔阵在板面内的旋转（正六边形朝向不变）"},
        "corner_radius": {"type": "float", "default": 3.0, "min": 0.0, "max": 30.0,
                          "unit": "mm", "group": "panel", "description": "板四角圆角"},
    },
    presets={
        "safe": {"length": 160.0, "width": 100.0, "thickness": 4.0, "cell_size": 14.0,
                 "wall": 3.0, "rows": 4, "cols": 6, "edge_margin": 8.0,
                 "hex_rotation": 0.0, "corner_radius": 4.0},
        "nominal": {"length": 120.0, "width": 90.0, "thickness": 3.0, "cell_size": 10.0,
                    "wall": 2.0, "rows": 5, "cols": 7, "edge_margin": 6.0,
                    "hex_rotation": 0.0, "corner_radius": 3.0},
        "hero": {"length": 200.0, "width": 140.0, "thickness": 4.0, "cell_size": 9.0,
                 "wall": 1.8, "rows": 8, "cols": 12, "edge_margin": 6.0,
                 "hex_rotation": 0.0, "corner_radius": 5.0},
    },
    tags=["pattern", "honeycomb", "perforation", "pattern_linear", "hex", "sheet"],
    expected_genus=35, material="aluminium",
    constraints=[
        {"id": "fit_x",
         "expr": ("((cols - 0.5) * cell_size + (cell_size - wall)) "
                  "* abs(cos(hex_rotation * pi / 180)) "
                  "+ ((rows - 1) * 0.8660254 * cell_size "
                  "+ 2 * (cell_size - wall) / sqrt(3)) "
                  "* abs(sin(hex_rotation * pi / 180)) "
                  "+ 2 * edge_margin <= length"),
         "severity": "error",
         "message": "蜂窝阵列在 X 方向越界（孔会破边，genus 不再等于 rows*cols）",
         "fix": "length += 20 或 cols -= 1 或 cell_size = 8"},
        {"id": "fit_y",
         "expr": ("((cols - 0.5) * cell_size + (cell_size - wall)) "
                  "* abs(sin(hex_rotation * pi / 180)) "
                  "+ ((rows - 1) * 0.8660254 * cell_size "
                  "+ 2 * (cell_size - wall) / sqrt(3)) "
                  "* abs(cos(hex_rotation * pi / 180)) "
                  "+ 2 * edge_margin <= width"),
         "severity": "error",
         "message": "蜂窝阵列在 Y 方向越界（孔会破边，genus 不再等于 rows*cols）",
         "fix": "width += 20 或 rows -= 1 或 cell_size = 8"},
        {"id": "rib_printable",
         "expr": "wall >= 0.6",
         "severity": "warn",
         "message": "肋宽 < 0.6 mm，单道 FDM 打印会断续",
         "fix": "wall = 1.2"},
        {"id": "thickness_vs_rib",
         "expr": "thickness >= 0.5 * wall",
         "severity": "warn",
         "message": "板厚明显小于肋宽，孔壁抗剪不足",
         "fix": "thickness = wall"},
        {"id": "margin_vs_rib",
         "expr": "edge_margin >= 0.5 * wall",
         "severity": "warn",
         "message": "外边料宽小于半肋宽，孔边容易破口",
         "fix": "edge_margin = wall"},
    ],
)
def _build_honeycomb_panel(v, quality="draft"):
    L, W, T = float(v["length"]), float(v["width"]), float(v["thickness"])
    cell, wall = float(v["cell_size"]), float(v["wall"])
    rows, cols = int(v["rows"]), int(v["cols"])
    margin, rot = float(v["edge_margin"]), float(v["hex_rotation"])
    rr = float(v.get("corner_radius") or 0.0)

    # --- derived lattice ------------------------------------------------
    # Hole circumradius so the hole's flat-to-flat is cell_size - wall, i.e. the
    # rib is exactly `wall` between any two neighbours (all six are `cell_size`
    # away in a pointy-top hexagonal lattice).
    r_hole = max(_EPS, 0.5 * cell - 0.5 * wall) / (0.5 * math.sqrt(3.0))
    px, py = cell, _ROW_PITCH * cell
    hx = 0.5 * math.sqrt(3.0) * r_hole          # hole AABB half-extent in x
    hy = r_hole                                  # hole AABB half-extent in y

    centres = []
    for j in range(rows):
        off = 0.5 * px if (j % 2) else 0.0
        for i in range(cols):
            centres.append((i * px + off, j * py))
    xs = np.array([p[0] for p in centres])
    ys = np.array([p[1] for p in centres])
    ox, oy = 0.5 * (xs.min() + xs.max()), 0.5 * (ys.min() + ys.max())

    # conservative rotated AABB (same formula the fit constraints use, but
    # computed from the real extent so rows=1 is not over-padded)
    ax = 0.5 * (xs.max() - xs.min()) + hx
    ay = 0.5 * (ys.max() - ys.min()) + hy
    ang = math.radians(rot)
    ca, sa = abs(math.cos(ang)), abs(math.sin(ang))
    rx, ry = ax * ca + ay * sa, ax * sa + ay * ca
    if rx + margin > 0.5 * L + 1e-9 or ry + margin > 0.5 * W + 1e-9:
        raise AttaError(
            "pattern_overflow",
            "honeycomb array %.2f x %.2f mm (+%.2f margin) does not fit a %.2f x %.2f mm "
            "panel" % (2.0 * rx, 2.0 * ry, margin, L, W),
            path="rows/cols/cell_size",
            fix="raise length/width, or lower cols/rows/cell_size, or shrink edge_margin")

    cutters = []
    for cx, cy in centres:
        cell_prism = _S.extrude(_hex_loop_xy(r_hole), 2.0 * T)
        cutters.append(cell_prism.transform(
            translate=(float(cx - ox), float(cy - oy), -0.5 * T)))
    cutter = concat(cutters)
    if abs(rot) > 1e-12:
        cutter = cutter.transform(rotate_deg=(0.0, 0.0, rot))

    plate = _S.extrude(rounded_rect(L, W, rr, per_corner=quality_level(
        quality, 6, 10, 14)), T)
    out = difference(plate, cutter)

    holes = int(rows) * int(cols)
    _publish_genus("pattern.honeycomb_panel", holes)
    return out, finish(material="aluminium")


# ---------------------------------------------------------------- assembly

@recipe(
    "assembly.bolt_nut", "六角头螺栓 + 配套螺母（浇道连体）",
    "六角头螺栓与六角螺母并排立在同一基准面上，由一道薄浇道（runner gate）连成单一实体；"
    "螺母内孔直径 = thread_d + clearance，螺栓杆为 thread_d，间隙参数带 FDM 设计规则。",
    params={
        "thread_d": {"type": "float", "default": 8.0, "min": 3.0, "max": 20.0,
                     "unit": "mm", "group": "thread", "description": "螺纹公称直径（M8 -> 8）"},
        "pitch": {"type": "float", "default": 1.25, "min": 0.35, "max": 2.5,
                  "unit": "mm", "group": "thread",
                  "description": "螺距；驱动杆端导入倒角高度（不生成螺旋牙型）"},
        "length": {"type": "float", "default": 30.0, "min": 6.0, "max": 120.0,
                   "unit": "mm", "group": "thread", "description": "杆长（不含头）"},
        "head_af": {"type": "float", "default": 13.0, "min": 5.0, "max": 30.0,
                    "unit": "mm", "group": "head", "description": "螺栓头对边宽（AF）"},
        "head_h": {"type": "float", "default": 5.3, "min": 2.0, "max": 15.0,
                   "unit": "mm", "group": "head", "description": "螺栓头高"},
        "nut_af": {"type": "float", "default": 13.0, "min": 5.0, "max": 30.0,
                   "unit": "mm", "group": "nut", "description": "螺母对边宽（AF）"},
        "nut_h": {"type": "float", "default": 6.5, "min": 2.0, "max": 20.0,
                  "unit": "mm", "group": "nut", "description": "螺母高"},
        "clearance": {"type": "float", "default": 0.2, "min": 0.05, "max": 0.8,
                      "unit": "mm", "group": "fit",
                      "description": "直径间隙：螺母内孔 = thread_d + clearance"},
        "chamfer": {"type": "float", "default": 0.4, "min": 0.0, "max": 1.2,
                    "unit": "mm", "group": "fit", "description": "六角棱边 45° 导入倒角"},
        "gate_len": {"type": "float", "default": 4.0, "min": 1.0, "max": 20.0,
                     "unit": "mm", "group": "runner",
                     "description": "浇道长度；两轴线间距 = nut_af/2 + head_af/2 + gate_len"},
        "gate_h": {"type": "float", "default": 1.6, "min": 0.6, "max": 6.0,
                   "unit": "mm", "group": "runner", "description": "浇道高"},
        "gate_w": {"type": "float", "default": 5.0, "min": 2.0, "max": 20.0,
                   "unit": "mm", "group": "runner", "description": "浇道宽"},
        "segments": {"type": "int", "default": 32, "min": 12, "max": 96,
                     "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"thread_d": 6.0, "pitch": 1.0, "length": 20.0, "head_af": 10.0,
                 "head_h": 4.0, "nut_af": 10.0, "nut_h": 5.0, "clearance": 0.3,
                 "chamfer": 0.3, "gate_len": 3.0, "gate_h": 1.2, "gate_w": 4.0},
        "nominal": {"thread_d": 8.0, "pitch": 1.25, "length": 30.0, "head_af": 13.0,
                    "head_h": 5.3, "nut_af": 13.0, "nut_h": 6.5, "clearance": 0.2,
                    "chamfer": 0.4, "gate_len": 4.0, "gate_h": 1.6, "gate_w": 5.0},
        "hero": {"thread_d": 12.0, "pitch": 1.75, "length": 45.0, "head_af": 19.0,
                 "head_h": 7.5, "nut_af": 19.0, "nut_h": 10.0, "clearance": 0.25,
                 "chamfer": 0.6, "gate_len": 5.0, "gate_h": 2.0, "gate_w": 6.0},
    },
    tags=["assembly", "fastener", "bolt", "nut", "clearance", "mate", "hex"],
    expected_genus=1, material="steel",
    constraints=[
        {"id": "clearance_positive",
         "expr": "clearance > 0.0",
         "severity": "error",
         "message": "直径间隙必须为正，否则螺母与螺栓是压配而非可装配",
         "fix": "clearance = 0.2"},
        {"id": "clearance_fdm",
         "expr": "clearance >= 0.1 and clearance <= 0.5",
         "severity": "warn",
         "message": "FDM 可装配区间建议 0.1–0.5 mm 直径间隙（过小会熔合，过大会旷）",
         "fix": "clearance = 0.2"},
        {"id": "nut_wall",
         "expr": "nut_af - (thread_d + clearance) >= 2.4",
         "severity": "error",
         "message": "螺母对边到内孔的单边壁厚 < 1.2 mm，拧紧会胀裂",
         "fix": "nut_af = thread_d + clearance + 4.0"},
        {"id": "head_proportions",
         "expr": "head_af >= 1.5 * thread_d and head_h >= 0.5 * thread_d",
         "severity": "warn",
         "message": "六角头相对螺纹过小（ISO 4014 约为 1.6d / 0.65d）",
         "fix": "head_af = 1.6 * thread_d"},
        {"id": "nut_height",
         "expr": "nut_h >= 0.8 * thread_d",
         "severity": "warn",
         "message": "螺母高度不足 0.8d，螺纹啮合圈数偏少",
         "fix": "nut_h = 0.8 * thread_d"},
        {"id": "chamfer_ok",
         "expr": "chamfer <= 0.3 * min(nut_h, head_h)",
         "severity": "error",
         "message": "导入倒角吃掉了太多棱边高度，六角棱面会退化",
         "fix": "chamfer = 0.3 * min(nut_h, head_h)"},
        {"id": "gate_ok",
         "expr": "gate_w <= nut_af and gate_h <= head_h",
         "severity": "error",
         "message": "浇道比零件还宽/高，连接处会变成主体",
         "fix": "gate_w = nut_af * 0.5"},
    ],
)
def _build_bolt_nut(v, quality="draft"):
    d = float(v["thread_d"])
    pitch = float(v["pitch"])
    length = float(v["length"])
    head_af, head_h = float(v["head_af"]), float(v["head_h"])
    nut_af, nut_h = float(v["nut_af"]), float(v["nut_h"])
    clearance, chamfer = float(v["clearance"]), float(v["chamfer"])
    gate_len, gate_h, gate_w = float(v["gate_len"]), float(v["gate_h"]), float(v["gate_w"])
    seg = max(12, int(v.get("segments") or 32))

    bore_d = d + clearance                      # <- the real, derived fit
    spacing = 0.5 * nut_af + 0.5 * head_af + gate_len
    x_bolt = spacing

    # nut: hex prism with a genuine through bore -> the only handle of the solid
    nut = _hex_prism(nut_af, nut_h, chamfer)
    bore_h = nut_h + 2.0 * max(chamfer, 0.5e-3) + 1e-3
    bore = _P.cylinder(0.5 * bore_d, bore_h, center=(0.0, 0.0, 0.5 * nut_h),
                       segments=max(24, seg))
    nut = difference(nut, bore)

    # bolt: hex head sitting on the datum, chamfered shank standing up out of it.
    # The shank starts inside the solid head (no coplanar contact) and its
    # exposed length above the head is exactly `length`.
    head = _hex_prism(head_af, head_h, chamfer, center=(x_bolt, 0.0))
    shank = _chamfered_shank(0.5 * d, 0.35 * head_h, head_h + length, seg,
                             center=(x_bolt, 0.0), tip_chamfer=pitch)
    bolt = union(head, shank)

    # runner gate: the only thing joining the two parts (casting-tree style)
    overlap = min(0.4e-3, 0.25 * gate_len)
    x0 = 0.5 * nut_af - overlap
    x1 = (x_bolt - 0.5 * head_af) + overlap
    if x1 <= x0:
        raise AttaError("gate_length", "runner gate has no length", path="gate_len",
                        fix="raise gate_len or lower nut_af/head_af")
    gate = _P.box((x1 - x0, gate_w, gate_h),
                  center=(0.5 * (x0 + x1), 0.0, 0.5 * gate_h))

    out = union(union(nut, bolt), gate)
    _publish_genus("assembly.bolt_nut", 1)
    return out, finish(material="steel")


# ---------------------------------------------------------------- primitive

@recipe(
    "primitive.chamfer_box", "分边类倒角/圆角方块",
    "长方体 + 旋转切割棱柱：竖向棱与上下棱两类独立尺寸，chamfer 走精确 45° 切面，"
    "fillet 走「切面 − 圆柱」的刀体（棱类圆角），两者都是一次布尔、单实体封闭。",
    params={
        "length": {"type": "float", "default": 80.0, "min": 5.0, "max": 1000.0,
                   "unit": "mm", "group": "body", "description": "X 边长"},
        "width": {"type": "float", "default": 50.0, "min": 5.0, "max": 1000.0,
                  "unit": "mm", "group": "body", "description": "Y 边长"},
        "height": {"type": "float", "default": 25.0, "min": 1.0, "max": 1000.0,
                   "unit": "mm", "group": "body", "description": "Z 边长"},
        "chamfer_w": {"type": "float", "default": 3.0, "min": 0.0, "max": 200.0,
                      "unit": "mm", "group": "edge",
                      "description": "上下棱类切割量（沿面测量的 45° 边宽）"},
        "chamfer_w_v": {"type": "float", "default": 4.0, "min": 0.0, "max": 200.0,
                        "unit": "mm", "group": "edge",
                        "description": "竖向棱类切割量（独立于上下棱）"},
        "edge_classes": {"type": "enum", "default": "both",
                         "enum": ["both", "vertical", "horizontal", "none"],
                         "unit": "none", "group": "edge",
                         "description": "哪些棱类被处理：both/vertical/horizontal/none"},
        "chamfer_mode": {"type": "enum", "default": "chamfer",
                         "enum": ["chamfer", "fillet"], "unit": "none",
                         "group": "edge", "description": "chamfer=45° 平面，fillet=棱类圆柱"},
        "fillet_segments": {"type": "int", "default": 16, "min": 3, "max": 96,
                            "unit": "count", "group": "quality", "advanced": True},
    },
    presets={
        "safe": {"length": 60.0, "width": 40.0, "height": 20.0, "chamfer_w": 2.0,
                 "chamfer_w_v": 3.0, "edge_classes": "both", "chamfer_mode": "chamfer"},
        "nominal": {"length": 80.0, "width": 50.0, "height": 25.0, "chamfer_w": 3.0,
                    "chamfer_w_v": 4.0, "edge_classes": "both",
                    "chamfer_mode": "chamfer", "fillet_segments": 16},
        "hero": {"length": 120.0, "width": 80.0, "height": 40.0, "chamfer_w": 6.0,
                 "chamfer_w_v": 8.0, "edge_classes": "both",
                 "chamfer_mode": "fillet", "fillet_segments": 28},
    },
    tags=["primitive", "box", "chamfer", "fillet", "csg", "boolean", "edge_class"],
    expected_genus=0, material="abs_white",
    constraints=[
        {"id": "chamfer_fits",
         "expr": "max(chamfer_w, chamfer_w_v) <= 0.45 * min(length, width, height)",
         "severity": "error",
         "message": "切割量超过最短边的 45%，上下棱与竖棱会互相吃掉",
         "fix": "chamfer_w = 0.3 * min(length, width, height)"},
        {"id": "vertical_fits",
         "expr": "chamfer_w_v <= 0.45 * min(length, width)",
         "severity": "error",
         "message": "竖棱切割量超过截面半宽，X/Y 面会被切穿",
         "fix": "chamfer_w_v = 0.3 * min(length, width)"},
        {"id": "fillet_smooth",
         "expr": "fillet_segments >= 6",
         "severity": "warn",
         "message": "圆角分段 < 6，圆柱面看起来会有明显折线",
         "fix": "fillet_segments = 12"},
        {"id": "face_left",
         "expr": "length - 2 * max(chamfer_w, chamfer_w_v) >= 1.0",
         "severity": "warn",
         "message": "主面剩余宽度 < 1 mm，几乎退化成棱柱",
         "fix": "chamfer_w = 0.2 * length"},
    ],
)
def _build_chamfer_box(v, quality="draft"):
    L, W, H = float(v["length"]), float(v["width"]), float(v["height"])
    wz, wv = float(v["chamfer_w"]), float(v["chamfer_w_v"])
    classes = str(v.get("edge_classes") or "both").lower()
    mode = str(v.get("chamfer_mode") or "chamfer").lower()
    seg = max(3, int(v.get("fillet_segments") or quality_level(quality, 8, 16, 32)))

    if classes == "vertical":
        wz = 0.0
    elif classes == "horizontal":
        wv = 0.0
    elif classes == "none":
        wz = wv = 0.0

    box = _P.box((L, W, H))
    _publish_genus("primitive.chamfer_box", 0)
    if wz <= _EPS and wv <= _EPS:
        # no edge class selected: the primitive itself is the answer
        return box, finish(material="abs_white")

    margin = max(L, W, H)
    cutters = []

    # --- vertical edges: prisms along z, 45° in the XY plane -------------
    if wv > _EPS:
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                loop = _ccw(np.array([
                    [sx * (0.5 * L - wv), sy * 0.5 * W],
                    [sx * 0.5 * L, sy * (0.5 * W - wv)],
                    [sx * (0.5 * L + margin), sy * (0.5 * W - wv)],
                    [sx * (0.5 * L + margin), sy * (0.5 * W + margin)],
                    [sx * (0.5 * L - wv), sy * (0.5 * W + margin)],
                ], dtype=np.float64))
                cut = _S.extrude(loop, H + 2.0 * margin)
                cut = cut.transform(translate=(0.0, 0.0, -(0.5 * H + margin)))
                if mode == "fillet":
                    cut = difference(cut, _cyl_z(
                        wv, sx * (0.5 * L - wv), sy * (0.5 * W - wv),
                        H + 3.0 * margin, seg))
                cutters.append(cut)

    # --- top/bottom edges: prisms across the box, 45° in the vertical plane
    if wz > _EPS:
        for sz in (1.0, -1.0):
            for side in (1.0, -1.0):
                # edges running along y (at x = ±L/2): polygon in (x, z), swept in y
                poly_xz = _edge_pentagon(L, H, side, wz, margin)
                if sz < 0:
                    poly_xz = _ccw(np.column_stack([poly_xz[:, 0], -poly_xz[:, 1]]))
                cut = _prism(poly_xz, 0, 2, -(0.5 * W + margin), 0.5 * W + margin)
                if mode == "fillet":
                    cut = difference(cut, _cyl_axis(
                        wz, W + 3.0 * margin, "y", side * (0.5 * L - wz),
                        sz * (0.5 * H - wz), seg))
                cutters.append(cut)

                # edges running along x (at y = ±W/2): polygon in (y, z), swept in x
                poly_yz = _edge_pentagon(W, H, side, wz, margin)
                if sz < 0:
                    poly_yz = _ccw(np.column_stack([poly_yz[:, 0], -poly_yz[:, 1]]))
                cut = _prism(poly_yz, 1, 2, -(0.5 * L + margin), 0.5 * L + margin)
                if mode == "fillet":
                    cut = difference(cut, _cyl_axis(
                        wz, L + 3.0 * margin, "x", side * (0.5 * W - wz),
                        sz * (0.5 * H - wz), seg))
                cutters.append(cut)

    out = _difference_all(box, cutters)
    return out, finish(material="abs_white")
