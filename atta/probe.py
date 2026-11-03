# -*- coding: utf-8 -*-
"""Probes: cheap, LLM-readable views of a mesh.

Every result is compact on purpose: small grids, rounded millimetres, an
ASCII rendering, and a few summary scalars.  Probes answer "what does it look
like from here / how far apart are these", not "is it certified".
"""

from __future__ import annotations

import numpy as np

from . import mesh as M
from .metrology import _ray_reduce, _tri_edges

_ASCII_RAMP = ".:-=+*#%@"
_DIRS = np.array([
    [0.5773, 0.5774, 0.5775],
    [-0.3100, 1.0000, 0.2700],
    [0.1300, -0.4400, 1.0000],
    [0.7100, 0.5300, -0.6100],
    [-0.5700, -0.2900, -0.8300],
], dtype=np.float64)
_DIRS /= np.linalg.norm(_DIRS, axis=1, keepdims=True)

_VIEW_DIRS = {
    "front": (0.0, 1.0, 0.0),
    "back": (0.0, -1.0, 0.0),
    "right": (-1.0, 0.0, 0.0),
    "left": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, -1.0),
    "bottom": (0.0, 0.0, 1.0),
    "iso": (1.0, 1.0, 1.0),
}

_LATTICE_CACHE: dict[int, np.ndarray] = {}


# ---------------------------------------------------------------- helpers

def _arr(mesh: M.Mesh):
    return (np.asarray(mesh.verts, dtype=np.float64),
            np.asarray(mesh.faces, dtype=np.int64))


def _as_res(res, default=32):
    if isinstance(res, (list, tuple)):
        return max(2, int(res[0])), max(2, int(res[1]))
    return max(2, int(res)), max(2, int(res))


def _axis(axis, direction=1):
    s = str(axis).strip().lower()
    sign = 1
    if s.startswith("-"):
        sign, s = -1, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    idx = {"x": 0, "y": 1, "z": 2}.get(s)
    if idx is None:
        raise ValueError("axis must be x/y/z (optionally + or -), got %r" % (axis,))
    return idx, sign * (1 if int(direction) >= 0 else -1)


def _bounds_m(bounds, bbox):
    lo = np.asarray(bbox["min"], dtype=np.float64).copy()
    hi = np.asarray(bbox["max"], dtype=np.float64).copy()
    if bounds is None:
        return lo, hi
    if isinstance(bounds, dict):
        if "min" in bounds and "max" in bounds:
            return (np.asarray(bounds["min"], dtype=np.float64) * 1e-3,
                    np.asarray(bounds["max"], dtype=np.float64) * 1e-3)
        for i, key in enumerate("xyz"):
            if key in bounds and bounds[key] is not None:
                win = bounds[key]
                if isinstance(win, (list, tuple)) and len(win) == 2:
                    lo[i] = float(win[0]) * 1e-3
                    hi[i] = float(win[1]) * 1e-3
    elif isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        return (np.asarray(bounds[0], dtype=np.float64) * 1e-3,
                np.asarray(bounds[1], dtype=np.float64) * 1e-3)
    return lo, hi


def _ascii_from_depth(depth, hit):
    d = np.asarray(depth, dtype=np.float64)
    h = np.asarray(hit, dtype=bool)
    vals = d[h]
    if len(vals) == 0:
        return [" " * d.shape[1] for _ in range(d.shape[0])]
    dmin, dmax = float(vals.min()), float(vals.max())
    rows = []
    for j in range(d.shape[0]):
        row = []
        for i in range(d.shape[1]):
            if not h[j, i]:
                row.append(" ")
                continue
            if dmax - dmin < 1e-9:
                t = 0.5
            else:
                t = (dmax - float(d[j, i])) / (dmax - dmin)
            k = int(round(max(0.0, min(1.0, t)) * (len(_ASCII_RAMP) - 1)))
            row.append(_ASCII_RAMP[k])
        rows.append("".join(row))
    return rows


