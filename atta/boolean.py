# -*- coding: utf-8 -*-
"""CSG booleans and symmetry/pattern helpers.  Units: metres.

Booleans go through trimesh's ``manifold`` engine (manifold3d).  Results are
welded and required to remain closed; a failure raises ``AttaError`` with a
concrete fix instead of returning a broken mesh.
"""

from __future__ import annotations

import math

import numpy as np

from .errors import AttaError
from .mesh import Mesh, concat, oriented, topology, weld

__all__ = ["boolean", "union", "difference", "intersection", "mirror",
           "pattern_linear", "pattern_polar"]

_OPS = ("union", "difference", "intersection")


def _to_trimesh(mesh):
    import trimesh
    return trimesh.Trimesh(vertices=np.asarray(mesh.verts, dtype=np.float64),
                           faces=np.asarray(mesh.faces, dtype=np.int64),
                           process=False)


def _from_trimesh(obj):
    import trimesh
    if isinstance(obj, trimesh.Scene):
        geoms = [g for g in obj.geometry.values() if len(g.faces)]
        if not geoms:
            raise AttaError("boolean_empty", "boolean produced an empty scene",
                            path="boolean", fix="make the two solids overlap")
        obj = geoms[0] if len(geoms) == 1 else trimesh.util.concatenate(geoms)
    if obj is None or len(obj.faces) == 0:
        raise AttaError("boolean_empty", "boolean produced an empty mesh", path="boolean",
                        fix="make the two solids overlap")
    return Mesh(np.asarray(obj.vertices, dtype=np.float64),
                np.asarray(obj.faces, dtype=np.int64), {"kind": "boolean"})


def _to_manifold(mesh):
    import manifold3d
    return manifold3d.Manifold(manifold3d.Mesh(
        vert_properties=np.asarray(mesh.verts, dtype=np.float32),
        tri_verts=np.asarray(mesh.faces, dtype=np.uint32)))


def _from_manifold(obj):
    mm = obj.to_mesh()
    verts = np.asarray(mm.vert_properties, dtype=np.float64)
    faces = np.asarray(mm.tri_verts, dtype=np.int64)
    if len(faces) == 0:
        raise AttaError("boolean_empty", "boolean produced an empty mesh", path="boolean",
                        fix="make the two solids overlap")
    return Mesh(verts[:, :3], faces, {"kind": "boolean"})


def _run(a, b, op):
    import trimesh
    try:
        out = getattr(trimesh.boolean, op)([_to_trimesh(a), _to_trimesh(b)],
                                           engine="manifold")
        return _from_trimesh(out)
    except AttaError:
        raise
    except Exception:
        pass
    try:
        ma, mb = _to_manifold(a), _to_manifold(b)
        out = {"union": ma + mb, "difference": ma - mb,
               "intersection": ma ^ mb}[op]
        return _from_manifold(out)
    except Exception as exc:  # noqa: BLE001
        raise AttaError("boolean_failed", "boolean %s failed: %s" % (op, exc),
                        path="boolean.%s" % op,
                        fix="check that both inputs are watertight and actually overlap; "
                            "reduce self-intersections or shrink the tool") from exc


def boolean(a, b, op="union") -> Mesh:
    """CSG of two closed solids: ``op`` is union, difference or intersection."""
    name = str(op).lower()
    if name not in _OPS:
        raise AttaError("bad_arg", "boolean op must be union, difference or intersection",
                        path="boolean.op", fix="use op='union'")
    for label, mesh in (("a", a), ("b", b)):
        if not isinstance(mesh, Mesh):
            raise AttaError("bad_arg", "%s must be a Mesh" % label, path="boolean.%s" % label,
                            fix="pass a Mesh from primitives/surfaces")
        if not topology(mesh)["closed"]:
            raise AttaError("boolean_input_open", "boolean input %s is not closed" % label,
                            path="boolean.%s" % label,
                            fix="pass a watertight mesh; close caps or thicken the surface")
    out = oriented(weld(_run(a, b, name), 1e-9))
    if not topology(out)["closed"]:
        raise AttaError("boolean_not_closed", "boolean %s result is not closed" % name,
                        path="boolean.%s" % name,
                        fix="nudge the tool by 1e-4 m, lower the overlap, or rebuild the "
                            "inputs with more segments")
    return out


