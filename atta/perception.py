# -*- coding: utf-8 -*-
"""Text/ASCII perception for LLMs — no render, no GPU, no Blender.

The loop this enables is deliberately cheap::

    model(draft) -> perceive -> model(patches) -> perceive -> verify

A perception pack contains four things:

* ``text``       one compact sentence with the numbers that matter;
* ``traits``     aspect, sphericity, convexity, symmetry, estimated holes;
* ``views``      low-resolution ASCII silhouettes (front/top/right);
* ``sections``   ASCII occupancy at a few Z fractions.

It is a *viewfinder*, not a verdict.  ``verify`` remains the only gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import time

import numpy as np

from . import features, metrology, probe, recipes
from . import mesh as M
from .errors import AttaError

METHOD = "atta.perceive/v1"
DEFAULT_VIEWS = ("front", "top", "right")
DEFAULT_SECTIONS = (0.5,)


# ---------------------------------------------------------------- helpers

def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 1)


def _size(mesh: M.Mesh) -> list[float]:
    b = M.bbox(mesh)
    return [round(float(x), 3) for x in b["size"]]


def _ascii(occ: np.ndarray, solid: str = "#", empty: str = ".") -> list[str]:
    return ["".join(solid if bool(b) else empty for b in row) for row in occ]


def _points_in_poly(x: np.ndarray, y: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon, vectorised over the grid."""
    inside = np.zeros(x.shape, dtype=bool)
    n = len(pts)
    if n < 3:
        return inside
    for i in range(n):
        x1, y1 = float(pts[i, 0]), float(pts[i, 1])
        x2, y2 = float(pts[(i + 1) % n, 0]), float(pts[(i + 1) % n, 1])
        cond = (y1 > y) != (y2 > y)
        if not cond.any():
            continue
        xint = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-15) + x1
        inside ^= cond & (x < xint)
    return inside


def _section_ascii(mesh: M.Mesh, fraction: float, res: int) -> dict:
    b = M.bbox(mesh)
    z0, z1 = float(b["min"][2]), float(b["max"][2])
    z_mm = (z0 + (z1 - z0) * float(fraction)) * 1000.0
    # probe.section offsets from the bbox centre, not from z=0.
    offset_mm = z_mm - float(b["center"][2]) * 1000.0
    sec = probe.section(mesh, plane="z", offset_mm=offset_mm, res=res)
    loops = [np.asarray(loop["points_mm"], dtype=np.float64)
             for loop in sec.get("loops") or []
             if len(loop.get("points_mm") or []) >= 3]
    out = {
        "plane": "z",
        "at_mm": round(z_mm, 3),
        "fraction": round(float(fraction), 4),
        "n_loops": int(sec.get("n_loops") or len(loops)),
        "areas_mm2": [round(float(loop.get("area_mm2") or 0.0), 3)
                      for loop in (sec.get("loops") or [])][:4],
        "ascii": [],
        "occupancy": 0.0,
    }
    if not loops:
        return out
    pts = np.vstack(loops)
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    span = max(float(hi[0] - lo[0]), float(hi[1] - lo[1]), 1e-9)
    cx, cy = (lo + hi) * 0.5
    xs = np.linspace(cx - span * 0.55, cx + span * 0.55, res)
    ys = np.linspace(cy + span * 0.55, cy - span * 0.55, res)
    gx, gy = np.meshgrid(xs, ys)
    occ = np.zeros((res, res), dtype=bool)
    for loop in loops:
        occ ^= _points_in_poly(gx, gy, loop)
    out["ascii"] = _ascii(occ)
    out["occupancy"] = round(float(occ.sum()) / float(res * res), 4)
    return out


def _convexity(mesh: M.Mesh):
    try:
        from scipy.spatial import ConvexHull
        if len(mesh.verts) < 4:
            return None
        hull = ConvexHull(mesh.verts)
        hv = float(abs(hull.volume))
        if hv <= 0.0:
            return None
        return round(float(mesh.volume()) / hv, 4)
    except Exception:  # noqa: BLE001 - a trait, not a gate
        return None