# ---------------------------------------------------------------- ray grid

def raycast_grid(mesh: M.Mesh, axis="z", res=(32, 32), bounds=None,
                 direction=1) -> dict:
    """Orthographic depth grid along one of six axis directions."""
    v, f = _arr(mesh)
    nx, ny = _as_res(res)
    idx, sign = _axis(axis, direction)
    axes = [i for i in range(3) if i != idx]
    bbox = M.bbox(mesh)
    lo, hi = _bounds_m(bounds, bbox)
    diag = float(np.linalg.norm(hi - lo)) if len(v) else 0.0
    margin = max(1e-6, diag * 1e-3)
    origin_coord = (lo[idx] - margin) if sign > 0 else (hi[idx] + margin)

    us = np.linspace(lo[axes[0]], hi[axes[0]], nx)
    vs = np.linspace(hi[axes[1]], lo[axes[1]], ny)  # row 0 is +axis1
    uu, vv = np.meshgrid(us, vs)
    origins = np.empty((nx * ny, 3), dtype=np.float64)
    origins[:, axes[0]] = uu.ravel()
    origins[:, axes[1]] = vv.ravel()
    origins[:, idx] = origin_coord
    dirs = np.zeros_like(origins)
    dirs[:, idx] = float(sign)

    if len(f) == 0:
        t = np.full(nx * ny, np.inf)
    else:
        a, e1, e2 = _tri_edges(v, f)
        t = _ray_reduce(origins, dirs, a, e1, e2, mode="min", t_min=margin * 0.5)
    hit = np.isfinite(t)
    depth = np.where(hit, np.maximum(t - margin, 0.0) * 1000.0, -1.0)
    depth = depth.reshape(ny, nx)
    hmask = hit.reshape(ny, nx)
    valid = depth[hmask]
    stats = {"rays": int(nx * ny), "hits": int(hmask.sum()),
             "coverage": round(float(hmask.sum()) / float(nx * ny), 4),
             "min_mm": (None if len(valid) == 0 else round(float(valid.min()), 3)),
             "p50_mm": (None if len(valid) == 0 else round(float(np.median(valid)), 3)),
             "max_mm": (None if len(valid) == 0 else round(float(valid.max()), 3))}
    return {
        "axis": axis,
        "direction": int(sign),
        "res": [nx, ny],
        "bounds_mm": [[round(float(x) * 1000.0, 3) for x in lo],
                      [round(float(x) * 1000.0, 3) for x in hi]],
        "near_plane_mm": round(float(origin_coord) * 1000.0, 3),
        "depth_mm": [[round(float(x), 3) for x in row] for row in depth],
        "hit_mask": ["".join("1" if b else "0" for b in row) for row in hmask],
        "ascii": _ascii_from_depth(depth, hmask),
        "stats": stats,
        "units": "mm",
        "exact": False,
    }


# ---------------------------------------------------------------- silhouette

def _basis(view):
    d = np.asarray(_VIEW_DIRS.get(str(view).lower(), _VIEW_DIRS["iso"]),
                   dtype=np.float64)
    d = d / np.linalg.norm(d)
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(d, up))) > 0.99:
        up = np.array([0.0, 1.0, 0.0])
    right = np.cross(d, up)
    right /= np.linalg.norm(right)
    up2 = np.cross(right, d)
    up2 /= np.linalg.norm(up2)
    return right, up2


def _lattice(k: int) -> np.ndarray:
    got = _LATTICE_CACHE.get(k)
    if got is not None:
        return got
    i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
    keep = (i + j) <= k
    i = i[keep] / float(k)
    j = j[keep] / float(k)
    out = np.stack([1.0 - i - j, i, j], axis=1)
    _LATTICE_CACHE[k] = out
    return out


