# -*- coding: utf-8 -*-
"""Constrained parameter solving over black-box mesh metrics.

The solver is deliberately independent of the geometry kernel: it takes any
``evaluate(params) -> {ok, metrics, error}`` callable.  The engine supplies one
that builds a draft mesh and measures it.  This keeps the numerical machinery
testable with synthetic functions and lets assembly use the same code for
clearance/envelope targets.

Design choices
--------------
* Variables live in a normalized unit box ``u in [0, 1]``; this makes step
  sizes, tolerances and sensitivity comparable across mm, degrees and counts.
* Constraints are *normalized violations*, so a 1 mm miss and a 1 deg miss are
  both "relative to their own scale".  Feasibility always outranks an
  objective: we never trade a failed constraint for a prettier score.
* Four strategies are provided, all derivative-free at the outer level:
  Latin hypercube (sample), compass/pattern search (robust, handles discrete
  steps), Nelder-Mead (local simplex), differential evolution (global), plus a
  Levenberg-Marquardt / SLSQP polish when the metrics are smooth enough.
* Every result carries a diagnosis: which constraints are active, the
  closest achievable point, a finite-difference sensitivity ranking, and a
  concrete "move this variable this way" recommendation.  A solver that only
  says "infeasible" is not useful to an LLM.

No fabricated measurements: this module only consumes values returned by the
caller's evaluator.  It never invents a metric.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Iterable, Mapping

import numpy as np

__all__ = [
    "flatten_metrics",
    "constraints_to_targets",
    "normalize_targets",
    "metric_value",
    "violation",
    "solve",
]

_EPS = 1e-12


# --------------------------------------------------------------------- metrics

def _num(value, default=None):
    if isinstance(value, (bool, np.bool_)):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = float(value)
        if math.isfinite(v):
            return v
    return default


def flatten_metrics(metrics: Mapping[str, Any] | None) -> dict:
    """Flatten the engine's metric document into scalar names.

    Accepted inputs are the output of ``metrology.measure`` (nested) or a
    pre-flattened dict.  Unknown keys are copied when their value is numeric.
    """
    src = dict(metrics or {})
    out: dict[str, float] = {}

    bbox = src.get("bbox_mm") or src.get("bbox") or {}
    size = bbox.get("size") if isinstance(bbox, Mapping) else None
    if isinstance(size, (list, tuple)) and len(size) >= 3:
        for axis, value in zip("xyz", size):
            v = _num(value)
            if v is not None:
                out["bbox_" + axis] = v
        out["bbox_diag"] = math.sqrt(sum(float(size[i]) ** 2 for i in range(3)))
    mn = bbox.get("min") if isinstance(bbox, Mapping) else None
    mx = bbox.get("max") if isinstance(bbox, Mapping) else None
    if isinstance(mn, (list, tuple)) and len(mn) >= 3:
        for axis, value in zip("xyz", mn):
            v = _num(value)
            if v is not None:
                out["bbox_min_" + axis] = v
    if isinstance(mx, (list, tuple)) and len(mx) >= 3:
        for axis, value in zip("xyz", mx):
            v = _num(value)
            if v is not None:
                out["bbox_max_" + axis] = v

    topo = src.get("topology") if isinstance(src.get("topology"), Mapping) else {}
    aliases = {
        "volume_mm3": ("volume_mm3", "volume"),
        "area_mm2": ("area_mm2", "area"),
        "min_wall_mm": ("min_wall_mm",),
        "genus": ("genus",),
        "n_components": ("n_components", "components"),
        "n_tris": ("n_tris", "triangles"),
        "closed": ("closed",),
        "dihedral_max_deg": ("dihedral_max_deg",),
        "curvature_p95_deg": ("curvature_p95_deg",),
    }
    for target, keys in aliases.items():
        found = None
        for key in keys:
            if key in src and _num(src[key]) is not None:
                found = _num(src[key])
                break
            if key in topo and _num(topo[key]) is not None:
                found = _num(topo[key])
                break
        if found is not None:
            out[target] = found

    # A derived metric is recorded only when it could actually be measured.
    # ``wall_thickness`` reports ``min_mm: None`` for a solid part (there is no
    # opposing surface, so there is no thickness), and writing that None via
    # ``setdefault`` would both admit a non-measurement as a metric and block a
    # later real value from overwriting it.
    wall = src.get("wall")
    if isinstance(wall, Mapping) and "min_mm" in wall:
        _min_mm = _num(wall["min_mm"])
        if _min_mm is not None:
            out.setdefault("min_wall_mm", _min_mm)
    dih = src.get("dihedral")
    if isinstance(dih, Mapping) and "max_deg" in dih:
        _max_deg = _num(dih["max_deg"])
        if _max_deg is not None:
            out.setdefault("dihedral_max_deg", _max_deg)
    curv = src.get("curvature")
    if isinstance(curv, Mapping) and "p95_deg" in curv:
        _p95 = _num(curv["p95_deg"])
        if _p95 is not None:
            out.setdefault("curvature_p95_deg", _p95)

    for key, value in src.items():
        if key in out or key in {"bbox_mm", "bbox", "topology", "wall",
                                 "dihedral", "curvature"}:
            continue
        v = _num(value)
        if v is not None:
            out[str(key)] = v
    return out


def metric_value(metrics: Mapping[str, Any], name: str, default=None):
    """Look up a metric by flat name, with a few friendly aliases."""
    flat = flatten_metrics(metrics)
    key = str(name)
    if key in flat:
        return flat[key]
    aliases = {
        "volume": "volume_mm3",
        "area": "area_mm2",
        "wall": "min_wall_mm",
        "components": "n_components",
        "triangles": "n_tris",
        "size_x": "bbox_x", "size_y": "bbox_y", "size_z": "bbox_z",
        "height": "bbox_z", "width": "bbox_x", "depth": "bbox_y",
    }
    alt = aliases.get(key)
    if alt and alt in flat:
        return flat[alt]
    return default


# --------------------------------------------------------------------- targets

def _window_from(raw) -> dict:
    if isinstance(raw, Mapping):
        lo, hi = raw.get("min"), raw.get("max")
        spec = {"lo": None if lo is None else float(lo),
                "hi": None if hi is None else float(hi)}
        if "tol" in raw:
            spec["tol"] = float(raw["tol"])
        return spec
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return {"lo": float(raw[0]), "hi": float(raw[1])}
    raise ValueError("expected [min, max] or {min, max}, got %r" % (raw,))


def _target_from(raw) -> dict:
    if isinstance(raw, bool):
        return {"kind": "eq", "target": 1.0 if raw else 0.0, "tol": 1e-6}
    if isinstance(raw, (int, float, np.integer, np.floating)):
        return {"kind": "eq", "target": float(raw), "tol": None}
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return {"kind": "window", **_window_from(raw)}
    if isinstance(raw, Mapping):
        if "target" in raw or "eq" in raw or "value" in raw:
            value = raw.get("target", raw.get("eq", raw.get("value")))
            return {"kind": "eq", "target": float(value),
                    "tol": raw.get("tol")}
        if "min" in raw or "max" in raw:
            return {"kind": "window", **_window_from(raw)}
    raise ValueError("bad target %r" % (raw,))


def normalize_targets(targets: Mapping[str, Any] | None) -> list[dict]:
    """Turn ``{metric: window|value|{target,tol}}`` into normalized rows."""
    rows: list[dict] = []
    for name, raw in dict(targets or {}).items():
        spec = _target_from(raw)
        spec["metric"] = str(name)
        spec["label"] = str(name)
        rows.append(spec)
    return rows


def constraints_to_targets(constraints: Mapping[str, Any] | None) -> tuple[dict, list[str]]:
    """Map verify-style constraints onto metric targets.

    Pair constraints (clearance/interference) and render contracts cannot be
    expressed as scalar recipe metrics; they are returned in ``ignored`` so the
    caller can report them instead of silently dropping them.
    """
    out: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in dict(constraints or {}).items():
        if key == "bbox":
            if isinstance(value, Mapping) and ("min" in value or "max" in value):
                mn = list(value.get("min") or [None, None, None])
                mx = list(value.get("max") or [None, None, None])
                for axis, lo, hi in zip("xyz", mn, mx):
                    out["bbox_" + axis] = {"min": lo, "max": hi}
            elif isinstance(value, Mapping):
                for axis in "xyz":
                    if axis in value and value[axis] is not None:
                        out["bbox_" + axis] = value[axis]
                if isinstance(value.get("size"), (list, tuple)):
                    for axis, v in zip("xyz", value["size"]):
                        out["bbox_" + axis] = v
            else:
                ignored.append(key)
        elif key == "min_wall_mm":
            out[key] = {"min": value} if isinstance(value, (int, float)) else value
        elif key in {"dihedral_max_deg", "curvature_p95_deg"}:
            out[key] = {"max": value} if isinstance(value, (int, float)) else value
        elif key in {"volume_mm3", "area_mm2", "genus", "closed",
                     "n_components"}:
            out[key] = value
        elif key == "envelope":
            ignored.append(key)
        elif key in {"clearance", "interference", "interfaces", "holes",
                     "render", "render_receipt", "name", "material"}:
            ignored.append(key)
        else:
            ignored.append(key)
    return out, ignored


def _scale_for(spec: dict, value: float | None) -> float:
    tol = spec.get("tol")
    if tol is not None and float(tol) > 0:
        return max(float(tol), _EPS)
    if spec["kind"] == "eq":
        target = float(spec["target"])
        return max(abs(target), 1.0)
    lo, hi = spec.get("lo"), spec.get("hi")
    if lo is not None and hi is not None and float(hi) > float(lo):
        return max(float(hi) - float(lo), 1.0)
    if value is not None:
        return max(abs(float(value)), 1.0)
    return 1.0


def violation(metrics: Mapping[str, Any], spec: dict) -> tuple[float, dict]:
    """Normalized constraint violation and a diagnostic row."""
    value = metric_value(metrics, spec["metric"])
    if value is None:
        return 1.0, {"metric": spec["metric"], "kind": spec["kind"],
                     "value": None, "target": spec.get("target"),
                     "window": [spec.get("lo"), spec.get("hi")],
                     "violation": 1.0, "reason": "metric_missing"}
    value = float(value)
    scale = _scale_for(spec, value)
    if spec["kind"] == "eq":
        target = float(spec["target"])
        delta = value - target
        return abs(delta) / scale, {
            "metric": spec["metric"], "kind": "eq", "value": value,
            "target": target, "delta": delta, "scale": scale,
            "violation": abs(delta) / scale,
        }
    lo, hi = spec.get("lo"), spec.get("hi")
    delta = 0.0
    if lo is not None and value < float(lo):
        delta = value - float(lo)
    elif hi is not None and value > float(hi):
        delta = value - float(hi)
    return abs(delta) / scale, {
        "metric": spec["metric"], "kind": "window", "value": value,
        "window": [lo, hi], "delta": delta, "scale": scale,
        "violation": abs(delta) / scale,
    }


def _objective_rows(objective=None) -> list[dict]:
    if objective is None:
        return []
    if isinstance(objective, str):
        return [{"metric": objective, "direction": "min", "weight": 1.0}]
    if isinstance(objective, Mapping):
        rows = []
        for key, value in objective.items():
            if isinstance(value, str):
                direction = "max" if value.lower().startswith("max") else "min"
                rows.append({"metric": str(key), "direction": direction,
                             "weight": 1.0})
            elif isinstance(value, (int, float)):
                rows.append({"metric": str(key),
                             "direction": "max" if float(value) > 0 else "min",
                             "weight": abs(float(value)) or 1.0})
            elif isinstance(value, Mapping):
                direction = str(value.get("direction") or "min").lower()
                direction = "max" if direction.startswith("max") else "min"
                rows.append({"metric": str(key), "direction": direction,
                             "weight": float(value.get("weight", 1.0))})
        return rows
    if isinstance(objective, (list, tuple)):
        rows = []
        for item in objective:
            if isinstance(item, str):
                rows.append({"metric": item, "direction": "min", "weight": 1.0})
            elif isinstance(item, Mapping) and item.get("metric"):
                direction = str(item.get("direction") or "min").lower()
                rows.append({"metric": str(item["metric"]),
                             "direction": "max" if direction.startswith("max")
                             else "min",
                             "weight": float(item.get("weight", 1.0))})
        return rows
    return []


# --------------------------------------------------------------------- engine

class _Problem:
    def __init__(self, evaluate: Callable[[dict], dict], variables: list[dict],
                 targets: list[dict], objectives: list[dict], *,
                 max_evaluations: int, seed: int, trace: bool = False):
        self.evaluate = evaluate
        self.vars = variables
        self.targets = targets
        self.objectives = objectives
        self.max_evaluations = max(1, int(max_evaluations))
        self.seed = int(seed)
        self.trace = bool(trace)
        self.cache: dict[tuple, dict] = {}
        self.rows: list[dict] = []
        self.n_eval = 0
        self.n_cache = 0
        self.n_fail = 0

    # -- variables ---------------------------------------------------------
    def params(self, u: np.ndarray) -> dict:
        out = {}
        for var, t in zip(self.vars, np.asarray(u, dtype=np.float64)):
            lo, hi = float(var["min"]), float(var["max"])
            value = lo + max(0.0, min(1.0, float(t))) * (hi - lo)
            if var.get("type") == "int":
                value = int(round(value))
                value = max(int(round(lo)), min(int(round(hi)), value))
            else:
                value = float(value)
            out[var["name"]] = value
        return out

    def key(self, params: dict) -> tuple:
        out = []
        for var in self.vars:
            value = params.get(var["name"])
            if var.get("type") == "int":
                out.append(int(value))
            else:
                out.append(round(float(value), 9))
        return tuple(out)

    # -- evaluation --------------------------------------------------------
    def run(self, params: dict, *, count: bool = True) -> dict:
        key = self.key(params)
        if key in self.cache:
            self.n_cache += 1
            row = dict(self.cache[key])
            row["cached"] = True
            return row
        if count and self.n_eval >= self.max_evaluations:
            return {"ok": False, "params": dict(params), "error": {
                "code": "budget_exhausted", "message": "evaluation budget reached"},
                "violation": 1e12, "feasible": False, "budget": True}
        self.n_eval += 1
        row: dict[str, Any]
        try:
            raw = self.evaluate(dict(params)) or {}
        except Exception as exc:  # noqa: BLE001 - a failed build is a value
            raw = {"ok": False, "error": {"code": "evaluate_failed",
                                          "message": "%s: %s" % (type(exc).__name__, exc)}}
        if not isinstance(raw, Mapping):
            raw = {"ok": False, "error": {"code": "bad_evaluate_result",
                                          "message": "evaluator returned %r" % (raw,)}}
        ok = bool(raw.get("ok", True))
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), Mapping) else raw
        flat = flatten_metrics(metrics)
        total = 0.0
        details = []
        for spec in self.targets:
            v, detail = violation(flat, spec)
            total += v * float(spec.get("weight", 1.0))
            details.append(detail)
        feasible = ok and total <= 1e-9
        row = {
            "ok": bool(ok),
            "params": dict(params),
            "metrics": flat,
            "raw_metrics": dict(metrics or {}),
            "verdict": raw.get("verdict"),
            "error": raw.get("error"),
            "violation": round(float(total), 12),
            "violations": details,
            "feasible": feasible,
            "score": self._score(flat, total, ok),
        }
        self.cache[key] = row
        self.rows.append(row)
        return row

    def _score(self, metrics: dict, total: float, ok: bool) -> float:
        # Feasibility dominates; within the feasible set the weighted objective
        # decides.  Infeasible points are ranked by how close they are, so the
        # "closest achievable" report is meaningful.
        if not ok:
            return -1e9 - float(total)
        if not self.objectives:
            return -float(total)
        total_obj = 0.0
        for obj in self.objectives:
            value = metric_value(metrics, obj["metric"])
            if value is None:
                continue
            sign = -1.0 if obj["direction"] == "min" else 1.0
            total_obj += float(obj.get("weight", 1.0)) * sign * float(value)
        return -float(total) * 1e6 + total_obj

    def best(self) -> dict | None:
        if not self.rows:
            return None
        return max(self.rows, key=lambda r: (
            1 if r.get("feasible") else 0,
            -float(r.get("violation", 1e12)),
            float(r.get("score", -1e18)),
        ))

    # -- sampling ----------------------------------------------------------
    def lhs(self, n: int) -> list[dict]:
        n = max(1, int(n))
        d = max(1, len(self.vars))
        rng = np.random.default_rng(self.seed)
        u = np.empty((n, d), dtype=np.float64)
        for j in range(d):
            cuts = (np.arange(n) + rng.random(n)) / n
            u[:, j] = cuts[rng.permutation(n)]
        return [self.params(row) for row in u]

    def random(self, n: int) -> list[dict]:
        rng = np.random.default_rng(self.seed + 7919)
        u = rng.random((max(1, int(n)), max(1, len(self.vars))))
        return [self.params(row) for row in u]

    # -- compass / pattern search -----------------------------------------
    def compass(self, start: dict, *, step0: float = 0.25,
                min_step: float = 1e-4) -> dict:
        current = self.run(start)
        u = np.array([self._u(start, var) for var in self.vars], dtype=np.float64)
        step = np.full(len(self.vars), max(1e-6, float(step0)), dtype=np.float64)
        while float(np.max(step)) >= float(min_step):
            if self.n_eval >= self.max_evaluations:
                break
            improved = False
            order = list(range(len(self.vars)))
            # Most sensitive variables first: cheap and helps a lot when the
            # evaluator is near the budget.
            order.sort(key=lambda i: abs(self.vars[i]["max"] - self.vars[i]["min"]),
                       reverse=True)
            for i in order:
                if self.n_eval >= self.max_evaluations:
                    break
                for sign in (1.0, -1.0):
                    trial_u = u.copy()
                    trial_u[i] = min(1.0, max(0.0, trial_u[i] + sign * step[i]))
                    if abs(trial_u[i] - u[i]) < 1e-12:
                        continue
                    trial_params = self.params(trial_u)
                    row = self.run(trial_params)
                    if self._better(row, current):
                        u, current = trial_u, row
                        improved = True
                        break
            if improved:
                step = np.minimum(step * 1.15, np.repeat(0.5, len(self.vars)))
            else:
                step *= 0.5
        return current

    def _u(self, params: dict, var: dict) -> float:
        lo, hi = float(var["min"]), float(var["max"])
        if hi <= lo:
            return 0.0
        return max(0.0, min(1.0, (float(params.get(var["name"], lo)) - lo) / (hi - lo)))

    @staticmethod
    def _better(a: dict, b: dict | None) -> bool:
        if b is None:
            return True
        return (
            (1 if a.get("feasible") else 0,
             -float(a.get("violation", 1e12)),
             float(a.get("score", -1e18)))
            > (1 if b.get("feasible") else 0,
               -float(b.get("violation", 1e12)),
               float(b.get("score", -1e18)))
        )


# --------------------------------------------------------------------- wrappers

def _pack_bounds(variables: list[dict]):
    lo = np.array([float(v["min"]) for v in variables], dtype=np.float64)
    hi = np.array([float(v["max"]) for v in variables], dtype=np.float64)
    hi = np.where(hi < lo, lo, hi)
    return lo, hi


def _nelder_mead(prob: _Problem, start: dict, *, maxiter: int) -> dict:
    from scipy.optimize import minimize
    lo, hi = _pack_bounds(prob.vars)
    x0 = np.array([prob._u(start, v) for v in prob.vars], dtype=np.float64)

    def objective(u):
        params = prob.params(np.clip(u, 0.0, 1.0))
        row = prob.run(params)
        return -float(row.get("score", -1e18))

    res = minimize(objective, x0, method="Nelder-Mead",
                   options={"maxiter": max(10, int(maxiter)),
                            "xatol": 1e-5, "fatol": 1e-7})
    return prob.run(prob.params(np.clip(res.x, 0.0, 1.0)))


def _differential_evolution(prob: _Problem, *, maxiter: int,
                            popsize: int = 8) -> dict:
    from scipy.optimize import differential_evolution
    if not prob.vars:
        return prob.run({})
    bounds = [(0.0, 1.0) for _ in prob.vars]

    def objective(u):
        params = prob.params(np.asarray(u, dtype=np.float64))
        row = prob.run(params)
        return -float(row.get("score", -1e18))

    res = differential_evolution(
        objective, bounds, seed=prob.seed, maxiter=max(1, int(maxiter)),
        popsize=max(2, int(popsize)), tol=1e-6, mutation=(0.4, 1.0),
        recombination=0.8, polish=False, updating="immediate", workers=1,
    )
    return prob.run(prob.params(np.clip(res.x, 0.0, 1.0)))


def _residual_vector(prob: _Problem, u: np.ndarray) -> np.ndarray:
    params = prob.params(np.clip(u, 0.0, 1.0))
    row = prob.run(params)
    res = []
    flat = row.get("metrics") or {}
    for spec in prob.targets:
        value = metric_value(flat, spec["metric"])
        scale = _scale_for(spec, value)
        if value is None:
            res.append(1.0)
        elif spec["kind"] == "eq":
            res.append((float(value) - float(spec["target"])) / scale)
        else:
            lo, hi = spec.get("lo"), spec.get("hi")
            d = 0.0
            if lo is not None and float(value) < float(lo):
                d = float(value) - float(lo)
            elif hi is not None and float(value) > float(hi):
                d = float(value) - float(hi)
            res.append(d / scale)
    # Weak objective term so the polish does not wander along a flat feasible
    # valley; it is much smaller than any constraint residual.
    for obj in prob.objectives:
        value = metric_value(flat, obj["metric"])
        if value is None:
            res.append(0.0)
        else:
            scale = max(1.0, abs(float(value)))
            sign = -1.0 if obj["direction"] == "min" else 1.0
            res.append(1e-4 * sign * float(value) / scale)
    # A very weak prior keeps an underdetermined solution near the initial
    # point.  It is excluded from DOF diagnostics by construction.
    if not res:
        res = [0.0]
    return np.asarray(res, dtype=np.float64)


def _coordinate_polish(prob: _Problem, start: dict, *, passes: int = 2) -> dict:
    """Coordinate secant polish.

    Each variable is nudged once per violated target, using a finite-difference
    slope.  It is much cheaper than a full Jacobian when the recipe has ten
    parameters but only two targets, and it handles monotone metrics exactly.
    """
    current = prob.run(start)
    for _ in range(max(1, int(passes))):
        improved = False
        for var in prob.vars:
            if prob.n_eval >= prob.max_evaluations:
                return current
            for spec in prob.targets:
                if prob.n_eval >= prob.max_evaluations:
                    return current
                v, detail = violation(current.get("metrics") or {}, spec)
                if v <= 1e-9:
                    continue
                value = detail.get("value")
                if value is None:
                    continue
                if spec["kind"] == "eq":
                    desired = float(spec["target"])
                else:
                    lo, hi = spec.get("lo"), spec.get("hi")
                    if lo is not None and float(value) < float(lo):
                        desired = float(lo)
                    elif hi is not None and float(value) > float(hi):
                        desired = float(hi)
                    else:
                        continue
                name = var["name"]
                cur = float(current["params"].get(name, var["min"]))
                step = max(1e-3, 0.01 * (float(var["max"]) - float(var["min"])))
                if var.get("type") == "int":
                    step = max(1.0, round(step))
                probe_params = dict(current["params"])
                probe_params[name] = cur + step
                probe_params[name] = (int(round(probe_params[name]))
                                      if var.get("type") == "int"
                                      else float(probe_params[name]))
                probe_row = prob.run(probe_params)
                probe_value = metric_value(probe_row.get("metrics") or {},
                                           spec["metric"])
                if probe_value is None or abs(float(probe_value) - float(value)) < 1e-12:
                    continue
                slope = (float(probe_value) - float(value)) / step
                if abs(slope) < 1e-12:
                    continue
                delta = (desired - float(value)) / slope
                target_value = cur + delta
                target_value = max(float(var["min"]), min(float(var["max"]),
                                                           target_value))
                if var.get("type") == "int":
                    target_value = int(round(target_value))
                if target_value == cur:
                    continue
                trial = dict(current["params"])
                trial[name] = target_value
                row = prob.run(trial)
                if prob._better(row, current):
                    current = row
                    improved = True
        if not improved:
            break
    return current


def _local_polish(prob: _Problem, start: dict, *, max_nfev: int) -> dict:
    from scipy.optimize import least_squares
    if not prob.vars:
        return prob.run(start)
    u0 = np.array([prob._u(start, v) for v in prob.vars], dtype=np.float64)
    try:
        res = least_squares(
            lambda u: _residual_vector(prob, u), u0,
            bounds=(0.0, 1.0), method="trf", loss="linear",
            xtol=1e-12, ftol=1e-12, gtol=1e-12, max_nfev=max(10, int(max_nfev)),
        )
        return prob.run(prob.params(np.clip(res.x, 0.0, 1.0)))
    except Exception:  # noqa: BLE001 - fall back to the incumbent
        return prob.run(start)


def _pareto_front(rows: Iterable[dict], objectives: list[dict]) -> list[dict]:
    pool = [r for r in rows if r.get("ok") and r.get("metrics")]
    feasible = [r for r in pool if r.get("feasible")]
    if feasible:
        pool = feasible
    if not pool or not objectives:
        return []
    vectors = []
    for row in pool:
        vec = []
        for obj in objectives:
            value = metric_value(row["metrics"], obj["metric"])
            if value is None:
                vec.append(float("inf"))
            else:
                vec.append(float(value) * (-1.0 if obj["direction"] == "min" else 1.0))
        vectors.append(tuple(vec))
    front = []
    for i, vi in enumerate(vectors):
        dominated = False
        for j, vj in enumerate(vectors):
            if i == j:
                continue
            if all(a <= b for a, b in zip(vj, vi)) and any(a < b for a, b in zip(vj, vi)):
                dominated = True
                break
        if not dominated:
            front.append(pool[i])
    # Deduplicate identical metric vectors, keep the first.
    seen = set()
    unique = []
    for row in front:
        key = tuple(round(float(metric_value(row["metrics"], o["metric"]) or 0.0), 9)
                    for o in objectives)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _screen_variables(prob: _Problem, start: dict | None,
                      targets: list[dict]) -> list[dict]:
    """Keep only variables that move a violated target at the start point.

    A ten-parameter recipe often has two parameters that matter for a given
    bbox/volume target.  Screening costs two evaluations per variable and
    removes the rest, which both speeds the search and stops the optimizer from
    wandering into parameter combinations that violate recipe constraints.
    """
    if not targets or len(prob.vars) <= 2:
        return list(prob.vars)
    base_params = start or {v["name"]: v.get("initial",
                                            v["min"] + 0.5 * (v["max"] - v["min"]))
                            for v in prob.vars}
    base = prob.run(base_params)
    if not base.get("metrics"):
        return list(prob.vars)
    metrics = set(t["metric"] for t in targets)
    keep = []
    for var in prob.vars:
        lo, hi = float(var["min"]), float(var["max"])
        span = hi - lo
        if span <= 0:
            keep.append(var)
            continue
        h = max(1e-3, 0.02 * span)
        if var.get("type") == "int":
            h = max(1.0, round(h))
        moved = False
        for sign in (1.0, -1.0):
            params = dict(base_params)
            value = float(params.get(var["name"], lo)) + sign * h
            value = max(lo, min(hi, value))
            if var.get("type") == "int":
                value = int(round(value))
            params[var["name"]] = value
            row = prob.run(params)
            if not row.get("metrics"):
                continue
            for metric in metrics:
                a = metric_value(base["metrics"], metric)
                b = metric_value(row["metrics"], metric)
                if a is None or b is None:
                    continue
                scale = max(1.0, abs(float(a)))
                if abs(float(b) - float(a)) > 1e-4 * scale:
                    moved = True
                    break
            if moved:
                break
        if moved:
            keep.append(var)
    return keep or list(prob.vars)


def _sensitivity(prob: _Problem, row: dict, *, variables: list[str] | None = None,
                 targets: list[dict] | None = None,
                 objectives: list[dict] | None = None) -> list[dict]:
    """Central-difference sensitivity at the incumbent, in normalized units."""
    if not row or not row.get("metrics") or not prob.vars:
        return []
    names = set(variables or [v["name"] for v in prob.vars])
    metrics = list({t["metric"] for t in (targets or [])} |
                   {o["metric"] for o in (objectives or [])})
    if not metrics:
        return []
    base = row["metrics"]
    out = []
    for var in prob.vars:
        if var["name"] not in names:
            continue
        lo, hi = float(var["min"]), float(var["max"])
        span = hi - lo
        if span <= _EPS:
            continue
        h = max(1e-4, 1e-3 * span)
        plus = dict(row["params"])
        minus = dict(row["params"])
        if var.get("type") == "int":
            h = max(1.0, round(0.02 * span))
            plus[var["name"]] = int(round(plus[var["name"]] + h))
            minus[var["name"]] = int(round(minus[var["name"]] - h))
        else:
            plus[var["name"]] = float(plus[var["name"]] + h)
            minus[var["name"]] = float(minus[var["name"]] - h)
        if plus[var["name"]] == minus[var["name"]]:
            continue
        rp = prob.run(plus)
        rm = prob.run(minus)
        if not rp.get("metrics") or not rm.get("metrics"):
            continue
        slopes = {}
        for metric in metrics:
            vp = metric_value(rp["metrics"], metric)
            vm = metric_value(rm["metrics"], metric)
            if vp is None or vm is None:
                continue
            slope = (float(vp) - float(vm)) / (float(plus[var["name"]]) -
                                               float(minus[var["name"]]))
            slopes[metric] = slope
        if slopes:
            out.append({
                "variable": var["name"],
                "range": [lo, hi],
                "unit": var.get("unit") or "mm",
                "slopes": {k: round(float(v), 9) for k, v in slopes.items()},
            })
    return out


def _recommendations(prob: _Problem, row: dict, targets: list[dict],
                     sensitivity: list[dict]) -> list[dict]:
    if not row:
        return []
    out = []
    flat = row.get("metrics") or {}
    for spec in targets:
        v, detail = violation(flat, spec)
        if v <= 1e-9:
            continue
        metric = spec["metric"]
        value = detail.get("value")
        ranked = []
        for item in sensitivity:
            slope = item["slopes"].get(metric)
            if slope is None or abs(float(slope)) < 1e-12:
                continue
            ranked.append((abs(float(slope)), item, float(slope)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        moves = []
        for _, item, slope in ranked[:3]:
            if spec["kind"] == "eq":
                delta = float(spec["target"]) - float(value) if value is not None else 0.0
            else:
                lo, hi = spec.get("lo"), spec.get("hi")
                delta = 0.0
                if value is not None and lo is not None and float(value) < float(lo):
                    delta = float(lo) - float(value)
                elif value is not None and hi is not None and float(value) > float(hi):
                    delta = float(hi) - float(value)
            step = delta / slope if abs(slope) > _EPS else 0.0
            lo, hi = item["range"]
            step = max(lo - float(row["params"].get(item["variable"], lo)),
                       min(hi - float(row["params"].get(item["variable"], lo)), step))
            side = []
            for other in targets:
                if other["metric"] == metric:
                    continue
                other_slope = item["slopes"].get(other["metric"])
                if other_slope is None or abs(float(other_slope)) < 1e-12:
                    continue
                projected = float(other_slope) * step
                if abs(projected) < 1e-12:
                    continue
                current_other = metric_value(
                    row.get("metrics") or {}, other["metric"])
                o_val, _ = violation(row.get("metrics") or {}, other)
                side.append({
                    "metric": other["metric"],
                    "projected_delta": round(projected, 6),
                    "current": None if current_other is None
                    else round(float(current_other), 6),
                    "already_violated": bool(o_val > 1e-9),
                })
            side.sort(key=lambda s: abs(float(s["projected_delta"])), reverse=True)
            moves.append({
                "variable": item["variable"],
                "move_to": round(float(row["params"].get(item["variable"], 0.0)) + step, 6),
                "delta": round(step, 6),
                "unit": item.get("unit"),
                "slope": round(slope, 9),
                "side_effects": side[:3],
            })
        out.append({
            "metric": metric,
            "value": value,
            "target": spec.get("target"),
            "window": [spec.get("lo"), spec.get("hi")],
            "violation": round(float(v), 9),
            "moves": moves,
        })
    return out


# --------------------------------------------------------------------- public

def _normalize_variables(ranges: Mapping[str, Any] | None,
                         variables: list[dict] | None = None,
                         integer_names: Iterable[str] = ()) -> list[dict]:
    ints = {str(x) for x in (integer_names or ())}
    if variables:
        out = []
        for var in variables:
            row = dict(var)
            row["name"] = str(row["name"])
            row.setdefault("min", 0.0)
            row.setdefault("max", 1.0)
            row.setdefault("type", "int" if row["name"] in ints else "float")
            if row["type"] == "int":
                row["min"] = int(round(float(row["min"])))
                row["max"] = int(round(float(row["max"])))
            else:
                row["min"] = float(row["min"])
                row["max"] = float(row["max"])
            if row["max"] < row["min"]:
                row["min"], row["max"] = row["max"], row["min"]
            out.append(row)
        return out
    out = []
    for name, raw in dict(ranges or {}).items():
        row = {"name": str(name)}
        if isinstance(raw, Mapping):
            lo = raw.get("min", 0.0)
            hi = raw.get("max", 1.0)
            typ = raw.get("type") or ("int" if name in ints else "float")
            if "step" in raw:
                row["step"] = float(raw["step"])
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            lo, hi, typ = raw[0], raw[1], raw[2] if len(raw) > 2 else (
                "int" if name in ints else "float")
        else:
            continue
        row["min"], row["max"] = lo, hi
        row["type"] = str(typ)
        if row["type"] == "int":
            row["min"], row["max"] = int(round(float(lo))), int(round(float(hi)))
        else:
            row["min"], row["max"] = float(lo), float(hi)
        if row["max"] < row["min"]:
            row["min"], row["max"] = row["max"], row["min"]
        out.append(row)
    return out


def solve(evaluate: Callable[[dict], dict],
          ranges: Mapping[str, Any] | None = None,
          *,
          variables: list[dict] | None = None,
          integer_names: Iterable[str] = (),
          targets: Mapping[str, Any] | None = None,
          constraints: Mapping[str, Any] | None = None,
          objectives=None,
          objective=None,
          strategy: str = "auto",
          count: int = 24,
          max_evaluations: int = 120,
          seed: int = 42,
          tolerance: float = 1e-6,
          initial: Mapping[str, Any] | None = None,
          sensitivity: bool = True,
          trace: bool = False) -> dict:
    """Solve for parameters that meet metric targets.

    ``evaluate(params)`` must return ``{"ok": bool, "metrics": {...}}``.  The
    engine wires this to a draft build + measurement, so every number is real.

    Returns a JSON-able report::

        {status, strategy, feasible, solution, best, closest, pareto,
         diagnostics, sensitivity, recommendations, evaluations, ...}
    """
    t0 = time.perf_counter()
    vars_ = _normalize_variables(ranges, variables, integer_names)
    if not vars_:
        raise ValueError("solve needs at least one variable; pass ranges="
                         "{'height': [80, 120]}")
    c_targets, ignored = constraints_to_targets(constraints)
    merged = dict(c_targets)
    merged.update(dict(targets or {}))
    tgt = normalize_targets(merged)
    objs = _objective_rows(objectives if objectives is not None else objective)
    if not tgt and not objs:
        raise ValueError("solve needs targets or an objective")

    strategy = str(strategy or "auto").strip().lower()
    full_budget = max(1, int(max_evaluations))
    do_polish = strategy not in {"lhs", "sample"}
    # Keep a slice of the budget for the smooth local polish; compass search
    # alone stops at the grid resolution and leaves residuals around 1e-4.
    reserve = (max(12, min(48, full_budget // 4)) + 2 * len(vars_)
               if do_polish else 0)
    prob = _Problem(evaluate, vars_, tgt, objs,
                    max_evaluations=max(1, full_budget - reserve),
                    seed=seed, trace=trace)
    start = None
    if initial:
        start = {v["name"]: initial.get(v["name"],
                                        v.get("initial", v["min"]))
                 for v in vars_}
    screened_out: list[str] = []
    if tgt and not objs and strategy not in {"lhs", "sample"} and len(vars_) > 2:
        active = _screen_variables(prob, start, tgt)
        if active and len(active) < len(vars_):
            kept = {v["name"] for v in active}
            screened_out = [v["name"] for v in vars_ if v["name"] not in kept]
            prob.vars = active
            vars_ = active
    used = strategy
    candidates: list[dict] = []

    if strategy in {"auto", "lhs", "sample"}:
        n = max(2, int(count))
        candidates = [prob.run(p) for p in prob.lhs(n)]
        if start:
            candidates.append(prob.run(start))
        if strategy == "lhs":
            used = "lhs"
        else:
            used = "lhs+compass"
            incumbent = prob.best()
            if incumbent and prob.n_eval < prob.max_evaluations:
                candidates.append(prob.compass(incumbent["params"]))
    elif strategy in {"compass", "pattern", "hooke", "hooke-jeeves"}:
        candidates = [prob.run(p) for p in prob.lhs(max(2, int(count)))]
        start_row = prob.best()
        if start_row:
            candidates.append(prob.compass(start_row["params"]))
        used = "compass"
    elif strategy in {"nelder", "nelder-mead", "simplex"}:
        candidates = [prob.run(p) for p in prob.lhs(max(2, min(int(count), 8)))]
        start_row = prob.best() or prob.run(prob.params(np.full(len(vars_), 0.5)))
        candidates.append(_nelder_mead(prob, start_row["params"],
                                       maxiter=max(20, prob.max_evaluations // 2)))
        used = "nelder-mead"
    elif strategy in {"de", "differential-evolution", "global"}:
        best = _differential_evolution(prob, maxiter=max(4, int(count)))
        candidates.append(best)
        candidates.append(prob.compass(best["params"])) if prob.n_eval < prob.max_evaluations else None
        used = "differential-evolution"
    elif strategy in {"local", "least-squares", "lm", "slsqp"}:
        candidates = [prob.run(p) for p in prob.lhs(max(2, min(int(count), 8)))]
        start_row = prob.best()
        if start_row:
            candidates.append(_local_polish(prob, start_row["params"],
                                             max_nfev=max(20, prob.max_evaluations)))
        used = "least-squares"
    elif strategy in {"pareto", "nsga"}:
        candidates = [prob.run(p) for p in prob.lhs(max(4, int(count)))]
        for row in sorted(candidates, key=lambda r: r.get("score", -1e18),
                          reverse=True)[:3]:
            if prob.n_eval >= prob.max_evaluations:
                break
            candidates.append(prob.compass(row["params"]))
        used = "lhs+compass+pareto"
    else:
        raise ValueError("unknown strategy %r; use auto|lhs|compass|nelder-mead|"
                         "differential-evolution|local|pareto" % strategy)

    # Restore the full budget for polish/diagnostics.
    prob.max_evaluations = full_budget
    best = prob.best()
    if best is None:
        best = {"ok": False, "params": {}, "metrics": {}, "violation": 1e12,
                "feasible": False, "score": -1e18}
    tol = max(float(tolerance), 1e-9)

    def _ok(row):
        return bool(row.get("ok", True)) and float(
            row.get("violation", 1e12)) <= tol

    feasible = _ok(best)
    if do_polish and (not feasible or prob.objectives) and prob.n_eval < full_budget:
        polished = _coordinate_polish(prob, best["params"], passes=2)
        if prob._better(polished, best):
            best = polished
            feasible = _ok(best)
    if do_polish and (not feasible or prob.objectives) and prob.n_eval < full_budget:
        polished = _local_polish(prob, best["params"],
                                 max_nfev=max(10, full_budget - prob.n_eval))
        if prob._better(polished, best):
            best = polished
            feasible = _ok(best)
    if do_polish and not feasible and prob.n_eval < full_budget:
        # A fine compass restart around the closest point handles metrics that
        # are piecewise constant (topology, counts) where LM cannot move.
        candidates.append(prob.compass(best["params"], step0=0.02,
                                       min_step=1e-6))
        best = prob.best()
        feasible = _ok(best)
    if feasible:
        best["feasible"] = True

    if sensitivity:
        # Diagnostics are part of the answer: allow the two probes per variable
        # even when the search consumed the numeric budget.  The reported
        # search budget still describes the optimization itself.
        saved_limit = prob.max_evaluations
        prob.max_evaluations = max(saved_limit, prob.n_eval + 2 * len(prob.vars))
        sens = _sensitivity(prob, best, targets=tgt, objectives=objs)
        prob.max_evaluations = saved_limit
    else:
        sens = []
    recs = _recommendations(prob, best, tgt, sens)
    pareto = _pareto_front(prob.rows, objs) if objs else []
    pareto = sorted(pareto, key=lambda r: (
        -float(r.get("score", -1e18)), float(r.get("violation", 1e12))))[:24]

    diagnostics = {
        "feasible": feasible,
        "violation": round(float(best.get("violation", 1e12)), 12),
        "targets": tgt,
        "violations": [dict(v, ok=float(v.get("violation", 1)) <= 1e-9)
                       for v in (best.get("violations") or [])],
        "ignored_constraints": ignored,
        "objectives": objs,
        "evaluations": prob.n_eval,
        "cache_hits": prob.n_cache,
        "screened_out": screened_out,
        "variables": [v["name"] for v in prob.vars],
        "unique_points": len(prob.rows),
        "failures": prob.n_fail,
        "budget": prob.max_evaluations,
        "budget_exhausted": prob.n_eval >= prob.max_evaluations,
    }
    return {
        "status": "ok" if feasible else "infeasible",
        "strategy": used,
        "feasible": feasible,
        "solution": _public_row(best) if feasible else None,
        "best": _public_row(best),
        "closest": _public_row(best),
        "pareto": [_public_row(r) for r in pareto],
        "diagnostics": diagnostics,
        "sensitivity": sens,
        "recommendations": recs,
        "iterations": len(prob.rows),
        "ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


def _public_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "params": dict(row.get("params") or {}),
        "metrics": dict(row.get("metrics") or {}),
        "violation": round(float(row.get("violation", 0.0)), 12),
        "feasible": bool(row.get("feasible")),
        "score": round(float(row.get("score", 0.0)), 9),
        "verdict": row.get("verdict"),
        "error": row.get("error"),
        "cached": bool(row.get("cached", False)),
    }