def _symmetry(mesh: M.Mesh) -> dict:
    """Mirror symmetry about the bbox centre, by nearest-vertex distance."""
    out = {"x": None, "y": None, "z": None, "tol_ratio": 0.01}
    try:
        from scipy.spatial import cKDTree
        v = mesh.verts
        if len(v) == 0:
            return out
        b = M.bbox(mesh)
        c = np.asarray(b["center"], dtype=np.float64)
        diag = max(float(b["diag"]), 1e-9)
        tree = cKDTree(v)
        for axis, name in enumerate(("x", "y", "z")):
            mirrored = v.copy()
            mirrored[:, axis] = 2.0 * c[axis] - v[:, axis]
            dist, _ = tree.query(mirrored, k=1)
            out[name] = bool(float(np.percentile(dist, 95)) <= diag * 0.01)
    except Exception:  # noqa: BLE001
        pass
    return out


def _interior_holes(occ: np.ndarray) -> int:
    """Count empty 4-connected regions that do not touch the border."""
    try:
        from scipy import ndimage
        empty = ~occ
        labels, n = ndimage.label(empty)
        if n == 0:
            return 0
        border = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
        border.discard(0)
        count = 0
        for label in range(1, n + 1):
            if label in border:
                continue
            area = int((labels == label).sum())
            if area >= 4:
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return 0


def _traits(mesh: M.Mesh, *, wall: bool = False, curvature: bool = False) -> dict:
    b = M.bbox(mesh)
    size = [float(x) for x in b["size"]]
    diag = float(b["diag"])
    vol = float(mesh.volume())
    area = float(mesh.area())
    topo = M.topology(mesh)
    dih = metrology.dihedral(mesh)
    m = max(size) if size else 0.0
    aspect = [round(x / m, 4) if m > 0 else 0.0 for x in size]
    sphericity = (math.pi ** (1.0 / 3.0) * (6.0 * vol) ** (2.0 / 3.0) / area
                  if area > 0 else None)
    bbox_vol = size[0] * size[1] * size[2] if len(size) == 3 else 0.0
    sil = probe.silhouette(mesh, views=("front", "top"), res=28)
    holes = {}
    for name, view in sil["views"].items():
        occ = np.array([[c == "1" for c in row] for row in view["bitmap"]], dtype=bool)
        holes[name] = _interior_holes(occ)
    wall_stats = metrology.wall_thickness(mesh) if wall else None
    curv = metrology.curvature(mesh) if curvature else None
    return {
        "size_mm": [round(x * 1000.0, 3) for x in size],
        "aspect": aspect,
        "diag_mm": round(diag * 1000.0, 3),
        "volume_mm3": round(vol * 1e9, 3),
        "area_mm2": round(area * 1e6, 3),
        "bbox_fill": round(vol / bbox_vol, 4) if bbox_vol > 0 else None,
        "sphericity": round(sphericity, 4) if sphericity is not None else None,
        "convexity": _convexity(mesh),
        "symmetry": _symmetry(mesh),
        "holes_est": holes,
        "closed": bool(topo["closed"]),
        "genus": topo["genus"],
        "components": int(topo["n_components"]),
        "n_tris": int(topo["n_tris"]),
        "dihedral_p95_deg": dih.get("p95_deg"),
        "dihedral_max_deg": dih.get("max_deg"),
        "sharp_edges_gt30": dih.get("count_gt_30"),
        "wall": wall_stats,
        "curvature": curv,
        "exact": False,
    }