def union(a, b) -> Mesh:
    return boolean(a, b, "union")


def difference(a, b) -> Mesh:
    return boolean(a, b, "difference")


def intersection(a, b) -> Mesh:
    return boolean(a, b, "intersection")


def mirror(mesh, plane="x") -> Mesh:
    """Mirror across a principal plane (``x``/``y``/``z`` or ``yz``/``xz``/``xy``)."""
    if not isinstance(mesh, Mesh):
        raise AttaError("bad_arg", "mesh must be a Mesh", path="mirror", fix="pass a Mesh")
    p = str(plane).lower()
    axes = {"x": 0, "y": 1, "z": 2, "yz": 0, "xz": 1, "xy": 2}
    if p not in axes:
        raise AttaError("bad_arg", "plane must be x, y, z, yz, xz or xy", path="mirror.plane",
                        fix="use plane='x'")
    v = mesh.verts.copy()
    v[:, axes[p]] *= -1.0
    faces = mesh.faces[:, ::-1].copy()
    return oriented(weld(Mesh(v, faces, {**mesh.meta, "kind": "mirror"}), 1e-9))


def _axis_index(axis):
    a = str(axis).lower()
    if a not in ("x", "y", "z"):
        raise AttaError("bad_arg", "axis must be x, y or z", path="pattern.axis",
                        fix="use axis='x'")
    return a


def pattern_linear(mesh, count, spacing, axis="x") -> Mesh:
    """``count`` copies translated by ``spacing`` metres along ``axis``."""
    if not isinstance(mesh, Mesh):
        raise AttaError("bad_arg", "mesh must be a Mesh", path="pattern", fix="pass a Mesh")
    n = int(count)
    if n < 1:
        raise AttaError("bad_arg", "count must be >= 1", path="pattern.count",
                        fix="use count >= 1")
    d = float(spacing)
    if not math.isfinite(d):
        raise AttaError("bad_arg", "spacing must be finite", path="pattern.spacing",
                        fix="pass a finite spacing in metres")
    a = _axis_index(axis)
    t = [0.0, 0.0, 0.0]
    t["xyz".index(a)] = d
    parts = [mesh if i == 0 else mesh.transform(translate=t) for i in range(n)]
    out = concat(parts)
    out.meta["kind"] = "pattern_linear"
    return out


def pattern_polar(mesh, count, axis="z", angle_deg=360.0, center=(0, 0, 0)) -> Mesh:
    """``count`` rotated copies; a full 360-degree sweep spaces them evenly."""
    if not isinstance(mesh, Mesh):
        raise AttaError("bad_arg", "mesh must be a Mesh", path="pattern", fix="pass a Mesh")
    n = int(count)
    if n < 1:
        raise AttaError("bad_arg", "count must be >= 1", path="pattern.count",
                        fix="use count >= 1")
    a = _axis_index(axis)
    total = float(angle_deg)
    if not math.isfinite(total):
        raise AttaError("bad_arg", "angle_deg must be finite", path="pattern.angle_deg",
                        fix="pass a finite angle in degrees")
    step = total / n if abs(abs(total) - 360.0) <= 1e-9 else (
        total / (n - 1) if n > 1 else 0.0)
    parts = []
    for i in range(n):
        rot = [0.0, 0.0, 0.0]
        rot["xyz".index(a)] = step * i
        parts.append(mesh if i == 0 else mesh.transform(rotate_deg=rot, pivot=center))
    out = concat(parts)
    out.meta["kind"] = "pattern_polar"
    return out
