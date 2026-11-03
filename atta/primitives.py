# -*- coding: utf-8 -*-
"""Closed analytic primitives.  Units: metres.

Every function returns a watertight, outward-wound triangle mesh.  ``axis``
accepts ``"x"``, ``"y"`` or ``"z"``; the solid is built along +Z and rotated.
"""

from __future__ import annotations

import math

import numpy as np

from .errors import AttaError
from .mesh import Mesh, oriented, weld

__all__ = ["box", "cylinder", "sphere", "cone", "torus"]

_EPS = 1e-12


def _check_axis(axis):
    a = str(axis).lower()
    if a not in ("x", "y", "z"):
        raise AttaError("bad_arg", "axis must be 'x', 'y' or 'z'", path="axis",
                        fix="use axis='z'")
    return a


def _axis_rotation(axis):
    return {"z": (0.0, 0.0, 0.0), "x": (0.0, 90.0, 0.0), "y": (-90.0, 0.0, 0.0)}[axis]


def _ring(radius, z, segments):
    th = np.linspace(0.0, 2.0 * math.pi, int(segments), endpoint=False)
    return np.column_stack([radius * np.cos(th), radius * np.sin(th),
                            np.full(int(segments), float(z))])


def box(size, center=(0, 0, 0), align="center") -> Mesh:
    """Axis-aligned box of side lengths ``size`` (metres)."""
    s = np.asarray(size, dtype=np.float64).reshape(-1)
    if s.shape != (3,) or not np.isfinite(s).all():
        raise AttaError("bad_arg", "size must be three finite numbers", path="size",
                        fix="pass size=[x, y, z] in metres")
    if np.any(s <= 0.0):
        raise AttaError("bad_arg", "size components must be > 0", path="size",
                        fix="use positive side lengths")
    c = np.asarray(center, dtype=np.float64).reshape(-1)
    if c.shape != (3,):
        raise AttaError("bad_arg", "center must be three numbers", path="center",
                        fix="pass center=[x, y, z]")
    a = str(align).lower()
    if a not in ("center", "min", "corner"):
        raise AttaError("bad_arg", "align must be 'center' or 'min'", path="align",
                        fix="use align='center'")
    lo = c - s * 0.5 if a == "center" else c
    x0, y0, z0 = lo
    x1, y1, z1 = lo + s
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
                  [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                  [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]], dtype=np.int64)
    return Mesh(v, f, {"kind": "box"})


def cylinder(radius, height, center=(0, 0, 0), axis="z", segments=64,
             caps=True) -> Mesh:
    """Solid cylinder of radius ``radius`` and ``height``; ``caps=False`` leaves an open tube."""
    r, h = float(radius), float(height)
    if not (math.isfinite(r) and math.isfinite(h)) or r <= 0.0 or h <= 0.0:
        raise AttaError("bad_arg", "radius and height must be finite and > 0",
                        path="radius/height", fix="pass positive radius and height")
    n = int(segments)
    if n < 3:
        raise AttaError("bad_arg", "segments must be >= 3", path="segments",
                        fix="use segments >= 8")
    a = _check_axis(axis)
    zb, zt = -0.5 * h, 0.5 * h
    vb, vt = _ring(r, zb, n), _ring(r, zt, n)
    verts = [vb, vt]
    faces = []
    for k in range(n):
        k1 = (k + 1) % n
        faces += [[k, k1, n + k1], [k, n + k1, n + k]]
    if caps:
        verts.append(np.array([[0.0, 0.0, zb]]))
        verts.append(np.array([[0.0, 0.0, zt]]))
        cb, ct = 2 * n, 2 * n + 1
        for k in range(n):
            k1 = (k + 1) % n
            faces += [[cb, k1, k], [ct, n + k, n + k1]]
    m = Mesh(np.vstack(verts), np.asarray(faces, dtype=np.int64), {"kind": "cylinder"})
    m = m.transform(translate=center, rotate_deg=_axis_rotation(a))
    return oriented(weld(m, 1e-9))


def sphere(radius, center=(0, 0, 0), segments=64, rings=32) -> Mesh:
    """UV sphere; pole rings are welded so the mesh stays closed."""
    r = float(radius)
    if not math.isfinite(r) or r <= 0.0:
        raise AttaError("bad_arg", "radius must be finite and > 0", path="radius",
                        fix="pass a positive radius")
    n = max(3, int(segments))
    m = max(2, int(rings))
    verts = []
    for j in range(m + 1):
        phi = -0.5 * math.pi + math.pi * j / m
        rr, z = r * math.cos(phi), r * math.sin(phi)
        verts.append(_ring(rr, z, n))
    v = np.vstack(verts)
    faces = []
    for j in range(m):
        for k in range(n):
            a = j * n + k
            b = j * n + (k + 1) % n
            c = (j + 1) * n + (k + 1) % n
            d = (j + 1) * n + k
            faces += [[a, b, c], [a, c, d]]
    mesh = Mesh(v, np.asarray(faces, dtype=np.int64), {"kind": "sphere"})
    mesh = mesh.transform(translate=center)
    return oriented(weld(mesh, 1e-9))