def _text(name: str, tr: dict) -> str:
    s = tr["size_mm"]
    a = tr["aspect"]
    bits = [
        "%s: %.1fx%.1fx%.1f mm" % (name, s[0], s[1], s[2]),
        "aspect %.2f:%.2f:%.2f" % (a[0], a[1], a[2]),
        "V=%.2f ml" % (tr["volume_mm3"] / 1000.0),
        "closed=%s" % ("yes" if tr["closed"] else "no"),
        "genus=%s" % ("?" if tr["genus"] is None else tr["genus"]),
        "comps=%d" % tr["components"],
    ]
    if tr.get("convexity") is not None:
        bits.append("convex=%.2f" % tr["convexity"])
    sym = tr.get("symmetry") or {}
    axes = [k for k in ("x", "y", "z") if sym.get(k)]
    bits.append("sym=%s" % ("".join(axes) if axes else "none"))
    holes = tr.get("holes_est") or {}
    bits.append("holes~%d" % max(holes.values() or [0]))
    if tr.get("dihedral_p95_deg") is not None:
        bits.append("dih95=%.1f" % tr["dihedral_p95_deg"])
    if (tr.get("wall") or {}).get("min_mm") is not None:
        bits.append("wall=%.2f mm" % tr["wall"]["min_mm"])
    return "; ".join(bits)[:360]


def _signature(views: dict, traits: dict) -> dict:
    payload = {
        "views": {k: v.get("bitmap") for k, v in (views or {}).items()},
        "traits": {k: traits.get(k) for k in (
            "size_mm", "aspect", "volume_mm3", "area_mm2", "closed", "genus",
            "components", "convexity", "symmetry", "holes_est")},
    }
    raw = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "method": "atta.signature/v1"}


# ---------------------------------------------------------------- public

def perceive(mesh: M.Mesh, record: dict | None = None, *,
             views=DEFAULT_VIEWS, res: int = 24,
             sections=DEFAULT_SECTIONS, wall: bool = False,
             curvature: bool = False) -> dict:
    """A cheap viewfinder: text + traits + ASCII silhouettes + sections."""
    t0 = time.perf_counter()
    rec = dict(record or {})
    res = max(8, min(64, int(res)))
    view_list = [str(v) for v in (views or DEFAULT_VIEWS)][:6]
    sec_list = [float(s) for s in (sections if sections is not None else DEFAULT_SECTIONS)]
    tr = _traits(mesh, wall=wall, curvature=curvature)
    name = str(rec.get("name") or rec.get("model_id") or "model")
    sil = probe.silhouette(mesh, views=tuple(view_list), res=res)
    view_out = {
        name_: {
            "ascii": v.get("ascii") or [],
            "bitmap": v.get("bitmap") or [],
            "coverage": v.get("coverage", 0.0),
            "legend": "# solid, . empty",
        }
        for name_, v in (sil.get("views") or {}).items()
    }
    section_out = [_section_ascii(mesh, frac, res) for frac in sec_list]
    pack = {
        "schema": "atta.perceive/v1",
        "model_id": rec.get("model_id"),
        "name": name,
        "text": _text(name, tr),
        "traits": tr,
        "views": view_out,
        "sections": section_out,
        "signature": _signature(view_out, tr),
        "units": "mm",
        "exact": False,
        "method": METHOD,
        "ms": _ms(t0),
    }
    return _jsonable(pack)


def diff(before: M.Mesh, after: M.Mesh, *, views=DEFAULT_VIEWS,
         res: int = 24) -> dict:
    """Silhouette IoU + ASCII +/- diff between two meshes."""
    t0 = time.perf_counter()
    views = tuple(str(v) for v in (views or DEFAULT_VIEWS))
    a = probe.silhouette(before, views=views, res=res)
    b = probe.silhouette(after, views=views, res=res)
    out = {}
    for name in views:
        va = a["views"].get(name) or {}
        vb = b["views"].get(name) or {}
        ma = np.array([[c == "1" for c in row] for row in (va.get("bitmap") or [])], dtype=bool)
        mb = np.array([[c == "1" for c in row] for row in (vb.get("bitmap") or [])], dtype=bool)
        if ma.shape != mb.shape:
            ma = mb = np.zeros((res, res), dtype=bool)
        inter = int((ma & mb).sum())
        union = int((ma | mb).sum())
        rows = []
        for i in range(res):
            row = []
            for j in range(res):
                if ma[i, j] and mb[i, j]:
                    row.append("#")
                elif mb[i, j]:
                    row.append("+")
                elif ma[i, j]:
                    row.append("-")
                else:
                    row.append(".")
            rows.append("".join(row))
        out[name] = {
            "iou": round(inter / union, 4) if union else 1.0,
            "ascii": rows,
            "legend": "# both, + added, - removed, . empty",
        }
    ba, bb = M.bbox(before), M.bbox(after)
    return _jsonable({
        "schema": "atta.diff/v1",
        "views": out,
        "bbox_before_mm": ba["size"],
        "bbox_after_mm": bb["size"],
        "bbox_delta_mm": [round(float(x) - float(y), 3) for x, y in zip(bb["size"], ba["size"])],
        "volume_before_mm3": round(before.volume() * 1e9, 3),
        "volume_after_mm3": round(after.volume() * 1e9, 3),
        "volume_delta_mm3": round((after.volume() - before.volume()) * 1e9, 3),
        "area_delta_mm2": round((after.area() - before.area()) * 1e6, 3),
        "topology_before": M.topology(before),
        "topology_after": M.topology(after),
        "units": "mm",
        "exact": False,
        "method": "atta.diff/v1",
        "ms": _ms(t0),
    })


