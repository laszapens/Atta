# -*- coding: utf-8 -*-
"""Lattice families — triply-periodic minimal surfaces sampled as real solids.

``lattice.gyroid_slab`` is a **gyroid** lattice slab: the implicit gyroid
surface ``F = sin x cos y + sin y cos z + sin z cos x = t`` is turned into a
*thick sheet* (a real printable solid, not a zero-thickness surface) and cut
out of a rectangular slab.

Why a solid field and not "mesh the surface then thicken"
---------------------------------------------------------
``surfaces.thicken`` needs an **open** patch and offsets along vertex normals;
on a periodic minimal surface that produces self-intersections wherever the
sheet's normal curvature radius is smaller than the half thickness.  Instead the
solid is defined directly by one scalar field and extracted with marching
tetrahedra::

    phi(x) = max( |F(x) - t_bias| - h ,  box(x) )

``phi < 0`` is exactly "inside the gyroid sheet **and** inside the slab", so the
isosurface of ``phi`` is the finished solid incl. the four flat cut walls, and it
is closed by construction because ``phi > 0`` on the whole sampled box border.

The physical wall thickness comes from calibrating the field gradient:
``h = 0.5 * thickness * median|grad F|`` over the sheet band, i.e. the field
offset is converted to a metric offset with the local |grad F|.  That keeps
``thickness`` a real millimetre value instead of a field-unit constant.

Watertightness
--------------
Marching tetrahedra (6 tets per cell, fan around one body diagonal) is used
instead of marching cubes: every tetrahedron emits a closed polygon, and because
the 6-tet split is translation invariant the triangulation induced on each
shared cube face matches exactly on both sides, so no cracks can appear.  Shared
vertices are deduplicated by an *exact* integer edge key (the two grid node
ids), not by a distance tolerance — no weld slack, no bucket-boundary cracks.
Triangle winding is fixed per triangle against the inside→outside centroid
direction, which is globally consistent because it follows ``grad phi``.
"""

from __future__ import annotations

import math

import numpy as np

from ..errors import AttaError
from ..kit import finish, quality_level
from ..mesh import Mesh, oriented
from ..registry import recipe

__all__ = []

#: lattice pattern phase, in cells.  The slab is cut a quarter cell off the
#: gyroid's inversion centre: cutting exactly through it chips two ~0.015 mm3
#: corner slivers off the sheet (measured: 3 components), and they are only
#: held to the body through material outside the slab.
_PHASE = (0.25, 0.0, 0.0)

#: cube corners, index = x + 2*y + 4*z
_CORNERS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
                     [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]], dtype=np.float64)
#: 6-tet fan around the 0-7 body diagonal (translation invariant => crack free)
_TETS = np.array([[0, 1, 3, 7], [0, 3, 2, 7], [0, 2, 6, 7],
                  [0, 6, 4, 7], [0, 4, 5, 7], [0, 5, 1, 7]], dtype=np.int64)
#: the 6 edges of a tetrahedron, by local vertex id
_TET_EDGES = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
#: case -> triangles, each triangle = 3 edge ids of ``_TET_EDGES``.
#: Winding is not trusted from this table; it is fixed per triangle by the
#: inside->outside direction, which keeps neighbours consistent.
_CASES = {
    1: ((0, 1, 2),), 2: ((0, 3, 4),), 4: ((1, 3, 5),), 8: ((2, 4, 5),),
    3: ((1, 2, 4), (1, 4, 3)), 5: ((0, 3, 5), (0, 5, 2)),
    6: ((0, 1, 5), (0, 5, 4)), 7: ((2, 4, 5),),
    9: ((0, 1, 5), (0, 5, 4)), 10: ((0, 2, 5), (0, 5, 3)),
    11: ((1, 3, 5),), 12: ((1, 3, 4), (1, 4, 2)),
    13: ((0, 3, 4),), 14: ((0, 1, 2),),
}