def cone(r_bottom, r_top, height, center=(0, 0, 0), axis="z", segments=64) -> Mesh:
    """Frustum/cone; a zero radius becomes a single apex vertex."""
    rb, rt, h = float(r_bottom), float(r_top), float(height)
    if not all(math.isfinite(x) for x in (rb, rt, h)) or rb < 0.0 or rt < 0.0 or h <= 0.0:
        raise AttaError("bad_arg", "r_bottom/r_top must be >= 0 and height > 0",
                        path="r_bottom/r_top/height", fix="pass non-negative radii and positive height")
    if rb <= _EPS and rt <= _EPS:
        raise AttaError("bad_arg", "at least one radius must be > 0", path="r_bottom/r_top",
                        fix="set r_bottom or r_top > 0")
    n = int(segments)
    if n < 3:
        raise AttaError("bad_arg", "segments must be >= 3", path="segments",
                        fix="use segments >= 8")
    a = _check_axis(axis)
    zb, zt = -0.5 * h, 0.5 * h
    verts, faces = [], []
    if rb > _EPS:
        vb = _ring(rb, zb, n)
        b0 = 0
        verts.append(vb)
    else:
        b0 = None
        verts.append(np.array([[0.0, 0.0, zb]]))
        apex_b = 0
    offset = len(np.vstack(verts))
    if rt > _EPS:
        vt = _ring(rt, zt, n)
        t0 = offset
        verts.append(vt)
    else:
        t0 = None
        verts.append(np.array([[0.0, 0.0, zt]]))
        apex_t = offset
    for k in range(n):
        k1 = (k + 1) % n
        if b0 is not None and t0 is not None:
            faces += [[b0 + k, b0 + k1, t0 + k1], [b0 + k, t0 + k1, t0 + k]]
        elif b0 is None:
            faces.append([apex_b, t0 + k1, t0 + k])
        else:
            faces.append([b0 + k, b0 + k1, apex_t])
    if b0 is not None:
        cb = len(np.vstack(verts))
        verts.append(np.array([[0.0, 0.0, zb]]))
        for k in range(n):
            faces.append([cb, b0 + (k + 1) % n, b0 + k])
    if t0 is not None:
        ct = len(np.vstack(verts))
        verts.append(np.array([[0.0, 0.0, zt]]))
        for k in range(n):
            faces.append([ct, t0 + k, t0 + (k + 1) % n])
    m = Mesh(np.vstack(verts), np.asarray(faces, dtype=np.int64), {"kind": "cone"})
    m = m.transform(translate=center, rotate_deg=_axis_rotation(a))
    return oriented(weld(m, 1e-9))


def torus(major_radius, minor_radius, center=(0, 0, 0), axis="z",
          seg_major=96, seg_minor=32) -> Mesh:
    """Torus with a closed genus-1 surface."""
    R, r = float(major_radius), float(minor_radius)
    if not all(math.isfinite(x) for x in (R, r)) or R <= 0.0 or r <= 0.0:
        raise AttaError("bad_arg", "major_radius and minor_radius must be > 0",
                        path="major_radius/minor_radius", fix="pass positive radii")
    if r >= R:
        raise AttaError("bad_arg", "minor_radius must be < major_radius", path="minor_radius",
                        fix="use minor_radius < major_radius to avoid a self-intersecting torus")
    nm, ns = int(seg_major), int(seg_minor)
    if nm < 3 or ns < 3:
        raise AttaError("bad_arg", "seg_major and seg_minor must be >= 3",
                        path="seg_major/seg_minor", fix="use at least 8 segments")
    a = _check_axis(axis)
    u = np.linspace(0.0, 2.0 * math.pi, nm, endpoint=False)
    w = np.linspace(0.0, 2.0 * math.pi, ns, endpoint=False)
    uu, ww = np.meshgrid(u, w, indexing="ij")
    rr = R + r * np.cos(ww)
    verts = np.column_stack([rr.ravel() * np.cos(uu).ravel(),
                             rr.ravel() * np.sin(uu).ravel(),
                             (r * np.sin(ww)).ravel()])
    faces = []
    for i in range(nm):
        i1 = (i + 1) % nm
        for j in range(ns):
            j1 = (j + 1) % ns
            a0 = i * ns + j
            b0 = i * ns + j1
            c0 = i1 * ns + j1
            d0 = i1 * ns + j
            faces += [[a0, b0, c0], [a0, c0, d0]]
    m = Mesh(verts, np.asarray(faces, dtype=np.int64), {"kind": "torus"})
    m = m.transform(translate=center, rotate_deg=_axis_rotation(a))
    return oriented(weld(m, 1e-9))
