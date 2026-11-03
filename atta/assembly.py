# -*- coding: utf-8 -*-
"""Multi-part assembly: parts, ports, mates, DOF, collision and a receipt.

The kernel is deliberately mesh-native.  A part is any recipe/graph that
produces a closed mesh; a *port* is a local frame (origin, axis, normal,
radius) that a mate can reference.  Ports can be declared in the spec or
derived from the part's bounding box (center, principal axes, faces).

Mates are residual equations in SE(3).  The solver is a damped non-linear
least-squares (``scipy.optimize.least_squares``) over the free parts' six pose
variables, with a weak prior that keeps under-constrained assemblies near
their initial transforms.  Translation residuals are in metres; rotational
residuals are multiplied by a characteristic length so both are comparable.

The receipt reports:

* per-mate residual and which mates conflict,
* the Jacobian rank, remaining degrees of freedom, redundant rows and
  per-part local mobility,
* AABB broadphase pairs, exact boolean interference, and sampled clearance,
* per-part and whole-assembly metrics.

Nothing here fabricates a measurement: every collision/clearance number comes
from ``atta.probe`` on the actual transformed meshes.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import numpy as np

from . import features, metrology, probe, recipes, state
from .errors import AttaError
from .mesh import Mesh, concat, transform

MATE_TYPES = (
    "coincident", "concentric", "axis", "plane", "distance", "angle",
    "tangent", "gear", "insert", "parallel", "perpendicular", "lock",
    "point_on_plane",
)

_DEFAULT_MATE_PORTS = {
    "concentric": ("axis_z", "axis_z"),
    "axis": ("axis_z", "axis_z"),
    "insert": ("axis_z", "axis_z"),
    "plane": ("face_z-", "face_z+"),
    "coincident": ("center", "center"),
    "distance": ("center", "center"),
    "angle": ("axis_z", "axis_z"),
    "tangent": ("center", "center"),
    "gear": ("axis_z", "axis_z"),
    "parallel": ("axis_z", "axis_z"),
    "perpendicular": ("axis_z", "axis_z"),
    "lock": ("center", "center"),
    "point_on_plane": ("center", "center"),
}

_TRAVEL_M = 0.5          # free parts may move ±500 mm from their initial pose
_ROT_LIMIT = math.pi     # and rotate a half turn
_PRIOR_T = 1e-6          # metres
_PRIOR_R = 1e-6          # radians


# --------------------------------------------------------------------- math

def euler_xyz_matrix(deg) -> np.ndarray:
    """Rz @ Ry @ Rx, matching ``atta.mesh.transform``."""
    rx, ry, rz = (math.radians(float(x)) for x in (deg or (0.0, 0.0, 0.0)))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    return mz @ my @ mx


def rotvec_matrix(rv) -> np.ndarray:
    """Rodrigues exponential map."""
    rv = np.asarray(rv, dtype=np.float64).reshape(3)
    theta = float(np.linalg.norm(rv))
    if theta < 1e-12:
        return np.eye(3)
    k = rv / theta
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(theta) * kx + (1.0 - math.cos(theta)) * (kx @ kx)


def matrix_rotvec(R) -> np.ndarray:
    """Inverse exponential map, stable through scipy when available."""
    R = np.asarray(R, dtype=np.float64).reshape(3, 3)
    try:
        from scipy.spatial.transform import Rotation
        return Rotation.from_matrix(R).as_rotvec()
    except Exception:  # noqa: BLE001 - fallback for a minimal environment
        c = max(-1.0, min(1.0, (float(np.trace(R)) - 1.0) * 0.5))
        theta = math.acos(c)
        if theta < 1e-9:
            return np.zeros(3)
        axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0],
                         R[1, 0] - R[0, 1]], dtype=np.float64)
        n = np.linalg.norm(axis)
        if n < 1e-12:
            return np.zeros(3)
        return axis / n * theta


def _unit(v, default=(0.0, 0.0, 1.0)) -> np.ndarray:
    v = np.asarray(v if v is not None else default, dtype=np.float64).reshape(3)
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        return np.asarray(default, dtype=np.float64)
    return v / n


def _basis(axis) -> np.ndarray:
    """Orthonormal columns x,y,z with z=axis."""
    z = _unit(axis)
    helper = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(z, helper))) > 0.9:
        helper = np.array([0.0, 1.0, 0.0])
    x = np.cross(helper, z)
    if np.linalg.norm(x) < 1e-12:
        x = np.cross(np.array([0.0, 0.0, 1.0]), z)
    x = _unit(x)
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


def _rot_error(R: np.ndarray) -> np.ndarray:
    return matrix_rotvec(R)


# --------------------------------------------------------------------- types

@dataclass
class Pose:
    t: np.ndarray = field(default_factory=lambda: np.zeros(3))
    R: np.ndarray = field(default_factory=lambda: np.eye(3))

    def point(self, p) -> np.ndarray:
        return self.t + self.R @ np.asarray(p, dtype=np.float64).reshape(3)

    def direction(self, d) -> np.ndarray:
        return self.R @ np.asarray(d, dtype=np.float64).reshape(3)

    def matrix(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.R
        m[:3, 3] = self.t
        return m


@dataclass
class Port:
    name: str
    origin: np.ndarray                 # part-local metres
    axis: np.ndarray                   # unit, part-local
    normal: np.ndarray                 # unit, part-local (plane mates)
    radius: float = 0.0
    kind: str = "axis"


@dataclass
class Part:
    id: str
    mesh: Mesh
    local_bbox: dict
    ports: dict[str, Port]
    pose: Pose
    fixed: bool
    spec: dict
    material: str | None = None
    quality: str = "draft"


@dataclass
class PortFrame:
    origin: np.ndarray
    axis: np.ndarray
    normal: np.ndarray
    radius: float = 0.0
    kind: str = "axis"

    @property
    def axes(self) -> np.ndarray:
        z = _unit(self.axis)
        helper = np.array([1.0, 0.0, 0.0])
        if abs(float(np.dot(z, helper))) > 0.9:
            helper = np.array([0.0, 1.0, 0.0])
        x = _unit(np.cross(helper, z))
        y = np.cross(z, x)
        return np.column_stack([x, y, z])


# ------------------------------------------------------------------ part load

def _part_source(part: Mapping) -> tuple[str, dict]:
    for key in ("recipe", "graph", "spec", "model_id"):
        if part.get(key) is not None:
            return key, dict(part)
    raise AttaError(
        "assembly_part_source",
        "part %r has no recipe/graph/spec/model_id" % (part.get("id"),),
        path="parts",
        fix="give each part a recipe (with params) or an inline graph",
    )


def _part_spec_doc(part: Mapping) -> dict:
    """Normalise a part definition into a standalone model spec."""
    source, raw = _part_source(part)
    if source == "recipe":
        return {
            "kind": "recipe",
            "id": str(part["recipe"]),
            "params": dict(part.get("params") or {}),
            "preset": str(part.get("preset") or "nominal"),
        }
    if source == "graph":
        graph = dict(part["graph"] or {})
        return {
            "kind": "graph",
            "features": graph.get("features") or [],
            "units": graph.get("units") or "mm",
            "output": graph.get("output"),
            "segments": graph.get("segments"),
            "params": dict(part.get("params") or graph.get("params") or {}),
        }
    if source == "spec":
        spec = dict(part["spec"] or {})
        # Accept either a model object ({kind, ...}) or a full Spec v3
        # document ({schema, model: {...}}).
        return dict(spec.get("model") or spec)
    # model_id: the referenced record is resolved by the engine before build;
    # storing the spec keeps the assembly rebuildable.
    return {"kind": "model_id", "id": str(part["model_id"])}


def _build_part_mesh(part_spec: dict, *, quality: str,
                     root=None) -> tuple[Mesh, dict]:
    kind = str(part_spec.get("kind") or "recipe")
    if kind == "recipe":
        recipe = recipes.get(str(part_spec.get("id")))
        resolved = recipe.resolve(patch=dict(part_spec.get("params") or {}),
                                  preset=str(part_spec.get("preset") or "nominal"))
        if getattr(resolved, "blocked", False):
            first = (resolved.issues or [{}])[0]
            raise AttaError(
                "assembly_part_blocked",
                "part recipe %s is blocked: %s" % (recipe.id, first.get("message")),
                path="parts",
                fix=first.get("fix") or "adjust the part parameters",
                details=resolved.issues,
            )
        mesh, _ = recipe.build_shape(resolved.values, quality)
        return mesh, {"kind": "recipe", "id": recipe.id,
                      "params": resolved.display, "preset": part_spec.get("preset")}
    if kind == "graph":
        mesh = features.evaluate(
            part_spec.get("features") or [],
            params=part_spec.get("params") or {},
            units=part_spec.get("units") or "mm",
            output=part_spec.get("output"),
        )
        return mesh, {"kind": "graph", "features": part_spec.get("features"),
                      "units": part_spec.get("units") or "mm",
                      "output": part_spec.get("output"),
                      "params": part_spec.get("params") or {}}
    if kind == "model_id":
        rec = state.get(str(part_spec.get("id")), root=root)
        if not rec:
            raise AttaError(
                "assembly_model_missing",
                "part model_id %r is not in this workspace" % part_spec.get("id"),
                path="parts",
                fix="build/copy that model into this workspace, or inline its spec",
            )
        spec = rec.get("spec") or {}
        model = spec.get("model") or {}
        if model.get("kind") == "recipe":
            return _build_part_mesh({"kind": "recipe", "id": model.get("id"),
                                     "params": model.get("params") or {},
                                     "preset": model.get("preset")}, quality=quality,
                                    root=root)
        if model.get("kind") == "graph":
            return _build_part_mesh({"kind": "graph",
                                     "features": model.get("features") or [],
                                     "units": model.get("units") or "mm",
                                     "output": model.get("output")}, quality=quality,
                                    root=root)
        raise AttaError("assembly_model_kind",
                        "model %r is not a recipe or graph" % part_spec.get("id"),
                        path="parts", fix="use an inline part spec instead")
    raise AttaError("assembly_part_kind", "unknown part kind %r" % kind,
                    path="parts", fix="use recipe, graph or model_id")


def _auto_ports(mesh: Mesh) -> dict[str, Port]:
    b = mesh.meta.get("bbox") or None
    if b is None:
        from . import mesh as M
        b = M.bbox(mesh)
    mn = np.asarray(b["min"], dtype=np.float64)
    mx = np.asarray(b["max"], dtype=np.float64)
    c = (mn + mx) * 0.5
    size = mx - mn
    ports: dict[str, Port] = {}
    for name, axis in (("axis_x", (1, 0, 0)), ("axis_y", (0, 1, 0)),
                       ("axis_z", (0, 0, 1))):
        ports[name] = Port(name, c.copy(), _unit(axis), _unit(axis),
                           radius=float(max(size)) * 0.5)
    ports["center"] = Port("center", c.copy(), _unit((0, 0, 1)),
                           _unit((0, 0, 1)), radius=float(max(size)) * 0.5)
    for axis, i in (("x", 0), ("y", 1), ("z", 2)):
        for sign, key in ((1.0, "face_%s+" % axis), (-1.0, "face_%s-" % axis)):
            o = c.copy()
            o[i] = mx[i] if sign > 0 else mn[i]
            n = np.zeros(3)
            n[i] = sign
            ports[key] = Port(key, o, n.copy(), n.copy(),
                              radius=float(max(size)) * 0.25, kind="plane")
    return ports


def _declared_port(raw: Mapping) -> Port:
    origin = np.asarray(raw.get("origin_mm") or (0.0, 0.0, 0.0),
                        dtype=np.float64).reshape(3) / 1000.0
    axis = _unit(raw.get("axis") or (0.0, 0.0, 1.0))
    normal = _unit(raw.get("normal") or axis)
    return Port(str(raw.get("name") or "port"), origin, axis, normal,
                radius=float(raw.get("radius_mm") or 0.0) / 1000.0,
                kind=str(raw.get("kind") or "axis"))


def _ports_for(part_spec: Mapping, mesh: Mesh) -> dict[str, Port]:
    ports = _auto_ports(mesh)
    for raw in part_spec.get("ports") or []:
        if isinstance(raw, Mapping):
            port = _declared_port(raw)
            ports[port.name] = port
    return ports


def _initial_pose(part_spec: Mapping) -> Pose:
    tr = part_spec.get("transform") or {}
    if isinstance(tr, Mapping) and isinstance(tr.get("matrix"), (list, tuple)):
        m = np.asarray(tr["matrix"], dtype=np.float64)
        if m.shape == (4, 4):
            # Spec matrices are in millimetres, like every other public length.
            return Pose(t=m[:3, 3].copy() / 1000.0, R=m[:3, :3].copy())
    t = np.asarray(tr.get("translate_mm") or tr.get("translate") or (0, 0, 0),
                   dtype=np.float64).reshape(3) / 1000.0
    R = euler_xyz_matrix(tr.get("rotate_deg") or tr.get("rotate") or (0, 0, 0))
    return Pose(t=t, R=R)


# -------------------------------------------------------------------- mating

def _mate_type(mate: Mapping) -> str:
    typ = str(mate.get("type") or "").strip().lower()
    if typ not in MATE_TYPES:
        raise AttaError(
            "bad_mate_type",
            "unknown mate type %r" % typ,
            path="mates",
            fix="use one of: %s" % ", ".join(MATE_TYPES),
        )
    return typ


def _default_ports(mate: Mapping, typ: str) -> tuple[str, str]:
    return _DEFAULT_MATE_PORTS.get(typ, ("center", "center"))


def _frame_for(part: Part, port_name: str) -> PortFrame:
    port = part.ports.get(port_name)
    if port is None:
        raise AttaError(
            "unknown_port",
            "part %r has no port %r" % (part.id, port_name),
            path="mates",
            fix="available ports: %s" % ", ".join(sorted(part.ports)),
        )
    pose = part.pose
    return PortFrame(
        origin=pose.point(port.origin),
        axis=_unit(pose.direction(port.axis)),
        normal=_unit(pose.direction(port.normal)),
        radius=float(port.radius),
        kind=port.kind,
    )


def _offset_m(mate: Mapping) -> float:
    if "offset_mm" in mate:
        return float(mate["offset_mm"]) / 1000.0
    if "offset" in mate:
        return float(mate["offset"]) / 1000.0
    return 0.0


def _mate_rows(mate: Mapping, fa: PortFrame, fb: PortFrame, L: float,
               initial_rel: tuple[np.ndarray, np.ndarray] | None = None
               ) -> list[float]:
    """Residual rows in metres.  Rotational rows are scaled by ``L``."""
    typ = _mate_type(mate)
    za, zb = _unit(fa.axis), _unit(fb.axis)
    na, nb = _unit(fa.normal), _unit(fb.normal)
    oa, ob = fa.origin, fb.origin
    d = ob - oa
    off = _offset_m(mate)
    weight = float(mate.get("weight", 1.0))
    align = str(mate.get("align") or "auto").strip().lower()
    rows: list[float] = []

    def add(*values):
        for value in values:
            rows.append(float(value) * weight)

    if typ in ("concentric", "axis", "insert"):
        # Point on a line: kill the perpendicular component of d.
        perp = d - za * float(np.dot(d, za))
        add(*perp)
        add(float(np.dot(d, za)) - off)
        # Parallel axes (cross product zero; rank 2).
        add(*(np.cross(za, zb) * L))
        if align in ("parallel", "same"):
            add((float(np.dot(za, zb)) - 1.0) * L)
        elif align in ("antiparallel", "opposite", "flip"):
            add((float(np.dot(za, zb)) + 1.0) * L)
    elif typ == "coincident":
        add(*(d - za * off))
        if align in ("parallel", "same", "antiparallel", "opposite", "flip"):
            add(*(np.cross(za, zb) * L))
            want = 1.0 if align in ("parallel", "same") else -1.0
            add((float(np.dot(za, zb)) - want) * L)
    elif typ == "plane":
        add(*(np.cross(na, nb) * L))
        if align in ("parallel", "same"):
            add((float(np.dot(na, nb)) - 1.0) * L)
        elif align in ("antiparallel", "opposite", "flip"):
            add((float(np.dot(na, nb)) + 1.0) * L)
        add(float(np.dot(na, d)) - off)
    elif typ == "point_on_plane":
        add(float(np.dot(na, d)) - off)
    elif typ == "distance":
        axis = str(mate.get("axis") or "").strip().lower()
        value = float(mate.get("value_mm", mate.get("distance_mm", 0.0))) / 1000.0
        if axis in {"x", "y", "z"}:
            idx = "xyz".index(axis)
            add(float(np.dot(d, fa.axes[:, idx])) - value)
        elif axis in {"ax", "ay", "az", "a_x", "a_y", "a_z"}:
            idx = "xyz".index(axis[-1])
            add(float(np.dot(d, fa.axes[:, idx])) - value)
        else:
            add(float(np.linalg.norm(d)) - value)
    elif typ == "angle":
        value = math.radians(float(mate.get("value_deg",
                                              mate.get("angle_deg", 0.0))))
        dot = max(-1.0, min(1.0, float(np.dot(za, zb))))
        add((math.acos(dot) - value) * L)
    elif typ == "parallel":
        add(*(np.cross(za, zb) * L))
        add((float(np.dot(za, zb)) - 1.0) * L)
    elif typ == "perpendicular":
        add(float(np.dot(za, zb)) * L)
    elif typ == "tangent":
        mode = str(mate.get("mode") or "external").lower()
        want = (fa.radius + fb.radius) if mode != "internal" \
            else abs(fa.radius - fb.radius)
        add(float(np.linalg.norm(d)) - want)
    elif typ == "gear":
        ratio = mate.get("ratio")
        if ratio is not None and fa.radius > 0:
            want = fa.radius * (1.0 + abs(float(ratio)))
        else:
            want = fa.radius + fb.radius
        add(float(np.linalg.norm(d)) - want)
        add(*(np.cross(za, zb) * L))
        if align in ("parallel", "same", "antiparallel", "opposite", "flip"):
            want_dot = 1.0 if align in ("parallel", "same") else -1.0
            add((float(np.dot(za, zb)) - want_dot) * L)
    elif typ == "lock":
        if initial_rel is None:
            add(0.0)
        else:
            rel_t, rel_R = initial_rel
            cur_R = fa.axes.T @ fb.axes
            cur_t = fa.axes.T @ d
            add(*(cur_t - rel_t))
            add(*(_rot_error(cur_R @ rel_R.T) * L))
    else:  # pragma: no cover - _mate_type guards this
        add(0.0)
    return rows


def mate_residuals(mates: Iterable[Mapping], parts: Mapping[str, Part],
                   *, L: float | None = None) -> tuple[np.ndarray, list[dict]]:
    """Evaluate every mate once; return rows and per-mate spans."""
    if L is None:
        L = _characteristic_length(parts)
    rows: list[float] = []
    report: list[dict] = []
    for mate in mates:
        mid = str(mate.get("id") or "mate")
        typ = _mate_type(mate)
        a, b = str(mate.get("a")), str(mate.get("b"))
        pa, pb = parts.get(a), parts.get(b)
        if pa is None or pb is None:
            raise AttaError("unknown_part", "mate %r references unknown part" % mid,
                            path="mates", fix="available parts: %s"
                            % ", ".join(sorted(parts)))
        dpa, dpb = _default_ports(mate, typ)
        fa = _frame_for(pa, str(mate.get("port_a") or dpa))
        fb = _frame_for(pb, str(mate.get("port_b") or dpb))
        initial_rel = None
        if typ == "lock":
            rel_t, rel_R = mate.get("_initial_rel") or (None, None)
            initial_rel = (rel_t, rel_R) if rel_t is not None else None
        block = _mate_rows(mate, fa, fb, L, initial_rel=initial_rel)
        start = len(rows)
        rows.extend(block)
        report.append({
            "id": mid,
            "type": typ,
            "a": a,
            "b": b,
            "span": [start, len(rows)],
            "rows": len(block),
        })
    return np.asarray(rows, dtype=np.float64), report


def _characteristic_length(parts: Mapping[str, Part]) -> float:
    diag = 0.01
    for part in parts.values():
        b = part.local_bbox
        size = np.asarray(b.get("size") or (0.1, 0.1, 0.1), dtype=np.float64)
        diag = max(diag, float(np.linalg.norm(size)))
    return max(0.005, min(1.0, diag))


# ------------------------------------------------------------------ pose solve

def _pack(parts: list[Part]) -> tuple[np.ndarray, list[Part], list[np.ndarray]]:
    free = [p for p in parts if not p.fixed]
    x = []
    starts = []
    for part in free:
        starts.append(np.array(len(x), dtype=np.int64))
        t, rv = part.pose.t, matrix_rotvec(part.pose.R)
        x.extend([*t, *rv])
    return np.asarray(x, dtype=np.float64), free, starts


def _apply_x(parts: list[Part], free: list[Part], x: np.ndarray) -> dict[str, Part]:
    out = {}
    index = {p.id: i for i, p in enumerate(free)}
    for part in parts:
        if part.fixed:
            out[part.id] = part
            continue
        i = index[part.id]
        block = x[i * 6:(i + 1) * 6]
        p = Part(part.id, part.mesh, part.local_bbox, part.ports,
                 Pose(t=block[:3].copy(), R=rotvec_matrix(block[3:])),
                 part.fixed, part.spec, part.material, part.quality)
        out[part.id] = p
    return out


def solve_poses(parts_in: list[Part], mates: list[dict], *,
                max_nfev: int = 200, tol_mm: float = 0.01,
                verbose: bool = False) -> dict:
    """Solve free part poses; return poses, mate residuals, DOF and status."""
    if not parts_in:
        raise AttaError("assembly_empty", "assembly has no parts",
                        path="parts", fix="add at least one part")
    parts = [Part(p.id, p.mesh, p.local_bbox, p.ports, p.pose, p.fixed,
                  p.spec, p.material, p.quality) for p in parts_in]
    # Anchor the first part when the spec forgot to fix one.
    if not any(p.fixed for p in parts):
        parts[0].fixed = True
    L = _characteristic_length({p.id: p for p in parts})
    part_map = {p.id: p for p in parts}

    # Capture the initial relative pose for `lock` mates.
    for mate in mates:
        if str(mate.get("type") or "").lower() != "lock":
            continue
        typ = "lock"
        dpa, dpb = _default_ports(mate, typ)
        a, b = str(mate.get("a")), str(mate.get("b"))
        fa = _frame_for(part_map[a], str(mate.get("port_a") or dpa))
        fb = _frame_for(part_map[b], str(mate.get("port_b") or dpb))
        mate["_initial_rel"] = (fa.axes.T @ (fb.origin - fa.origin),
                                fa.axes.T @ fb.axes)

    x0, free, _ = _pack(parts)
    lo = np.empty_like(x0)
    hi = np.empty_like(x0)
    for i in range(len(free)):
        lo[i * 6:i * 6 + 3] = x0[i * 6:i * 6 + 3] - _TRAVEL_M
        hi[i * 6:i * 6 + 3] = x0[i * 6:i * 6 + 3] + _TRAVEL_M
        lo[i * 6 + 3:i * 6 + 6] = -_ROT_LIMIT
        hi[i * 6 + 3:i * 6 + 6] = _ROT_LIMIT

    def base_rows(x):
        mapped = _apply_x(parts, free, x)
        rows, spans = mate_residuals(mates, mapped, L=L)
        return rows, spans, mapped

    def residuals(x):
        rows, _, _ = base_rows(x)
        prior = []
        for i, part in enumerate(free):
            t0 = part.pose.t
            r0 = matrix_rotvec(part.pose.R)
            prior.extend((x[i * 6:i * 6 + 3] - t0) * _PRIOR_T)
            prior.extend((x[i * 6 + 3:i * 6 + 6] - r0) * _PRIOR_R)
        return np.concatenate([rows, np.asarray(prior, dtype=np.float64)]) \
            if prior else rows

    solved_x = x0
    info = {"status": "ok", "nfev": 0, "method": "initial"}
    if len(free) and mates:
        try:
            from scipy.optimize import least_squares
            res = least_squares(
                residuals, x0, bounds=(lo, hi), method="trf",
                loss="soft_l1", f_scale=1e-4,
                xtol=1e-11, ftol=1e-11, gtol=1e-11,
                max_nfev=max(20, int(max_nfev)),
                x_scale="jac",
            )
            solved_x = res.x
            info = {"status": "ok" if res.success else "max_nfev",
                    "nfev": int(res.nfev), "method": "least_squares",
                    "cost": float(res.cost), "optimality": float(res.optimality)}
        except Exception as exc:  # noqa: BLE001 - report, never crash a build
            info = {"status": "solver_error", "nfev": 0,
                    "method": "least_squares",
                    "error": "%s: %s" % (type(exc).__name__, exc)}
    elif len(free):
        info = {"status": "unconstrained", "nfev": 0, "method": "initial"}

    rows, spans, mapped = base_rows(solved_x)
    L_mm = L * 1000.0
    mate_report = []
    tol = max(1e-9, float(tol_mm) / 1000.0)
    worst = 0.0
    for entry in spans:
        lo_i, hi_i = entry["span"]
        block = rows[lo_i:hi_i]
        if len(block) == 0:
            norm = 0.0
        else:
            # Rows are metres after the L scaling; rotation rows are already
            # L-scaled, so the Euclidean norm is a comparable length.
            norm = float(np.linalg.norm(block))
        worst = max(worst, norm)
        mate_report.append(dict(entry,
                                residual_mm=round(norm * 1000.0, 6),
                                max_mm=round(float(np.max(np.abs(block)) if len(block)
                                                   else 0.0) * 1000.0, 6),
                                ok=bool(norm <= tol),
                                rows_values=[round(float(v), 9) for v in block]))
    conflicts = [m for m in mate_report if not m["ok"]]

    dof = _dof_report(mapped, free, mates, L, tol=tol)
    status = "ok"
    if conflicts:
        status = "conflict"
    elif len(free) and (dof.get("dof") or 0) > 0:
        status = "under_constrained"
    return {
        "status": status,
        "parts": mapped,
        "mates": mate_report,
        "conflicts": conflicts,
        "worst_residual_mm": round(worst * 1000.0, 6),
        "dof": dof,
        "solver": info,
        "scale_mm": round(L_mm, 6),
        "tolerance_mm": float(tol_mm),
    }


def _dof_report(parts: Mapping[str, Part], free: list[Part],
                mates: list[dict], L: float, *, tol: float) -> dict:
    n = 6 * len(free)
    if n == 0:
        return {"dof": 0, "rank": 0, "rows": 0, "redundant": 0,
                "under_constrained": False, "over_constrained": False,
                "per_part": {}, "note": "no free parts"}
    if n > 60:
        return {"dof": None, "rank": None, "rows": None, "redundant": None,
                "under_constrained": None, "over_constrained": None,
                "per_part": {}, "note": "too many free variables for a dense "
                "Jacobian; split the assembly into subassemblies"}

    # Rebuild x from the solved poses.
    x, _, _ = _pack(list(parts.values()))
    free_ids = [p.id for p in free]
    x0 = []
    for pid in free_ids:
        p = parts[pid]
        x0.extend([*p.pose.t, *matrix_rotvec(p.pose.R)])
    x0 = np.asarray(x0, dtype=np.float64)

    def rows_at(x):
        mapped = _apply_x(list(parts.values()), free, x)
        rows, _ = mate_residuals(mates, mapped, L=L)
        return rows

    r0 = rows_at(x0)
    J = np.zeros((len(r0), n), dtype=np.float64)
    h = 1e-6
    for j in range(n):
        xp = x0.copy()
        xp[j] += h
        J[:, j] = (rows_at(xp) - r0) / h
    if len(r0) == 0:
        rank, s, Vh = 0, np.zeros(0), np.zeros((0, n))
    else:
        try:
            _, s, Vh = np.linalg.svd(J, full_matrices=False)
            threshold = max(1e-9, 1e-7 * float(s[0]) if len(s) else 1e-9)
            rank = int(np.sum(s > threshold))
        except np.linalg.LinAlgError:
            rank, s, Vh = 0, np.zeros(0), np.zeros((0, n))
    dof = max(0, n - rank)
    per_part = {}
    if dof and len(Vh):
        null = Vh[rank:]
        for i, pid in enumerate(free_ids):
            # Columns of the null-space matrix are independent motion modes;
            # the rank of one part's 6 rows is how many of those modes move it.
            block = null[:, i * 6:(i + 1) * 6]
            if block.size == 0:
                per_part[pid] = 0
                continue
            try:
                per_part[pid] = int(np.linalg.matrix_rank(
                    block, tol=max(1e-9, 1e-7 * float(np.max(np.abs(block)))
                                   if block.size else 1e-9)))
            except np.linalg.LinAlgError:
                per_part[pid] = 0
    else:
        per_part = {pid: 0 for pid in free_ids}
    return {
        "dof": int(dof),
        "rank": int(rank),
        "rows": int(len(r0)),
        "redundant": int(max(0, len(r0) - rank)),
        "under_constrained": bool(dof > 0),
        "over_constrained": bool(len(r0) > rank and
                                 np.max(np.abs(r0)) > tol if len(r0) else False),
        "per_part": per_part,
        "singular_values": [round(float(v), 9) for v in s[:8]],
        "note": "Jacobian rank of mate residuals; priors excluded",
    }


# ------------------------------------------------------------------ collision

def _bbox_of(mesh: Mesh) -> dict:
    from . import mesh as M
    return M.bbox(mesh)


def _bbox_gap_mm(a: dict, b: dict) -> float:
    mn_a, mx_a = np.asarray(a["min"]), np.asarray(a["max"])
    mn_b, mx_b = np.asarray(b["min"]), np.asarray(b["max"])
    gap = np.maximum(0.0, np.maximum(mn_a - mx_b, mn_b - mx_a))
    return float(np.linalg.norm(gap) * 1000.0)


def broadphase(items: Mapping[str, dict], *, margin_mm: float = 0.0) -> list[tuple[str, str]]:
    """Sweep-and-prune on x, then y/z; returns candidate pairs."""
    margin = max(0.0, float(margin_mm)) / 1000.0
    rows = []
    for name, b in items.items():
        mn = np.asarray(b["min"], dtype=np.float64) - margin
        mx = np.asarray(b["max"], dtype=np.float64) + margin
        rows.append((float(mn[0]), float(mx[0]), float(mn[1]), float(mx[1]),
                     float(mn[2]), float(mx[2]), str(name)))
    rows.sort(key=lambda r: r[0])
    active: list[tuple] = []
    pairs: list[tuple[str, str]] = []
    for row in rows:
        min_x = row[0]
        still = []
        for other in active:
            if other[1] < min_x:
                continue
            still.append(other)
            if (row[2] <= other[3] and other[2] <= row[3]
                    and row[4] <= other[5] and other[4] <= row[5]):
                pairs.append((other[6], row[6]))
        active = still + [row]
    return pairs


def collision_report(meshes: Mapping[str, Mesh], *,
                     allow: set[tuple[str, str]] | None = None,
                     max_samples: int = 4000) -> dict:
    allow = allow or set()
    bboxes = {name: _bbox_of(mesh) for name, mesh in meshes.items()}
    pairs = broadphase(bboxes, margin_mm=0.0)
    collisions = []
    for a, b in pairs:
        key = tuple(sorted((a, b)))
        if key in allow:
            continue
        info = probe.interference(meshes[a], meshes[b])
        if info.get("intersects"):
            collisions.append({"a": a, "b": b,
                               "volume_mm3": info.get("volume_mm3"),
                               "overlap_bbox_mm": info.get("overlap_bbox_mm"),
                               "method": info.get("method")})
    return {
        "pairs": len(pairs),
        "candidates": [list(p) for p in pairs],
        "collisions": collisions,
        "has_collision": bool(collisions),
        "method": "sweep_and_prune+aabb+manifold_boolean",
    }


def clearance_report(meshes: Mapping[str, Mesh], *, threshold_mm: float = 5.0,
                     max_samples: int = 300) -> dict:
    threshold = max(0.0, float(threshold_mm))
    bboxes = {name: _bbox_of(mesh) for name, mesh in meshes.items()}
    pairs = broadphase(bboxes, margin_mm=threshold)
    rows = []
    for a, b in pairs:
        gap = _bbox_gap_mm(bboxes[a], bboxes[b])
        if gap > threshold + 1e-6:
            continue
        info = probe.clearance(meshes[a], meshes[b], max_samples=max_samples)
        rows.append({"a": a, "b": b, "min_mm": info.get("min_mm"),
                     "bbox_gap_mm": round(gap, 4),
                     "point_a_mm": info.get("point_a_mm"),
                     "point_b_mm": info.get("point_b_mm")})
    return {
        "threshold_mm": threshold,
        "pairs": len(rows),
        "clearances": rows,
        "min_mm": min((r["min_mm"] for r in rows), default=None),
        "method": "sweep_and_prune+sampled_clearance",
    }


# --------------------------------------------------------------------- build

def _part_cache_key(part_spec: dict, quality: str) -> str:
    import hashlib
    import json
    raw = json.dumps({"spec": part_spec, "quality": quality},
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build(spec: Mapping, *, quality: str = "draft", root=None,
          solve: bool = True, max_nfev: int = 200,
          tol_mm: float = 0.01, cache: dict | None = None) -> dict:
    """Build an assembly from a validated ``kind=assembly`` model spec.

    ``spec`` is the top-level Spec v3 document (the ``model`` object is used).
    Returns meshes and reports; the caller decides what to persist.
    """
    t0 = time.perf_counter()
    model = dict(spec.get("model") or {})
    raw_parts = model.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise AttaError("assembly_empty", "assembly has no parts", path="parts",
                        fix="add at least one part")
    q = str(quality or spec.get("quality") or "draft")
    if q not in ("draft", "review", "deliver"):
        q = "draft"
    mates = [dict(m) for m in (model.get("mates") or []) if isinstance(m, Mapping)]
    solver_opts = dict(model.get("solver") or {})
    max_nfev = int(solver_opts.get("max_nfev", max_nfev))
    tol_mm = float(solver_opts.get("tolerance_mm", tol_mm))

    parts: list[Part] = []
    part_reports: list[dict] = []
    for raw in raw_parts:
        if not isinstance(raw, Mapping):
            raise AttaError("assembly_part", "each part must be an object",
                            path="parts", fix='use {"id": "...", "recipe": "..."}')
        pid = str(raw.get("id") or "").strip()
        if not pid:
            raise AttaError("assembly_part_id", "every part needs an id",
                            path="parts", fix="give each part a unique id")
        part_spec = _part_spec_doc(raw)
        key = _part_cache_key(part_spec, q)
        if cache is not None and key in cache:
            cached_mesh, source_report = cache[key]
            mesh = cached_mesh.copy()
        else:
            mesh, source_report = _build_part_mesh(part_spec, quality=q, root=root)
            if cache is not None:
                cache[key] = (mesh.copy(), dict(source_report))
        if len(mesh.verts) == 0:
            raise AttaError("assembly_part_empty",
                            "part %r built an empty mesh" % pid, path="parts",
                            fix="check the part recipe/graph")
        mesh = mesh.copy()
        mesh.meta.setdefault("part_id", pid)
        ports = _ports_for(raw, mesh)
        pose = _initial_pose(raw)
        parts.append(Part(pid, mesh, _bbox_of(mesh), ports, pose,
                          bool(raw.get("fixed", False)), part_spec,
                          raw.get("material"), q))
        topo = mesh.topology()
        part_reports.append({
            "id": pid, "source": source_report,
            "material": raw.get("material") or spec.get("material"),
            "fixed": bool(raw.get("fixed", False)),
            "metrics": {
                "bbox_mm": metrology.bbox_mm(mesh),
                "volume_mm3": metrology.volume_mm3(mesh),
                "closed": bool(topo.get("closed")),
                "genus": topo.get("genus"),
                "n_components": topo.get("n_components"),
            },
            "ports": sorted(ports),
        })

    solved = solve_poses(parts, mates, max_nfev=max_nfev, tol_mm=tol_mm) \
        if solve else _no_solve(parts, mates)
    placed = solved["parts"]

    world_meshes: dict[str, Mesh] = {}
    placements: dict[str, dict] = {}
    for part in placed.values():
        pose = part.pose
        wm = Mesh(part.mesh.verts @ pose.R.T + pose.t, part.mesh.faces.copy(),
                  dict(part.mesh.meta))
        world_meshes[part.id] = wm
        placements[part.id] = {
            "translate_mm": [round(float(x) * 1000.0, 6) for x in pose.t],
            "rotate_deg": _euler_deg(pose.R),
            "matrix": [[round(float(v), 9) for v in row] for row in pose.matrix()],
            "fixed": bool(part.fixed),
        }

    allow: set[tuple[str, str]] = set()
    for mate in mates:
        if mate.get("allow_interference") is True:
            allow.add(tuple(sorted((str(mate.get("a")), str(mate.get("b"))))))
    for row in model.get("allow_interference") or []:
        if isinstance(row, Mapping):
            allow.add(tuple(sorted((str(row.get("a")), str(row.get("b"))))))
    collision = collision_report(world_meshes, allow=allow)
    clearance_cfg = model.get("clearance") or {}
    threshold = float(clearance_cfg.get("threshold_mm", 5.0)) \
        if isinstance(clearance_cfg, Mapping) else 5.0
    clearance = clearance_report(world_meshes, threshold_mm=threshold)

    composite = concat([world_meshes[p.id] for p in placed.values()])
    metrics = metrology.measure(composite, wall=False, dihedral=True,
                                curvature=False)
    metrics["bbox_mm"] = metrology.bbox_mm(composite)

    assembly = {
        "part_count": len(placed),
        "parts": part_reports,
        "mates": solved["mates"],
        "conflicts": solved["conflicts"],
        "worst_residual_mm": solved["worst_residual_mm"],
        "dof": solved["dof"],
        "solver": solved["solver"],
        "collision": collision,
        "clearance": clearance,
        "placements": placements,
        "allow_interference": [list(p) for p in sorted(allow)],
        "units": "mm",
        "exact": False,
        "ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }
    return {
        "parts": placed,
        "part_meshes": world_meshes,
        "placements": placements,
        "mesh": composite,
        "metrics": metrics,
        "assembly": assembly,
        "status": solved["status"],
    }


def _no_solve(parts: list[Part], mates: list[dict]) -> dict:
    rows, spans = mate_residuals(mates, {p.id: p for p in parts})
    report = []
    for entry in spans:
        lo, hi = entry["span"]
        block = rows[lo:hi]
        norm = float(np.linalg.norm(block)) if len(block) else 0.0
        report.append(dict(entry, residual_mm=round(norm * 1000.0, 6),
                           max_mm=round(float(np.max(np.abs(block)) if len(block)
                                              else 0.0) * 1000.0, 6),
                           ok=bool(norm <= 0.01 / 1000.0),
                           rows_values=[round(float(v), 9) for v in block]))
    return {
        "status": "not_solved",
        "parts": {p.id: p for p in parts},
        "mates": report,
        "conflicts": [m for m in report if not m["ok"]],
        "worst_residual_mm": round(max((m["residual_mm"] for m in report),
                                       default=0.0), 6),
        "dof": {"dof": None, "note": "solve disabled"},
        "solver": {"status": "skipped", "nfev": 0},
        "scale_mm": 0.0,
        "tolerance_mm": 0.01,
    }


def _euler_deg(R: np.ndarray) -> list[float]:
    """XYZ Euler degrees that reproduce R (Rz Ry Rx)."""
    R = np.asarray(R, dtype=np.float64)
    sy = -float(R[2, 0])
    sy = max(-1.0, min(1.0, sy))
    ry = math.asin(sy)
    if abs(math.cos(ry)) > 1e-8:
        rx = math.atan2(float(R[2, 1]), float(R[2, 2]))
        rz = math.atan2(float(R[1, 0]), float(R[0, 0]))
    else:
        rx = math.atan2(-float(R[1, 2]), float(R[1, 1]))
        rz = 0.0
    return [round(math.degrees(rx), 6), round(math.degrees(ry), 6),
            round(math.degrees(rz), 6)]


# -------------------------------------------------------------------- verify

def _check(checks: list[dict], cid: str, ok: bool, severity: str = "error",
           value=None, expected=None, message=None, fix=None) -> dict:
    row = {
        "id": str(cid),
        "ok": bool(ok),
        "severity": str(severity),
        "value": _plain(value),
        "expected": _plain(expected),
        "message": str(message) if message is not None
        else ("ok" if ok else "%s failed" % cid),
        "fix": None if ok else (fix or "inspect the assembly and re-run verify"),
    }
    checks.append(row)
    return row


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _rows(value):
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    return [x for x in value if isinstance(x, Mapping)]


def verify(build_result: Mapping, *,
           constraints: Mapping | None = None,
           level: str = "standard") -> dict:
    """Turn an assembly build into a three-state receipt with fixes."""
    t0 = time.perf_counter()
    level = str(level or "standard").lower()
    if level not in ("basic", "standard", "strict"):
        level = "standard"
    cons = dict(constraints or {})
    checks: list[dict] = []
    asm = build_result.get("assembly") or {}

    for part in asm.get("parts") or []:
        pid = part.get("id")
        m = part.get("metrics") or {}
        _check(checks, "part.%s.closed" % pid, bool(m.get("closed")),
               "error", m.get("closed"), True,
               "part %s is a closed solid" % pid if m.get("closed")
               else "part %s is not closed" % pid,
               "fix the part geometry; an open part has no reliable inside")
        _check(checks, "part.%s.volume" % pid,
               float(m.get("volume_mm3") or 0.0) > 1e-6, "error",
               m.get("volume_mm3"), "> 0",
               "part %s has positive volume" % pid if
               float(m.get("volume_mm3") or 0.0) > 1e-6
               else "part %s has zero volume" % pid,
               "rebuild the part or check its boolean inputs")

    for mate in asm.get("mates") or []:
        mid = mate.get("id")
        _check(checks, "mate.%s" % mid, bool(mate.get("ok")),
               "error", mate.get("residual_mm"), "<= tolerance",
               "mate %s is satisfied" % mid if mate.get("ok")
               else "mate %s cannot be satisfied (conflict)" % mid,
               "loosen the offset, change the port, or remove the redundant mate")

    dof = asm.get("dof") or {}
    require = bool(cons.get("fully_constrained", False))
    if dof.get("dof") is not None:
        _check(checks, "dof", (not require) or int(dof.get("dof") or 0) == 0,
               "warn" if not require else "error",
               dof.get("dof"), "0" if require else "informational",
               "assembly has %s remaining DOF" % dof.get("dof")
               if dof.get("dof") else "assembly is fully constrained",
               "add mates to remove the remaining degrees of freedom")

    collisions = (asm.get("collision") or {}).get("collisions") or []
    _check(checks, "collision", not collisions, "error",
           [{"a": c.get("a"), "b": c.get("b"),
             "volume_mm3": c.get("volume_mm3")} for c in collisions],
           "no unintended interference",
           "no unintended interference" if not collisions
           else "%d part pair(s) interfere" % len(collisions),
           "move the parts apart, adjust mates, or declare allow_interference")

    envelope = cons.get("envelope")
    bbox = (build_result.get("metrics") or {}).get("bbox_mm") or {}
    if envelope:
        mn = list(envelope.get("min") or [None, None, None])
        mx = list(envelope.get("max") or [None, None, None])
        for i, axis in enumerate("xyz"):
            lo = None if i >= len(mn) else mn[i]
            hi = None if i >= len(mx) else mx[i]
            got = bbox.get("min") if bbox else None
            got_max = bbox.get("max") if bbox else None
            ok = ((lo is None or (got and got[i] >= float(lo))) and
                  (hi is None or (got_max and got_max[i] <= float(hi))))
            _check(checks, "envelope.%s" % axis, ok, "error",
                   {"min": got[i] if got else None,
                    "max": got_max[i] if got_max else None},
                   {"min": lo, "max": hi},
                   "assembly envelope %s inside bounds" % axis if ok
                   else "assembly envelope %s outside bounds" % axis,
                   "move parts or reduce the overall size")

    # Pair constraints use part names exactly as declared.
    part_meshes = build_result.get("part_meshes") or {}
    for i, row in enumerate(_rows(cons.get("clearance"))):
        a, b = row.get("a"), row.get("b")
        need = float(row.get("min", row.get("min_mm", 0.0)))
        if a not in part_meshes or b not in part_meshes:
            _check(checks, "clearance.%d" % i, False, "error",
                   {"a": a, "b": b}, ">=%s" % need,
                   "clearance references an unknown part",
                   "available parts: %s" % ", ".join(sorted(part_meshes)))
            continue
        got = probe.clearance(part_meshes[a], part_meshes[b])["min_mm"]
        ok = float(got) >= need
        _check(checks, "clearance.%d" % i, ok, "error",
               {"a": a, "b": b, "min_mm": got}, ">=%s" % need,
               "clearance %s/%s meets the limit" % (a, b) if ok
               else "clearance %s/%s is too small" % (a, b),
               "increase the gap between '%s' and '%s'" % (a, b))

    for i, row in enumerate(_rows(cons.get("interference"))):
        a, b = row.get("a"), row.get("b")
        cap = float(row.get("max", row.get("max_mm3", 1e-6)))
        if a not in part_meshes or b not in part_meshes:
            _check(checks, "interference.%d" % i, False, "error",
                   {"a": a, "b": b}, "<=%s mm3" % cap,
                   "interference references an unknown part",
                   "available parts: %s" % ", ".join(sorted(part_meshes)))
            continue
        got = float(probe.interference(part_meshes[a], part_meshes[b])
                    .get("volume_mm3") or 0.0)
        ok = got <= cap
        _check(checks, "interference.%d" % i, ok, "error",
               {"a": a, "b": b, "volume_mm3": got}, "<=%s" % cap,
               "interference %s/%s within allowance" % (a, b) if ok
               else "interference %s/%s exceeds the allowance" % (a, b),
               "adjust the mate offset or declare allow_interference")

    failed = [c for c in checks if not c["ok"]]
    errors = [c for c in failed if c["severity"] == "error"]
    warns = [c for c in failed if c["severity"] != "error"]
    verdict = "fail" if errors else ("degraded" if warns else "pass")
    issues = [{"check": c["id"], "severity": c["severity"],
               "value": c["value"], "expected": c["expected"],
               "message": c["message"], "fix": c["fix"]} for c in failed]
    score = round((len(checks) - len(failed)) / max(1, len(checks)), 4)
    return {
        "method": "atta.assembly.verify/v1",
        "verdict": verdict,
        "level": level,
        "score": score,
        "checks": checks,
        "issues": issues,
        "assembly": asm,
        "metrics": _plain(build_result.get("metrics") or {}),
        "ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


# -------------------------------------------------------------------- export

def export_scene(build_result: Mapping, *, formats=("glb", "obj", "stl"),
                 outdir=None, stem: str = "assembly") -> dict:
    """Export the assembly with trimesh (preserves part names in GLB)."""
    from pathlib import Path
    import hashlib
    import trimesh

    wanted = [str(f).lower() for f in formats]
    out = Path(outdir).expanduser().resolve() if outdir else Path.cwd()
    out.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    errors: list[dict] = []
    part_meshes = build_result.get("part_meshes") or {}
    placements = build_result.get("placements") or {}

    def add(path, kind, part=None, ok=True, error=None):
        row = {"path": str(path), "format": kind, "ok": bool(ok),
               "part": part}
        if path.exists():
            row["bytes"] = int(path.stat().st_size)
            row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if error:
            row["error"] = str(error)
        files.append(row)

    if "glb" in wanted or "gltf" in wanted:
        try:
            scene = trimesh.Scene()
            for pid, mesh in part_meshes.items():
                tm = trimesh.Trimesh(vertices=np.asarray(mesh.verts),
                                     faces=np.asarray(mesh.faces),
                                     process=False)
                scene.add_geometry(tm, node_name=str(pid),
                                   geom_name=str(pid))
            path = out / ("%s.glb" % stem)
            scene.export(str(path), file_type="glb")
            add(path, "glb")
        except Exception as exc:  # noqa: BLE001 - report, do not hide
            errors.append({"format": "glb", "error": "%s: %s"
                           % (type(exc).__name__, exc)})

    for pid, mesh in part_meshes.items():
        tm = trimesh.Trimesh(vertices=np.asarray(mesh.verts),
                             faces=np.asarray(mesh.faces), process=False)
        safe = str(pid).replace("/", "_")
        for fmt in wanted:
            if fmt in ("glb", "gltf"):
                continue
            try:
                path = out / ("%s_%s.%s" % (stem, safe, fmt))
                tm.export(str(path), file_type=fmt)
                add(path, fmt, part=pid)
            except Exception as exc:  # noqa: BLE001
                errors.append({"format": fmt, "part": pid,
                               "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"files": files, "errors": errors, "outdir": str(out)}


__all__ = [
    "MATE_TYPES", "Pose", "Port", "Part", "PortFrame", "build", "verify",
    "solve_poses", "mate_residuals", "broadphase", "collision_report",
    "clearance_report", "export_scene", "euler_xyz_matrix", "rotvec_matrix",
]
