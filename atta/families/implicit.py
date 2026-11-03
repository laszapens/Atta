# -*- coding: utf-8 -*-
"""Composable implicit fields — ``sdf`` as a first-class feature-graph op.

The lattice recipe ships one hard-wired gyroid slab.  This module turns the
same machinery into a general op: any field from the triply-periodic minimal
surface family, a mandelbulb shell, or a primitive SDF can be sampled on a
regular grid, clipped to a box or sphere, and extracted as a watertight mesh by
the Marching Tetrahedra kernel in :mod:`atta.families.lattice`.

Everything public is in millimetres; ``_prepare`` converts to metres at the
feature-graph boundary, exactly like ``gyroid``.
"""

from __future__ import annotations

import math

import numpy as np

from ..errors import AttaError
from ..kit import finish, quality_level
from ..mesh import Mesh
from .lattice import _MAX_RES, _MAX_SAMPLES, _MIN_VOX, marching_tets

__all__ = []

_FIELDS = ("gyroid", "schwarz_p", "neovius", "mandelbulb",
           "sphere", "box", "torus")
_CLIPS = ("box", "sphere")


def _tpms(field, X, Y, Z, period, offset):
    k = 2.0 * math.pi / period
    # Quarter-cell phase on x keeps a box cut away from the inversion centre,
    # which otherwise chips corner slivers off the sheet (same convention as
    # lattice.gyroid_slab).
    X = X - 0.25 * period
    if field == "gyroid":
        sx, cx = np.sin(k * X), np.cos(k * X)
        sy, cy = np.sin(k * Y), np.cos(k * Y)
        sz, cz = np.sin(k * Z), np.cos(k * Z)
        F = sx * cy + sy * cz + sz * cx
    elif field == "schwarz_p":
        F = np.cos(k * X) + np.cos(k * Y) + np.cos(k * Z)
    elif field == "neovius":
        cx, cy, cz = np.cos(k * X), np.cos(k * Y), np.cos(k * Z)
        F = 3.0 * (cx + cy + cz) + 4.0 * cx * cy * cz
    else:  # pragma: no cover - guarded by _FIELDS
        raise AttaError("bad_arg", "unknown field %r" % field, path="field")
    return F - float(offset)


def _mandelbulb(X, Y, Z, power=8.0, iterations=8):
    """Distance estimate for the Mandelbulb, in metres (non-negative)."""
    zx, zy, zz = X.copy(), Y.copy(), Z.copy()
    dr = np.ones_like(zx)
    r = np.zeros_like(zx)
    for _ in range(int(iterations)):
        r = np.sqrt(zx * zx + zy * zy + zz * zz)
        # Escape-time iteration: stop updating points that already escaped.
        # Without this the eighth power overflows float64 on the outer grid.
        active = r <= 4.0
        safe_r = np.minimum(r, 4.0)
        theta = np.arctan2(np.sqrt(zx * zx + zy * zy), zz)
        phi = np.arctan2(zy, zx)
        zr = np.where(active, np.power(safe_r, power - 1.0), 0.0)
        dr = np.where(active, zr * power * dr + 1.0, dr)
        theta = np.where(active, theta * power, theta)
        phi = np.where(active, phi * power, phi)
        nx = np.sin(theta) * np.cos(phi) * zr * safe_r + X
        ny = np.sin(theta) * np.sin(phi) * zr * safe_r + Y
        nz = np.cos(theta) * zr * safe_r + Z
        zx = np.where(active, nx, zx)
        zy = np.where(active, ny, zy)
        zz = np.where(active, nz, zz)
    r = np.sqrt(zx * zx + zy * zy + zz * zz)
    de = 0.5 * np.log(np.maximum(r, 1e-12)) * r / np.maximum(dr, 1e-12)
    return np.nan_to_num(de, nan=1.0, posinf=1.0, neginf=1.0)


