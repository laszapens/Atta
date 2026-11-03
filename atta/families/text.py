# -*- coding: utf-8 -*-
"""Text family — single-stroke engraving and embossing.

Why a stroke font
-----------------
Atta has no font files and no outline-font pipeline: a stroke font turns text
into polylines on the *centre line* of each mark, and a caller-chosen stroke
width sweeps them into solids.  That is exactly the primitive CSG wants — no
Bézier flattening, no polygon-with-holes offsetting, no font licence.  Glyph
data lives in :mod:`atta.families.stroke_font` (Hershey Simplex Roman, public
domain, embedded and cited there); this module owns the geometry, the new
feature op and the plate recipe.

The op: ``text_engrave``
------------------------
Registered through :func:`atta.features.register_op`, so the feature DAG can
use it next to the built-ins::

    {"id": "label", "op": "text_engrave",
     "args": {"text": "ATTA", "height": 20, "depth": 1.2, "stroke_width": 1.6,
              "at": [0, 0], "angle_deg": 0, "mode": "engrave",
              "align": "center"}}

``height`` (cap height), ``depth`` and ``stroke_width`` are lengths in graph
units (mm by default); the core scales them to metres because the op declares
``lengths``.  ``at`` is a scaled 2-vector.  ``text``/``mode``/``align`` are
passed through as strings and validated here.

The returned mesh is the text as **solid strokes** with ``z = 0`` at the
surface it is meant for:

``mode="engrave"``
    the cutter, spanning ``[-depth, +0.5*depth]``.  Subtract it from a body
    whose top face sits at ``z = 0``.
``mode="emboss"``
    the raised body, spanning ``[-0.35*depth, +depth]``.  Union it onto a body
    whose top face sits at ``z = 0``; sinking it below the surface guarantees
    volumetric overlap, so the union can never leave a floating shell.

Every segment of a stroke becomes a box and every vertex a short cylinder, so
bends and stroke ends are round; the parts are unioned with the Manifold
kernel, which keeps the result a closed manifold.  A glyph may be a *ring*
(e.g. ``A`` with its crossbar), so the returned mesh can legitimately hold one
closed shell per glyph and is not necessarily a single component — the boolean
that consumes it is what merges everything.

What this is *not* — the honest limits of a stroke font
------------------------------------------------------
A stroke font is a **centre-line extrusion**, not an outline font: no type, no
outline table, no kerning pairs (advances are per glyph and uniform), no
hinting, no unicode — 95 printable ASCII characters, and anything else falls
back to ``?`` (reported by ``stroke_font.missing`` and in the recipe's mesh
meta).  Consequence for closed shapes: there are no glyph *counters*, so an
engraved ``O`` is a groove with a raised island inside rather than a
ring-shaped cut-out, and an engraved ``A`` (two legs meeting at the apex, plus
a separate crossbar) leaves a triangular island.  That is also why a Z-section
through the groove does not show one contour per letter: measured here, ``A``
contributes 2 contours, ``T`` 1 (bar and stem merge into one simply-connected
band), ``O`` 2 (outer + island) and ``8`` 3 — so ``ATTA`` gives 1 plate contour
+ 6 glyph contours = 7 loops.  That loop count is the rigorous proof that the
letters exist as geometry; the ASCII section is the readable one.

The recipe: ``text.engraved_plate``
-----------------------------------
A nameplate (optionally rounded, optionally with one through mounting hole)
carrying engraved or embossed text on its top face, in one or two lines.
Watertightness is the interesting part: an engraved groove must not reach the
bottom face, so the recipe requires ``depth < 0.6 * thickness``.  The cut is
then a trench open at the top — the enclosed islands inside ``O``/``A``/``8``
stay attached to the plate, and the plate keeps genus 0.  Embossed text is
unioned, never glued: the body is sunk into the plate by ``0.35 * depth``, so
it is one connected solid.
"""

from __future__ import annotations

import math

import numpy as np

from .. import features as _F
from .. import primitives as _P
from ..boolean import boolean, difference, union
from ..errors import AttaError
from ..kit import finish, quality_level, rounded_rect
from ..mesh import Mesh, oriented, topology, weld
from ..registry import recipe
from ..surfaces import extrude
from . import stroke_font as _SF

__all__ = ["TEXT_OP", "EMBOSS_OP", "text_engrave", "text_emboss", "text_mesh",
           "stroke_prisms", "ENGRAVE_MODES", "ALIGNMENTS"]