def _rasterize(v, f, right, up, res):
    if len(v) == 0:
        return np.zeros((res, res), dtype=bool)
    u = v @ right
    vv = v @ up
    umin, umax = float(u.min()), float(u.max())
    vmin, vmax = float(v.min()), float(v.max())
    uc, vc = (umin + umax) * 0.5, (vmin + vmax) * 0.5
    span = max(umax - umin, vmax - vmin, 1e-12)

    def to_px(x, y):
        px = (x - uc) / span * (res - 1) + (res - 1) * 0.5
        py = -(y - vc) / span * (res - 1) + (res - 1) * 0.5
        return px, py

    occ = np.zeros((res, res), dtype=bool)
    px, py = to_px(u, vv)
    px = np.clip(np.rint(px).astype(np.int64), 0, res - 1)
    py = np.clip(np.rint(py).astype(np.int64), 0, res - 1)
    occ.ravel()[py * res + px] = True
    if len(f) == 0:
        return occ

    coords = np.stack([u, vv], axis=1)
    p0 = coords[f[:, 0]]
    p1 = coords[f[:, 1]]
    p2 = coords[f[:, 2]]
    edge = np.maximum.reduce([
        np.linalg.norm(p1 - p0, axis=1),
        np.linalg.norm(p2 - p1, axis=1),
        np.linalg.norm(p0 - p2, axis=1),
    ]) / max(1e-12, span) * (res - 1)
    ks = np.clip(np.ceil(edge).astype(np.int64), 1, max(2, 2 * res))
    p0 = coords[f[:, 0]]
    p1 = coords[f[:, 1]]
    p2 = coords[f[:, 2]]

    for k in np.unique(ks):
        sel = np.where(ks == k)[0]
        lat = _lattice(int(k))
        chunk = max(64, int(2_000_000 // max(1, len(lat))))
        for s in range(0, len(sel), chunk):
            ids = sel[s:s + chunk]
            q0, q1, q2 = p0[ids], p1[ids], p2[ids]
            sx = (lat[None, :, 0] * q0[:, None, 0] +
                  lat[None, :, 1] * q1[:, None, 0] +
                  lat[None, :, 2] * q2[:, None, 0])
            sy = (lat[None, :, 0] * q0[:, None, 1] +
                  lat[None, :, 1] * q1[:, None, 1] +
                  lat[None, :, 2] * q2[:, None, 1])
            ax, ay = to_px(sx, sy)
            ix = np.clip(np.rint(ax).astype(np.int64), 0, res - 1)
            iy = np.clip(np.rint(ay).astype(np.int64), 0, res - 1)
            occ.ravel()[iy * res + ix] = True
    return occ


def silhouette(mesh: M.Mesh, views=("front", "top", "left", "iso"),
               res=32) -> dict:
    """Occupancy bitmaps for orthographic views."""
    v, f = _arr(mesh)
    res = max(4, int(res))
    if isinstance(views, str):
        views = (views,)
    out = {}
    for name in views:
        right, up = _basis(name)
        occ = _rasterize(v, f, right, up, res)
        bitmap = ["".join("1" if b else "0" for b in row) for row in occ]
        ascii_rows = ["".join("#" if b else "." for b in row) for row in occ]
        out[str(name)] = {
            "bitmap": bitmap,
            "ascii": ascii_rows,
            "coverage": round(float(occ.sum()) / float(res * res), 4),
            "n_pixels": int(occ.sum()),
        }
    return {"res": res, "views": out, "units": "mm", "exact": False}


# ---------------------------------------------------------------- section

def _plane_normal(plane):
    if isinstance(plane, str):
        s = plane.strip().lower()
        sign = 1
        if s.startswith("-"):
            sign, s = -1, s[1:]
        elif s.startswith("+"):
            s = s[1:]
        idx = {"x": 0, "y": 1, "z": 2}.get(s)
        if idx is None:
            raise ValueError("plane must be x/y/z or a normal vector")
        n = np.zeros(3)
        n[idx] = float(sign)
        return n, idx
    n = np.asarray(plane, dtype=np.float64).reshape(3)
    ln = float(np.linalg.norm(n))
    if ln < 1e-12:
        raise ValueError("plane normal is zero")
    return n / ln, None


def _resample(pts: np.ndarray, n: int, closed: bool) -> np.ndarray:
    if len(pts) <= n:
        return pts
    if closed:
        loop = np.vstack([pts, pts[:1]])
    else:
        loop = pts
    seg = np.linalg.norm(np.diff(loop, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 0.0:
        return pts[:1]
    target = np.linspace(0.0, total, n, endpoint=not closed)
    x = np.interp(target, cum, loop[:, 0])
    y = np.interp(target, cum, loop[:, 1])
    return np.stack([x, y], axis=1)


def _poly_area(p: np.ndarray) -> float:
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def section(mesh: M.Mesh, plane="z", offset_mm=0.0, res=64) -> dict:
    """Slice the mesh with a plane and return 2-D contour polylines (mm)."""
    v, f = _arr(mesh)
    res = max(4, int(res))
    n, axis_idx = _plane_normal(plane)
    bbox = M.bbox(mesh)
    origin = np.asarray(bbox["center"], dtype=np.float64) + n * (float(offset_mm) / 1000.0)
    least = int(np.argmin(np.abs(n)))
    basis_u = np.cross(n, np.eye(3)[least])
    basis_u /= np.linalg.norm(basis_u)
    basis_v = np.cross(n, basis_u)
    basis_v /= np.linalg.norm(basis_v)

    d = (v - origin) @ n
    empty = {"plane": plane, "offset_mm": float(offset_mm),
             "plane_mm": (None if axis_idx is None
                          else round(float(origin[axis_idx]) * 1000.0, 4)),
             "loops": [], "n_loops": 0, "n_segments": 0, "res": res,
             "units": "mm", "exact": False}
    if len(f) == 0:
        return empty

    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=0)
    face_ids = np.tile(np.arange(len(f), dtype=np.int64), 3)
    d0, d1 = d[e[:, 0]], d[e[:, 1]]
    crossing = (d0 > 0.0) != (d1 > 0.0)
    if not crossing.any():
        return empty
    ee = e[crossing]
    ff = face_ids[crossing]
    da, db = d0[crossing], d1[crossing]
    den = da - db
    den = np.where(np.abs(den) < 1e-15, 1e-15, den)
    tt = da / den
    pa, pb = v[ee[:, 0]], v[ee[:, 1]]
    pts = pa + tt[:, None] * (pb - pa)

    order = np.argsort(ff, kind="stable")
    ff_s, pts_s = ff[order], pts[order]
    _, starts, counts = np.unique(ff_s, return_index=True, return_counts=True)
    segs = []
    for st, ct in zip(starts, counts):
        if ct < 2:
            continue
        block = pts_s[st:st + ct]
        q = np.round(block / 1e-9).astype(np.int64)
        _, keep = np.unique(q, axis=0, return_index=True)
        block = block[np.sort(keep)]
        if len(block) >= 2:
            segs.append((block[0], block[1]))
    if not segs:
        return empty

    def key(p):
        return tuple(np.round(p / 1e-9).astype(np.int64).tolist())

    adj: dict = {}
    coords: dict = {}
    for i, (p0, p1) in enumerate(segs):
        k0, k1 = key(p0), key(p1)
        coords[k0], coords[k1] = p0, p1
        adj.setdefault(k0, []).append(i)
        adj.setdefault(k1, []).append(i)

    used = [False] * len(segs)
    chains = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        start = key(segs[i][0])
        cur = key(segs[i][1])
        chain = [start, cur]
        closed = False
        while True:
            nxt = None
            for j in adj.get(cur, ()):
                if not used[j]:
                    nxt = j
                    break
            if nxt is None:
                break
            used[nxt] = True
            a0 = key(segs[nxt][0])
            a1 = key(segs[nxt][1])
            other = a1 if a0 == cur else a0
            if other == start:
                closed = True
                break
            chain.append(other)
            cur = other
            if len(chain) > len(segs) + 2:
                break
        chains.append((chain, closed))

    loops = []
    for chain, closed in chains:
        if len(chain) < 2:
            continue
        pts3 = np.array([coords[k] for k in chain], dtype=np.float64)
        rel = pts3 - origin
        p2 = np.stack([rel @ basis_u, rel @ basis_v], axis=1) * 1000.0
        area = _poly_area(p2) if closed else 0.0
        perim = float(np.linalg.norm(np.diff(p2, axis=0), axis=1).sum())
        if closed:
            perim += float(np.linalg.norm(p2[0] - p2[-1]))
        shown = _resample(p2, res, closed)
        loops.append({
            "points_mm": [[round(float(x), 4), round(float(y), 4)]
                          for x, y in shown],
            "n": int(len(shown)),
            "closed": bool(closed),
            "area_mm2": round(area, 4),
            "perimeter_mm": round(perim, 4),
        })
    loops.sort(key=lambda r: -r["area_mm2"])
    out = dict(empty)
    out.update({"loops": loops, "n_loops": len(loops),
                "n_segments": len(segs)})
    if axis_idx is not None:
        out["plane"] = str(plane)
    return out


# ---------------------------------------------------------------- distance

def _closest_on_tris(points, a, b, c):
    """Closest points on triangles (Ericson), vectorised over (P,K)."""
    p = np.asarray(points, dtype=np.float64)[:, None, :]
    A = np.asarray(a, dtype=np.float64)
    B = np.asarray(b, dtype=np.float64)
    C = np.asarray(c, dtype=np.float64)
    if A.ndim == 2:
        A, B, C = A[None], B[None], C[None]
    ab = B - A
    ac = C - A
    ap = p - A
    d1 = np.einsum("pkj,pkj->pk", ab, ap)
    d2 = np.einsum("pkj,pkj->pk", ac, ap)
    bp = p - B
    d3 = np.einsum("pkj,pkj->pk", ab, bp)
    d4 = np.einsum("pkj,pkj->pk", ac, bp)
    cp = p - C
    d5 = np.einsum("pkj,pkj->pk", ab, cp)
    d6 = np.einsum("pkj,pkj->pk", ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4

    def div(num, den):
        den = np.where(np.abs(den) < 1e-15, 1e-15, den)
        return num / den

    cand_a = A + np.zeros_like(ap)
    cand_b = B + np.zeros_like(ap)
    cand_ab = A + div(d1, d1 - d3)[..., None] * ab
    cand_c = C + np.zeros_like(ap)
    cand_ac = A + div(d2, d2 - d6)[..., None] * ac
    cand_bc = B + div(d4 - d3, (d4 - d3) + (d5 - d6))[..., None] * (C - B)
    denom = div(1.0, va + vb + vc)
    v = vb * denom
    w = vc * denom
    cand_face = A + v[..., None] * ab + w[..., None] * ac

    cond = [
        (d1 <= 0) & (d2 <= 0),
        (d3 >= 0) & (d4 <= d3),
        (vc <= 0) & (d1 >= 0) & (d3 <= 0),
        (d6 >= 0) & (d5 <= d6),
        (vb <= 0) & (d2 >= 0) & (d6 <= 0),
        (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0),
    ]
    choice = [cand_a, cand_b, cand_ab, cand_c, cand_ac, cand_bc]
    closest = np.select([c[..., None] for c in cond], choice, default=cand_face)
    dist = np.linalg.norm(p - closest, axis=-1)
    return dist, closest


def _nearest_to_mesh(points, verts, faces, k=8):
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    if len(points) == 0 or len(faces) == 0:
        return np.full(len(points), np.inf), np.zeros((len(points), 3))
    kk = min(int(k), len(faces))
    try:
        from scipy.spatial import cKDTree
        cent = (a + b + c) / 3.0
        tree = cKDTree(cent)
        _, idx = tree.query(points, k=kk)
        if kk == 1:
            idx = np.asarray(idx)[:, None]
        d, pts = _closest_on_tris(points, a[idx], b[idx], c[idx])
        j = np.argmin(d, axis=1)
        rows = np.arange(len(points))
        return d[rows, j], pts[rows, j]
    except Exception:  # noqa: BLE001 - brute force is the fallback
        best = np.full(len(points), np.inf)
        bestp = np.zeros((len(points), 3))
        chunk = 32768
        for s in range(0, len(faces), chunk):
            aa, bb, cc = a[s:s + chunk], b[s:s + chunk], c[s:s + chunk]
            d, pts = _closest_on_tris(points, aa, bb, cc)
            j = np.argmin(d, axis=1)
            rows = np.arange(len(points))
            cand = d[rows, j]
            better = cand < best
            best[better] = cand[better]
            bestp[better] = pts[rows, j][better]
        return best, bestp


def _sample_surface(mesh: M.Mesh, k: int):
    v, f = _arr(mesh)
    if len(v) == 0:
        return np.zeros((0, 3))
    k = max(1, int(k))
    nv = min(len(v), max(1, k // 2))
    iv = np.linspace(0, len(v) - 1, nv).astype(np.int64)
    pts = [v[iv]]
    if len(f) and len(pts[0]) < k:
        cen = v[f].mean(axis=1)
        nf = min(len(cen), k - len(pts[0]))
        iff = np.linspace(0, len(cen) - 1, max(1, nf)).astype(np.int64)
        pts.append(cen[iff])
    return np.vstack(pts)


def clearance(a: M.Mesh, b: M.Mesh, max_samples: int = 2000) -> dict:
    """Approximate minimum distance between two meshes (mm)."""
    av, af = _arr(a)
    bv, bf = _arr(b)
    k = max(2, int(max_samples))
    pa = _sample_surface(a, max(1, k // 2))
    pb = _sample_surface(b, max(1, k - len(pa)))
    d_ab, p_ab = _nearest_to_mesh(pa, bv, bf)
    d_ba, p_ba = _nearest_to_mesh(pb, av, af)
    best = float("inf")
    best_a = np.zeros(3)
    best_b = np.zeros(3)
    if len(d_ab):
        j = int(np.argmin(d_ab))
        if float(d_ab[j]) < best:
            best, best_a, best_b = float(d_ab[j]), pa[j], p_ab[j]
    if len(d_ba):
        j = int(np.argmin(d_ba))
        if float(d_ba[j]) < best:
            best, best_a, best_b = float(d_ba[j]), p_ba[j], pb[j]
    if not np.isfinite(best):
        best = float("inf")
    return {
        "min_mm": round(best * 1000.0, 4),
        "distance_mm": round(best * 1000.0, 4),
        "point_a_mm": [round(float(x) * 1000.0, 3) for x in best_a],
        "point_b_mm": [round(float(x) * 1000.0, 3) for x in best_b],
        "n_samples": int(len(pa) + len(pb)),
        "exact": False,
        "method": "kd_centroid+point_triangle",
    }


def nearest(mesh: M.Mesh, point_mm) -> dict:
    v, f = _arr(mesh)
    p = np.asarray(point_mm, dtype=np.float64).reshape(1, 3) / 1000.0
    if len(f) == 0:
        return {"distance_mm": None, "point_mm": None, "face_index": None,
                "exact": False}
    d, pts = _nearest_to_mesh(p, v, f, k=len(f))
    i = int(np.argmin(d))
    return {
        "distance_mm": round(float(d[i]) * 1000.0, 4),
        "point_mm": [round(float(x) * 1000.0, 4) for x in pts[i]],
        "face_index": i,
        "exact": False,
        "method": "point_triangle",
    }


def _inside_many(points_m, mesh: M.Mesh, n_dirs=5):
    v, f = _arr(mesh)
    p = np.asarray(points_m, dtype=np.float64).reshape(-1, 3)
    if len(f) == 0 or len(p) == 0:
        return np.zeros(len(p), dtype=bool)
    a, e1, e2 = _tri_edges(v, f)
    votes = np.zeros(len(p), dtype=np.int64)
    used = 0
    for d in _DIRS[:max(1, int(n_dirs))]:
        origins = p + d * 1e-7
        dirs = np.tile(d, (len(p), 1))
        cnt = _ray_reduce(origins, dirs, a, e1, e2, mode="count",
                          t_min=1e-9)
        votes += (cnt % 2 == 1).astype(np.int64)
        used += 1
    return votes >= (used // 2 + 1)


def inside(mesh: M.Mesh, point_mm) -> bool:
    p = np.asarray(point_mm, dtype=np.float64).reshape(1, 3) / 1000.0
    return bool(_inside_many(p, mesh, n_dirs=5)[0])


# ---------------------------------------------------------------- booleans

def _to_trimesh(mesh: M.Mesh):
    import trimesh
    return trimesh.Trimesh(vertices=np.asarray(mesh.verts, dtype=np.float64),
                           faces=np.asarray(mesh.faces, dtype=np.int64),
                           process=True)


def _voxel_overlap_m3(a: M.Mesh, b: M.Mesh, n=12) -> tuple[float, dict]:
    ba, bb = M.bbox(a), M.bbox(b)
    lo = np.maximum(ba["min"], bb["min"])
    hi = np.minimum(ba["max"], bb["max"])
    ext = hi - lo
    if np.any(ext <= 0.0):
        return 0.0, {"min": lo, "max": hi}
    step = ext / float(max(2, int(n)))
    axes = []
    for i in range(3):
        cnt = int(max(2, round(n * ext[i] / max(ext.max(), 1e-12))))
        axes.append(np.linspace(lo[i] + step[i] * 0.5, hi[i] - step[i] * 0.5, cnt))
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    ina = _inside_many(grid, a, n_dirs=1)
    inb = _inside_many(grid, b, n_dirs=1)
    frac = float(np.count_nonzero(ina & inb)) / float(len(grid))
    return frac * float(np.prod(ext)), {"min": lo, "max": hi}


def interference(a: M.Mesh, b: M.Mesh) -> dict:
    """Boolean intersection volume of two meshes (mm^3)."""
    ba, bb = M.bbox(a), M.bbox(b)
    lo = np.maximum(ba["min"], bb["min"])
    hi = np.minimum(ba["max"], bb["max"])
    vol_m3 = 0.0
    method = "bbox_disjoint"
    if np.all(hi > lo):
        try:
            import trimesh
            ta, tb = _to_trimesh(a), _to_trimesh(b)
            res = trimesh.boolean.intersection([ta, tb], engine="manifold")
            if res is None:
                vol_m3 = 0.0
            elif hasattr(res, "volume"):
                vol_m3 = abs(float(res.volume))
            elif hasattr(res, "geometry"):
                vol_m3 = abs(sum(float(g.volume) for g in res.geometry.values()))
            method = "trimesh.boolean.intersection"
            if not np.isfinite(vol_m3):
                raise ValueError("non-finite boolean volume")
        except Exception:  # noqa: BLE001 - coarse fallback only
            vol_m3, _ = _voxel_overlap_m3(a, b)
            method = "voxel_sampling"
    vol_mm3 = vol_m3 * 1e9
    return {
        "volume_mm3": round(float(vol_mm3), 4),
        "intersects": bool(vol_mm3 > 1e-3),
        "overlap_bbox_mm": {
            "min": [round(float(x) * 1000.0, 3) for x in lo],
            "max": [round(float(x) * 1000.0, 3) for x in hi],
        },
        "exact": False,
        "method": method,
    }


__all__ = ["raycast_grid", "silhouette", "section", "clearance",
           "interference", "nearest", "inside"]