# ---------------------------------------------------------------- trace

def trace(record: dict, mesh: M.Mesh | None = None) -> dict:
    """Construction trace: what was built, in order, with measured deltas."""
    t0 = time.perf_counter()
    rec = dict(record or {})
    spec = rec.get("spec") or {}
    model = spec.get("model") or {}
    kind = model.get("kind")
    if kind == "recipe" or rec.get("recipe"):
        rid = rec.get("recipe") or model.get("id")
        try:
            recipe = recipes.get(rid)
            description = recipe.description
            tags = list(recipe.tags)
            presets = list(recipe.presets)
        except Exception:  # noqa: BLE001
            description, tags, presets = "", [], []
        step = {
            "id": rid,
            "op": "recipe",
            "description": description,
            "tags": tags,
            "preset": rec.get("preset"),
            "params": rec.get("params") or {},
            "presets": presets,
        }
        if mesh is not None:
            step.update(_step_facts(mesh))
        return _jsonable({
            "schema": "atta.trace/v1", "kind": "recipe", "recipe": rid,
            "steps": [step], "text": "recipe %s (%s) with %d params"
            % (rid, description, len(step["params"])), "units": "mm",
            "exact": False, "method": "atta.trace/v1", "ms": _ms(t0),
        })

    feats = list(model.get("features") or [])
    units = model.get("units", "mm")
    params = model.get("params") or {}
    steps = []
    prev_vol = None
    for i, feat in enumerate(feats):
        fid = str(feat.get("id"))
        row = {
            "index": i, "id": fid, "op": feat.get("op"),
            "inputs": list(feat.get("inputs") or []),
            "args": feat.get("args") or {},
        }
        try:
            m = features.evaluate(feats[:i + 1], params=params, units=units,
                                  output=fid)
            row.update(_step_facts(m))
            if prev_vol is not None:
                row["volume_delta_mm3"] = round(row["volume_mm3"] - prev_vol, 3)
            prev_vol = row["volume_mm3"]
        except AttaError as exc:
            row["error"] = exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            row["error"] = {"code": type(exc).__name__, "message": str(exc)}
        steps.append(row)
    text = "graph: " + " -> ".join("%s(%s)" % (s.get("id"), s.get("op")) for s in steps)
    return _jsonable({
        "schema": "atta.trace/v1", "kind": "graph",
        "output": model.get("output"), "steps": steps,
        "text": text[:500], "units": "mm", "exact": False,
        "method": "atta.trace/v1", "ms": _ms(t0),
    })


def _step_facts(mesh: M.Mesh) -> dict:
    topo = M.topology(mesh)
    return {
        "bbox_mm": [round(float(x) * 1000.0, 3) for x in M.bbox(mesh)["size"]],
        "volume_mm3": round(mesh.volume() * 1e9, 3),
        "n_tris": int(topo["n_tris"]),
        "closed": bool(topo["closed"]),
        "genus": topo["genus"],
        "components": int(topo["n_components"]),
    }


__all__ = ["perceive", "diff", "trace", "METHOD", "DEFAULT_VIEWS", "DEFAULT_SECTIONS"]