#: minimum samples across the sheet.  Two things were measured on the way here:
#: below ~2.2 voxels the marching surface invents/merges handles (genus 462 at
#: 1.1 voxels), and below ~2.5 voxels the extracted sheet is systematically
#: thinner than ordered (ray-sampled p50 2.51 mm for a 3.0 mm sheet, plus a bogus
#: 0.07 mm "wall" where marching cuts a corner off).  At 3 voxels the same build
#: measures p50 3.19 mm / min 2.40 mm, so 3.0 is the floor.
_MIN_VOX = 3.0
_MAX_RES = 30
_MAX_SAMPLES = 2_500_000


def marching_tets(field, origin, spacing) -> Mesh:
    """Extract the ``field < 0`` solid from a regular grid as a closed mesh.

    ``field`` is ``(nx, ny, nz)`` sampled at ``origin + (i, j, k) * spacing``.
    The result is watertight and single-winding provided ``field > 0`` on the
    whole boundary of the grid (so the surface is fully enclosed).
    """
    f = np.asarray(field, dtype=np.float64)
    spacing = np.broadcast_to(np.asarray(spacing, dtype=np.float64).reshape(-1),
                              (3,)).copy()
    origin = np.asarray(origin, dtype=np.float64).reshape(3)
    if f.ndim != 3:
        raise AttaError("bad_arg", "field must be a 3D array", path="field",
                        fix="pass field[i, j, k]")
    nx, ny, nz = f.shape
    cx, cy, cz = nx - 1, ny - 1, nz - 1
    if min(cx, cy, cz) < 1:
        raise AttaError("bad_arg", "field needs at least 2 samples per axis",
                        path="field", fix="increase resolution")
    n_tot = np.int64(nx) * np.int64(ny) * np.int64(nz)

    # grid node id, and the per-corner id offsets
    gx, gy, gz = np.meshgrid(np.arange(cx), np.arange(cy), np.arange(cz),
                             indexing="ij")
    cell = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()]).astype(np.int64)
    del gx, gy, gz
    base_node = (cell[:, 0] * ny + cell[:, 1]) * nz + cell[:, 2]
    delta = ((_CORNERS[:, 0].astype(np.int64) * ny
              + _CORNERS[:, 1].astype(np.int64)) * nz
             + _CORNERS[:, 2].astype(np.int64))

    F8 = np.empty((len(cell), 8))
    for c in range(8):
        o = _CORNERS[c].astype(np.int64)
        F8[:, c] = f[cell[:, 0] + o[0], cell[:, 1] + o[1], cell[:, 2] + o[2]]

    base = origin + cell * spacing
    keys_out, pos_out = [], []
    for tet in _TETS:
        fv = F8[:, tet]                                     # (C,4)
        inside = fv < 0.0
        cid = (inside.astype(np.int64) * np.array([1, 2, 4, 8])).sum(axis=1)
        if not np.any((cid > 0) & (cid < 15)):
            continue
        nodes4 = base_node[:, None] + delta[tet][None, :]    # (C,4) int node ids
        P = base[:, None, :] + _CORNERS[tet][None, :, :] * spacing   # (C,4,3)
        # 6 intersection points, one per tet edge
        E = np.empty((len(cell), 6, 3))
        K = np.empty((len(cell), 6), dtype=np.int64)
        for k, (a, b) in enumerate(_TET_EDGES):
            fa, fb = fv[:, a], fv[:, b]
            den = fa - fb
            safe = np.where(np.abs(den) > 0.0, den, 1.0)
            t = np.clip(fa / safe, 1e-3, 1.0 - 1e-3)
            E[:, k] = P[:, a] + t[:, None] * (P[:, b] - P[:, a])
            na, nb = nodes4[:, a], nodes4[:, b]
            lo, hi = np.minimum(na, nb), np.maximum(na, nb)
            K[:, k] = lo * n_tot + hi
        # inside -> outside reference direction (the field gradient's sense)
        cnt_in = inside.sum(axis=1)[:, None]
        cin = np.where(inside[:, :, None], P, 0.0).sum(axis=1) / np.maximum(cnt_in, 1)
        cout = (np.where(~inside[:, :, None], P, 0.0).sum(axis=1)
                / np.maximum(4 - cnt_in, 1))
        d = cout - cin
        for case, tris in _CASES.items():
            m = cid == case
            if not m.any():
                continue
            for (e0, e1, e2) in tris:
                q0, q1, q2 = E[m, e0], E[m, e1], E[m, e2]
                nrm = np.cross(q1 - q0, q2 - q0)
                flip = np.einsum("ij,ij->i", nrm, d[m]) < 0.0
                keys_out.append(np.stack([K[m, e0],
                                          np.where(flip, K[m, e2], K[m, e1]),
                                          np.where(flip, K[m, e1], K[m, e2])], axis=1))
                pos_out.append(np.stack([q0,
                                         np.where(flip[:, None], q2, q1),
                                         np.where(flip[:, None], q1, q2)], axis=1))
    if not keys_out:
        raise AttaError("lattice_empty", "the field has no negative region",
                        path="field", fix="increase thickness or cell_size")
    keys = np.concatenate(keys_out, axis=0)          # (T,3)
    pos = np.concatenate(pos_out, axis=0)            # (T,3,3)
    uniq, first, inv = np.unique(keys.ravel(), return_index=True,
                                 return_inverse=True)
    verts = pos.reshape(-1, 3)[first]
    faces = inv.reshape(-1, 3).astype(np.int64)
    mesh = Mesh(verts, faces, {"kind": "marching_tets"})
    return oriented(mesh)


