# -*- coding: utf-8 -*-
"""Polylines, Catmull-Rom sampling, arc-length resampling, transport frames.

Units are metres; no angle input here.  All functions return ``(n, k)``
float64 arrays with the same dimension as their input (2D or 3D).
"""

from __future__ import annotations

import numpy as np

from .errors import AttaError

__all__ = ["polyline", "catmull_rom", "resample_arclength", "parallel_frames"]

_EPS = 1e-12


def _points(value, name="points"):
    p = np.asarray(value, dtype=np.float64)
    if p.ndim != 2 or p.shape[1] not in (2, 3) or len(p) < 2:
        raise AttaError("bad_arg", "%s must be an (n,2) or (n,3) array with n>=2" % name,
                        path=name, fix="pass a list of two or more coordinate pairs/triples")
    if not np.isfinite(p).all():
        raise AttaError("bad_arg", "%s contains NaN or Inf" % name, path=name,
                        fix="remove non-finite coordinates")
    return p


def _compress(p, closed=False):
    keep = [0]
    for i in range(1, len(p)):
        if np.linalg.norm(p[i] - p[keep[-1]]) > _EPS:
            keep.append(i)
    q = p[keep]
    if closed and len(q) > 1 and np.linalg.norm(q[0] - q[-1]) <= _EPS:
        q = q[:-1]
    return q


def polyline(points, closed=False) -> np.ndarray:
    """Return the points as an array; optionally append the first point."""
    p = _points(points)
    if closed and np.linalg.norm(p[0] - p[-1]) > _EPS:
        p = np.vstack([p, p[:1]])
    return np.ascontiguousarray(p, dtype=np.float64)


def catmull_rom(points, samples_per_seg=16, closed=False) -> np.ndarray:
    """Sample a Catmull-Rom spline through the points.

    A closed curve returns ``len(points) * samples_per_seg`` samples without
    a duplicated endpoint; an open curve returns ``(n-1)*spp + 1`` samples.
    """
    p = _compress(_points(points), closed=bool(closed))
    spp = int(samples_per_seg)
    if spp < 1:
        raise AttaError("bad_arg", "samples_per_seg must be >= 1", path="samples_per_seg",
                        fix="use samples_per_seg >= 1, e.g. 16")
    out = []
    if closed:
        if len(p) < 3:
            raise AttaError("bad_arg", "closed catmull_rom needs >= 3 distinct points",
                            path="points", fix="provide at least three distinct points")
        m = len(p)
        for i in range(m):
            p0, p1, p2, p3 = p[(i - 1) % m], p[i], p[(i + 1) % m], p[(i + 2) % m]
            for k in range(spp):
                t = k / float(spp)
                t2, t3 = t * t, t * t * t
                out.append(0.5 * ((2.0 * p1) + (-p0 + p2) * t
                                  + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                                  + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3))
        return np.asarray(out, dtype=np.float64)
    if len(p) < 2:
        raise AttaError("bad_arg", "catmull_rom needs >= 2 distinct points", path="points",
                        fix="provide at least two distinct points")
    ext = np.vstack([p[:1], p, p[-1:]])
    for i in range(len(p) - 1):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        for k in range(spp):
            t = k / float(spp)
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2.0 * p1) + (-p0 + p2) * t
                              + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                              + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3))
    out.append(p[-1])
    return np.asarray(out, dtype=np.float64)


def resample_arclength(points, n, closed=False) -> np.ndarray:
    """Resample a polyline to ``n`` points evenly spaced by arc length."""
    p = _compress(_points(points), closed=bool(closed))
    n = int(n)
    if n < 2:
        raise AttaError("bad_arg", "n must be >= 2", path="n", fix="use n >= 2")
    if len(p) < 2:
        raise AttaError("bad_arg", "resample needs >= 2 distinct points", path="points",
                        fix="provide at least two distinct points")
    q = np.vstack([p, p[:1]]) if closed else p
    seg = np.linalg.norm(np.diff(q, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= _EPS:
        raise AttaError("bad_arg", "polyline has zero length", path="points",
                        fix="provide points that are not all coincident")
    s = np.linspace(0.0, total, n, endpoint=not closed)
    out = np.empty((n, q.shape[1]), dtype=np.float64)
    for k in range(q.shape[1]):
        out[:, k] = np.interp(s, cum, q[:, k])
    return out


def parallel_frames(path, up=(0, 0, 1)):
    """Rotation-minimising frames along a 3D path.

    Returns ``(tangents, normals, binormals)``, each ``(n,3)`` and unit length,
    with ``binormal = tangent x normal``.  Consecutive duplicate points are an
    error because a tangent is undefined there.
    """
    p = np.asarray(path, dtype=np.float64)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2:
        raise AttaError("bad_arg", "path must be (n,3) with n>=2", path="path",
                        fix="pass a 3D polyline with at least two points")
    if not np.isfinite(p).all():
        raise AttaError("bad_arg", "path contains NaN or Inf", path="path",
                        fix="remove non-finite coordinates")
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    if np.any(seg <= _EPS):
        raise AttaError("bad_arg", "path has duplicate consecutive points", path="path",
                        fix="remove duplicate consecutive points before sweeping")
    t = np.gradient(p, axis=0)
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), _EPS)

    ref = np.asarray(up, dtype=np.float64).reshape(3)
    n0 = ref - t[0] * float(ref @ t[0])
    if np.linalg.norm(n0) <= 1e-9:
        alt = np.array([1.0, 0.0, 0.0]) if abs(t[0, 0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        n0 = alt - t[0] * float(alt @ t[0])
    n = np.empty_like(p)
    n[0] = n0 / np.linalg.norm(n0)
    for i in range(1, len(p)):
        axis = np.cross(t[i - 1], t[i])
        sn = float(np.linalg.norm(axis))
        cs = float(np.clip(t[i - 1] @ t[i], -1.0, 1.0))
        v = n[i - 1]
        if sn < 1e-12:
            n[i] = v
        else:
            axis /= sn
            ang = np.arctan2(sn, cs)
            n[i] = (v * np.cos(ang) + np.cross(axis, v) * np.sin(ang)
                    + axis * float(axis @ v) * (1.0 - np.cos(ang)))
        n[i] -= t[i] * float(n[i] @ t[i])
        ln = np.linalg.norm(n[i])
        if ln <= 1e-9:
            raise AttaError("bad_arg", "transport frame collapsed at index %d" % i,
                            path="path", fix="avoid a path that folds back on itself")
        n[i] /= ln
    b = np.cross(t, n)
    b /= np.maximum(np.linalg.norm(b, axis=1, keepdims=True), _EPS)
    return t, n, b
