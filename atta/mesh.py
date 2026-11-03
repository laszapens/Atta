# -*- coding: utf-8 -*-
"""The geometry truth layer: a triangle mesh plus topology facts.

Why a small mesh type instead of a full B-rep kernel?
-----------------------------------------------------
The LLM does not need NURBS bookkeeping to reason about shape; it needs
stable facts (closed, genus, volume, wall, clearance) and a deterministic
rebuild.  Meshes are cheap, inspectable, and Blender-native.  B-rep can be
added later behind the same `Mesh` boundary without changing the agent surface.

Units: **metres**.  Public specs use millimetres and convert at the boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "Mesh", "topology", "canonical_hash", "weld", "concat", "transform",
    "bbox", "volume", "area", "centroid", "oriented",
]


@dataclass
class Mesh:
    verts: np.ndarray
    faces: np.ndarray
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.verts = np.ascontiguousarray(self.verts, dtype=np.float64)
        self.faces = np.ascontiguousarray(self.faces, dtype=np.int64)
        if self.verts.ndim != 2 or self.verts.shape[1] != 3:
            raise ValueError("verts must be (n, 3), got %r" % (self.verts.shape,))
        if self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError("faces must be (m, 3), got %r" % (self.faces.shape,))
        if len(self.faces):
            lo = int(self.faces.min())
            hi = int(self.faces.max())
            if lo < 0 or hi >= len(self.verts):
                raise ValueError("face index out of range: [%d, %d] for %d verts"
                                 % (lo, hi, len(self.verts)))
        if not isinstance(self.meta, dict):
            self.meta = {}

    # ------------------------------------------------------------ basics

    @property
    def tris(self) -> np.ndarray:
        return self.faces

    def copy(self) -> "Mesh":
        return Mesh(self.verts.copy(), self.faces.copy(), dict(self.meta))

    def __len__(self) -> int:
        return int(len(self.faces))

    def __repr__(self) -> str:  # pragma: no cover - display only
        return "Mesh(verts=%d, tris=%d)" % (len(self.verts), len(self.faces))

    # ------------------------------------------------------------ measures

    def bbox(self) -> dict:
        return bbox(self)

    def volume(self) -> float:
        return volume(self)

    def area(self) -> float:
        return area(self)

    def centroid(self) -> np.ndarray:
        return centroid(self)

    def hash(self, q: float = 1e-9) -> str:
        return canonical_hash(self, q=q)

    def topology(self) -> dict:
        return topology(self)

    def oriented(self) -> "Mesh":
        return oriented(self)

    # ------------------------------------------------------------ edits

    def transform(self, *, translate=(0.0, 0.0, 0.0),
                   rotate_deg=(0.0, 0.0, 0.0), scale=1.0,
                   pivot=(0.0, 0.0, 0.0)) -> "Mesh":
        return transform(self, translate=translate, rotate_deg=rotate_deg,
                         scale=scale, pivot=pivot)

    def concat(self, other: "Mesh") -> "Mesh":
        return concat([self, other])


# ---------------------------------------------------------------- measures

def bbox(mesh: Mesh) -> dict:
    v = mesh.verts
    if len(v) == 0:
        z = [0.0, 0.0, 0.0]
        return {"min": z, "max": z, "size": z, "center": z, "diag": 0.0}
    lo = v.min(axis=0)
    hi = v.max(axis=0)
    size = hi - lo
    return {
        "min": [float(x) for x in lo],
        "max": [float(x) for x in hi],
        "size": [float(x) for x in size],
        "center": [float(x) for x in (lo + hi) * 0.5],
        "diag": float(np.linalg.norm(size)),
    }


def _signed_volume_and_centroid(mesh: Mesh):
    v = mesh.verts
    f = mesh.faces
    if len(f) == 0:
        return 0.0, np.zeros(3)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    six = np.einsum("ij,ij->i", a, np.cross(b, c))
    total6 = float(six.sum())
    # tetrahedron centroid = (a+b+c+0)/4, weighted by signed volume
    weighted = ((a + b + c) / 4.0) * six[:, None]
    return total6 / 6.0, weighted.sum(axis=0) / max(abs(total6), 1e-300)


def volume(mesh: Mesh) -> float:
    return abs(_signed_volume_and_centroid(mesh)[0])


def centroid(mesh: Mesh) -> np.ndarray:
    _, c = _signed_volume_and_centroid(mesh)
    if not np.all(np.isfinite(c)):
        return np.zeros(3)
    return c


def area(mesh: Mesh) -> float:
    v = mesh.verts
    f = mesh.faces
    if len(f) == 0:
        return 0.0
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() * 0.5)


def oriented(mesh: Mesh) -> Mesh:
    """Flip all faces if the mesh is inside-out."""
    signed, _ = _signed_volume_and_centroid(mesh)
    if signed < 0.0:
        return Mesh(mesh.verts.copy(), mesh.faces[:, ::-1].copy(), dict(mesh.meta))
    return mesh.copy()


# ---------------------------------------------------------------- edits

def transform(mesh: Mesh, *, translate=(0.0, 0.0, 0.0),
              rotate_deg=(0.0, 0.0, 0.0), scale=1.0,
              pivot=(0.0, 0.0, 0.0)) -> Mesh:
    """Rotate (X then Y then Z), scale, then translate about a pivot.

    All values are in metres / degrees.  The spec compiler converts mm to m.
    """
    v = mesh.verts
    p = np.asarray(pivot, dtype=np.float64)
    t = np.asarray(translate, dtype=np.float64)
    s = np.asarray(scale, dtype=np.float64)
    if s.ndim == 0:
        s = np.repeat(s, 3)

    rx, ry, rz = (np.deg2rad(float(x)) for x in rotate_deg)
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    r = mz @ my @ mx

    out = (v - p) * s @ r.T + p + t
    meta = dict(mesh.meta)
    meta["transform"] = {"translate": t.tolist(), "rotate_deg": list(rotate_deg),
                         "scale": s.tolist(), "pivot": p.tolist()}
    return Mesh(out, mesh.faces.copy(), meta)


def concat(meshes) -> Mesh:
    meshes = [m for m in meshes if m is not None and len(m.verts) > 0]
    if not meshes:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), {})
    if len(meshes) == 1:
        return meshes[0].copy()
    verts, faces = [], []
    offset = 0
    parts = []
    for m in meshes:
        verts.append(m.verts)
        faces.append(m.faces + offset)
        offset += len(m.verts)
        parts.append(dict(m.meta))
    meta = {"parts": parts}
    return Mesh(np.vstack(verts), np.vstack(faces), meta)


def weld(mesh: Mesh, tol: float = 1e-9) -> Mesh:
    """Merge vertices closer than `tol` and drop degenerate/duplicate faces."""
    if len(mesh.verts) == 0:
        return mesh.copy()
    q = np.round(mesh.verts / max(float(tol), 1e-15)).astype(np.int64)
    _, idx, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    verts = mesh.verts[idx]
    faces = inv[mesh.faces].astype(np.int64)
    if len(faces):
        keep = ((faces[:, 0] != faces[:, 1]) &
                (faces[:, 1] != faces[:, 2]) &
                (faces[:, 2] != faces[:, 0]))
        faces = faces[keep]
    if len(faces):
        # rotate each face so the smallest index is first, then dedupe
        roll = np.argmin(faces, axis=1)
        rows = np.arange(len(faces))[:, None]
        cols = (roll[:, None] + np.arange(3)[None, :]) % 3
        faces = faces[rows, cols]
        faces = np.unique(faces, axis=0)
    meta = dict(mesh.meta)
    meta["welded"] = {"tol": float(tol), "removed_verts": int(len(mesh.verts) - len(verts))}
    return Mesh(verts, faces, meta)


# ---------------------------------------------------------------- topology

def _components(n_verts: int, edges: np.ndarray) -> int:
    if n_verts == 0:
        return 0
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
        if len(edges) == 0:
            return n_verts
        rows = np.concatenate([edges[:, 0], edges[:, 1]])
        cols = np.concatenate([edges[:, 1], edges[:, 0]])
        adj = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)),
                         shape=(n_verts, n_verts))
        n, _ = connected_components(adj, directed=False)
        return int(n)
    except Exception:  # noqa: BLE001 - fallback must not hide geometry
        parent = np.arange(n_verts, dtype=np.int64)

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in edges:
            ra, rb = find(int(a)), find(int(b))
            if ra != rb:
                parent[rb] = ra
        roots = {find(i) for i in range(n_verts)}
        return len(roots)


def _unique_undirected(edges: np.ndarray, n_verts: int):
    base = max(1, int(n_verts))
    lo = np.minimum(edges[:, 0], edges[:, 1]).astype(np.int64)
    hi = np.maximum(edges[:, 0], edges[:, 1]).astype(np.int64)
    key = lo * base + hi
    uniq, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    reps = np.column_stack([uniq // base, uniq % base])
    return reps, inv, counts, key


def topology(mesh: Mesh) -> dict:
    """Topology facts used by verify and the fact card.

    `genus` is defined only for closed orientable meshes; otherwise `None`.
    """
    v, f = mesh.verts, mesh.faces
    nv, nf = len(v), len(f)
    if nf == 0:
        return {
            "n_vertices": nv, "n_tris": 0, "n_edges": 0,
            "boundary_edges": 0, "non_manifold_edges": 0,
            "inconsistent_winding_edges": 0, "euler": nv,
            "n_components": 0, "genus": None, "closed": False,
            "degenerate_faces": 0, "empty": True,
        }

    degenerate = int(((f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) |
                      (f[:, 2] == f[:, 0])).sum())
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=0)
    reps, inv, counts, key = _unique_undirected(e, nv)
    ne = int(len(reps))
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())

    sign = np.where(e[:, 0] < e[:, 1], 1, -1).astype(np.int64)
    sums = np.zeros(ne, dtype=np.int64)
    np.add.at(sums, inv, sign)
    inconsistent = int((np.abs(sums) > 1).sum())

    euler = int(nv - ne + nf)
    ncomp = _components(nv, reps)
    closed = bool(boundary == 0 and nonmanifold == 0 and inconsistent == 0 and nf > 0)
    genus = None
    if closed and ncomp > 0:
        g = (2.0 * ncomp - euler) / 2.0
        genus = int(round(g)) if abs(g - round(g)) < 1e-6 else float(g)

    return {
        "n_vertices": int(nv),
        "n_tris": int(nf),
        "n_edges": ne,
        "boundary_edges": boundary,
        "non_manifold_edges": nonmanifold,
        "inconsistent_winding_edges": inconsistent,
        "euler": euler,
        "n_components": int(ncomp),
        "genus": genus,
        "closed": closed,
        "degenerate_faces": degenerate,
        "empty": False,
    }


# ---------------------------------------------------------------- hashing

def canonical_hash(mesh: Mesh, q: float = 1e-9) -> str:
    """Order-independent content hash.

    Vertices are quantised, lexicographically sorted, and faces are rotated
    to their smallest index before sorting.  Two meshes that differ only in
    construction order therefore share a hash.
    """
    if len(mesh.verts) == 0:
        return hashlib.sha256(b"atta:empty").hexdigest()
    vq = np.round(mesh.verts / max(float(q), 1e-15)).astype(np.int64)
    order = np.lexsort((vq[:, 2], vq[:, 1], vq[:, 0]))
    vq = vq[order]
    remap = np.empty(len(order), dtype=np.int64)
    remap[order] = np.arange(len(order), dtype=np.int64)
    f = remap[mesh.faces]
    if len(f):
        roll = np.argmin(f, axis=1)
        rows = np.arange(len(f))[:, None]
        cols = (roll[:, None] + np.arange(3)[None, :]) % 3
        f = f[rows, cols]
        f = f[np.lexsort((f[:, 2], f[:, 1], f[:, 0]))]
    h = hashlib.sha256()
    h.update(b"atta.mesh/v1\n")
    h.update(str(vq.shape).encode("ascii"))
    h.update(vq.tobytes())
    h.update(str(f.shape).encode("ascii"))
    h.update(f.tobytes())
    return h.hexdigest()
