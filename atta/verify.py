# -*- coding: utf-8 -*-
"""Three-state verification with an auditable receipt.

    pass      every check passed
    degraded  only warn-level checks failed
    fail      at least one error-level check failed

Assumptions ride along in the receipt as provenance.  They never change the
verdict.  Every failed check carries a concrete ``fix``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from . import facts as F
from . import mesh as M
from . import metrology
from . import probe

METHOD = "atta.verify/v3"
LEVELS = ("basic", "standard", "strict")
SUPPORTED = (
    "bbox", "volume_mm3", "min_wall_mm", "genus", "closed", "n_components",
    "dihedral_max_deg", "curvature_p95_deg", "clearance", "interference",
    "interfaces", "envelope", "holes", "render", "render_receipt",
    "name", "material",
)


# ---------------------------------------------------------------- helpers

def _plain(x):
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    return x


def _check(checks, cid, ok, severity="error", value=None, expected=None,
           message=None, fix=None, skipped=False):
    row = {
        "id": str(cid),
        "ok": bool(ok),
        "severity": str(severity),
        "value": _plain(value),
        "expected": _plain(expected),
        "message": str(message) if message is not None
        else ("ok" if ok else "%s failed" % cid),
        "fix": None if ok else (fix or "inspect the model and re-run verify"),
    }
    if skipped:
        # Not evaluated at this level.  Explicit, so a cheap `basic` gate can
        # never look like a full pass by silently dropping a constraint.
        row["skipped"] = True
    checks.append(row)
    return row


def _skip(checks, cid, level, why):
    """Record that a constraint was deliberately not evaluated at ``level``."""
    return _check(checks, cid, True, "info", None, None,
                  "%s not evaluated at level=%s: %s; re-run at level=standard"
                  % (cid, level, why),
                  skipped=True)


class _Malformed(Exception):
    """A constraint value that cannot be interpreted as a number."""


def _snum(value):
    """Coerce a constraint number, or raise ``_Malformed``.

    Constraint values arrive as untrusted JSON, so ``float()`` must never be
    allowed to escape as a bare ValueError: a malformed constraint is a check
    failure with a ``fix``, not a crashed verify (DEFECT 3).
    """
    if isinstance(value, bool):
        raise _Malformed("%r is a boolean, not a number" % (value,))
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise _Malformed("%r is not a number" % (value,)) from None
    if not math.isfinite(x):
        raise _Malformed("%r is not finite" % (value,))
    return x


def _window(value, spec):
    if spec is None:
        return True
    if isinstance(spec, dict):
        lo, hi = spec.get("min"), spec.get("max")
        return ((lo is None or value >= _snum(lo)) and
                (hi is None or value <= _snum(hi)))
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        return _snum(spec[0]) <= value <= _snum(spec[1])
    ref = _snum(spec)
    return abs(value - ref) <= max(1e-9, abs(ref) * 1e-6)


def _malformed(checks, cid, exc, fix):
    """Record a malformed-constraint failure row instead of raising."""
    return _check(checks, cid, False, "error", None, None,
                  "malformed constraint %s: %s" % (cid, exc), fix)


def _window_check(checks, cid, value, spec, severity, ok_msg, bad_msg, fix):
    """Evaluate one ``_window`` constraint, reporting malformed values."""
    try:
        ok = _window(value, spec)
    except _Malformed as exc:
        return _malformed(checks, cid, exc,
                          "pass a finite number, [min, max] or {min, max}")
    return _check(checks, cid, ok, severity, value, spec,
                  ok_msg if ok else bad_msg, fix)


def _bound(checks, cid, raw, fix):
    """Coerce an optional min/max bound; ``None`` stays ``None``.

    Returns ``(ok, value)``: a malformed bound is recorded as a check failure
    and reported as ``ok=False`` so the caller can skip the comparison.
    """
    if raw is None:
        return True, None
    try:
        return True, _snum(raw)
    except _Malformed as exc:
        _malformed(checks, cid, exc, fix)
        return False, None


def _rows(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [x for x in value if isinstance(x, dict)]
    return []


def _lookup(parts, name):
    if not isinstance(parts, dict) or name is None:
        return None
    if name in parts:
        return parts[name]
    low = str(name).lower()
    for key, value in parts.items():
        if str(key).lower() == low:
            return value
    return None


def _available(parts):
    if not isinstance(parts, dict) or not parts:
        return "(none)"
    return ", ".join(sorted(str(k) for k in parts))


def _receipt_dict(receipt):
    if isinstance(receipt, dict):
        return receipt
    if not receipt:
        return None
    try:
        p = Path(str(receipt))
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad receipt is a check failure
        return None
    return None


# ---------------------------------------------------------------- render

def _img_coverage(im, data):
    for key in ("coverage", "fill", "fill_ratio", "occupancy_ratio"):
        if im.get(key) is not None:
            return float(im[key])
    hits, rays = im.get("hits"), im.get("rays")
    if hits is not None and rays:
        return float(hits) / float(rays)
    for key in ("coverage", "fill", "occupancy_ratio"):
        if data.get(key) is not None:
            return float(data[key])
    return None


def _render_checks(checks, spec, receipt):
    spec = spec if isinstance(spec, dict) else {}
    data = _receipt_dict(receipt)
    if data is None:
        _check(checks, "render.gate", False, "warn", None, spec,
               "render constraint without a readable render receipt",
               "run preview(render_gate=...) and pass the receipt path or dict")
        return
    images = data.get("images")
    if images is None:
        images = data.get("views")
    if isinstance(images, dict):
        images = list(images.values())
    if not isinstance(images, list) or not images:
        _check(checks, "render.gate", False, "error", 0, spec,
               "render receipt contains no images",
               "re-run preview with at least one view")
        return
    images = [im for im in images if isinstance(im, dict)]
    if not images:
        _check(checks, "render.gate", False, "error", 0, spec,
               "render receipt images are not objects",
               "re-run preview; each image row needs width/height/samples")
        return

    need_cov = spec.get("min_coverage", spec.get("coverage"))
    if isinstance(need_cov, dict):
        need_cov = need_cov.get("min")
    if need_cov is not None:
        vals = [_img_coverage(im, data) for im in images]
        missing = any(v is None for v in vals)
        worst = None
        if not missing:
            worst = min(vals)
        ok = (not missing) and worst is not None and worst >= float(need_cov)
        _check(checks, "render.coverage", ok, "error",
               {"min": worst, "missing": missing},
               ">=%s" % need_cov,
               "rendered coverage meets the gate" if ok
               else "rendered coverage below the gate",
               "increase the model's screen coverage or use a tighter camera; "
               "re-run preview and pass the new receipt")

    empties = []
    for i, im in enumerate(images):
        cov = _img_coverage(im, data)
        if (bool(im.get("empty_frame")) or bool(im.get("empty"))
                or (cov is not None and cov <= 0.0)
                or im.get("n_pixels") == 0):
            empties.append(im.get("view", i))
    if data.get("empty_frame") is True:
        empties.append("receipt")
    _check(checks, "render.empty_frame", not empties, "error",
           empties, "no empty frames",
           "no empty frames" if not empties else "empty frame(s) detected",
           "the camera missed the model; frame the bbox and re-render")

    need_ndc = bool(spec.get("require_ndc", False)) or ("max_ndc" in spec) \
        or ("ndc" in spec)
    if need_ndc:
        max_ndc = spec.get("max_ndc", 1.0)
        ndc_spec = spec.get("ndc")
        if isinstance(ndc_spec, dict):
            max_ndc = ndc_spec.get("max", max_ndc)
        elif ndc_spec is not None and not isinstance(ndc_spec, bool):
            max_ndc = ndc_spec
        vals = []
        for im in images:
            v = im.get("worst_ndc", data.get("worst_ndc"))
            vals.append(None if v is None else float(v))
        ok = all(v is not None and v <= float(max_ndc) for v in vals)
        _check(checks, "render.ndc", ok, "error",
               {"worst": max(vals) if all(v is not None for v in vals) else None},
               "<=%s" % max_ndc,
               "all images stay inside the frame" if ok
               else "geometry leaves the frame (NDC > limit)",
               "widen the camera framing or reduce the model; re-render")

    need_res = spec.get("res")
    if need_res:
        failed = []
        for im in images:
            w, h = im.get("width", im.get("w")), im.get("height", im.get("h"))
            if w is None or h is None:
                failed.append({"view": im.get("view"), "reason": "size_missing"})
                continue
            if isinstance(need_res, (list, tuple)) and len(need_res) == 2:
                if int(w) < int(need_res[0]) or int(h) < int(need_res[1]):
                    failed.append({"view": im.get("view"), "actual": [w, h],
                                   "expected": list(need_res)})
            elif max(int(w), int(h)) < int(need_res):
                failed.append({"view": im.get("view"),
                               "actual": max(int(w), int(h)),
                               "expected": int(need_res)})
        _check(checks, "render.res", not failed, "error",
               failed, need_res,
               "render resolution meets the gate" if not failed
               else "render resolution below the gate",
               "re-run preview with res=%s" % (need_res,))

    need_samples = int(spec.get("samples") or 0)
    if need_samples:
        bad = [im.get("view") for im in images
               if int(im.get("samples") or 0) < need_samples]
        _check(checks, "render.samples", not bad, "error", bad, need_samples,
               "sample count meets the gate" if not bad
               else "sample count below the gate",
               "re-run preview with samples>=%d" % need_samples)


# ---------------------------------------------------------------- holes

def _detect_holes(mesh: M.Mesh) -> list[dict]:
    """Approximate holes from inner contours of axis-aligned mid-sections."""
    size = metrology.bbox_mm(mesh)["size"]
    found: dict = {}
    for axis, span in zip("xyz", size):
        if span <= 1e-9:
            continue
        for frac in (-0.35, 0.0, 0.35):
            try:
                sec = probe.section(mesh, plane=axis, offset_mm=frac * span,
                                    res=48)
            except Exception:  # noqa: BLE001 - holes are best-effort
                continue
            loops = [l for l in sec.get("loops", [])
                     if l.get("closed") and l.get("area_mm2", 0.0) > 1e-9]
            if len(loops) < 2:
                continue
            loops.sort(key=lambda l: -l["area_mm2"])
            outer = loops[0]
            ox = [p[0] for p in outer["points_mm"]]
            oy = [p[1] for p in outer["points_mm"]]
            ox0, ox1, oy0, oy1 = min(ox), max(ox), min(oy), max(oy)
            for loop in loops[1:]:
                area = float(loop["area_mm2"])
                perim = float(loop["perimeter_mm"])
                if area >= 0.85 * float(outer["area_mm2"]) or perim <= 0.0:
                    continue
                circ = 4.0 * math.pi * area / (perim * perim)
                if circ < 0.5:
                    continue
                lx = [p[0] for p in loop["points_mm"]]
                ly = [p[1] for p in loop["points_mm"]]
                lx0, lx1, ly0, ly1 = min(lx), max(lx), min(ly), max(ly)
                if (lx0 < ox0 - 1e-6 or lx1 > ox1 + 1e-6
                        or ly0 < oy0 - 1e-6 or ly1 > oy1 + 1e-6):
                    continue
                d = (2.0 * math.sqrt(area / math.pi) + 4.0 * area / perim) / 2.0
                center = ((lx0 + lx1) * 0.5, (ly0 + ly1) * 0.5)
                key = (axis, round(center[0] / 2.0), round(center[1] / 2.0),
                       round(d / 0.5))
                found[key] = {
                    "diameter_mm": round(d, 3),
                    "center_mm": [round(center[0], 3), round(center[1], 3)],
                    "axis": axis,
                    "plane_mm": sec.get("plane_mm"),
                    "circularity": round(circ, 3),
                }
    return sorted(found.values(), key=lambda h: -h["diameter_mm"])


# ---------------------------------------------------------------- verify

def verify(mesh: M.Mesh, *, constraints=None, expected_genus=None,
           level="standard", render_receipt=None, parts=None,
           assumptions=None) -> dict:
    level = str(level or "standard").lower()
    if level not in LEVELS:
        level = "standard"
    cons = dict(constraints or {})
    quality_sev = "error" if level == "strict" else "warn"
    topo = M.topology(mesh)
    checks: list[dict] = []

    # ------------------------------------------------ base topology gate
    _check(checks, "empty", not topo["empty"], "error", topo["n_tris"], "> 0",
           "mesh has triangles" if not topo["empty"] else "mesh is empty",
           "build the model first; an empty mesh cannot be verified")
    _check(checks, "closed", topo["closed"], "error",
           {"boundary_edges": topo["boundary_edges"],
            "non_manifold_edges": topo["non_manifold_edges"],
            "inconsistent_winding_edges": topo["inconsistent_winding_edges"]},
           "0 boundary / 0 non-manifold / 0 inconsistent",
           "closed manifold with consistent winding" if topo["closed"]
           else "mesh is not a closed manifold",
           "cap open ends, weld seams, or remesh; open shells fail delivery")
    _check(checks, "manifold", topo["non_manifold_edges"] == 0, "error",
           topo["non_manifold_edges"], 0,
           "no non-manifold edges" if topo["non_manifold_edges"] == 0
           else "non-manifold edges present",
           "split or weld the offending junction; non-manifold geometry has "
           "no reliable inside/outside")
    _check(checks, "winding", topo["inconsistent_winding_edges"] == 0, "error",
           topo["inconsistent_winding_edges"], 0,
           "face winding is consistent"
           if topo["inconsistent_winding_edges"] == 0
           else "inconsistent face winding",
           "recalculate normals outward (atta.mesh.oriented) and re-run")
    vol = metrology.volume_mm3(mesh)
    _check(checks, "volume", vol > 1e-6, "error", vol, "> 0 mm3",
           "enclosed volume is positive" if vol > 1e-6
           else "volume is zero (open, inverted, or degenerate)",
           "make the mesh closed and outward-facing before measuring volume")
    _check(checks, "degenerate_faces", topo["degenerate_faces"] == 0,
           quality_sev, topo["degenerate_faces"], 0,
           "no zero-area triangles" if topo["degenerate_faces"] == 0
           else "zero-area triangles present",
           "weld or remove degenerate triangles; they break normals and export")
    _check(checks, "components", topo["n_components"] == 1, quality_sev,
           topo["n_components"], 1,
           "single connected solid" if topo["n_components"] == 1
           else "multiple disconnected components",
           "union the parts, or verify each part separately and declare an "
           "assembly")

    # ``basic`` is a cheap gate: it may skip the *expensive geometric* checks
    # (wall, clearance/interference, hole detection, render receipts) but it
    # must never silently drop a constraint.  Skipped ones are recorded with
    # skipped=True so a cheap pass can never be mistaken for a full pass.
    gates = level != "basic"
    need_wall = gates and "min_wall_mm" in cons
    need_curv = gates and "curvature_p95_deg" in cons
    metrics = metrology.measure(mesh, wall=need_wall, dihedral=True,
                                curvature=need_curv)
    dih = metrics["dihedral"]
    wall = metrics.get("wall")
    curv = metrics.get("curvature")
    bbox = metrics["bbox_mm"]

    try:
        # -------------------------------------------- dimensional gates
        # Cheap constraints run at every level.  v1.1 wrapped this whole block
        # in ``if level != "basic"``, so a volume violation reported pass.
        bbox_spec = cons.get("bbox")
        if bbox_spec is not None:
            size = bbox["size"]
            if isinstance(bbox_spec, dict) and ("min" in bbox_spec
                                                or "max" in bbox_spec):
                mn = list(bbox_spec.get("min") or [None, None, None])
                mx = list(bbox_spec.get("max") or [None, None, None])
                for i, axis in enumerate("xyz"):
                    cid = "bbox.%s" % axis
                    lo_ok, lo = _bound(checks, cid,
                                       None if i >= len(mn) else mn[i],
                                       "pass min:[x,y,z] as finite numbers")
                    hi_ok, hi = _bound(checks, cid,
                                       None if i >= len(mx) else mx[i],
                                       "pass max:[x,y,z] as finite numbers")
                    if not (lo_ok and hi_ok):
                        continue
                    ok = ((lo is None or bbox["min"][i] >= lo) and
                          (hi is None or bbox["max"][i] <= hi))
                    _check(checks, cid, ok, "error",
                           {"min": bbox["min"][i], "max": bbox["max"][i]},
                           {"min": lo, "max": hi},
                           "bbox %s inside bounds" % axis if ok
                           else "bbox %s outside bounds" % axis,
                           "move/resize the part; bbox is the world envelope")
            else:
                for i, axis in enumerate("xyz"):
                    spec = None
                    if isinstance(bbox_spec, dict):
                        spec = bbox_spec.get(axis)
                        if spec is None and isinstance(bbox_spec.get("size"),
                                                       (list, tuple)):
                            sz = bbox_spec["size"]
                            spec = sz[i] if i < len(sz) else None
                    else:
                        spec = bbox_spec
                    if spec is None:
                        continue
                    _window_check(checks, "bbox.%s" % axis, float(size[i]), spec,
                                  "error",
                                  "bbox %s size in window" % axis,
                                  "bbox %s size outside window" % axis,
                                  "change the %s dimension; use atta_solve or a "
                                  "recipe parameter" % axis)

        if "volume_mm3" in cons:
            _window_check(checks, "volume_mm3", float(vol), cons["volume_mm3"],
                          "error", "volume in window", "volume outside window",
                          "change wall thickness or overall size; volume scales "
                          "with the cube of dimensions")

        if "envelope" in cons:
            spec = cons["envelope"]
            if not isinstance(spec, dict):
                _malformed(checks, "envelope", _Malformed("%r is not an object"
                                                          % (spec,)),
                           "Use {min:[x,y,z], max:[x,y,z]}.")
            else:
                mn = list(spec.get("min") or [None, None, None])
                mx = list(spec.get("max") or [None, None, None])
                for i, axis in enumerate("xyz"):
                    cid = "envelope.%s" % axis
                    lo_ok, lo = _bound(checks, cid,
                                       None if i >= len(mn) else mn[i],
                                       "pass min:[x,y,z] as finite numbers")
                    hi_ok, hi = _bound(checks, cid,
                                       None if i >= len(mx) else mx[i],
                                       "pass max:[x,y,z] as finite numbers")
                    if not (lo_ok and hi_ok):
                        continue
                    ok = ((lo is None or bbox["min"][i] >= lo) and
                          (hi is None or bbox["max"][i] <= hi))
                    _check(checks, cid, ok, "error",
                           {"min": bbox["min"][i], "max": bbox["max"][i]},
                           {"min": lo, "max": hi},
                           "envelope %s inside bounds" % axis if ok
                           else "envelope %s outside bounds" % axis,
                           "translate the part back into the world envelope")

        if "min_wall_mm" in cons:
            if not gates:
                _skip(checks, "min_wall_mm", level,
                      "wall thickness needs ray sampling")
            else:
                spec = cons["min_wall_mm"]
                need_ok, need = _bound(
                    checks, "min_wall_mm",
                    spec.get("min", 0.0) if isinstance(spec, dict) else spec,
                    "pass min_wall_mm: 2.5 or {min: 2.5}")
                if need_ok:
                    got = None if not wall else wall.get("min_mm")
                    if got is None:
                        # Honest reporting: metrology says the part has no
                        # measurable wall (a solid), so there is nothing to
                        # compare.  Not a failure, and not a number either.
                        why = (wall or {}).get("note") \
                            or "no opposing surface was found"
                        _check(checks, "min_wall_mm", True, "info", None,
                               ">=%s" % need,
                               "not applicable: %s" % why,
                               skipped=True)
                    else:
                        ok = float(got) >= need
                        _check(checks, "min_wall_mm", ok, quality_sev, got,
                               ">=%s" % need,
                               "sampled wall meets the limit" if ok
                               else "sampled wall below the limit",
                               "thicken the thin wall or reduce the cavity; "
                               "wall is a ray sample, so confirm the thin "
                               "region explicitly")

        if "genus" in cons:
            g = topo["genus"]
            if g is None:
                _check(checks, "genus", False, "error", None, cons["genus"],
                       "genus is undefined for an open mesh",
                       "cap the mesh; genus needs a closed solid")
            else:
                _window_check(checks, "genus", float(g), cons["genus"], "error",
                              "genus matches", "genus does not match",
                              "check through-holes / disconnected handles; "
                              "genus is defined only for closed meshes")

        if "closed" in cons:
            want = bool(cons["closed"])
            ok = bool(topo["closed"]) == want
            _check(checks, "closed.expected", ok, "error", topo["closed"], want,
                   "closed matches the expected state" if ok
                   else "closed state differs from expected",
                   "cap or open the shell as intended, then re-verify")

        if "n_components" in cons:
            _window_check(checks, "n_components",
                          float(topo["n_components"]), cons["n_components"],
                          "error", "component count in window",
                          "component count outside window",
                          "union the intended parts or split the unintended ones")

        if "dihedral_max_deg" in cons:
            limit_ok, limit = _bound(checks, "dihedral_max_deg",
                                     cons["dihedral_max_deg"],
                                     "pass dihedral_max_deg: 30 (degrees)")
            if limit_ok:
                got = float(dih["max_deg"])
                ok = got <= limit
                _check(checks, "dihedral_max_deg", ok, quality_sev, got,
                       "<=%.3f" % limit,
                       "max dihedral within limit" if ok
                       else "max dihedral exceeds the limit",
                       "add bevel/subdivision or raise the allowed dihedral "
                       "for intentional hard edges")

        if "curvature_p95_deg" in cons:
            if not gates:
                _skip(checks, "curvature_p95_deg", level,
                      "curvature needs per-vertex sampling")
            else:
                limit_ok, limit = _bound(checks, "curvature_p95_deg",
                                         cons["curvature_p95_deg"],
                                         "pass curvature_p95_deg: 20 "
                                         "(degrees)")
                if limit_ok:
                    got = float(curv["p95_deg"]) if curv else 0.0
                    ok = got <= limit
                    _check(checks, "curvature_p95_deg", ok, quality_sev, got,
                           "<=%.3f" % limit,
                           "curvature p95 within limit" if ok
                           else "curvature p95 exceeds the limit",
                           "increase profile/segment sampling or smooth the "
                           "surface; curvature is a vertex-normal proxy")

        # -------------------------------------------- pair gates
        if cons.get("clearance") and not gates:
            _skip(checks, "clearance", level, "clearance needs mesh distance queries")
        for i, row in enumerate([] if not gates
                                else _rows(cons.get("clearance"))):
            a = row.get("a")
            b = row.get("b")
            need_ok, need = _bound(checks, "clearance.%d" % i,
                                   row.get("min", row.get("min_mm", 0.0)),
                                   "pass each clearance row a min in mm")
            pa, pb = _lookup(parts, a), _lookup(parts, b)
            if pa is None or pb is None:
                _check(checks, "clearance.%d" % i, False, "error",
                       {"a": a, "b": b}, ">=%s" % need,
                       "clearance references an unknown part",
                       "pass parts={name: Mesh}; available: %s"
                       % _available(parts))
                continue
            if not need_ok:
                continue
            got = probe.clearance(pa, pb)["min_mm"]
            ok = float(got) >= need
            _check(checks, "clearance.%d" % i, ok, "error",
                   {"a": a, "b": b, "min_mm": got}, ">=%s" % need,
                   "clearance %s/%s meets the limit" % (a, b) if ok
                   else "clearance %s/%s is too small" % (a, b),
                   "increase the gap between '%s' and '%s' or shrink the "
                   "mating feature" % (a, b))

        if cons.get("interference") and not gates:
            _skip(checks, "interference", level,
                  "interference needs a boolean intersection")
        for i, row in enumerate([] if not gates
                                else _rows(cons.get("interference"))):
            a = row.get("a")
            b = row.get("b")
            cap_ok, cap = _bound(checks, "interference.%d" % i,
                                  row.get("max", row.get("max_mm3", 1e-6)),
                                  "pass each interference row a max in mm3")
            pa, pb = _lookup(parts, a), _lookup(parts, b)
            if pa is None or pb is None:
                _check(checks, "interference.%d" % i, False, "error",
                       {"a": a, "b": b}, "<=%s mm3" % cap,
                       "interference references an unknown part",
                       "pass parts={name: Mesh}; available: %s"
                       % _available(parts))
                continue
            if not cap_ok:
                continue
            info = probe.interference(pa, pb)
            got = float(info["volume_mm3"])
            ok = got <= cap
            _check(checks, "interference.%d" % i, ok, "error",
                   {"a": a, "b": b, "volume_mm3": got}, "<=%s mm3" % cap,
                   "no interference between %s/%s" % (a, b) if ok
                   else "interference between %s/%s" % (a, b),
                   "separate or resize '%s' and '%s'; boolean intersection "
                   "volume is %s mm3" % (a, b, got))

        if cons.get("interfaces") and not gates:
            _skip(checks, "interface", level,
                  "interface needs a boolean intersection")
        for i, row in enumerate([] if not gates
                                else _rows(cons.get("interfaces"))):
            a = row.get("a")
            b = row.get("b")
            cap_ok, cap = _bound(checks, "interface.%d" % i,
                                  row.get("max_mm3", row.get("max", 1e-6)),
                                  "pass each interfaces row a max_mm3")
            pa, pb = _lookup(parts, a), _lookup(parts, b)
            if pa is None or pb is None:
                _check(checks, "interface.%d" % i, False, "error",
                       {"a": a, "b": b}, "<=%s mm3" % cap,
                       "interface references an unknown part",
                       "pass parts={name: Mesh}; available: %s"
                       % _available(parts))
                continue
            if not cap_ok:
                continue
            got = float(probe.interference(pa, pb)["volume_mm3"])
            ok = got <= cap
            _check(checks, "interface.%d" % i, ok, "error",
                   {"a": a, "b": b, "volume_mm3": got}, "<=%s mm3" % cap,
                   "designed overlap within allowance" if ok
                   else "designed overlap exceeds the allowance",
                   "raise max_mm3 only if the press-fit overlap is intended; "
                   "otherwise change the fit")

        if "holes" in cons:
            if not gates:
                _skip(checks, "holes", level,
                      "hole detection needs mid-sections")
            else:
                actual = _detect_holes(mesh)
                for i, row in enumerate(_rows(cons["holes"])):
                    cid = "holes.%d" % i
                    diameter = row.get("diameter", row.get("d"))
                    tol_ok, tol = _bound(checks, cid,
                                         row.get("tolerance", 0.05),
                                         "pass tolerance in mm")
                    cnt_ok, count = _bound(checks, cid, row.get("count", 0),
                                           "pass count as a whole number")
                    if not (tol_ok and cnt_ok):
                        continue
                    if diameter is None:
                        _check(checks, cid, False, "error", None,
                               "a diameter in mm",
                               "hole row has no diameter",
                               "add diameter (mm) to the holes row")
                        continue
                    try:
                        want_d = _snum(diameter)
                    except _Malformed as exc:
                        _malformed(checks, cid, exc, "pass diameter in mm")
                        continue
                    matched = [h for h in actual
                               if abs(float(h["diameter_mm"]) - want_d) <= tol]
                    want_n = count if count else 1
                    ok = len(matched) >= want_n
                    _check(checks, cid, ok, "warn",
                           {"detected": len(actual), "matched": len(matched)},
                           ">=" + str(want_n),
                           "hole count/diameter matches" if ok
                           else "hole count/diameter does not match",
                           "check hole diameter and position; detection uses "
                           "mid-sections (exact:false)")

        if "render" in cons or render_receipt is not None:
            if not gates:
                _skip(checks, "render.gate", level,
                      "the render gate needs a receipt and image stats")
            else:
                _render_checks(checks,
                               cons["render"] if "render" in cons else {},
                               render_receipt if render_receipt is not None
                               else cons.get("render_receipt"))
    except Exception as exc:  # noqa: BLE001 - a check must never crash verify
        # Last-resort containment for DEFECT 3: constraint evaluation handles
        # untrusted JSON, so an unforeseen malformed value becomes an
        # error-level check with a fix instead of a traceback.
        _check(checks, "constraints.internal", False, "error", None, None,
               "constraint evaluation failed: %s: %s"
               % (type(exc).__name__, exc),
               "report this as a bug with the constraint JSON; verify the "
               "constraint types are numbers/windows")

    unknown = [k for k in cons if k not in SUPPORTED]
    for key in unknown:
        _check(checks, "constraint.%s" % key, False, "warn", key, SUPPORTED,
               "unknown constraint key %r is ignored" % key,
               "use one of: %s" % ", ".join(SUPPORTED))

    if expected_genus is not None and "genus" not in cons:
        g = topo["genus"]
        ok = g is not None and abs(float(g) - float(expected_genus)) < 0.5
        _check(checks, "genus.expected", ok, "error", g, expected_genus,
               "genus matches the expected value" if ok
               else "genus differs from the expected value",
               "check through-holes; genus needs a closed mesh")

    evaluated = [c for c in checks if not c.get("skipped")]
    skipped = [c for c in checks if c.get("skipped")]
    passed = sum(1 for c in evaluated if c["ok"])
    failed = [c for c in evaluated if not c["ok"]]
    errors = [c for c in failed if c["severity"] == "error"]
    warns = [c for c in failed if c["severity"] == "warn"]
    verdict = "fail" if errors else ("degraded" if warns else "pass")
    # ``score`` is the pass rate over checks that actually ran: counting a
    # constraint we chose not to evaluate would flatter a cheap basic gate.
    score = round(float(passed) / max(1, len(evaluated)), 4)

    fixes = []
    for row in failed:
        if row.get("fix") and row["fix"] not in fixes:
            fixes.append(row["fix"])
    if verdict == "pass":
        nxt = ["preview for judgement images",
               "deliver GLB/OBJ together with this receipt"]
    elif verdict == "degraded":
        nxt = fixes[:4] + ["accept degraded only with allow_degraded=true "
                           "and a written reason"]
    else:
        nxt = fixes[:4] + ["re-run atta_model, then verify again"]

    fact_card = F.facts(mesh, {"name": cons.get("name"),
                               "material": cons.get("material"),
                               "_metrics": metrics},
                        wall=need_wall, curvature=need_curv)

    return {
        "verdict": verdict,
        "pass": verdict == "pass",
        "score": score,
        "level": level,
        "checks": checks,
        "issues": failed,
        "skipped": skipped,
        "metrics": metrics,
        "facts": fact_card,
        "assumptions": list(assumptions or []),
        "exact": False,
        "method": METHOD,
        "next": nxt,
        "units": "mm",
        "mesh_hash": mesh.hash(),
        "spec_hash": None,
    }


__all__ = ["verify", "METHOD", "SUPPORTED", "LEVELS"]