def _gyroid(x, y, z, k):
    """Gyroid field and its analytic gradient magnitude."""
    sx, cx = np.sin(k * x), np.cos(k * x)
    sy, cy = np.sin(k * y), np.cos(k * y)
    sz, cz = np.sin(k * z), np.cos(k * z)
    f = sx * cy + sy * cz + sz * cx
    gx = k * (cx * cy - sz * sx)
    gy = k * (-sx * sy + cy * cz)
    gz = k * (-sy * sz + cz * cx)
    return f, np.sqrt(gx * gx + gy * gy + gz * gz)


def _gyroid_slab_mesh(cell, thickness, t_bias, slab, res, quality="draft"):
    """Sample ``|F - t| < h`` inside the slab box; return ``(mesh, info)``."""
    L, W, H = (float(slab[0]), float(slab[1]), float(slab[2]))
    half = np.array([L, W, H], dtype=np.float64) * 0.5
    rq = int(quality_level(quality, res, res + 2, res + 5))
    r_need = int(math.ceil(_MIN_VOX * cell / max(thickness, 1e-9)))
    res_eff = int(min(_MAX_RES, max(8, max(rq, r_need))))
    spacing = cell / float(res_eff)
    margin = 2.5 * spacing
    shape = tuple(int(math.ceil((2.0 * half[i] + 2.0 * margin) / spacing)) + 1
                  for i in range(3))
    if int(np.prod(shape)) > _MAX_SAMPLES:
        raise AttaError(
            "lattice_too_dense",
            "gyroid slab needs %d samples at resolution %d" % (int(np.prod(shape)), res_eff),
            path="resolution",
            fix="lower resolution, use a thicker sheet or a smaller slab")

    org = -half - margin
    ax = org[0] + np.arange(shape[0]) * spacing
    ay = org[1] + np.arange(shape[1]) * spacing
    az = org[2] + np.arange(shape[2]) * spacing
    px, py, pz = (float(_PHASE[0]) * cell, float(_PHASE[1]) * cell,
                  float(_PHASE[2]) * cell)
    X, Y, Z = np.meshgrid(ax - px, ay - py, az - pz, indexing="ij")
    F, gmag = _gyroid(X, Y, Z, 2.0 * math.pi / cell)
    band = np.abs(F - t_bias) < 0.35
    g_ref = float(np.median(gmag[band])) if bool(band.any()) else float(np.median(gmag))
    if not math.isfinite(g_ref) or g_ref <= 0.0:
        raise AttaError("lattice_field", "gyroid gradient is degenerate",
                        path="cell_size", fix="use a larger cell_size")
    # Both terms below are METRES: dividing |F - t| by |grad F| turns the field
    # offset into a distance, so max(sheet_distance, box) is a proper (if
    # approximate) signed distance.  Mixing unitless field values with metric
    # box distances is silently wrong: max(a,b) < 0 still picks the same set,
    # but the voxel crossing points -- and therefore the extracted thickness and
    # volume -- drift with the unit scale (measured: identical inputs, 45866 vs
    # 26188 mm3 depending on whether lengths were in m or mm).
    sheet = np.abs(F - t_bias) / g_ref - 0.5 * float(thickness)
    # slab box in *unshifted* world coordinates (the pattern carries the phase)
    WX = org[0] + np.arange(shape[0]) * spacing
    WY = org[1] + np.arange(shape[1]) * spacing
    WZ = org[2] + np.arange(shape[2]) * spacing
    GX, GY, GZ = np.meshgrid(WX, WY, WZ, indexing="ij")
    box = np.maximum(np.maximum(np.abs(GX) - half[0], np.abs(GY) - half[1]),
                     np.abs(GZ) - half[2])
    phi = np.maximum(sheet, box)
    mesh = marching_tets(phi, org, spacing)
    return mesh, {"res_eff": res_eff, "shape": shape, "spacing": spacing,
                  "g_ref": g_ref, "cells": [L / cell, W / cell, H / cell]}