def _primitive(field, X, Y, Z, size):
    half = 0.5 * np.asarray(size, dtype=np.float64)
    if field == "sphere":
        r = 0.5 * float(min(size))
        return np.sqrt(X * X + Y * Y + Z * Z) - r
    if field == "box":
        qx = np.abs(X) - half[0]
        qy = np.abs(Y) - half[1]
        qz = np.abs(Z) - half[2]
        outside = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2
                          + np.maximum(qz, 0) ** 2)
        inside = np.minimum(np.maximum(qx, np.maximum(qy, qz)), 0.0)
        return outside + inside
    # torus: major radius from the two largest dimensions, minor from the smallest
    s = sorted(float(v) for v in size)
    R = 0.5 * s[0] if len(set(s)) == 1 else 0.5 * min(s[0], s[1])
    R = max(R, 1e-9)
    r = max(0.18 * s[0], 1e-9)
    return np.sqrt((np.sqrt(X * X + Y * Y) - R) ** 2 + Z * Z) - r


def sdf_mesh(size, field="gyroid", period=None, thickness=None, offset=0.0,
             clip="box", clip_radius=None, resolution=16, power=8.0,
             quality="draft") -> tuple[Mesh, dict]:
    """Sample an implicit field and return ``(mesh, info)`` in metres."""
    size = np.asarray(size, dtype=np.float64).reshape(-1)
    if size.shape != (3,) or not np.isfinite(size).all() or np.any(size <= 0):
        raise AttaError("bad_arg", "sdf.size must be three positive lengths",
                        path="sdf.size", fix="pass [x,y,z] in mm")
    if field not in _FIELDS:
        raise AttaError("bad_arg", "sdf.field must be one of %s" % (", ".join(_FIELDS),),
                        path="sdf.field", fix="use field='gyroid'")
    if clip not in _CLIPS:
        raise AttaError("bad_arg", "sdf.clip must be 'box' or 'sphere'",
                        path="sdf.clip", fix="use clip='box'")
    size = size.astype(np.float64)
    half = 0.5 * size
    period = float(period) if period else float(min(size))
    if not math.isfinite(period) or period <= 0:
        raise AttaError("bad_arg", "sdf.period must be positive",
                        path="sdf.period", fix="use period = 0.8 * min(size)")
    if thickness is None:
        # A sheet thinner than ~1/10 of the period cannot be resolved; this
        # default matches the lattice recipe's 0.12 * cell rule.
        thickness = 0.12 * period
    thickness = float(thickness)
    if not math.isfinite(thickness) or thickness <= 0:
        raise AttaError("bad_arg", "sdf.thickness must be positive",
                        path="sdf.thickness", fix="use thickness in mm")

    is_tpms = field in ("gyroid", "schwarz_p", "neovius")
    is_shell = field in ("gyroid", "schwarz_p", "neovius", "mandelbulb")
    # resolution is "samples per period" for TPMS, "samples across the part"
    # for primitives; a thin shell always gets at least 3 voxels across it.
    rq = int(quality_level(quality, resolution, resolution + 2, resolution + 5))
    if is_tpms:
        r_need = int(math.ceil(_MIN_VOX * period / max(thickness, 1e-9)))
        res_eff = int(min(48, max(8, rq, r_need)))
        spacing = period / float(res_eff)
    else:
        res_eff = int(min(_MAX_RES, max(8, rq)))
        spacing = float(min(size)) / float(res_eff)
    margin = 2.5 * spacing
    shape = tuple(int(math.ceil((size[i] + 2.0 * margin) / spacing)) + 1
                  for i in range(3))
    if int(np.prod(shape)) > _MAX_SAMPLES:
        raise AttaError(
            "sdf_too_dense",
            "sdf needs %d samples at resolution %d" % (int(np.prod(shape)), res_eff),
            path="sdf.resolution",
            fix="lower resolution or increase period/thickness")

    org = -half - margin
    ax = org[0] + np.arange(shape[0]) * spacing
    ay = org[1] + np.arange(shape[1]) * spacing
    az = org[2] + np.arange(shape[2]) * spacing
    X, Y, Z = np.meshgrid(ax, ay, az, indexing="ij")

    if is_tpms:
        F = _tpms(field, X, Y, Z, period, offset)
        gx, gy, gz = np.gradient(F, spacing, spacing, spacing)
        gmag = np.sqrt(gx * gx + gy * gy + gz * gz)
        band = np.abs(F) < 0.35
        g_ref = float(np.median(gmag[band])) if bool(band.any()) else float(np.median(gmag))
        if not math.isfinite(g_ref) or g_ref <= 0:
            raise AttaError("sdf_field", "field gradient is degenerate",
                            path="sdf.period", fix="use a larger period")
        phi = np.abs(F) / g_ref - 0.5 * thickness
    elif field == "mandelbulb":
        R = 0.5 * float(min(size))
        phi = _mandelbulb(X / max(R, 1e-9), Y / max(R, 1e-9), Z / max(R, 1e-9),
                          power=power) * R - 0.5 * thickness
    else:
        phi = _primitive(field, X, Y, Z, size)

    if clip == "sphere":
        R = float(clip_radius) if clip_radius else 0.5 * float(min(size))
        if not math.isfinite(R) or R <= 0:
            raise AttaError("bad_arg", "sdf.clip_radius must be positive",
                            path="sdf.clip_radius", fix="omit it or pass mm")
        phi = np.maximum(phi, np.sqrt(X * X + Y * Y + Z * Z) - R)
    else:
        box = np.maximum(np.maximum(np.abs(X) - half[0], np.abs(Y) - half[1]),
                         np.abs(Z) - half[2])
        phi = np.maximum(phi, box)

    mesh = marching_tets(phi, org, spacing)
    info = {"field": field, "res_eff": res_eff, "spacing": spacing,
            "period": period, "clip": clip, "samples": list(shape)}
    if is_shell:
        info["thickness"] = thickness
    return mesh, info


