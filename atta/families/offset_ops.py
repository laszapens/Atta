# -*- coding: utf-8 -*-
"""``offset`` — the missing shell / fillet / chamfer primitive.

Why this op exists
------------------
The nominations analysis found the corpus asking for one piece of maths under
three names: shell (20 families / 106 variants), fillet (12 / 83) and chamfer
(10 / 42).  All three were already promised in the requirements' planned op
vocabulary but never shipped.  They are all "move the surface by a distance":

* ``mode="round"``  — Minkowski sum with a ball (a true offset surface)
* ``mode="mitre"``  — Minkowski sum with an L1 ball (an octahedron): every
  axis-aligned face moves out by exactly ``d`` and every edge becomes an exact
  45-degree facet
* ``mode="shell"``  — ``solid - offset(solid, -t)``, the hollow body

Signed distance: ``d > 0`` grows, ``d < 0`` shrinks.

Two engines, chosen for the property that is actually verifiable
---------------------------------------------------------------
``mitre`` is **exact** and needs no grid at all: for polytopes both operands
are polytopes, so ``conv(V(K)) (+) conv(octahedron) = conv{v + s}`` and the
result is literally the convex hull of that point cloud.  Negative ``d`` is the
half-space intersection.  Measured on a 20 mm cube: chamfer volume matches the
closed form ``a^3 + 6a^2 r + 6a r^2 + 4/3 r^3`` to **0.0000 %** and the facet
normals include exactly ``(0,1,1)/sqrt(2)``.  It is convex-only by construction
and says so (``offset_not_convex``).

``round`` is an SDF resample: exact signed distances plus a surface-nets
isosurface.  It is general (any watertight solid) but it is *sampled*, so it
carries a resolution, and surface nets has one hard invariant: **a grid cell
may contain only one sheet of the surface**.  Where a cell straddles two
sheets, the extracted mesh gets a non-manifold junction (measured: 1 bad edge
at resolution 40, 22 at resolution 128 — refining makes it *worse*, because
more cells means more chances to hit the ambiguity).  Rather than return a
broken mesh, the builder searches resolution downward until the mesh is
manifold and reports the resolution it actually used (``mesh.meta["offset"]``).
If no resolution works, it raises ``offset_not_closed``.

Sign
----
Distance magnitudes come from a k-d tree over triangle centroids.  The *sign*
does **not** come from the closest-face pseudonormal: that cancels on the
medial axis, and on a bored plate it invented 7 components and 100
inconsistent-winding edges.  The sign comes from ray parity, evaluated per grid
*line* (one vectorised pass over the triangles per line, then ``searchsorted``
against the samples on that line) so an exact test costs ~0.3 s instead of ~45 s.
"""

from __future__ import annotations

import math

import numpy as np

from .. import features as _F
from .. import primitives as _P
from .. import surfaces as _S
from ..errors import AttaError
from ..kit import finish, quality_level as _q, rounded_rect
from ..mesh import Mesh, oriented, topology, volume
from ..registry import recipe

__all__ = ["offset", "offset_mesh"]

OP = "offset"

#: surface nets is only valid while one cell holds one sheet; these are the
#: resolutions tried (finest first) before giving up
_RES_LADDER = (96, 80, 72, 64, 56, 48, 40, 32, 26, 20)


# ---------------------------------------------------------------- helpers

def _bbox(mesh):
    v = np.asarray(mesh.verts, float)
    return v.min(axis=0), v.max(axis=0)