ENGRAVE_MODES = ("engrave", "emboss")
ALIGNMENTS = ("left", "center", "centre", "right")

#: Op names as seen by the feature DAG.  ``text_engrave`` and ``text_emboss``
#: are both in the normalised op vocabulary (requirements §3.2,
#: ``docs/coverage_matrix.json`` "must_have_features"), so both names are
#: registered; ``text_emboss`` is the same builder with ``mode="emboss"``.
TEXT_OP = "text_engrave"
EMBOSS_OP = "text_emboss"

#: How far an emboss body sinks below the surface, as a fraction of depth.
_EMBOSS_SINK = 0.35


# ------------------------------------------------------------------ helpers

def _segments(quality) -> int:
    """Joint/cylinder tessellation for a quality level."""
    return quality_level(quality, 8, 12, 20)


def _plain(value):
    return np.asarray(value, dtype=np.float64).reshape(-1)


def _finite(name, value, *, path="args"):
    x = float(value)
    if not math.isfinite(x):
        raise AttaError("bad_arg", "%s must be finite" % name, path=path + "." + name,
                        fix="pass a finite number of metres")
    return x


def _positive(name, value, *, path="args"):
    x = _finite(name, value, path=path)
    if x <= 0.0:
        raise AttaError("bad_arg", "%s must be > 0" % name, path=path + "." + name,
                        fix="pass a positive length in metres (recipe params are mm)")
    return x


def _union_all(parts: list) -> Mesh:
    """Union closed solids in one pass.

    The fast path is Manifold's ``batch_boolean`` (a single tree-reduction over
    every part); the fallback is a pairwise reduction through
    :func:`atta.boolean.boolean` so the module still works if that API is ever
    unavailable.  The result is required to be closed: a broken cutter here
    would silently poison the plate boolean later.
    """
    if not parts:
        raise AttaError("bad_arg", "text produced no solids", path="text",
                        fix="pass text with at least one drawable character")
    if len(parts) == 1:
        return parts[0]
    out = None
    try:
        import manifold3d

        mans = [manifold3d.Manifold(manifold3d.Mesh(
            vert_properties=np.asarray(m.verts, dtype=np.float32),
            tri_verts=np.asarray(m.faces, dtype=np.uint32))) for m in parts]
        merged = manifold3d.Manifold.batch_boolean(mans, manifold3d.OpType.Add)
        raw = merged.to_mesh()
        out = oriented(weld(Mesh(np.asarray(raw.vert_properties, dtype=np.float64)[:, :3],
                                 np.asarray(raw.tri_verts, dtype=np.int64),
                                 {"kind": "text_union"}), 1e-9))
    except Exception:  # noqa: BLE001 - fall back to the pairwise path
        out = None
    if out is None or not topology(out)["closed"]:
        acc = parts[0]
        for other in parts[1:]:
            acc = boolean(acc, other, "union")
        out = acc
    topo = topology(out)
    if not topo["closed"] or topo["non_manifold_edges"]:
        raise AttaError("text_not_closed", "the swept text strokes did not union into "
                        "a closed solid", path="text",
                        fix="raise stroke_width relative to cap_height, or use fewer "
                            "than ~40 characters per call")
    return out


def _z_range(mode, depth):
    """Vertical extent of the text solid, with z = 0 at the target surface."""
    if mode == "engrave":
        return (-float(depth), 0.5 * float(depth))
    return (-_EMBOSS_SINK * float(depth), float(depth))


def stroke_prisms(strokes, stroke_width, z0, z1, segments=12):
    """Sweep stroke polylines into one closed solid of ``stroke_width``.

    Each segment is a box rotated into place and each vertex a cylinder, so
    joins and stroke ends are round.  Segments overshoot the vertex by
    ``0.18 * stroke_width`` so every part overlaps its neighbours *in volume* —
    exactly-touching prisms would union into separate shells instead.
    """
    w = float(stroke_width)
    h = float(z1) - float(z0)
    if h <= 0.0:
        raise AttaError("bad_arg", "text solid height must be > 0", path="depth",
                        fix="pass a positive depth")
    zc = 0.5 * (float(z0) + float(z1))
    over = 0.18 * w
    radius = 0.5 * w
    parts = []
    for stroke in strokes:
        pts = np.asarray(stroke, dtype=np.float64).reshape(-1, 2)
        if len(pts) < 2:
            continue
        for a, b in zip(pts, pts[1:]):
            d = b - a
            length = float(math.hypot(d[0], d[1]))
            if length <= 1e-12:
                continue
            ang = math.degrees(math.atan2(d[1], d[0]))
            mid = 0.5 * (a + b)
            parts.append(_P.box([length + 2.0 * over, w, h]).transform(
                rotate_deg=(0.0, 0.0, ang),
                translate=(float(mid[0]), float(mid[1]), zc)))
        for p in pts:
            parts.append(_P.cylinder(radius, h,
                                     center=[float(p[0]), float(p[1]), zc],
                                     segments=max(6, int(segments))))
    if not parts:                      # every stroke was a single point
        raise AttaError("bad_arg", "text has no drawable segments", path="text",
                        fix="pass characters with real strokes, not only spaces")
    return _union_all(parts)