@recipe(
    "lattice.gyroid_slab", "陀螺晶格板",
    "隐式陀螺面 sin x cos y + sin y cos z + sin z cos x = t 加厚成实体薄片，"
    "再用板坯盒求交；行进四面体提取，精确顶点去重，闭合单实体。",
    params={
        "slab_l": {"type": "float", "default": 66.0, "min": 20.0, "max": 200.0,
                   "unit": "mm", "group": "slab"},
        "slab_w": {"type": "float", "default": 44.0, "min": 20.0, "max": 200.0,
                   "unit": "mm", "group": "slab"},
        "slab_h": {"type": "float", "default": 22.0, "min": 8.0, "max": 120.0,
                   "unit": "mm", "group": "slab"},
        "cell_size": {"type": "float", "default": 22.0, "min": 8.0, "max": 48.0,
                      "unit": "mm", "group": "lattice",
                      "description": "晶格周期（一个完整陀螺单元）"},
        "thickness": {"type": "float", "default": 3.0, "min": 1.0, "max": 8.0,
                      "unit": "mm", "group": "lattice",
                      "description": "陀螺薄片实体壁厚"},
        "t_bias": {"type": "float", "default": 0.0, "min": -0.8, "max": 0.8,
                   "unit": "none", "group": "lattice",
                   "description": "等值面偏移（0 = 平衡陀螺面）"},
        "resolution": {"type": "int", "default": 14, "min": 8, "max": 32,
                       "unit": "count", "group": "quality", "advanced": True,
                       "description": "每个晶格周期的采样数（壁厚会再抬升它）"},
    },
    presets={
        "safe": {"cell_size": 24.0, "thickness": 3.6, "slab_l": 72.0,
                 "slab_w": 48.0, "slab_h": 24.0, "resolution": 12, "t_bias": 0.0},
        "nominal": {"cell_size": 22.0, "thickness": 3.0, "slab_l": 66.0,
                    "slab_w": 44.0, "slab_h": 22.0, "resolution": 14, "t_bias": 0.0},
        "hero": {"cell_size": 18.0, "thickness": 2.2, "slab_l": 54.0,
                 "slab_w": 36.0, "slab_h": 18.0, "resolution": 20, "t_bias": 0.1},
    },
    tags=["lattice", "gyroid", "implicit", "marching_tets", "porous"],
    expected_genus=39, material="nylon",
    constraints=[
        {"id": "wall_min_ratio", "expr": "thickness >= 0.09 * cell_size",
         "severity": "error", "message": "薄片壁厚小于 0.09 倍晶格周期，采样无法分辨",
         "fix": "thickness = 0.12 * cell_size"},
        {"id": "wall_max_ratio", "expr": "thickness <= 0.16 * cell_size",
         "severity": "error", "message": "薄片过厚会自熔、改变晶格拓扑（实测 genus 漂移）",
         "fix": "thickness = 0.12 * cell_size"},
        {"id": "bias_band", "expr": "abs(t_bias) <= 0.3",
         "severity": "error", "message": "等值面偏移过大会把薄片撕裂成多个实体",
         "fix": "t_bias = 0.0"},
        {"id": "slab_holds_cells", "expr": "slab_l >= 1.5 * cell_size and slab_w >= 1.5 * cell_size",
         "severity": "warn", "message": "板坯小于 1.5 个晶格周期，边界效应占主导",
         "fix": "slab_l = 3 * cell_size"},
        {"id": "genus_envelope",
         "expr": "abs(slab_l / cell_size - 3) <= 0.6 and abs(slab_w / cell_size - 2) <= 0.6"
                 " and abs(slab_h / cell_size - 1) <= 0.45",
         "severity": "warn",
         "message": "板坯不是 3×2×1 个晶格单元：expected_genus=39 只在这一族的单元数下成立，"
                    "改单元数会改变亏格（实测 1×1×1=5, 2×2×1=25, 3×3×1=61, 3×2×2=87, 4×2×1=53）",
         "fix": "slab_l = 3 * cell_size; slab_w = 2 * cell_size; slab_h = cell_size"},
    ],
)
def _build_gyroid_slab(v, quality="draft"):
    mesh, info = _gyroid_slab_mesh(
        float(v["cell_size"]), float(v["thickness"]), float(v["t_bias"]),
        (v["slab_l"], v["slab_w"], v["slab_h"]), int(v["resolution"]), quality)
    mesh.meta.update({"lattice": "gyroid", **info})
    return mesh, finish(smooth_angle_deg=35.0, material="nylon")