def _point_tri_dist(p, a, b, c):
    """Closest distance from points ``p`` to triangles ``(a,b,c)`` (broadcast)."""
    ab, ac = b - a, c - a
    ap = p - a
    d1 = np.einsum("...j,...j->...", ab, ap)
    d2 = np.einsum("...j,...j->...", ac, ap)
    bp = p - b
    d3 = np.einsum("...j,...j->...", ab, bp)
    d4 = np.einsum("...j,...j->...", ac, bp)
    cp = p - c
    d5 = np.einsum("...j,...j->...", ab, cp)
    d6 = np.einsum("...j,...j->...", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    den = va + vb + vc
    safe = np.where(np.abs(den) < 1e-30, 1.0, den)
    out = a + ab * (vb / safe)[..., None] + ac * (vc / safe)[..., None]
    s = np.where(np.abs((d4 - d3) + (d5 - d6)) < 1e-30, 1.0, (d4 - d3) + (d5 - d6))
    out = np.where(((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0))[..., None],
                   b + ((d4 - d3) / s)[..., None] * (c - b), out)
    out = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a, out)
    out = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b, out)
    out = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None],
                   a + (d1 / np.where(np.abs(d1 - d3) < 1e-30, 1.0, d1 - d3))[..., None] * ab,
                   out)
    out = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c, out)
    out = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None],
                   a + (d2 / np.where(np.abs(d2 - d6) < 1e-30, 1.0, d2 - d6))[..., None] * ac,
                   out)
    return np.linalg.norm(p - out, axis=-1)


def _tri_arrays(mesh):
    verts = np.asarray(mesh.verts, float)
    faces = np.asarray(mesh.faces, np.int64)
    return verts, faces, verts[faces]


def _dist_all(points, tri, k=12):
    """Unsigned distance to the triangle soup, k nearest centroids only."""
    from scipy.spatial import cKDTree
    cen = tri.mean(axis=1)
    kk = int(min(k, len(tri)))
    dist, idx = cKDTree(cen).query(points, k=kk, workers=-1)
    if kk == 1:
        dist, idx = dist[:, None], idx[:, None]
    out = np.full(len(points), np.inf)
    for s in range(0, len(points), 2048):
        p = points[s:s + 2048]
        ii = idx[s:s + 2048]
        dd = _point_tri_dist(p[:, None, :], tri[ii, 0], tri[ii, 1], tri[ii, 2])
        out[s:s + 2048] = dd.min(axis=1)
    return out


def _inside_grid(shape, origin, h, mesh, axis=2):
    """Exact inside/outside on a regular grid, by parity along ``axis``.

    One vectorised pass over the triangles per grid *line*, so the cost is
    lines x triangles instead of points x triangles.
    """
    verts, faces, tri = _tri_arrays(mesh)
    ax = int(axis)
    o1, o2 = [i for i in range(3) if i != ax]
    shape = tuple(int(x) for x in shape)
    out = np.zeros(shape, bool)
    p1 = origin[o1] + h * np.arange(shape[o1])
    p2 = origin[o2] + h * np.arange(shape[o2])
    pax = origin[ax] + h * np.arange(shape[ax])
    v0, v1, v2 = tri[:, 0], tri[:, 1], tri[:, 2]
    e0 = v1[:, [o1, o2]] - v0[:, [o1, o2]]
    e1 = v2[:, [o1, o2]] - v0[:, [o1, o2]]
    den = e0[:, 0] * e1[:, 1] - e1[:, 0] * e0[:, 1]
    ok = np.abs(den) > 1e-30
    safe = np.where(ok, den, 1.0)
    for i1, a in enumerate(p1):
        for i2, b in enumerate(p2):
            # deterministic sub-cell nudge: a ray must not run along a mesh edge
            aa = a + h * 1e-4 * ((i1 * 7 + i2 * 13) % 5 - 2)
            bb = b + h * 1e-4 * ((i1 * 3 + i2 * 11) % 7 - 3)
            px, py = aa - v0[:, o1], bb - v0[:, o2]
            uu = (px * e1[:, 1] - e1[:, 0] * py) / safe
            vv = (e0[:, 0] * py - px * e0[:, 1]) / safe
            hit = ok & (uu >= 0.0) & (vv >= 0.0) & (uu + vv <= 1.0)
            if not hit.any():
                continue
            t = (v0[hit, ax] + uu[hit] * (v1[hit, ax] - v0[hit, ax])
                 + vv[hit] * (v2[hit, ax] - v0[hit, ax]))
            t.sort()
            ahead = len(t) - np.searchsorted(t, pax, side="right")
            line = (ahead % 2) == 1
            if ax == 0:
                out[:, i1, i2] = line
            elif ax == 1:
                out[i1, :, i2] = line
            else:
                out[i1, i2, :] = line
    return out