def text_mesh(rows, cap_height, stroke_width, depth, *, mode="engrave",
              at=(0.0, 0.0), angle_deg=0.0, align="left", tracking=0.0,
              quality="draft", path="args", op_name=TEXT_OP):
    """Build the text solid for one or more baseline rows.

    ``rows`` is a sequence of ``(text, dy)`` pairs where ``dy`` shifts a row in
    y (metres) — that is how the plate stacks two lines.  All rows share the
    cap height, stroke width, mode and alignment.
    """
    cap = _positive("height", cap_height, path=path)
    w = _positive("stroke_width", stroke_width, path=path)
    d = _positive("depth", depth, path=path)
    mode = str(mode or "engrave").strip().lower()
    if mode not in ENGRAVE_MODES:
        raise AttaError("bad_arg", "mode must be 'engrave' or 'emboss'",
                        path=path + ".mode", fix="use mode='engrave'")
    a = str(align or "left").strip().lower()
    if a not in ALIGNMENTS:
        raise AttaError("bad_arg", "align must be 'left', 'center' or 'right'",
                        path=path + ".align", fix="use align='center'")
    ang = _finite("angle_deg", angle_deg, path=path)
    track = _finite("tracking", tracking, path=path)
    centre = _plain(at)
    if centre.size != 2:
        raise AttaError("bad_arg", "at must be two numbers (x, y)", path=path + ".at",
                        fix="pass at=[x, y] in the graph unit")

    strokes = []
    for text, dy in rows:
        laid_out, _ink = _SF.layout_strokes(str(text), cap_height=cap,
                                           stroke_width=w, tracking=track,
                                           align=a, baseline=float(dy))
        strokes.extend(laid_out)
    z0, z1 = _z_range(mode, d)
    solid = stroke_prisms(strokes, w, z0, z1, segments=_segments(quality))
    solid = solid.transform(rotate_deg=(0.0, 0.0, ang),
                            translate=(float(centre[0]), float(centre[1]), 0.0))
    meta = dict(solid.meta)
    meta.update({"kind": "text", "op": str(op_name), "mode": mode, "align": a,
                 "cap_height": cap, "stroke_width": w, "depth": d,
                 "angle_deg": ang, "at": [float(centre[0]), float(centre[1])],
                 "z_range": [z0, z1], "rows": [str(t) for t, _ in rows],
                 "font": "hershey-simplex"})
    return Mesh(solid.verts, solid.faces, meta)


def text_engrave(text="", height=0.02, depth=0.0012, at=(0.0, 0.0), angle_deg=0.0,
                 mode="engrave", stroke_width=0.0012, align="left"):
    """Feature op: text as solid strokes with ``z = 0`` at the surface.

    All lengths are **metres** (the feature graph converts them before calling
    this), ``at`` is the XY anchor and ``angle_deg`` rotates the text about
    that anchor around +Z.  See the module docstring for the z extent of each
    mode; the returned mesh is what you subtract (engrave) or union (emboss).
    """
    body = str(text if text is not None else "")
    if not body.strip():
        raise AttaError("bad_arg", "text is empty", path="args.text",
                        fix="pass at least one character, e.g. text='ATTA'")
    solid = text_mesh([(body, 0.0)], height, stroke_width, depth, mode=mode,
                      at=at, angle_deg=angle_deg, align=align, quality="review",
                      op_name=TEXT_OP)
    meta = dict(solid.meta)
    meta["text"] = body
    meta["missing"] = _SF.missing(body)
    return Mesh(solid.verts, solid.faces, meta)


