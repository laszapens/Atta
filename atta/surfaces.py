# -*- coding: utf-8 -*-
"""Implicit sweep/revolve/loft surfaces and shelling.  Units: metres, degrees.

Closed solids: ``revolve`` (looped profile, or open profile with ``caps``),
``loft`` (``closed_ring`` and ``caps``), ``sweep`` (closed path, or open path
with ``caps``), ``extrude`` (with ``caps``), ``thicken``.

Open surfaces: ``coons`` and ``gordon`` return an open quad patch (a single
layer); they are not watertight until passed through ``thicken``.
"""

from __future__ import annotations

import math

import numpy as np

from . import curves as _curves
from .errors import AttaError
from .mesh import Mesh, oriented, topology, weld

__all__ = ["revolve", "loft", "sweep", "coons", "gordon", "thicken", "lobes",
           "extrude"]

_EPS = 1e-12


def _cross2(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _signed_area(p):
    return 0.5 * float(np.sum(p[:, 0] * np.roll(p[:, 1], -1)
                              - p[:, 1] * np.roll(p[:, 0], -1)))


def _point_in_tri(p, a, b, c, eps):
    d1 = _cross2(a, b, p)
    d2 = _cross2(b, c, p)
    d3 = _cross2(c, a, p)
    return ((d1 >= -eps and d2 >= -eps and d3 >= -eps)
            or (d1 <= eps and d2 <= eps and d3 <= eps))


def _earclip(points):
    """Ear-clip a 2D polygon; returns triangles wound CCW in that plane."""
    p = np.asarray(points, dtype=np.float64)
    if p.ndim != 2 or p.shape[1] != 2 or len(p) < 3:
        raise AttaError("bad_arg", "polygon must be (n>=3,2)", path="polygon",
                        fix="pass at least three 2D points")
    n = len(p)
    area = _signed_area(p)
    scale = max(float(np.linalg.norm(p.max(axis=0) - p.min(axis=0))), 1e-12)
    if abs(area) <= 1e-14 * scale * scale:
        raise AttaError("bad_arg", "polygon is degenerate (zero area)", path="polygon",
                        fix="remove duplicate or collinear points")
    idx = list(range(n)) if area > 0.0 else list(range(n))[::-1]
    eps = 1e-12 * scale * scale
    out, guard = [], 0
    while len(idx) > 3:
        guard += 1
        if guard > 4 * n * n:
            break
        found = False
        for pos in range(len(idx)):
            ia = idx[pos - 1]
            ib = idx[pos]
            ic = idx[(pos + 1) % len(idx)]
            if _cross2(p[ia], p[ib], p[ic]) <= eps:
                continue
            if any(_point_in_tri(p[j], p[ia], p[ib], p[ic], eps)
                   for j in idx if j not in (ia, ib, ic)):
                continue
            out.append((ia, ib, ic))
            del idx[pos]
            found = True
            break
        if not found:
            break
    if len(idx) != 3:
        raise AttaError("bad_arg", "polygon could not be triangulated", path="polygon",
                        fix="the loop is probably self-intersecting; simplify it")
    out.append((idx[0], idx[1], idx[2]))
    return out


def _polygon_normal(pts):
    p = np.asarray(pts, dtype=np.float64)
    n = np.zeros(3)
    for i in range(len(p)):
        n += np.cross(p[i], p[(i + 1) % len(p)])
    ln = float(np.linalg.norm(n))
    if ln <= _EPS:
        raise AttaError("bad_arg", "polygon normal is undefined", path="polygon",
                        fix="remove duplicate points or pass a non-degenerate ring")
    return n / ln


def _plane_coords(pts, normal):
    ref = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = ref - normal * float(ref @ normal)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    p = np.asarray(pts, dtype=np.float64)
    return np.column_stack([p @ u, p @ v])


def _triangulate_polygon(pts):
    pts = np.asarray(pts, dtype=np.float64)
    n = _polygon_normal(pts)
    return _earclip(_plane_coords(pts, n))


def _cap_tris(ring, outward):
    tris = _triangulate_polygon(ring)
    if float(_polygon_normal(ring) @ np.asarray(outward, dtype=np.float64)) < 0.0:
        tris = [(a, c, b) for a, b, c in tris]
    return tris


def _compress_profile(prof, tol):
    keep = [0]
    for i in range(1, len(prof)):
        if np.linalg.norm(prof[i] - prof[keep[-1]]) > tol:
            keep.append(i)
    return prof[keep]


def revolve(profile_rz, segments=96, axis="z", caps=True) -> Mesh:
    """Revolve an ``(r, z)`` profile about +Z.

    A profile whose first and last points coincide is treated as a closed
    cross-section (thin-wall cup/vase loops).  An open profile is revolved as
    a strip and, with ``caps=True``, closed by prepending/appending axis
    points, which produces flat end discs.  ``caps=False`` returns an open
    surface.  ``r <= 0`` points collapse to a single pole vertex.
    """
    prof = np.asarray(profile_rz, dtype=np.float64)
    if prof.ndim != 2 or prof.shape[1] != 2 or len(prof) < 2:
        raise AttaError("bad_arg", "profile_rz must be (n>=2,2) as [r, z]", path="profile_rz",
                        fix="pass a list of [radius, z] pairs in metres")
    if not np.isfinite(prof).all():
        raise AttaError("bad_arg", "profile_rz contains NaN or Inf", path="profile_rz",
                        fix="remove non-finite coordinates")
    if np.any(prof[:, 0] < -1e-9):
        raise AttaError("bad_arg", "profile radius must be >= 0", path="profile_rz",
                        fix="clip negative radii to 0")
    prof = prof.copy()
    prof[:, 0] = np.maximum(prof[:, 0], 0.0)
    seg = int(segments)
    if seg < 3:
        raise AttaError("bad_arg", "segments must be >= 3", path="segments",
                        fix="use segments >= 8")
    ax = str(axis).lower()
    if ax not in ("x", "y", "z"):
        raise AttaError("bad_arg", "axis must be 'x', 'y' or 'z'", path="axis",
                        fix="use axis='z'")

    tol = max(1e-12, 1e-9 * float(np.max(prof[:, 0])))
    looped = len(prof) >= 3 and np.linalg.norm(prof[0] - prof[-1]) <= tol
    if looped:
        prof = prof[:-1]
    prof = _compress_profile(prof, tol)
    if not looped and caps:
        if prof[0, 0] > tol:
            prof = np.vstack([[0.0, prof[0, 1]], prof])
        if prof[-1, 0] > tol:
            prof = np.vstack([prof, [0.0, prof[-1, 1]]])
        prof = _compress_profile(prof, tol)
    closed = bool(looped or caps)

    verts, rows = [], []
    thetas = np.linspace(0.0, 2.0 * math.pi, seg, endpoint=False)
    ct, st = np.cos(thetas), np.sin(thetas)
    for r, z in prof:
        if r <= tol:
            rows.append((True, len(verts)))
            verts.append([0.0, 0.0, float(z)])
        else:
            base = len(verts)
            verts.extend(np.column_stack([r * ct, r * st, np.full(seg, float(z))]))
            rows.append((False, base))

    def v(row, k):
        return row[1] if row[0] else row[1] + k

    faces = []
    pairs = [(i, (i + 1) % len(rows)) for i in range(len(rows))] if closed \
        else [(i, i + 1) for i in range(len(rows) - 1)]
    for ia, ib in pairs:
        ra, rb = rows[ia], rows[ib]
        if ra[0] and rb[0]:
            continue
        for k in range(seg):
            k1 = (k + 1) % seg
            if ra[0]:
                faces.append((v(ra, k), v(rb, k1), v(rb, k)))
            elif rb[0]:
                faces.append((v(ra, k), v(ra, k1), v(rb, k)))
            else:
                faces += [(v(ra, k), v(ra, k1), v(rb, k1)),
                          (v(ra, k), v(rb, k1), v(rb, k))]
    mesh = Mesh(np.asarray(verts, dtype=np.float64),
                np.asarray(faces, dtype=np.int64), {"kind": "revolve"})
    mesh = weld(mesh, 1e-9)
    if closed:
        mesh = oriented(mesh)
    if ax != "z":
        rot = (0.0, 90.0, 0.0) if ax == "x" else (-90.0, 0.0, 0.0)
        mesh = mesh.transform(rotate_deg=rot)
    return mesh


def loft(rings, closed_ring=True, caps=True) -> Mesh:
    """Loft between ``rings`` (each an ``(n,3)`` loop with the same ``n``).

    With ``closed_ring=True`` and ``caps=True`` the result is watertight.
    """
    rs = [np.asarray(r, dtype=np.float64) for r in rings]
    if len(rs) < 2:
        raise AttaError("bad_arg", "loft needs >= 2 rings", path="rings",
                        fix="provide at least two rings")
    n = len(rs[0])
    if n < 3 or any(r.ndim != 2 or r.shape != (n, 3) for r in rs):
        raise AttaError("bad_arg", "every ring must be (n,3) with the same n>=3",
                        path="rings", fix="pass equally sized [x,y,z] rings")
    if not all(np.isfinite(r).all() for r in rs):
        raise AttaError("bad_arg", "rings contain NaN or Inf", path="rings",
                        fix="remove non-finite coordinates")
    m = len(rs)
    verts = np.vstack(rs)
    faces = []
    span = n if closed_ring else n - 1
    for i in range(m - 1):
        for k in range(span):
            k1 = (k + 1) % n
            a = i * n + k
            b = i * n + k1
            c = (i + 1) * n + k1
            d = (i + 1) * n + k
            faces += [[a, b, c], [a, c, d]]
    if caps and closed_ring:
        c0 = rs[0].mean(axis=0)
        c1 = rs[1].mean(axis=0)
        for base, ring, outward in ((0, rs[0], c0 - c1),
                                    ((m - 1) * n, rs[-1], rs[-1].mean(axis=0) - rs[-2].mean(axis=0))):
            for a, b, c in _cap_tris(ring, outward):
                faces.append([base + a, base + b, base + c])
    mesh = Mesh(verts, np.asarray(faces, dtype=np.int64), {"kind": "loft"})
    mesh = weld(mesh, 1e-9)
    if closed_ring and caps:
        mesh = oriented(mesh)
    return mesh


def sweep(section_xy, path, closed=False, twist_deg=0.0, scale_end=1.0) -> Mesh:
    """Sweep a closed 2D section along a 3D path using transport frames.

    A closed path produces a closed tube (no caps); an open path is capped
    when ``scale_end`` stays positive so the end rings remain non-degenerate.
    """
    loop = np.asarray(section_xy, dtype=np.float64)
    if loop.ndim != 2 or loop.shape[1] != 2 or len(loop) < 3:
        raise AttaError("bad_arg", "section_xy must be (n>=3,2)", path="section_xy",
                        fix="pass a closed 2D section with at least three points")
    if not np.isfinite(loop).all():
        raise AttaError("bad_arg", "section_xy contains NaN or Inf", path="section_xy",
                        fix="remove non-finite coordinates")
    se = float(scale_end)
    if not math.isfinite(se) or se <= 0.0:
        raise AttaError("bad_arg", "scale_end must be finite and > 0", path="scale_end",
                        fix="use scale_end=1.0 for a constant section")
    p = np.asarray(path, dtype=np.float64)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2:
        raise AttaError("bad_arg", "path must be (n>=2,3)", path="path",
                        fix="pass a 3D polyline with at least two points")
    if closed and len(p) > 2 and np.linalg.norm(p[0] - p[-1]) <= 1e-12:
        p = p[:-1]
    if closed and len(p) < 3:
        raise AttaError("bad_arg", "closed sweep needs >= 3 path points", path="path",
                        fix="provide at least three points for a closed path")
    probe = np.vstack([p, p[:1]]) if closed else p
    t, nn, bb = _curves.parallel_frames(probe)
    if closed:
        t, nn, bb = t[:-1], nn[:-1], bb[:-1]
    m = len(p)
    frac = np.arange(m) / (float(m) if closed else float(max(1, m - 1)))
    tw = math.radians(float(twist_deg))
    rings = []
    for i in range(m):
        s = 1.0 + (se - 1.0) * float(frac[i])
        a = tw * float(frac[i])
        ca, sa = math.cos(a), math.sin(a)
        x = loop[:, 0] * ca - loop[:, 1] * sa
        y = loop[:, 0] * sa + loop[:, 1] * ca
        rings.append(p[i] + (s * x)[:, None] * nn[i] + (s * y)[:, None] * bb[i])
    n = len(loop)
    verts = np.concatenate(rings, axis=0)
    faces = []
    band = m if closed else m - 1
    for i in range(band):
        i1 = (i + 1) % m
        for k in range(n):
            k1 = (k + 1) % n
            a = i * n + k
            b = i * n + k1
            c = i1 * n + k1
            d = i1 * n + k
            faces += [[a, b, c], [a, c, d]]
    if not closed:
        tris = _earclip(loop)
        for a, b, c in tris:
            faces.append([a, c, b])
        base = (m - 1) * n
        for a, b, c in tris:
            faces.append([base + a, base + b, base + c])
    mesh = Mesh(verts, np.asarray(faces, dtype=np.int64), {"kind": "sweep"})
    mesh = weld(mesh, 1e-9)
    mesh = oriented(mesh)
    return mesh


def _resample_curve(points, n, name="curve"):
    p = np.asarray(points, dtype=np.float64)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2:
        raise AttaError("bad_arg", "%s must be (n>=2,3)" % name, path=name,
                        fix="pass a 3D polyline")
    if len(p) == n:
        return p.copy()
    return _curves.resample_arclength(p, n, closed=False)


def _quad_faces(nu, nv, flip=False):
    faces = []
    for i in range(int(nu)):
        for j in range(int(nv)):
            a = i * (nv + 1) + j
            b = (i + 1) * (nv + 1) + j
            c = (i + 1) * (nv + 1) + (j + 1)
            d = i * (nv + 1) + (j + 1)
            faces += [[d, c, b], [d, b, a]] if flip else [[a, b, c], [a, c, d]]
    return faces


def coons(edges, u_samples=32, v_samples=32, flip=False) -> Mesh:
    """Bilinear Coons patch from ``edges=(bottom, right, top, left)``.

    The four curves are expected to run around the patch
    (bottom: c00->c10, right: c10->c11, top: c11->c01, left: c01->c00).
    Returns an **open** single-layer surface; call ``thicken`` for a solid.
    """
    if not isinstance(edges, (list, tuple)) or len(edges) != 4:
        raise AttaError("bad_arg", "coons needs exactly 4 boundary curves",
                        path="edges", fix="pass (bottom, right, top, left) curves")
    nu, nv = max(2, int(u_samples)), max(2, int(v_samples))
    bottom = _resample_curve(edges[0], nu + 1, "edges[0]")
    right = _resample_curve(edges[1], nv + 1, "edges[1]")
    top = _resample_curve(edges[2], nu + 1, "edges[2]")
    left = _resample_curve(edges[3], nv + 1, "edges[3]")
    c0 = bottom.copy()
    c1 = top[::-1].copy()
    d0 = left[::-1].copy()
    d1 = right.copy()
    p00 = 0.5 * (c0[0] + d0[0])
    p10 = 0.5 * (c0[-1] + d1[0])
    p01 = 0.5 * (c1[0] + d0[-1])
    p11 = 0.5 * (c1[-1] + d1[-1])
    c0[0], c0[-1] = p00, p10
    d0[0], d0[-1] = p00, p01
    c1[0], c1[-1] = p01, p11
    d1[0], d1[-1] = p10, p11

    s = np.linspace(0.0, 1.0, nu + 1)
    t = np.linspace(0.0, 1.0, nv + 1)
    ss, tt = s[:, None, None], t[None, :, None]
    surf = ((1.0 - tt) * c0[:, None, :] + tt * c1[:, None, :]
            + (1.0 - ss) * d0[None, :, :] + ss * d1[None, :, :]
            - ((1.0 - ss) * (1.0 - tt)) * p00
            - (ss * (1.0 - tt)) * p10
            - ((1.0 - ss) * tt) * p01
            - (ss * tt) * p11)
    return Mesh(surf.reshape(-1, 3), np.asarray(_quad_faces(nu, nv, bool(flip)),
                                                dtype=np.int64),
                {"kind": "coons", "open": True})


def _hats(nodes, xs):
    w = np.zeros((len(nodes), len(xs)))
    for k, x in enumerate(xs):
        if x <= nodes[0]:
            w[0, k] = 1.0
        elif x >= nodes[-1]:
            w[-1, k] = 1.0
        else:
            j = int(np.searchsorted(nodes, x) - 1)
            j = max(0, min(j, len(nodes) - 2))
            u = (x - nodes[j]) / (nodes[j + 1] - nodes[j])
            w[j, k] = 1.0 - u
            w[j + 1, k] = u
    return w


def gordon(u_curves, v_curves, u_samples=32, v_samples=32, flip=False) -> Mesh:
    """Transfinite (Gordon) patch through two curve families.

    ``u_curves`` run along u at fixed v; ``v_curves`` run along v at fixed u.
    Returns an **open** single-layer surface; call ``thicken`` for a solid.
    """
    if len(u_curves) < 2 or len(v_curves) < 2:
        raise AttaError("bad_arg", "gordon needs >= 2 u curves and >= 2 v curves",
                        path="u_curves/v_curves", fix="provide at least two curves per family")
    nu, nv = max(2, int(u_samples)), max(2, int(v_samples))
    m, n = len(u_curves), len(v_curves)
    C = np.stack([_resample_curve(c, nu + 1, "u_curves[%d]" % i)
                  for i, c in enumerate(u_curves)], axis=0)
    D = np.stack([_resample_curve(c, nv + 1, "v_curves[%d]" % i)
                  for i, c in enumerate(v_curves)], axis=0)
    beta = np.linspace(0.0, 1.0, m)
    alpha = np.linspace(0.0, 1.0, n)
    us = np.linspace(0.0, 1.0, nu + 1)
    vs = np.linspace(0.0, 1.0, nv + 1)
    L = _hats(beta, vs)
    M = _hats(alpha, us)
    P = np.empty((m, n, 3))
    for c in range(m):
        for d in range(n):
            pu = np.array([np.interp(alpha[d], us, C[c, :, k]) for k in range(3)])
            pv = np.array([np.interp(beta[c], vs, D[d, :, k]) for k in range(3)])
            P[c, d] = 0.5 * (pu + pv)
    t1 = np.einsum("cj,cik->ijk", L, C)
    t2 = np.einsum("di,djk->ijk", M, D)
    t3 = np.einsum("cj,di,cdk->ijk", L, M, P)
    surf = t1 + t2 - t3
    return Mesh(surf.reshape(-1, 3), np.asarray(_quad_faces(nu, nv, bool(flip)),
                                                dtype=np.int64),
                {"kind": "gordon", "open": True})


def _vertex_normals(verts, tris):
    vn = np.zeros_like(verts)
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    fn = np.cross(b - a, c - a)
    for k in range(3):
        np.add.at(vn, tris[:, k], fn)
    ln = np.linalg.norm(vn, axis=1)
    good = ln > _EPS
    vn[good] /= ln[good, None]
    vn[~good] = (0.0, 0.0, 1.0)
    return vn


def thicken(surface, thickness, offset=0.0) -> Mesh:
    """Offset an **open** surface along its normals into a closed shell.

    ``offset`` in ``[-1, 1]``: 0 centres the shell, 1 puts all material on the
    +normal side, -1 on the -normal side.  Boundary edges become side walls.
    """
    if not isinstance(surface, Mesh):
        raise AttaError("bad_arg", "surface must be a Mesh", path="surface",
                        fix="pass a Mesh produced by coons/gordon")
    t = float(thickness)
    if not math.isfinite(t) or t <= 0.0:
        raise AttaError("bad_arg", "thickness must be finite and > 0", path="thickness",
                        fix="pass a positive thickness in metres")
    if topology(surface)["closed"]:
        raise AttaError("bad_arg", "thicken needs an open surface", path="surface",
                        fix="use boolean shells for closed solids")
    off = max(-1.0, min(1.0, float(offset)))
    verts = surface.verts
    faces = surface.faces
    if len(faces) == 0:
        raise AttaError("bad_arg", "surface has no faces", path="surface",
                        fix="pass a tessellated surface")
    vn = _vertex_normals(verts, faces)
    half = 0.5 * t
    top = verts + vn * (half * (1.0 + off))
    bot = verts + vn * (-half * (1.0 - off))
    n = len(verts)
    all_verts = np.vstack([top, bot])
    out_faces = [[int(a), int(b), int(c)] for a, b, c in faces]
    out_faces += [[n + int(c), n + int(b), n + int(a)] for a, b, c in faces]

    count, directed = {}, {}
    for f in faces:
        for a, b in ((int(f[0]), int(f[1])), (int(f[1]), int(f[2])), (int(f[2]), int(f[0]))):
            key = (a, b) if a < b else (b, a)
            count[key] = count.get(key, 0) + 1
            directed.setdefault(key, (a, b))
    boundary = [(a, b) for key, (a, b) in directed.items() if count[key] == 1]
    if not boundary:
        raise AttaError("bad_arg", "surface has no boundary; cannot thicken", path="surface",
                        fix="pass an open patch (coons/gordon)")
    for a, b in boundary:
        out_faces += [[b, a, n + a], [b, n + a, n + b]]
    mesh = Mesh(all_verts, np.asarray(out_faces, dtype=np.int64),
                {"kind": "thicken", "thickness": t, "offset": off})
    mesh = oriented(weld(mesh, 1e-9))
    if not topology(mesh)["closed"]:
        raise AttaError("thicken_failed", "thickened shell is not closed", path="surface",
                        fix="reduce thickness or increase sampling of the patch")
    return mesh


def lobes(mesh, count=12, amplitude=0.03, phase_deg=0.0, twist_deg=0.0) -> Mesh:
    """Radial modulation ``r *= 1 + amplitude * cos(k*theta + phase + twist*t)``.

    ``t`` is normalised height along +Z.  ``|amplitude| < 1`` keeps the solid
    from folding through the axis.
    """
    k = int(count)
    amp = float(amplitude)
    if abs(amp) >= 1.0:
        raise AttaError("bad_arg", "amplitude must satisfy |amplitude| < 1", path="amplitude",
                        fix="use amplitude in (-0.5, 0.5)")
    if k <= 0 or abs(amp) <= _EPS:
        return mesh.copy()
    v = mesh.verts.copy()
    z = v[:, 2]
    span = float(z.max() - z.min())
    if span <= _EPS:
        return mesh.copy()
    theta = np.arctan2(v[:, 1], v[:, 0])
    tt = (z - z.min()) / span
    phase = math.radians(float(phase_deg))
    twist = math.radians(float(twist_deg))
    scale = 1.0 + amp * np.cos(k * theta + phase + twist * tt)
    v[:, 0] *= scale
    v[:, 1] *= scale
    return Mesh(v, mesh.faces.copy(), dict(mesh.meta))


def _direction_frame(direction):
    d = np.asarray(direction, dtype=np.float64).reshape(-1)
    ln = np.linalg.norm(d)
    if d.shape != (3,) or ln <= _EPS:
        raise AttaError("bad_arg", "direction must be a non-zero 3-vector", path="direction",
                        fix="use direction=[0,0,1]")
    d = d / ln
    ref = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    e1 = ref - d * float(ref @ d)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)
    return d, e1, e2