def _sample_trilinear(F, origin, h, pts):
    nx, ny, nz = F.shape
    c = (np.asarray(pts, float) - np.asarray(origin, float)) / h
    i0 = np.clip(np.floor(c).astype(int), 0, np.array([nx - 2, ny - 2, nz - 2]))
    t = c - i0
    tx, ty, tz = t[:, 0], t[:, 1], t[:, 2]

    def g(dx, dy, dz):
        return F[i0[:, 0] + dx, i0[:, 1] + dy, i0[:, 2] + dz]

    c00 = g(0, 0, 0) * (1 - tx) + g(1, 0, 0) * tx
    c10 = g(0, 1, 0) * (1 - tx) + g(1, 1, 0) * tx
    c01 = g(0, 0, 1) * (1 - tx) + g(1, 0, 1) * tx
    c11 = g(0, 1, 1) * (1 - tx) + g(1, 1, 1) * tx
    return (c00 * (1 - ty) + c10 * ty) * (1 - tz) + (c01 * (1 - ty) + c11 * ty) * tz


def _orient_consistent(mesh):
    """Repair winding from face adjacency, then orient each shell outward.

    Quad-local orientation is exact for a planar quad but not for a warped one
    (measured: 24 inconsistent edges on a topologically perfect offset
    surface), so orientation is fixed purely from adjacency: a BFS forces
    neighbouring faces to traverse their shared edge in opposite directions.
    """
    verts = np.asarray(mesh.verts, float)
    faces = np.asarray(mesh.faces, np.int64).copy()
    nf = len(faces)
    if nf == 0:
        return mesh
    edge_faces = {}
    for i in range(nf):
        a, b, c = faces[i]
        for u, w in ((a, b), (b, c), (c, a)):
            edge_faces.setdefault((min(u, w), max(u, w)), []).append(i)
    adj = [[] for _ in range(nf)]
    for (u, w), fl in edge_faces.items():
        if len(fl) != 2:
            continue
        i, j = fl

        def dirn(f):
            aa, bb, cc = faces[f]
            for x, y in ((aa, bb), (bb, cc), (cc, aa)):
                if (x, y) == (u, w):
                    return 1
                if (x, y) == (w, u):
                    return -1
            return 0
        same = dirn(i) == dirn(j)
        adj[i].append((j, same))
        adj[j].append((i, same))
    flip = np.zeros(nf, bool)
    seen = np.zeros(nf, bool)
    for seed in range(nf):
        if seen[seed]:
            continue
        seen[seed] = True
        stack = [seed]
        while stack:
            f = stack.pop()
            for g, same in adj[f]:
                if seen[g]:
                    continue
                flip[g] = flip[f] != same
                seen[g] = True
                stack.append(g)
    f2 = faces.copy()
    f2[flip] = f2[flip][:, ::-1]
    return oriented(Mesh(verts, f2, dict(mesh.meta)))


