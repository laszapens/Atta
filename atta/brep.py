# -*- coding: utf-8 -*-
"""Optional B-rep / STEP backend behind the same Spec v3 graph.

Atta's primary kernel is a numpy mesh kernel because it is fast, dependency-free
and good enough for draft iteration and measured verification.  A mesh is not a
B-rep: it has no analytic faces, so it cannot carry a STEP hand-off to a CAD
system or a CAM tool.  This module adds that second backend for the subset of
feature graphs that map cleanly onto OpenCASCADE via ``build123d``.

The contract is deliberately narrow and honest:

* the mesh build and :func:`atta.engine.verify` still decide the verdict;
* STEP is a *delivery* format, produced from the same mm-valued spec;
* an unsupported op fails with ``brep_unsupported`` and a concrete ``fix`` —
  it never silently falls back to a tessellated STEP, which would look like a
  B-rep while carrying only triangles.

Supported graph ops: box, cylinder, sphere, cone, torus, revolve, extrude,
union, difference, intersection, transform, mirror, pattern_linear,
pattern_polar.  Loft/sweep/coons/gordon/thicken/lobes and the implicit ops are
mesh-only by design.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping

from .errors import AttaError

__all__ = ["available", "build_graph", "export_step"]

SUPPORTED = frozenset({
    "box", "cylinder", "sphere", "cone", "torus", "revolve", "extrude",
    "union", "difference", "intersection", "transform", "mirror",
    "pattern_linear", "pattern_polar",
})


def available() -> bool:
    try:
        import build123d  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _require():
    try:
        import build123d as b
    except Exception as exc:  # noqa: BLE001
        raise AttaError(
            "brep_unavailable",
            "the B-rep backend needs build123d/OCCT, which is not importable: %s" % exc,
            path="backend",
            fix="pip install build123d, or deliver glb/obj/stl instead",
        ) from exc
    return b


def _vec(value, default=(0.0, 0.0, 0.0), path="args"):
    raw = default if value is None else value
    if raw is None:
        raise AttaError("bad_arg", "%s is required" % path, path=path,
                        fix="use [x,y,z] in mm")
    try:
        out = [float(v) for v in raw]
    except (TypeError, ValueError) as exc:
        raise AttaError("bad_arg", "%s must be a number or 3-vector" % path,
                        path=path, fix="use [x,y,z] in mm") from exc
    if len(out) == 1:
        out = out * 3
    if len(out) != 3 or not all(math.isfinite(v) for v in out):
        raise AttaError("bad_arg", "%s must be three finite numbers" % path,
                        path=path, fix="use [x,y,z] in mm")
    return out


def _axis(name):
    raw = str(name or "z").lower()
    if raw in ("x", "y", "z"):
        return raw
    raise AttaError("brep_unsupported", "axis %r is not supported by the B-rep backend" % raw,
                    path="axis", fix="use axis='x', 'y' or 'z'")


def _rotation(b, axis, center=(0.0, 0.0, 0.0)):
    """Place a Z-axis primitive on x/y/z through ``center``."""
    axis = _axis(axis)
    if axis == "z":
        return b.Pos(*center)
    if axis == "x":
        return b.Pos(*center) * b.Rot(Y=90)
    return b.Pos(*center) * b.Rot(X=-90)


def _primitive(b, feat):
    op = feat["op"]
    args = feat.get("args") or {}
    center = _vec(args.get("center"), (0, 0, 0), "args.center")
    if op == "box":
        size = _vec(args.get("size"), None, "args.size")
        if any(v <= 0 for v in size):
            raise AttaError("bad_arg", "box.size must be positive",
                            path="args.size", fix="pass [x,y,z] in mm")
        return b.Pos(*center) * b.Box(*size)
    if op == "cylinder":
        r = float(args.get("radius"))
        h = float(args.get("height"))
        if r <= 0 or h <= 0:
            raise AttaError("bad_arg", "cylinder radius/height must be positive",
                            path="args", fix="use radius > 0 and height > 0")
        return _rotation(b, args.get("axis", "z"), center) * b.Cylinder(r, h)
    if op == "sphere":
        r = float(args.get("radius"))
        if r <= 0:
            raise AttaError("bad_arg", "sphere.radius must be positive",
                            path="args.radius", fix="use radius > 0")
        return b.Pos(*center) * b.Sphere(r)
    if op == "cone":
        rb = float(args.get("r_bottom"))
        rt = float(args.get("r_top", 0.0))
        h = float(args.get("height"))
        if h <= 0 or rb < 0 or rt < 0 or (rb <= 0 and rt <= 0):
            raise AttaError("bad_arg", "cone needs positive height and a radius",
                            path="args", fix="use r_bottom, r_top, height in mm")
        return _rotation(b, args.get("axis", "z"), center) * b.Cone(rb, rt, h)
    if op == "torus":
        major = float(args.get("major_radius"))
        minor = float(args.get("minor_radius"))
        if major <= 0 or minor <= 0 or minor >= major:
            raise AttaError("bad_arg", "torus needs 0 < minor_radius < major_radius",
                            path="args", fix="use major_radius > minor_radius > 0")
        return _rotation(b, args.get("axis", "z"), center) * b.Torus(major, minor)
    raise AttaError("brep_unsupported", "op %r is not a B-rep primitive" % op,
                    path="op", fix="use a supported primitive")


def _revolve(b, args):
    profile = args.get("profile_rz")
    if not isinstance(profile, (list, tuple)) or len(profile) < 3:
        raise AttaError("bad_arg", "revolve.profile_rz needs >= 3 [r,z] points",
                        path="args.profile_rz", fix="pass [[r,z], ...] in mm")
    pts = []
    for row in profile:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise AttaError("bad_arg", "revolve points must be [r,z]",
                            path="args.profile_rz", fix="pass [[r,z], ...] in mm")
        pts.append((float(row[0]), float(row[1])))
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    face = b.make_face(b.Plane.XZ * b.Polyline(*pts, close=True))
    segs = int(args.get("segments", 64))
    return b.revolve(face, axis=b.Axis.Z, revolution_arc=360).clean()


def _extrude(b, args):
    loop = args.get("loop_xy")
    if not isinstance(loop, (list, tuple)) or len(loop) < 3:
        raise AttaError("bad_arg", "extrude.loop_xy needs >= 3 points",
                        path="args.loop_xy", fix="pass [[x,y], ...] in mm")
    pts = []
    for row in loop:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise AttaError("bad_arg", "extrude points must be [x,y]",
                            path="args.loop_xy", fix="pass [[x,y], ...] in mm")
        pts.append((float(row[0]), float(row[1])))
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    face = b.make_face(b.Polyline(*pts, close=True))
    height = float(args.get("height"))
    if height <= 0:
        raise AttaError("bad_arg", "extrude.height must be positive",
                        path="args.height", fix="use height in mm")
    body = b.extrude(face, amount=height)
    direction = _vec(args.get("direction"), (0, 0, 1), "args.direction")
    if direction != [0.0, 0.0, 1.0]:
        # A direction is a shear-free extrusion vector; build along it directly.
        body = b.extrude(face, amount=height, dir=b.Vector(*direction))
    holes = args.get("holes_xy") or []
    if holes:
        for hole in holes:
            hpts = [(float(p[0]), float(p[1])) for p in hole]
            if hpts[0] != hpts[-1]:
                hpts.append(hpts[0])
            hface = b.make_face(b.Polyline(*hpts, close=True))
            # Over-long cutter: coplanar end faces are the classic boolean
            # failure mode, so the cutter starts below and ends above the body.
            cutter = b.extrude(hface, amount=height * 1.2)
            body = body - (b.Pos(0, 0, -0.1 * height) * cutter)
    return body


def _transform(b, shape, args):
    translate = _vec(args.get("translate"), (0, 0, 0), "args.translate")
    rot = args.get("rotate_deg") or [0.0, 0.0, 0.0]
    rot = [float(v) for v in rot]
    if len(rot) != 3:
        raise AttaError("bad_arg", "transform.rotate_deg must be [rx,ry,rz]",
                        path="args.rotate_deg", fix="use [rx,ry,rz] in degrees")
    pivot = _vec(args.get("pivot"), (0, 0, 0), "args.pivot")
    scale = args.get("scale", 1.0)
    if isinstance(scale, (list, tuple)):
        s = [float(v) for v in scale]
        if len(s) != 3 or len(set(round(v, 12) for v in s)) != 1:
            raise AttaError(
                "brep_unsupported",
                "the B-rep backend supports uniform scale only",
                path="args.scale", fix="use a single scale factor, or the mesh backend")
        scale = s[0]
    shape = b.Pos(-pivot[0], -pivot[1], -pivot[2]) * shape
    if any(abs(v) > 1e-12 for v in rot):
        shape = b.Rot(Z=rot[2]) * b.Rot(Y=rot[1]) * b.Rot(X=rot[0]) * shape
    shape = b.Pos(pivot[0], pivot[1], pivot[2]) * shape
    if abs(float(scale) - 1.0) > 1e-12:
        shape = shape.scale(float(scale))
    return b.Pos(*translate) * shape


def _pattern(b, shape, feat):
    args = feat.get("args") or {}
    op = feat["op"]
    count = int(args.get("count", 1))
    if count < 1:
        raise AttaError("bad_arg", "pattern count must be >= 1",
                        path="args.count", fix="use count >= 2")
    copies = []
    if op == "pattern_linear":
        spacing = float(args.get("spacing"))
        axis = _axis(args.get("axis", "x"))
        unit = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        for i in range(count):
            copies.append(b.Pos(*(spacing * i * v for v in unit)) * shape)
    else:
        angle = float(args.get("angle_deg", 360.0))
        center = _vec(args.get("center"), (0, 0, 0), "args.center")
        axis = _axis(args.get("axis", "z"))
        step = angle / count if count else angle
        for i in range(count):
            rot = {"x": b.Rot(X=step * i), "y": b.Rot(Y=step * i),
                   "z": b.Rot(Z=step * i)}[axis]
            copies.append(b.Pos(*center) * rot * b.Pos(-center[0], -center[1], -center[2]) * shape)
    out = copies[0]
    for copy in copies[1:]:
        out = out + copy
    return out


def build_graph(spec: Mapping) -> dict:
    """Build a B-rep from a validated Spec v3 graph; return a small report."""
    if not available():
        _require()
    b = _require()
    model = spec.get("model") if isinstance(spec.get("model"), Mapping) else spec
    kind = str((model or {}).get("kind") or "graph")
    if kind != "graph":
        raise AttaError(
            "brep_unsupported",
            "the B-rep backend currently accepts feature graphs only (kind=%r)" % kind,
            path="model.kind",
            fix="deliver the mesh (glb/obj/stl), or express the part as a graph",
        )
    feats = list((model or {}).get("features") or [])
    if not feats:
        raise AttaError("bad_spec", "graph has no features", path="model.features",
                        fix="add at least one primitive feature")

    built: dict[str, object] = {}
    used: list[str] = []
    for feat in feats:
        op = str(feat.get("op") or "")
        fid = str(feat.get("id") or "")
        if op not in SUPPORTED:
            raise AttaError(
                "brep_unsupported",
                "op %r has no B-rep mapping (mesh-only)" % op,
                path="features.%s" % fid,
                fix="remove it for STEP delivery, or use the mesh backend",
            )
        args = feat.get("args") or {}
        inputs = [built[str(i)] for i in (feat.get("inputs") or []) if str(i) in built]
        if op in ("box", "cylinder", "sphere", "cone", "torus"):
            shape = _primitive(b, feat)
        elif op == "revolve":
            shape = _revolve(b, args)
        elif op == "extrude":
            shape = _extrude(b, args)
        elif op in ("union", "difference", "intersection"):
            if not inputs:
                raise AttaError("bad_feature", "%s needs inputs" % op,
                                path="features.%s.inputs" % fid,
                                fix="list two or more earlier feature ids")
            shape = inputs[0]
            for other in inputs[1:]:
                shape = (shape + other if op == "union"
                         else shape - other if op == "difference"
                         else shape & other)
        elif op == "transform":
            if not inputs:
                raise AttaError("bad_feature", "transform needs one input",
                                path="features.%s.inputs" % fid,
                                fix="list the feature to transform")
            shape = _transform(b, inputs[0], args)
        elif op == "mirror":
            if not inputs:
                raise AttaError("bad_feature", "mirror needs one input",
                                path="features.%s.inputs" % fid,
                                fix="list the feature to mirror")
            plane = str(args.get("plane", "x")).lower()
            about = {"x": b.Plane.YZ, "y": b.Plane.XZ, "z": b.Plane.XY}.get(plane)
            if about is None:
                raise AttaError("bad_arg", "mirror.plane must be x/y/z",
                                path="args.plane", fix="use plane='x'")
            shape = inputs[0] + b.mirror(inputs[0], about=about)
        elif op in ("pattern_linear", "pattern_polar"):
            if not inputs:
                raise AttaError("bad_feature", "%s needs one input" % op,
                                path="features.%s.inputs" % fid,
                                fix="list the feature to pattern")
            shape = _pattern(b, inputs[0], feat)
        else:  # pragma: no cover - SUPPORTED guards this
            raise AttaError("brep_unsupported", "op %r" % op, path="features.%s" % fid)
        if shape is None or getattr(shape, "volume", 0.0) <= 0:
            raise AttaError("brep_empty", "feature %r produced an empty solid" % fid,
                            path="features.%s" % fid,
                            fix="check the boolean operand order and dimensions")
        built[fid] = shape
        used.append(op)

    output = str((model or {}).get("output") or feats[-1].get("id"))
    if output not in built:
        raise AttaError("bad_output", "graph.output %r is not a feature id" % output,
                        path="model.output", fix="use one of: %s" % ", ".join(built))
    part = built[output]
    return {"ok": True, "backend": "brep", "engine": "build123d/OCCT",
            "shape": part, "volume_mm3": round(float(part.volume), 6),
            "ops": used, "output": output}


def export_step(spec: Mapping, path: str) -> dict:
    """Build a graph and write ``path`` as STEP (millimetres)."""
    b = _require()
    report = build_graph(spec)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    b.export_step(report["shape"], path, unit=b.Unit.MM)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size <= 0:
        raise AttaError("brep_export", "STEP export produced no bytes",
                        path="path", fix="check write permissions")
    return {"ok": True, "path": path, "bytes": size, "backend": "brep",
            "engine": report["engine"], "volume_mm3": report["volume_mm3"],
            "ops": report["ops"]}