# ---------------------------------------------------------------- graph op

def _gyroid_op(*, size, cell_size, thickness, t_bias=0.0, resolution=14):
    """Composable gyroid slab for feature graphs (all values in metres)."""
    mesh, info = _gyroid_slab_mesh(
        float(cell_size), float(thickness), float(t_bias),
        tuple(float(x) for x in size), int(resolution), "draft")
    mesh.meta.update({"lattice": "gyroid", **info})
    return mesh


def _gyroid_prepare(args, scale, path):
    size = np.asarray(args.get("size"), dtype=np.float64).reshape(-1)
    if size.shape != (3,) or not np.isfinite(size).all() or np.any(size <= 0):
        raise AttaError("bad_arg", "gyroid.size must be three positive lengths",
                        path=path + ".args.size", fix="pass [x,y,z] in mm")
    cell = float(args.get("cell_size"))
    thick = float(args.get("thickness"))
    if not math.isfinite(cell) or cell <= 0 or not math.isfinite(thick) or thick <= 0:
        raise AttaError("bad_arg", "gyroid cell_size/thickness must be positive",
                        path=path + ".args",
                        fix="use cell_size in mm and a thickness in mm")
    if thick >= 0.5 * cell:
        raise AttaError("bad_arg", "gyroid thickness must be < 0.5 * cell_size",
                        path=path + ".args.thickness",
                        fix="use thickness = 0.12 * cell_size")
    t_bias = float(args.get("t_bias", 0.0))
    if abs(t_bias) > 0.8:
        raise AttaError("bad_arg", "gyroid t_bias must be within [-0.8, 0.8]",
                        path=path + ".args.t_bias", fix="use t_bias = 0")
    res = int(round(float(args.get("resolution", 14))))
    if res < 8:
        raise AttaError("bad_arg", "gyroid resolution must be >= 8",
                        path=path + ".args.resolution", fix="use resolution = 14")
    return {"size": size * scale, "cell_size": cell * scale,
            "thickness": thick * scale, "t_bias": t_bias,
            "resolution": res}


_gyroid_op.prepare = _gyroid_prepare
try:
    from ..features import register_op as _register_op

    _register_op(
        "gyroid",
        {"required": ("size", "cell_size", "thickness"),
         "optional": ("t_bias", "resolution"), "arity": 0,
         "doc": "Gyroid lattice slab: size=[x,y,z] mm, cell_size mm, "
                "thickness mm, t_bias, resolution."},
        _gyroid_op,
    )
except Exception:  # pragma: no cover - a family must never break the engine
    pass