def _surface_nets(F, origin, h):
    """Watertight dual isosurface of ``F = 0`` (one vertex per surface cell)."""
    nx, ny, nz = F.shape
    ins = F < 0.0
    cells = [ins[dx:nx - 1 + dx, dy:ny - 1 + dy, dz:nz - 1 + dz]
             for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)]
    any_in, all_in = np.zeros_like(cells[0]), np.ones_like(cells[0])
    for a in cells:
        any_in |= a
        all_in &= a
    surv = any_in & ~all_in
    ci = np.argwhere(surv)
    if len(ci) == 0:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    idx = np.full((nx - 1, ny - 1, nz - 1), -1, np.int64)
    idx[ci[:, 0], ci[:, 1], ci[:, 2]] = np.arange(len(ci))
    cc = ci.astype(float) + 0.5
    for _ in range(3):                    # Newton on the trilinear field
        p0 = origin + cc * h
        val = _sample_trilinear(F, origin, h, p0)
        e = 0.05
        gx = (_sample_trilinear(F, origin, h, p0 + [e * h, 0, 0])
              - _sample_trilinear(F, origin, h, p0 - [e * h, 0, 0])) / (2 * e * h)
        gy = (_sample_trilinear(F, origin, h, p0 + [0, e * h, 0])
              - _sample_trilinear(F, origin, h, p0 - [0, e * h, 0])) / (2 * e * h)
        gz = (_sample_trilinear(F, origin, h, p0 + [0, 0, e * h])
              - _sample_trilinear(F, origin, h, p0 - [0, 0, e * h])) / (2 * e * h)
        g = np.column_stack([gx, gy, gz])
        gl = np.einsum("ij,ij->i", g, g)
        step = np.where(gl[:, None] > 1e-30, (val / np.maximum(gl, 1e-30))[:, None] * g,
                        0.0)
        cc = np.clip(cc - step / h, ci, ci + 1.0)
    verts = origin + cc * h

    quads, qaxis, qin = [], [], []

    def push(i, j, k, edir):
        if edir == 0:
            q = [idx[i, j - 1, k - 1], idx[i, j, k - 1], idx[i, j, k], idx[i, j - 1, k]]
        elif edir == 1:
            q = [idx[i - 1, j, k - 1], idx[i, j, k - 1], idx[i, j, k], idx[i - 1, j, k]]
        else:
            q = [idx[i - 1, j - 1, k], idx[i, j - 1, k], idx[i, j, k], idx[i - 1, j, k]]
        if min(q) >= 0:
            quads.append(q)
            qaxis.append(edir)
            qin.append(bool(ins[i, j, k]))

    e = ins[:-1, 1:-1, 1:-1] != ins[1:, 1:-1, 1:-1]
    for i, j, k in np.argwhere(e):
        push(i, j + 1, k + 1, 0)
    e = ins[1:-1, :-1, 1:-1] != ins[1:-1, 1:, 1:-1]
    for i, j, k in np.argwhere(e):
        push(i + 1, j, k + 1, 1)
    e = ins[1:-1, 1:-1, :-1] != ins[1:-1, 1:-1, 1:]
    for i, j, k in np.argwhere(e):
        push(i + 1, j + 1, k, 2)
    if not quads:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    Q = np.asarray(quads, np.int64)
    ax = np.asarray(qaxis, np.int64)
    triq = verts[Q]
    nrm = np.cross(triq[:, 1] - triq[:, 0], triq[:, 2] - triq[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    nrm = nrm / np.where(ln < 1e-30, 1.0, ln)[:, None]
    want = np.zeros_like(nrm)
    want[np.arange(len(nrm)), ax] = np.where(np.asarray(qin, bool), 1.0, -1.0)
    Q[np.einsum("ij,ij->i", nrm, want) < 0.0] = Q[np.einsum(
        "ij,ij->i", nrm, want) < 0.0][:, ::-1]
    # Triangulate avoiding a shared diagonal: two quads that reuse one diagonal
    # give an edge with FOUR incident triangles, which is non-manifold and not
    # repairable from adjacency (measured: 18 such edges on a bored plate).
    faces, used, extra = [], set(), []
    for i in range(len(Q)):
        a0, b0, c0, d0 = (int(x) for x in Q[i])
        dac = (min(a0, c0), max(a0, c0))
        dbd = (min(b0, d0), max(b0, d0))
        if dac not in used:
            faces += [[a0, b0, c0], [a0, c0, d0]]
            used.add(dac)
        elif dbd not in used:
            faces += [[a0, b0, d0], [b0, c0, d0]]
            used.add(dbd)
        else:
            ci2 = len(verts) + len(extra)
            extra.append(verts[[a0, b0, c0, d0]].mean(axis=0))
            for u, w in ((a0, b0), (b0, c0), (c0, d0), (d0, a0)):
                faces.append([u, w, ci2])
    if extra:
        verts = np.vstack([verts, np.asarray(extra, float)])
    f = np.asarray(faces, np.int64)
    keep = ((f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2]))
    return verts, f[keep]


# ---------------------------------------------------------------- engines

def _hull_mesh(hull, kind):
    used = np.unique(np.asarray(hull.simplices, np.int64))
    remap = -np.ones(len(hull.points), np.int64)
    remap[used] = np.arange(len(used))
    verts = np.asarray(hull.points, float)[used]
    faces = remap[np.asarray(hull.simplices, np.int64)]
    tri = verts[faces]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    flip = np.einsum("ij,ij->i", nrm, tri.mean(axis=1) - verts.mean(axis=0)) < 0.0
    faces[flip] = faces[flip][:, ::-1]
    from ..mesh import weld
    return oriented(weld(Mesh(verts, faces, {"kind": kind}), 1e-9))


def _hull_from_halfspaces(eq):
    """Vertices of ``{x : n_i.x + o_i <= 0}`` for a bounded small polytope."""
    from scipy.spatial import ConvexHull
    n_, o_ = np.asarray(eq[:, :3], float), np.asarray(eq[:, 3], float)
    m = len(n_)
    pts = []
    for i in range(m - 2):
        for j in range(i + 1, m - 1):
            for k in range(j + 1, m):
                A = np.array([n_[i], n_[j], n_[k]])
                if abs(np.linalg.det(A)) < 1e-12:
                    continue
                x = np.linalg.solve(A, -np.array([o_[i], o_[j], o_[k]]))
                if np.all(n_ @ x + o_ <= 1e-9):
                    pts.append(x)
    if len(pts) < 4:
        raise AttaError("offset_vanished",
                        "the offset consumed the whole solid", path="distance",
                        fix="reduce |distance| below half the smallest dimension")
    return ConvexHull(np.unique(np.round(np.array(pts), 12), axis=0))


def _offset_mitre(mesh, d):
    from scipy.spatial import ConvexHull
    verts = np.asarray(mesh.verts, float)
    try:
        hull = ConvexHull(verts)
    except Exception as exc:  # noqa: BLE001
        raise AttaError("offset_not_supported",
                        "mitre offset needs a convex polyhedron: %s" % exc,
                        path="mode", fix="use mode='round'") from exc
    eq = np.asarray(hull.equations, float)
    # a vertex is on the hull iff its MAX residual over all facet planes is ~0
    resid = verts @ eq[:, :3].T + eq[:, 3]
    tol = 1e-6 * max(1e-9, float(np.abs(verts).max()))
    if bool((resid.max(axis=1) < -tol).any()):
        raise AttaError(
            "offset_not_convex",
            "mode='mitre' is exact only for convex solids; this solid has "
            "vertices strictly inside its convex hull",
            path="mode",
            fix="use mode='round' for concave geometry (it resamples an exact "
                "signed field and supports concavity)")
    r = abs(float(d))
    if d >= 0:
        s = np.array([[r, 0, 0], [-r, 0, 0], [0, r, 0], [0, -r, 0],
                      [0, 0, r], [0, 0, -r]])
        cloud = (verts[:, None, :] + s[None, :, :]).reshape(-1, 3)
        cloud = np.unique(np.round(cloud, 12), axis=0)
        return _hull_mesh(ConvexHull(cloud), "offset_mitre")
    return _hull_mesh(_hull_from_halfspaces(
        np.column_stack([eq[:, :3], eq[:, 3] + r])), "offset_mitre")


def _offset_round(mesh, d, resolution):
    """SDF resample; ``resolution`` overrides the ladder (diagnostics/tests)."""
    if resolution:
        start = int(resolution)
        ladder = tuple(r for r in _RES_LADDER if r <= start) or (start,)
        if start not in ladder:
            ladder = (start,) + ladder
    else:
        ladder = _RES_LADDER
    v = np.asarray(mesh.verts, float)
    lo, hi = v.min(axis=0), v.max(axis=0)
    smallest = float((hi - lo).min())
    last = None
    for res in ladder:
        h = float((hi - lo).max()) / max(8, int(res))
        pad = abs(d) + 3.0 * h
        lo2, hi2 = lo - pad, hi + pad
        n = np.ceil((hi2 - lo2) / h).astype(int) + 1
        xs = lo2[0] + h * np.arange(n[0])
        ys = lo2[1] + h * np.arange(n[1])
        zs = lo2[2] + h * np.arange(n[2])
        gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
        pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
        dist = _dist_all(pts, _tri_arrays(mesh)[2])
        org = np.array([xs[0], ys[0], zs[0]])
        inside = _inside_grid(n, org, h, mesh, axis=2).ravel()
        F = (np.where(inside, -dist, dist) - d).reshape(n)
        if not (F[0].max() > 0 and F[-1].max() > 0 and F[:, 0].max() > 0
                and F[:, -1].max() > 0 and F[:, :, 0].max() > 0
                and F[:, :, -1].max() > 0):
            last = "clipped"
            continue
        verts, faces = _surface_nets(F, org, h)
        if len(faces) == 0:
            last = "empty"
            continue
        out = _orient_consistent(Mesh(verts, faces, {"kind": "offset"}))
        topo = topology(out)
        if not topo["closed"] or topo["non_manifold_edges"]:
            last = "nm=%d" % topo["non_manifold_edges"]
            continue
        out.meta["offset"] = {"resolution": int(res), "cell_mm": h * 1e3,
                              "distance_mm": d * 1e3, "engine": "sdf_surface_nets"}
        return out
    raise AttaError(
        "offset_not_closed",
        "surface-nets resampling could not produce a closed manifold for this "
        "solid at any resolution (last: %s)" % last,
        path="mode",
        fix="use mode='mitre' (exact, convex only), or offset a coarser/larger "
            "version of this shape — a grid cell may hold only one sheet of the "
            "surface, and thin or doubly-curved details break that invariant")


def offset_mesh(mesh, distance, mode="round", resolution=None, thickness=None,
                open_faces=None):
    """Move a closed solid's surface by ``distance`` (metres).  See module doc."""
    if not isinstance(mesh, Mesh):
        raise AttaError("bad_arg", "offset input must be a Mesh", path="mesh",
                        fix="wire a solid feature into the op's inputs")
    topo = topology(mesh)
    if not topo["closed"]:
        raise AttaError("offset_open_input",
                        "offset needs a closed solid; the input has %d boundary edge(s)"
                        % topo["boundary_edges"], path="mesh",
                        fix="close the input (thicken a surface, cap the ends) first")
    v = np.asarray(mesh.verts, float)
    lo, hi = v.min(axis=0), v.max(axis=0)
    smallest = float((hi - lo).min())
    d = float(distance)
    mode = str(mode or "round").lower()
    if mode not in ("round", "mitre", "shell"):
        raise AttaError("bad_arg", "mode must be round, mitre or shell", path="mode",
                        fix="use mode='round'")
    if mode in ("round", "mitre") and abs(d) > 0.5 * smallest:
        raise AttaError(
            "offset_too_large",
            "|distance| %.4g mm exceeds half the smallest bbox dimension (%.4g mm)"
            % (abs(d) * 1e3, smallest * 0.5e3), path="distance",
            fix="use |distance| <= %.4g mm" % (smallest * 0.5e3))
    if mode == "shell":
        t = float(thickness if thickness is not None else abs(d))
        t = t if t > 0 else abs(d)
        if t <= 0.0:
            raise AttaError("bad_arg", "shell needs a positive thickness",
                            path="thickness", fix="pass thickness in mm")
        if 2.0 * t >= smallest:
            raise AttaError("offset_too_large",
                            "shell thickness %.4g mm is >= half the smallest "
                            "dimension (%.4g mm)" % (t * 1e3, smallest * 0.5e3),
                            path="thickness",
                            fix="use thickness < %.4g mm" % (smallest * 0.5e3))
        inner = offset_mesh(mesh, -t, mode="round", resolution=resolution)
        from ..boolean import difference
        return difference(mesh, inner)
    if d == 0.0:
        return Mesh(v.copy(), np.asarray(mesh.faces, np.int64).copy(), dict(mesh.meta))
    if mode == "mitre":
        return _offset_mitre(mesh, d)
    return _offset_round(mesh, d, resolution)


def _warn_if_thin(mesh, d):
    """Detect the classic offset failure: a feature thinner than 2|d| vanishes."""
    if d >= 0.0:
        return None
    from .. import metrology
    try:
        wall = metrology.wall_thickness(mesh, max_samples=400)
    except Exception:  # noqa: BLE001 - diagnostics only
        return None
    got = wall.get("min_mm")
    if got is None:
        return None
    need = 2.0 * abs(d) * 1e3
    if float(got) <= need:
        return ("offset_thin_features: sampled min wall %.3f mm <= 2*|distance| "
                "%.3f mm — features that thin are erased by this offset "
                "(shrink the distance to < %.3f mm)" % (float(got), need, float(got) / 2.0))
    return None


# ---------------------------------------------------------------- the op

def offset(mesh=None, distance=None, mode="round", resolution=None,
           thickness=None, open_faces=None, **kw):
    """Op entry point: ``features.evaluate`` passes the wired input as ``mesh``."""
    if mesh is None:
        raise AttaError("offset_no_input_mesh",
                        "offset needs an input mesh; the feature graph row must "
                        "wire one in", path="args.mesh",
                        fix='pass inputs=["<solid feature id>"]')
    mode_l = str(mode or "round").lower()
    if distance is None and mode_l != "shell":
        raise AttaError("bad_arg", "offset needs a 'distance' (mm, signed)",
                        path="args.distance",
                        fix="add args.distance (e.g. 2.0 grows, -1.5 shrinks)")
    if distance is None:
        distance = 0.0
    note = _warn_if_thin(mesh, float(distance))
    out = offset_mesh(mesh, distance, mode=mode, resolution=resolution,
                      thickness=thickness, open_faces=open_faces)
    if note:
        meta = dict(out.meta)
        meta["offset_warning"] = note
        out = Mesh(out.verts, out.faces, meta)
    return out


_F.register_op(OP, {
    "required": (),
    "optional": ("distance", "mode", "resolution", "thickness", "open_faces"),
    "arity": 1,
    "lengths": ("distance", "thickness"),
    "ints": ("resolution",),
    "doc": "Move a closed solid's surface by a signed distance (grow/shrink): "
           "mode='round' is a true offset surface (Minkowski with a ball, SDF "
           "resampled), mode='mitre' is an exact chamfer (convex solids only, "
           "exact 45-degree facets), mode='shell' hollows the solid by "
           "`thickness`. Units follow the graph (mm by default).",
}, offset)


# ---------------------------------------------------------------- family

@recipe(
    "surface.fillet_network", "圆角板 / 双半径过渡块",
    "双半径圆角板：纵向棱（平面内）与横向棱（顶/底边）半径独立控制。"
    "核心是 offset(op)：先把平面轮廓做成 2D 圆角再拉伸，最后对整体做一次 "
    "offset —— 这正是 BOSL2 的「先布尔后圆角」特征顺序。",
    params={
        "size_x": {"type": "float", "default": 60.0, "min": 5.0, "max": 500.0,
                   "unit": "mm", "group": "body"},
        "size_y": {"type": "float", "default": 40.0, "min": 5.0, "max": 500.0,
                   "unit": "mm", "group": "body"},
        "size_z": {"type": "float", "default": 20.0, "min": 5.0, "max": 500.0,
                   "unit": "mm", "group": "body"},
        "r_vertical": {"type": "float", "default": 3.0, "min": 0.0, "max": 20.0,
                       "unit": "mm", "group": "edge",
                       "description": "最终纵向棱半径（平面内的圆角）"},
        "r_horizontal": {"type": "float", "default": 1.0, "min": 0.0, "max": 20.0,
                         "unit": "mm", "group": "edge",
                         "description": "顶/底边半径，由 offset 的 distance 给出"},
        "mode": {"type": "enum", "default": "round", "enum": ["round", "mitre"],
                 "unit": "none", "group": "edge",
                 "description": "round=球面过渡；mitre=精确 45° 倒角"},
        "resolution": {"type": "int", "default": 0, "min": 0, "max": 128,
                       "unit": "count", "group": "quality", "advanced": True,
                       "description": "0 = 自动（op 自选可用分辨率）"},
    },
    presets={
        "safe": {"r_vertical": 5.0, "r_horizontal": 1.5, "mode": "round",
                 "resolution": 40},
        "nominal": {"r_vertical": 3.0, "r_horizontal": 1.0, "mode": "round",
                    "resolution": 0},
        "hero": {"r_vertical": 6.0, "r_horizontal": 2.5, "mode": "mitre",
                 "resolution": 0},
    },
    tags=["surface", "offset", "fillet", "chamfer", "feature_order"],
    expected_genus=0, material="powder_steel",
    constraints=[
        {"id": "radius_fits", "expr": "max(r_vertical, r_horizontal) <= min(size_x, size_y, size_z) / 2",
         "severity": "error",
         "message": "圆角半径超过最小尺寸的一半，圆角会吞掉整个形体",
         "fix": "r_vertical = min(size_x, size_y, size_z) * 0.25"},
        {"id": "core_positive", "expr": "size_z > 2 * r_horizontal + 2",
         "severity": "error",
         "message": "芯体高度 <= 0：两个横向圆角在中间相遇",
         "fix": "r_horizontal = (size_z - 2) / 2.5"},
        {"id": "core_plan", "expr": "size_x > 2 * r_vertical + 2 and size_y > 2 * r_vertical + 2",
         "severity": "error",
         "message": "芯体平面尺寸 <= 0：纵向圆角在中间相遇",
         "fix": "r_vertical = (min(size_x, size_y) - 2) / 2.5"},
        {"id": "steps_ok", "expr": "resolution == 0 or resolution >= 24",
         "severity": "warn",
         "message": "分辨率过低时圆角会变成多边形，曲率会跳变",
         "fix": "resolution = 0"},
        {"id": "edge_share", "expr": "r_horizontal <= size_z * 0.4",
         "severity": "warn",
         "message": "顶/底边半径超过厚度的 40%：倒角会吃掉侧面，圆角会挤压中段",
         "fix": "r_horizontal = size_z * 0.2"},
    ],
)
def _build_fillet_network(v, quality="draft"):
    # builder inputs are ALREADY metres (resolve() converts mm -> m)
    sx, sy, sz = float(v["size_x"]), float(v["size_y"]), float(v["size_z"])
    rv, rh = float(v["r_vertical"]), float(v["r_horizontal"])
    mode = str(v["mode"] or "round").lower()
    res = int(v.get("resolution") or 0)
    if res <= 0:
        res = None

    # ---- feature order: build the *core* first, round last (BOSL2 idiom).
    # The final vertical radius is r_v, and the offset adds r_h to it, so the
    # core's plan radius is r_v - r_h (clamped) and the core is inset by r_h
    # in Z so that the offset reproduces size_z exactly.
    # The offset grows the core by rh in EVERY direction, so the core is inset
    # by rh (not by rv) and its plan radius is rv - rh; then the offset lands
    # the final body exactly on size_x x size_y x size_z with corner radius rv.
    rv_core = max(0.0, rv - rh)
    core_z = max(1e-4, sz - 2.0 * rh)
    plan = rounded_rect(max(1e-4, sx - 2.0 * rh), max(1e-4, sy - 2.0 * rh),
                        rv_core, per_corner=_q(quality, 8, 12, 16))
    core = _S.extrude(plan, core_z)
    core = core.transform(translate=(0.0, 0.0, rh))

    if rh <= 1e-6:
        body = core
    else:
        body = offset_mesh(core, rh, mode=mode, resolution=res)

    return body, finish(bevel_width=min(0.0004, 0.15 * rh) if rh > 0 else None,
                        bevel_segments=_q(quality, 1, 2, 2),
                        bevel_angle_deg=40.0, material="powder_steel")