def text_emboss(text="", height=0.02, depth=0.0012, at=(0.0, 0.0), angle_deg=0.0,
                stroke_width=0.0012, align="left"):
    """Feature op: the **raised** text body (``text_engrave`` with mode=emboss).

    Same arguments and units as :func:`text_engrave` minus ``mode``.  Union the
    result onto a body whose top face is at ``z = 0``: the solid dips
    ``0.35 * depth`` below the surface so the union always overlaps in volume.
    Registered separately because ``text_emboss`` is its own entry in the
    normalised op vocabulary (requirements §3.2) and in the text category's
    ``must_have_features``.
    """
    body = str(text if text is not None else "")
    if not body.strip():
        raise AttaError("bad_arg", "text is empty", path="args.text",
                        fix="pass at least one character, e.g. text='ATTA'")
    solid = text_mesh([(body, 0.0)], height, stroke_width, depth, mode="emboss",
                      at=at, angle_deg=angle_deg, align=align, quality="review",
                      op_name=EMBOSS_OP)
    meta = dict(solid.meta)
    meta["text"] = body
    meta["missing"] = _SF.missing(body)
    return Mesh(solid.verts, solid.faces, meta)


_F.register_op(TEXT_OP, {
    "required": ("text", "height", "depth"),
    "optional": ("at", "angle_deg", "mode", "stroke_width", "align"),
    "arity": 0,
    "lengths": ("height", "depth", "stroke_width"),
    "vectors": ("at",),
    "doc": "Single-stroke text as solid strokes (z=0 at the surface); mode="
           "engrave returns the cutter, mode=emboss the raised body.",
}, text_engrave)

_F.register_op(EMBOSS_OP, {
    "required": ("text", "height", "depth"),
    "optional": ("at", "angle_deg", "stroke_width", "align"),
    "arity": 0,
    "lengths": ("height", "depth", "stroke_width"),
    "vectors": ("at",),
    "doc": "Single-stroke text as a raised body (z=0 at the surface, sunk "
           "0.35*depth for a volumetric union); the emboss half of "
           "text_engrave, as its own op name.",
}, text_emboss)


# ------------------------------------------------------------------- recipe

def _line_ink(text, cap, stroke_width, tracking, align):
    """Ink box ``(x0, y0, x1, y1)`` of one line, already aligned about x = 0."""
    _strokes, ink = _SF.layout_strokes(str(text), cap_height=cap,
                                      stroke_width=stroke_width,
                                      tracking=tracking, align=align)
    return ink