def extrude(loop_xy, height, direction=(0.0, 0.0, 1.0), caps=True,
            holes_xy=None) -> Mesh:
    """Extrude a closed 2D loop along ``direction``; capped is a closed solid.

    ``holes_xy`` is a list of inner loops in the same 2D frame.  Each hole is
    cut by subtracting a slightly over-long prism, which keeps the cap
    triangulation simple and makes the result a genuine through-hole (genus
    increases by one per hole) without a polygon-with-holes triangulator.
    """
    loop = np.asarray(loop_xy, dtype=np.float64)
    if loop.ndim != 2 or loop.shape[1] != 2 or len(loop) < 3:
        raise AttaError("bad_arg", "loop_xy must be (n>=3,2)", path="loop_xy",
                        fix="pass a closed 2D polygon")
    if not np.isfinite(loop).all():
        raise AttaError("bad_arg", "loop_xy contains NaN or Inf", path="loop_xy",
                        fix="remove non-finite coordinates")
    holes = []
    for i, raw in enumerate(holes_xy or []):
        hole = np.asarray(raw, dtype=np.float64)
        if hole.ndim != 2 or hole.shape[1] != 2 or len(hole) < 3:
            raise AttaError("bad_arg", "holes_xy[%d] must be (n>=3,2)" % i,
                            path="holes_xy",
                            fix="pass a closed 2D polygon for every hole")
        if not np.isfinite(hole).all():
            raise AttaError("bad_arg", "holes_xy[%d] contains NaN or Inf" % i,
                            path="holes_xy", fix="remove non-finite coordinates")
        holes.append(hole)
    if holes and not caps:
        raise AttaError("bad_arg", "holes_xy needs caps=True", path="holes_xy",
                        fix="extruded through-holes are only defined for a capped solid")
    h = float(height)
    if not math.isfinite(h) or h <= 0.0:
        raise AttaError("bad_arg", "height must be finite and > 0", path="height",
                        fix="pass a positive height in metres")
    loop = loop.copy()
    if _signed_area(loop) < 0.0:
        loop = loop[::-1]
    d, e1, e2 = _direction_frame(direction)
    base = loop[:, 0][:, None] * e1 + loop[:, 1][:, None] * e2
    top = base + d * h
    n = len(loop)
    verts = np.vstack([base, top])
    faces = []
    for k in range(n):
        k1 = (k + 1) % n
        faces += [[k, k1, n + k1], [k, n + k1, n + k]]
    if caps:
        tris = _earclip(loop)
        for a, b, c in tris:
            faces.append([c, b, a])
        for a, b, c in tris:
            faces.append([n + a, n + b, n + c])
    mesh = Mesh(verts, np.asarray(faces, dtype=np.int64), {"kind": "extrude"})
    mesh = weld(mesh, 1e-9)
    if caps:
        mesh = oriented(mesh)
    if holes:
        # A hole prism is over-long on both ends so the boolean has a clean
        # through cut and no coplanar cap faces.
        from .boolean import difference
        margin = max(0.02 * h, 1e-4)
        for hole in holes:
            cutter = extrude(hole, h + 2.0 * margin, direction=direction,
                             caps=True)
            cutter = cutter.transform(translate=tuple(-d * margin))
            mesh = difference(mesh, cutter)
            mesh = oriented(mesh)
    return mesh