def _sdf_op(*, size, field="gyroid", period=None, thickness=None, offset=0.0,
            clip="box", clip_radius=None, resolution=16, power=8.0):
    mesh, info = sdf_mesh(
        tuple(float(x) for x in size), field=field, period=period,
        thickness=thickness, offset=offset, clip=clip,
        clip_radius=clip_radius, resolution=int(resolution), power=float(power),
        quality="draft")
    mesh.meta.update({"implicit": field, **info})
    return mesh


def _sdf_prepare(args, scale, path):
    size = np.asarray(args.get("size"), dtype=np.float64).reshape(-1)
    if size.shape != (3,) or not np.isfinite(size).all() or np.any(size <= 0):
        raise AttaError("bad_arg", "sdf.size must be three positive lengths",
                        path=path + ".args.size", fix="pass [x,y,z] in mm")
    field = str(args.get("field") or "gyroid")
    if field not in _FIELDS:
        raise AttaError("bad_arg", "sdf.field must be one of %s" % (", ".join(_FIELDS),),
                        path=path + ".args.field", fix="use field='gyroid'")
    clip = str(args.get("clip") or "box")
    if clip not in _CLIPS:
        raise AttaError("bad_arg", "sdf.clip must be 'box' or 'sphere'",
                        path=path + ".args.clip", fix="use clip='box'")
    period = args.get("period")
    period = float(period) * scale if period else None
    thickness = args.get("thickness")
    thickness = float(thickness) * scale if thickness else None
    if thickness is not None and (not math.isfinite(thickness) or thickness <= 0):
        raise AttaError("bad_arg", "sdf.thickness must be positive",
                        path=path + ".args.thickness", fix="use thickness in mm")
    if period is not None and (not math.isfinite(period) or period <= 0):
        raise AttaError("bad_arg", "sdf.period must be positive",
                        path=path + ".args.period", fix="use period in mm")
    offset = float(args.get("offset", 0.0))
    radius = args.get("clip_radius")
    radius = float(radius) * scale if radius else None
    res = int(round(float(args.get("resolution", 16))))
    if res < 6:
        raise AttaError("bad_arg", "sdf.resolution must be >= 6",
                        path=path + ".args.resolution", fix="use resolution = 16")
    return {"size": size * scale, "field": field, "period": period,
            "thickness": thickness, "offset": offset, "clip": clip,
            "clip_radius": radius, "resolution": res,
            "power": float(args.get("power", 8.0))}


_sdf_op.prepare = _sdf_prepare
try:
    from ..features import register_op as _register_op

    _register_op(
        "sdf",
        {"required": ("size",), "arity": 0,
         "optional": ("field", "period", "thickness", "offset", "clip",
                      "clip_radius", "resolution", "power"),
         "doc": "Implicit field: size=[x,y,z] mm; field=gyroid|schwarz_p|"
                "neovius|mandelbulb|sphere|box|torus; period mm; thickness mm; "
                "clip=box|sphere; resolution samples/period."},
        _sdf_op,
    )
except Exception:  # pragma: no cover - a family must never break the engine
    pass
