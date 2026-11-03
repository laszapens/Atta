# -*- coding: utf-8 -*-
"""Metrology: turn a triangle mesh into millimetre facts.

The mesh lives in metres; every public length here is millimetres.  Volume,
area, centroid and bbox are exact for the given triangulation.  Wall
thickness, dihedral and curvature are sampled or derived diagnostics, so all
results are marked ``exact: False``.
"""

from __future__ import annotations

import numpy as np

from . import mesh as M

EPS = 1e-12
_DET_EPS = 1e-14
_RAY_RAYS = 32
_RAY_TRIS = 512


# ---------------------------------------------------------------- helpers

def _tri_arrays(v: np.ndarray, f: np.ndarray):
    return v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]


def _face_normals(v: np.ndarray, f: np.ndarray):
    a, b, c = _tri_arrays(v, f)
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    out = np.zeros_like(n)
    ok = ln > EPS
    out[ok] = n[ok] / ln[ok, None]
    return out, 0.5 * ln


def _signed_volume(v: np.ndarray, f: np.ndarray) -> float:
    if len(f) == 0:
        return 0.0
    a, b, c = _tri_arrays(v, f)
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _edge_table(v: np.ndarray, f: np.ndarray):
    """Undirected edges with face ownership, vectorised.

    Returns ``(reps, counts, sorted_order, starts, face_ids)`` where
    ``sorted_order`` indexes the 3M directed-edge array sorted by edge key.
    """
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=0)
    face_ids = np.tile(np.arange(len(f), dtype=np.int64), 3)
    base = max(1, int(len(v)))
    key = np.minimum(e[:, 0], e[:, 1]).astype(np.int64) * base + \
        np.maximum(e[:, 0], e[:, 1]).astype(np.int64)
    order = np.argsort(key, kind="stable")
    ks = key[order]
    uniq, starts, counts = np.unique(ks, return_index=True, return_counts=True)
    reps = e[order[starts]]
    return reps, counts, order, starts, face_ids


# ---------------------------------------------------------------- rays

def _ray_block(o, d, a, e1, e2, t_min):
    """Moller-Trumbore for a block of rays ``(R,3)`` and tris ``(T,3)``."""
    h = np.cross(d[:, None, :], e2[None, :, :])
    det = np.einsum("tj,rtj->rt", e1, h)
    ok = np.abs(det) > _DET_EPS
    inv = np.zeros_like(det)
    np.divide(1.0, det, out=inv, where=ok)
    s = o[:, None, :] - a[None, :, :]
    u = np.einsum("rtj,rtj->rt", s, h) * inv
    q = np.cross(s, e1[None, :, :])
    vv = np.einsum("rtj,rj->rt", q, d) * inv
    t = np.einsum("tj,rtj->rt", e2, q) * inv
    hit = ok & (u >= -1e-9) & (vv >= -1e-9) & (u + vv <= 1.0 + 1e-9) & (t > t_min)
    return t, hit


def _ray_reduce(origins, dirs, a, e1, e2, mode="min", t_min=1e-7,
                normals=None, min_dot=None):
    """Reduce ray/triangle hits per ray: ``min`` distance or ``count``.

    ``normals`` + ``min_dot`` filter hits whose surface faces away from the
    ray (a grazing hit is not a wall thickness).  For an inward wall ray the
    opposite surface normal is roughly parallel to the ray, so ``min_dot=0.5``
    keeps real walls and rejects bevel/lip slivers.
    """
    origins = np.ascontiguousarray(origins, dtype=np.float64)
    dirs = np.ascontiguousarray(dirs, dtype=np.float64)
    n = len(origins)
    out = (np.full(n, np.inf) if mode == "min"
           else np.zeros(n, dtype=np.int64))
    if n == 0 or len(a) == 0:
        return out
    for s in range(0, n, _RAY_RAYS):
        o = origins[s:s + _RAY_RAYS]
        d = dirs[s:s + _RAY_RAYS]
        for k in range(0, len(a), _RAY_TRIS):
            t, hit = _ray_block(o, d, a[k:k + _RAY_TRIS],
                                e1[k:k + _RAY_TRIS], e2[k:k + _RAY_TRIS], t_min)
            if mode == "min":
                if normals is not None and min_dot is not None:
                    align = np.einsum("tj,rj->rt", normals[k:k + _RAY_TRIS],
                                      d)
                    hit = hit & (align > float(min_dot))
                tt = np.where(hit, t, np.inf)
                out[s:s + len(o)] = np.minimum(out[s:s + len(o)],
                                               tt.min(axis=1))
            else:
                out[s:s + len(o)] += hit.sum(axis=1)
    return out


def _tri_edges(v, f):
    a, b, c = _tri_arrays(v, f)
    return a, b - a, c - a


# ---------------------------------------------------------------- measures

def bbox_mm(mesh: M.Mesh) -> dict:
    b = M.bbox(mesh)
    out = {k: [round(float(x) * 1000.0, 4) for x in b[k]]
           for k in ("min", "max", "size", "center")}
    out["diag_mm"] = round(float(b["diag"]) * 1000.0, 4)
    return out