def _build_text_plate(v, quality="draft"):
    """Plate with engraved/embossed text; returns ``(mesh, finish)``."""
    q = str(quality or "draft")
    seg = _segments(q)

    length = float(v["length"])
    width = float(v["width"])
    thickness = float(v["thickness"])
    corner_r = float(v.get("corner_r", 0.0))
    edge = float(v.get("edge_margin", 0.004))
    cap = float(v["cap_height"])
    depth = float(v["depth"])
    stroke_width = float(v["stroke_width"])
    line_gap = float(v.get("line_gap", 0.003))
    tracking = float(v.get("tracking", 0.0))
    angle = float(v.get("angle_deg", 0.0))
    text_x = float(v.get("text_x", 0.0))
    text_y = float(v.get("text_y", 0.0))
    hole_d = float(v.get("hole_d", 0.0) or 0.0)
    n_lines = int(v.get("lines", 1) or 1)
    mode = str(v.get("mode", "engrave") or "engrave").strip().lower()
    align = str(v.get("align", "center") or "center").strip().lower()
    text = str(v.get("text", "") or "")
    line2 = str(v.get("line2", "") or "")

    if mode not in ENGRAVE_MODES:
        raise AttaError("bad_arg", "mode must be engrave or emboss", path="mode",
                        fix="use mode='engrave'")
    if align not in ("left", "center", "centre", "right"):
        raise AttaError("bad_arg", "align must be left, center or right", path="align",
                        fix="use align='center'")
    if n_lines not in (1, 2):
        raise AttaError("bad_arg", "lines must be 1 or 2", path="lines",
                        fix="use lines=1 or lines=2")
    if n_lines == 2 and not line2.strip():
        raise AttaError("bad_arg", "lines=2 needs a second line", path="line2",
                        fix="pass line2='...' or set lines=1")
    if depth >= thickness:
        raise AttaError("bad_arg", "depth must be smaller than thickness",
                        path="depth",
                        fix="use depth < thickness*0.6 so the groove keeps a floor")

    # ------------------------------------------------------------ the plate
    plate = extrude(rounded_rect(length, width, corner_r, per_corner=max(4, seg // 2)),
                    thickness)
    plate = oriented(weld(plate, 1e-9))

    # ----------------------------------------------------------- the text
    rows = [(text, 0.0)]
    if n_lines == 2:
        rows.append((line2, 0.0))
    block = n_lines * cap + (n_lines - 1) * line_gap
    usable_l = length - 2.0 * edge
    usable_w = width - 2.0 * edge

    # vertical placement: the nominal line boxes are centred on the plate,
    # with the block's top line first; text_y shifts the whole block.
    placed = []
    for i, (line, _dy) in enumerate(rows):
        baseline = 0.5 * block - i * (cap + line_gap) - cap
        placed.append((line, baseline + text_y))

    # fit check on the *real ink* (strokes included) inside the rotation
    # envelope, so a rotated line cannot quietly run off the plate edge.
    ink_lo = ink_hi = None
    worst = 0.0
    for line, dy in placed:
        ink = _line_ink(line, cap, stroke_width, tracking, align)
        worst = max(worst, float(ink[2] - ink[0]))
        lo = (float(ink[0]), float(ink[1]) + dy)
        hi = (float(ink[2]), float(ink[3]) + dy)
        if ink_lo is None:
            ink_lo, ink_hi = lo, hi
        else:
            ink_lo = (min(ink_lo[0], lo[0]), min(ink_lo[1], lo[1]))
            ink_hi = (max(ink_hi[0], hi[0]), max(ink_hi[1], hi[1]))
    if ink_lo is None:                      # blank lines: nothing to place
        ink_lo, ink_hi = (0.0, 0.0), (0.0, 0.0)
    span_x = ink_hi[0] - ink_lo[0]
    span_y = ink_hi[1] - ink_lo[1]
    rad = math.radians(angle)
    cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
    need_l = span_x * cos_a + span_y * sin_a
    need_w = span_x * sin_a + span_y * cos_a
    leftmost = ink_lo[0]

    if need_l > usable_l + 1e-12:
        raise AttaError(
            "text_too_wide",
            "the text ink needs %.2f mm across but the plate allows %.2f mm%s"
            % (need_l * 1000.0, usable_l * 1000.0,
               " (rotation included)" if angle else ""),
            path="text",
            fix="lower cap_height/text length or raise length: cap_height <= "
                "%.2f mm would fit" % ((cap * usable_l / max(need_l, 1e-12)) * 1000.0))
    if need_w > usable_w + 1e-12:
        raise AttaError(
            "text_too_tall",
            "the text block needs %.2f mm across the plate but the plate allows %.2f mm%s"
            % (need_w * 1000.0, usable_w * 1000.0,
               " (rotation included)" if angle else ""),
            path="cap_height",
            fix="lower cap_height, line_gap or the rotation angle, or widen the plate: "
                "cap_height <= %.2f mm would fit"
                % ((cap * usable_w / max(need_w, 1e-12)) * 1000.0))

    if any(str(line).strip() for line, _ in placed):
        solid = text_mesh(placed, cap, stroke_width, depth, mode=mode,
                          at=(text_x, 0.0), angle_deg=angle, align=align,
                          tracking=tracking, quality=q)
        solid = solid.transform(translate=(0.0, 0.0, thickness))

        if hole_d > 0.0:
            hole_x = -0.5 * length + edge + 0.5 * hole_d
            ink_lo = text_x + leftmost
            if ink_lo <= hole_x + 0.5 * hole_d:
                raise AttaError(
                    "text_hits_hole", "the text would run into the mounting hole",
                    path="hole_d",
                    fix="use hole_d=0, shrink cap_height, or raise length so the text "
                        "starts right of %.1f mm" % ((hole_x + 0.5 * hole_d) * 1000.0))

        plate = union(plate, solid) if mode == "emboss" else difference(plate, solid)

    # -------------------------------------------------------- mounting hole
    if hole_d > 0.0:
        hole_x = -0.5 * length + edge + 0.5 * hole_d
        clear = 0.5 * thickness
        drill = _P.cylinder(0.5 * hole_d, thickness + 2.0 * clear,
                            center=[hole_x, 0.0, 0.5 * thickness],
                            segments=max(16, seg * 2))
        plate = difference(plate, drill)

    topo = topology(plate)
    if not topo["closed"]:
        raise AttaError("not_closed", "the plate did not stay watertight",
                        path="text", fix="raise thickness or reduce depth")

    meta = dict(plate.meta)
    meta["text"] = {
        "font": "hershey-simplex",
        "text": text,
        "line2": line2 if n_lines == 2 else "",
        "lines": n_lines,
        "mode": mode,
        "align": align,
        "cap_height_mm": round(cap * 1000.0, 4),
        "depth_mm": round(depth * 1000.0, 4),
        "stroke_width_mm": round(stroke_width * 1000.0, 4),
        "ink_width_mm": round(worst * 1000.0, 4),
        "block_height_mm": round(block * 1000.0, 4),
        "n_strokes": int(sum(len(_SF.glyph_polylines(ch, cap_height=cap))
                             for line, _y in placed for ch in str(line))),
        "missing": "".join(sorted({c for line, _ in placed for c in _SF.missing(line)})),
    }
    return Mesh(plate.verts, plate.faces, meta), finish(
        material="aluminum", smooth_angle_deg=25.0)


_MODES = list(ENGRAVE_MODES)
_ALIGNS = ["left", "center", "right"]

recipe(
    "text.engraved_plate",
    "铭牌 / engraved text plate",
    "Flat nameplate carrying single-stroke (Hershey Simplex) text on its top "
    "face: engraved as a trench with vertical walls, or embossed as raised "
    "strokes that are sunk into the plate so the union is one solid. One or "
    "two lines, optional rounded corners and one through mounting hole. It is "
    "a stroke font (median lines, not letter outlines): glyphs are drawn by "
    "sweeping stroke_width along the centre line, which is why it needs no "
    "font files and stays licence-clean; the trade-off is no outline counters "
    "(engraved O and A leave an island, not a hole), no kerning and printable "
    "ASCII only — other characters fall back to '?' and are listed in the "
    "mesh meta. Use preset='proof' for a fat-stroked label whose glyphs are "
    "legible in the ASCII section view.",
    params={
        "length": {"type": "float", "default": 160.0, "min": 30.0, "max": 400.0,
                   "unit": "mm", "group": "plate",
                   "description": "plate length (X)"},
        "width": {"type": "float", "default": 60.0, "min": 20.0, "max": 200.0,
                  "unit": "mm", "group": "plate",
                  "description": "plate width (Y)"},
        "thickness": {"type": "float", "default": 6.0, "min": 1.5, "max": 20.0,
                      "unit": "mm", "group": "plate",
                      "description": "plate thickness (Z)"},
        "corner_r": {"type": "float", "default": 6.0, "min": 0.0, "max": 20.0,
                     "unit": "mm", "group": "plate",
                     "description": "corner radius, 0 for square corners"},
        "edge_margin": {"type": "float", "default": 6.0, "min": 1.0, "max": 40.0,
                        "unit": "mm", "group": "plate",
                        "description": "text keep-out band from the plate edge"},
        "hole_d": {"type": "float", "default": 0.0, "min": 0.0, "max": 12.0,
                   "unit": "mm", "group": "plate",
                   "description": "one through mounting hole at the left end "
                                  "(0 disables)"},
        "text": {"type": "str", "default": "ATTA", "group": "text",
                 "description": "first line (printable ASCII)"},
        "line2": {"type": "str", "default": "", "group": "text",
                  "description": "second line, used when lines=2"},
        "lines": {"type": "int", "default": 1, "min": 1, "max": 2,
                  "group": "text", "unit": "count",
                  "description": "1 or 2 text lines"},
        "line_gap": {"type": "float", "default": 3.0, "min": 0.5, "max": 20.0,
                     "unit": "mm", "group": "text",
                     "description": "gap between line boxes"},
        "cap_height": {"type": "float", "default": 20.0, "min": 3.0, "max": 40.0,
                       "unit": "mm", "group": "text",
                       "description": "cap height of the stroke font"},
        "stroke_width": {"type": "float", "default": 1.6, "min": 0.3, "max": 3.0,
                         "unit": "mm", "group": "text",
                         "description": "swept stroke width (line weight)"},
        "tracking": {"type": "float", "default": 0.0, "min": -0.2, "max": 0.6,
                     "unit": "ratio", "group": "text",
                     "description": "extra space between glyphs, in cap heights"},
        "align": {"type": "enum", "default": "center", "enum": _ALIGNS,
                  "group": "text", "description": "horizontal text alignment"},
        "mode": {"type": "enum", "default": "engrave", "enum": _MODES,
                 "group": "finish", "description": "engrave a trench or emboss raised strokes"},
        "depth": {"type": "float", "default": 1.2, "min": 0.1, "max": 5.0,
                  "unit": "mm", "group": "finish",
                  "description": "engraving depth / emboss height"},
        "angle_deg": {"type": "float", "default": 0.0, "min": -30.0, "max": 30.0,
                      "unit": "deg", "group": "text",
                      "description": "rotate the text about its anchor"},
        "text_x": {"type": "float", "default": 0.0, "min": -100.0, "max": 100.0,
                   "unit": "mm", "group": "text", "description": "text X offset"},
        "text_y": {"type": "float", "default": 0.0, "min": -50.0, "max": 50.0,
                   "unit": "mm", "group": "text", "description": "text Y offset"},
    },
    presets={
        "safe": {"length": 80.0, "width": 40.0, "thickness": 4.0, "corner_r": 3.0,
                 "edge_margin": 4.0, "text": "ATTA", "lines": 1, "cap_height": 12.0,
                 "stroke_width": 1.0, "depth": 0.6, "mode": "engrave",
                 "align": "center", "hole_d": 0.0},
        "nominal": {"length": 160.0, "width": 60.0, "thickness": 6.0, "corner_r": 6.0,
                    "edge_margin": 6.0, "text": "ATTA", "lines": 1, "cap_height": 20.0,
                    "stroke_width": 1.6, "depth": 1.2, "mode": "engrave",
                    "align": "center", "hole_d": 0.0},
        "hero": {"length": 220.0, "width": 80.0, "thickness": 8.0, "corner_r": 10.0,
                 "edge_margin": 8.0, "text": "ATTA", "line2": "MODEL 24", "lines": 2,
                 "line_gap": 4.0, "cap_height": 22.0, "stroke_width": 2.0,
                 "depth": 2.0, "mode": "emboss", "align": "center", "hole_d": 0.0},
        # not a product configuration: a big, fat-stroked label so the 64-column
        # ASCII section of ``atta_describe/atta_perceive`` resolves the glyphs
        # instead of aliasing them away (1.6 mm strokes vanish at nameplate size).
        "proof": {"length": 150.0, "width": 60.0, "thickness": 6.0, "corner_r": 5.0,
                  "edge_margin": 6.0, "text": "ATTA", "lines": 1, "cap_height": 34.0,
                  "stroke_width": 4.0, "depth": 1.5, "mode": "engrave",
                  "align": "center", "hole_d": 0.0},
    },
    constraints=[
        {"id": "groove_floor", "expr": "depth < thickness * 0.6", "severity": "error",
         "message": "an engraving deeper than 60% of the plate leaves no floor: the "
                    "groove would break through or weaken the plate",
         "fix": "depth = min(depth, thickness*0.6); the default 1.2 mm suits a 6 mm plate"},
        {"id": "block_fits", "expr": "lines * cap_height + (lines - 1) * line_gap "
                                    "<= width - 2 * edge_margin",
         "severity": "error",
         "message": "the text block is taller than the plate's usable width",
         "fix": "lower cap_height or line_gap, or raise width"},
        {"id": "stroke_ratio", "expr": "stroke_width <= cap_height * 0.25",
         "severity": "error",
         "message": "strokes thicker than a quarter of the cap height merge letters "
                    "together ('A' and 'H' bleed into blobs)",
         "fix": "stroke_width = min(stroke_width, cap_height*0.18)"},
        {"id": "hole_margin", "expr": "hole_d <= 2 * edge_margin", "severity": "error",
         "message": "the mounting hole needs at least half a diameter of material to "
                    "the plate edge",
         "fix": "raise edge_margin to hole_d/2, or shrink hole_d"},
        {"id": "corner_fits", "expr": "corner_r <= width / 2", "severity": "error",
         "message": "corner radius larger than half the plate width",
         "fix": "corner_r = width*0.2"},
    ],
    expected_genus=0,
    material="aluminum",
    tags=["text", "engrave", "emboss", "nameplate", "single_stroke", "hershey",
          "boolean_difference"],
)(_build_text_plate)
