# -*- coding: utf-8 -*-
"""A low-token fact card: what is it, how big, is it a solid?

Facts are measured from geometry, never inferred from a render.  ``one_line``
is the exact sentence an agent should quote back to a user; everything else is
structured evidence behind it.
"""

from __future__ import annotations

from . import mesh as M
from . import metrology

__all__ = ["facts", "one_line"]


def _name(rec: dict) -> str:
    return str(rec.get("name") or rec.get("model_id") or rec.get("recipe")
               or "model")[:40]


def one_line(f: dict) -> str:
    name = str(f.get("name") or "model")[:40]
    size = f.get("size_mm") or [0.0, 0.0, 0.0]
    genus = f.get("genus")
    bits = [
        "%s: %.1fx%.1fx%.1f mm" % (name, size[0], size[1], size[2]),
        "V=%.2f ml" % float(f.get("volume_ml") or 0.0),
        "A=%.2f cm2" % (float(f.get("area_mm2") or 0.0) / 100.0),
        "closed=%s" % ("yes" if f.get("closed") else "no"),
        "genus=%s" % ("?" if genus is None else genus),
        "comps=%d" % int(f.get("components") or 0),
        "tris=%d" % int(f.get("n_tris") or 0),
    ]
    wall = f.get("wall") or {}
    if wall.get("min_mm") is not None:
        bits.append("wall=%.2f mm" % float(wall["min_mm"]))
    dih = f.get("dihedral") or {}
    if dih.get("p95_deg") is not None:
        bits.append("dih95=%.1f deg" % float(dih["p95_deg"]))
    if f.get("material"):
        bits.append("mat=%s" % f["material"])
    text = "; ".join(bits)
    return text[:199]


def facts(mesh: M.Mesh, record=None, *, wall: bool = False,
          curvature: bool = False) -> dict:
    rec = dict(record or {})
    metrics = rec.pop("_metrics", None)
    if not isinstance(metrics, dict):
        metrics = metrology.measure(mesh, wall=wall, dihedral=True,
                                    curvature=curvature)
    if wall and "wall" not in metrics:
        metrics["wall"] = metrology.wall_thickness(mesh)
    if curvature and "curvature" not in metrics:
        metrics["curvature"] = metrology.curvature(mesh)
    if "dihedral" not in metrics:
        metrics["dihedral"] = metrology.dihedral(mesh)
    topo = metrics.get("topology") or M.topology(mesh)
    dih = metrics["dihedral"]
    vol_mm3 = float(metrics.get("volume_mm3") or 0.0)
    bbox = metrics.get("bbox_mm") or metrology.bbox_mm(mesh)

    out = {
        "model_id": rec.get("model_id"),
        "recipe": rec.get("recipe"),
        "name": rec.get("name"),
        "quality": rec.get("quality"),
        "bbox_mm": bbox,
        "size_mm": bbox.get("size"),
        "volume_mm3": round(vol_mm3, 4),
        "volume_ml": round(vol_mm3 / 1000.0, 4),
        "area_mm2": round(float(metrics.get("area_mm2") or 0.0), 4),
        "centroid_mm": metrics.get("centroid_mm"),
        "closed": bool(topo["closed"]),
        "genus": topo["genus"],
        "components": int(topo["n_components"]),
        "n_components": int(topo["n_components"]),
        "n_vertices": int(topo["n_vertices"]),
        "n_tris": int(topo["n_tris"]),
        "euler": int(topo["euler"]),
        "boundary_edges": int(topo["boundary_edges"]),
        "non_manifold_edges": int(topo["non_manifold_edges"]),
        "inconsistent_winding_edges": int(topo["inconsistent_winding_edges"]),
        "wall": metrics.get("wall"),
        "dihedral": dih,
        "dihedral_p95_deg": dih.get("p95_deg"),
        "dihedral_max_deg": dih.get("max_deg"),
        "curvature": metrics.get("curvature"),
        "material": rec.get("material"),
        "units": "mm",
        "exact": False,
        "method": "atta.facts/v1",
    }
    out["one_line"] = one_line(out)
    return out