def volume_mm3(mesh: M.Mesh) -> float:
    return round(float(M.volume(mesh)) * 1e9, 6)


def area_mm2(mesh: M.Mesh) -> float:
    return round(float(M.area(mesh)) * 1e6, 6)


def centroid_mm(mesh: M.Mesh) -> list[float]:
    return [round(float(x) * 1000.0, 4) for x in M.centroid(mesh)]


def mass_properties(mesh: M.Mesh, density_kg_m3: float | None = None) -> dict:
    vol_m3 = float(M.volume(mesh))
    out = {
        "volume_mm3": round(vol_m3 * 1e9, 6),
        "centroid_mm": centroid_mm(mesh),
        "density_kg_m3": (None if density_kg_m3 is None
                          else float(density_kg_m3)),
        "mass_kg": None,
        "exact": False,
    }
    if density_kg_m3 is not None:
        out["mass_kg"] = float(vol_m3 * float(density_kg_m3))
    return out


# ---------------------------------------------------------------- wall

def concave_edge_count(mesh: M.Mesh) -> int:
    """Number of reflex (concave) manifold edges.

    A convex solid -- cube, sphere, cylinder, cone, plate -- has none: every
    two-face edge bends away from the material.  A part with a cavity, a bore,
    or a pocket necessarily has at least one.  This is the honest discriminator
    between "this part has walls" and "this part is a lump of material".
    """
    v = np.asarray(mesh.verts, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    if len(f) == 0:
        return 0
    reps, counts, order, starts, face_ids = _edge_table(v, f)
    two = np.where(counts == 2)[0]
    if len(two) == 0:
        return 0
    f0 = face_ids[order[starts[two]]]
    f1 = face_ids[order[starts[two] + 1]]
    rep = reps[two]
    f1v = f[f1]
    # The third vertex of f1 (the one not on the shared edge).
    mask = (f1v != rep[:, 0:1]) & (f1v != rep[:, 1:2])
    other = v[f1v[np.arange(len(f1v)), mask.argmax(axis=1)]]
    fn, _ = _face_normals(v, f)
    a, _, _ = _tri_arrays(v, f)
    # Positive => the neighbour lies above f0's plane => reflex/concave edge.
    d = np.einsum("ij,ij->i", fn[f0], other - a[f0])
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    tol = max(1e-12, diag * 1e-7)
    return int((d > tol).sum())


def wall_thickness(mesh: M.Mesh, max_samples: int = 600,
                   seed: int = 0) -> dict:
    """Wall thickness by inward face-normal rays.

    Face centres are sampled (area is not weighted); the nearest forward hit
    gives a local thickness.  This is a sampled lower bound, never a proof.

    A ray from an inward-facing surface only measures a *wall* if the part is
    actually hollow there.  On a solid part the ray simply crosses the body and
    reports the chord -- a 20 mm cube would "measure" a 20 mm wall, which is a
    plausible-looking lie.  So when the mesh has no reflex edge at all (a
    convex solid) the measurement is reported as not applicable: ``min_mm`` and
    its percentiles are ``None`` and ``applicable`` is ``False``.
    """
    v = np.asarray(mesh.verts, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    empty = {"min_mm": None, "p05_mm": None, "p50_mm": None, "p95_mm": None,
             "n_samples": 0, "n_hits": 0, "coverage": 0.0, "exact": False,
             "applicable": False, "note": "empty mesh"}
    if len(f) == 0:
        return empty
    if concave_edge_count(mesh) == 0:
        out = dict(empty)
        out.update({
            "n_samples": int(min(max(1, max_samples), len(f))),
            "note": "solid part: no opposing surface, wall thickness is not "
                    "applicable (a convex solid has no wall to measure)",
        })
        return out
    fn, _ = _face_normals(v, f)
    a, b, c = _tri_arrays(v, f)
    centroids = (a + b + c) / 3.0
    signed = _signed_volume(v, f)
    inward = fn if signed < 0.0 else -fn
    k = max(1, min(int(max_samples), len(f)))
    if k < len(f):
        rng = np.random.default_rng(int(seed))
        idx = np.unique(rng.integers(0, len(f), size=k))
    else:
        idx = np.arange(len(f), dtype=np.int64)
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    off = max(1e-7, diag * 1e-6)
    origins = centroids[idx] + inward[idx] * off
    dirs = inward[idx]
    a3, e1, e2 = _tri_edges(v, f)
    t = _ray_reduce(origins, dirs, a3, e1, e2, mode="min", t_min=off * 0.5,
                    normals=fn, min_dot=0.8)
    good = np.isfinite(t) & (t > 0.0)
    d = t[good] * 1000.0
    if len(d) == 0:
        out = dict(empty)
        out.update({"n_samples": int(len(idx)), "n_hits": 0,
                    "applicable": False,
                    "note": "no inward hit; open or zero-thickness shell"})
        return out
    return {
        "min_mm": round(float(d.min()), 4),
        "p05_mm": round(float(np.percentile(d, 5)), 4),
        "p50_mm": round(float(np.percentile(d, 50)), 4),
        "p95_mm": round(float(np.percentile(d, 95)), 4),
        "n_samples": int(len(idx)),
        "n_hits": int(len(d)),
        "coverage": round(float(len(d)) / max(1, len(idx)), 4),
        "exact": False,
        "applicable": True,
        "method": "ray_face_center_inward",
    }


# ---------------------------------------------------------------- angles

def dihedral(mesh: M.Mesh, thresholds=(30.0, 60.0)) -> dict:
    """Dihedral angles across two-face edges; 0 means coplanar."""
    v = np.asarray(mesh.verts, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    th = [float(x) for x in (thresholds or (30.0, 60.0))] or [30.0, 60.0]
    empty = {"p50_deg": 0.0, "p95_deg": 0.0, "max_deg": 0.0, "n_edges": 0,
             "count_gt_30": 0, "count_gt_60": 0,
             "thresholds_deg": th,
             "counts_above": [{"threshold_deg": t, "count": 0} for t in th],
             "exact": False}
    if len(f) == 0:
        return empty
    _, counts, order, starts, face_ids = _edge_table(v, f)
    two = np.where(counts == 2)[0]
    if len(two) == 0:
        return empty
    f0 = face_ids[order[starts[two]]]
    f1 = face_ids[order[starts[two] + 1]]
    fn, _ = _face_normals(v, f)
    dots = np.clip(np.einsum("ij,ij->i", fn[f0], fn[f1]), -1.0, 1.0)
    ang = np.degrees(np.arccos(dots))
    return {
        "p50_deg": round(float(np.percentile(ang, 50)), 4),
        "p95_deg": round(float(np.percentile(ang, 95)), 4),
        "max_deg": round(float(ang.max()), 4),
        "n_edges": int(len(ang)),
        "count_gt_30": int((ang > th[0]).sum()),
        "count_gt_60": int((ang > th[-1]).sum()),
        "thresholds_deg": th,
        "counts_above": [{"threshold_deg": t, "count": int((ang > t).sum())}
                         for t in th],
        "exact": False,
    }


def vertex_normals(mesh: M.Mesh) -> np.ndarray:
    v = np.asarray(mesh.verts, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    if len(f) == 0:
        return np.zeros_like(v)
    fn, area = _face_normals(v, f)
    weighted = fn * area[:, None]
    out = np.zeros_like(v)
    for c in range(3):
        out[:, c] = np.bincount(f.ravel(), weights=np.repeat(weighted[:, c], 3),
                                minlength=len(v))
    ln = np.linalg.norm(out, axis=1)
    ok = ln > EPS
    out[ok] /= ln[ok, None]
    out[~ok] = 0.0
    return out


def curvature(mesh: M.Mesh) -> dict:
    """Curvature proxy: angle between vertex normals across shared edges."""
    v = np.asarray(mesh.verts, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    empty = {"p50_deg": 0.0, "p95_deg": 0.0, "max_deg": 0.0, "n_edges": 0,
             "exact": False}
    if len(f) == 0:
        return empty
    reps, _, _, _, _ = _edge_table(v, f)
    vn = vertex_normals(mesh)
    n0 = np.linalg.norm(vn[reps[:, 0]], axis=1)
    n1 = np.linalg.norm(vn[reps[:, 1]], axis=1)
    good = (n0 > 0.5) & (n1 > 0.5)
    if not good.any():
        return empty
    dots = np.clip(np.einsum("ij,ij->i", vn[reps[good, 0]], vn[reps[good, 1]]),
                   -1.0, 1.0)
    ang = np.degrees(np.arccos(dots))
    return {
        "p50_deg": round(float(np.percentile(ang, 50)), 4),
        "p95_deg": round(float(np.percentile(ang, 95)), 4),
        "max_deg": round(float(ang.max()), 4),
        "n_edges": int(len(ang)),
        "exact": False,
        "method": "vertex_normal_variation",
    }


# ---------------------------------------------------------------- aggregate

_dihedral_fn = dihedral
_curvature_fn = curvature


def measure(mesh: M.Mesh, *, wall: bool = False, dihedral: bool = True,
            curvature: bool = False,
            density_kg_m3: float | None = None) -> dict:
    out = {
        "bbox_mm": bbox_mm(mesh),
        "volume_mm3": volume_mm3(mesh),
        "area_mm2": area_mm2(mesh),
        "centroid_mm": centroid_mm(mesh),
        "topology": M.topology(mesh),
        "units": "mm",
        "exact": False,
    }
    if density_kg_m3 is not None:
        out["mass"] = mass_properties(mesh, density_kg_m3)
    if wall:
        out["wall"] = wall_thickness(mesh)
    if dihedral:
        out["dihedral"] = _dihedral_fn(mesh)
    if curvature:
        out["curvature"] = _curvature_fn(mesh)
    return out


__all__ = ["bbox_mm", "volume_mm3", "area_mm2", "centroid_mm",
           "mass_properties", "wall_thickness", "concave_edge_count",
           "dihedral", "curvature", "vertex_normals", "measure"]
