# -*- coding: utf-8 -*-
"""Atta engine: the ten verbs the agent actually sees.

Everything here returns plain JSON-able dicts.  Failures raise
:class:`atta.errors.AttaError` with a concrete ``fix``; the agent layer turns
that into a value.  The engine never imports bpy: Blender is reached through
``atta.blender.runtime`` inside the few verbs that need finish/render/export.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from . import assembly as assemblymod
from . import facts as factsmod
from . import features, materials, metrology, perception, probe, recipes
from . import solve as solvemod
from . import spec as specmod
from . import state
from . import verify as verifymod
from .errors import AttaError
from .mesh import Mesh

QUALITY = {
    "draft": {"res": 512, "samples": 8, "views": ["iso"]},
    "review": {"res": 768, "samples": 24, "views": ["iso", "front", "top"]},
    "deliver": {"res": 1200, "samples": 64, "views": ["iso", "front", "top"]},
}

_QUALITY_ALIASES = {
    "draft": "draft", "preview": "draft", "low": "draft", "safe": "draft",
    "review": "review", "standard": "review", "med": "review", "medium": "review",
    "deliver": "deliver", "hero": "deliver", "high": "deliver", "final": "deliver",
}


# ---------------------------------------------------------------- helpers

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _q(value) -> str:
    key = str(value or "draft").strip().lower()
    return _QUALITY_ALIASES.get(key, "draft")


def _jsonable(value: Any):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 1)


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(mesh: Mesh) -> dict:
    return metrology.measure(mesh, wall=False, dihedral=True, curvature=False)


def _facts(mesh: Mesh, record: dict, metrics: dict | None = None,
           *, wall: bool = False, curvature: bool = False) -> dict:
    rec = dict(record)
    if metrics is not None:
        rec["_metrics"] = metrics
    return factsmod.facts(mesh, rec, wall=wall, curvature=curvature)


def _require_models(model_id: str | None):
    rec = state.get(model_id) if model_id else state.last()
    if not rec:
        raise AttaError(
            "no_models",
            "no model found in this workspace",
            path="model_id",
            fix="call atta_model first, or pass a valid model_id from a prior result",
        )
    return rec


def _load(model_id: str | None = None):
    """Rebuild a record's geometry from its spec."""
    rec = _require_models(model_id)
    spec = rec.get("spec")
    if not isinstance(spec, dict):
        raise AttaError("bad_record", "model record has no spec to rebuild from",
                        path=rec.get("model_id"),
                        fix="rebuild with atta_model; records are specs, not meshes")
    comp = specmod.compile_spec(spec)
    if not comp.get("ok"):
        raise AttaError(
            "rebuild_failed",
            "model %s cannot be rebuilt from its spec" % rec.get("model_id"),
            path="spec",
            fix="inspect errors and rebuild with a valid spec",
            details=comp.get("errors"),
        )
    c = comp["compiled"]
    if c["kind"] == "assembly":
        built = assemblymod.build(spec, quality=c["quality"])
        finish = rec.get("finish") or _apply_finish(
            {}, spec.get("finish"), material=spec.get("material"))
        return rec, built["mesh"], finish
    if c["kind"] == "recipe":
        recipe = recipes.get(c["recipe"])
        mesh, base_finish = recipe.build_shape(c["params_m"], c["quality"])
    else:
        mesh = features.evaluate(
            c["features"],
            params=c.get("params_m") or {},
            units=c.get("units", "mm"),
            output=c.get("output"),
        )
        base_finish = {}
    finish = rec.get("finish") or _apply_finish(base_finish, spec.get("finish"),
                                                material=spec.get("material"))
    return rec, mesh, finish


def _apply_finish(base: dict | None, public: dict | None,
                  material: str | None = None) -> dict:
    from .blender.finish import normalize_finish
    f = normalize_finish(base or {})
    for key, value in dict(public or {}).items():
        if key == "bevel" and isinstance(value, dict):
            if "width_mm" in value:
                f["bevel"]["width"] = float(value["width_mm"]) / 1000.0
            if "width" in value:
                f["bevel"]["width"] = float(value["width"])
            if "segments" in value:
                f["bevel"]["segments"] = int(value["segments"])
            if "angle_deg" in value:
                f["bevel"]["angle_deg"] = float(value["angle_deg"])
        elif key == "bevel_mm":
            f["bevel"]["width"] = float(value) / 1000.0
        elif key in ("smooth_deg", "smooth_angle_deg"):
            f["smooth_angle_deg"] = float(value)
        elif key == "bevel_segments":
            f["bevel"]["segments"] = int(value)
        elif key == "bevel_angle_deg":
            f["bevel"]["angle_deg"] = float(value)
        elif key == "smooth":
            f["smooth"] = bool(value)
        elif key == "material":
            f["material"] = str(value)
    if material:
        f["material"] = str(material)
    return normalize_finish(f)


def _public_finish(finish: dict | None) -> dict:
    from .blender.finish import normalize_finish
    f = normalize_finish(finish or {})
    out: dict[str, Any] = {
        "smooth": bool(f["smooth"]),
        "smooth_deg": float(f["smooth_angle_deg"]),
    }
    if float(f["bevel"]["width"]) > 0.0:
        out["bevel_mm"] = round(float(f["bevel"]["width"]) * 1000.0, 6)
        out["bevel_segments"] = max(1, int(f["bevel"]["segments"]))
        out["bevel_angle_deg"] = float(f["bevel"]["angle_deg"])
    if f.get("material"):
        out["material"] = str(f["material"])
    return out


def _recipe_spec(recipe, display: dict, preset: str, quality: str,
                 material: str, finish: dict, name: str,
                 constraints: dict | None = None) -> dict:
    return {
        "schema": specmod.SCHEMA,
        "name": str(name or recipe.id),
        "model": {
            "kind": "recipe",
            "id": recipe.id,
            "preset": preset,
            "params": _jsonable(display),
        },
        "quality": quality,
        "material": material,
        "finish": _public_finish(finish),
        "constraints": _jsonable(constraints or {}),
        "assumptions": [],
    }


def _summary(rec: dict) -> dict:
    return {
        "model_id": rec.get("model_id"),
        "name": rec.get("name"),
        "kind": rec.get("kind"),
        "recipe": rec.get("recipe"),
        "quality": rec.get("quality"),
        "material": rec.get("material"),
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": rec.get("mesh_hash"),
        "expected_genus": rec.get("expected_genus"),
        "created": rec.get("created"),
    }


def _commit(mesh: Mesh, spec_obj: dict, finish: dict, *, name: str,
            recipe_id: str | None, kind: str, quality: str,
            params_display: dict, preset: str | None,
            expected_genus, issues: list, applied: list) -> dict:
    mesh_hash = mesh.hash()
    metrics = _metrics(mesh)
    record = {
        "schema": state.SCHEMA,
        "model_id": state.new_id(mesh_hash),
        "name": str(name),
        "kind": kind,
        "recipe": recipe_id,
        "spec": _jsonable(spec_obj),
        "spec_hash": specmod.spec_hash(spec_obj),
        "mesh_hash": mesh_hash,
        "params": _jsonable(params_display),
        "preset": preset,
        "quality": quality,
        "material": finish.get("material"),
        "finish": _jsonable(finish),
        "expected_genus": expected_genus,
        "constraints": _jsonable(spec_obj.get("constraints") or {}),
        "assumptions": _jsonable(spec_obj.get("assumptions") or []),
        "metrics": _jsonable(metrics),
        "objects": [str(name)],
        "created": _now(),
        "units": "mm",
        "exact": False,
        "artifacts": {},
    }
    record["facts"] = _facts(mesh, record, metrics)
    record = _jsonable(record)
    state.put(record)
    return {
        "status": "repaired" if any(i.get("severity") in ("warn", "warning")
                                    for i in (issues or [])) else "ok",
        "model_id": record["model_id"],
        "model": _summary(record),
        "metrics": _jsonable(metrics),
        "facts": record["facts"],
        "issues": _jsonable(issues or []),
        "applied_patches": _jsonable(applied or []),
        "next": [
            "atta_describe model_id=%s" % record["model_id"],
            "atta_preview model_id=%s" % record["model_id"],
            "atta_verify model_id=%s" % record["model_id"],
        ],
    }


def _commit_assembly(built: dict, spec_obj: dict, *, name: str,
                     quality: str) -> dict:
    mesh = built["mesh"]
    metrics = built.get("metrics") or _metrics(mesh)
    asm = built.get("assembly") or {}
    mesh_hash = mesh.hash()
    record = {
        "schema": state.SCHEMA,
        "model_id": state.new_id(mesh_hash),
        "name": str(name),
        "kind": "assembly",
        "recipe": None,
        "spec": _jsonable(spec_obj),
        "spec_hash": specmod.spec_hash(spec_obj),
        "mesh_hash": mesh_hash,
        "params": {},
        "preset": None,
        "quality": quality,
        "material": spec_obj.get("material"),
        "finish": _jsonable(spec_obj.get("finish") or {}),
        "expected_genus": None,
        "constraints": _jsonable(spec_obj.get("constraints") or {}),
        "assumptions": _jsonable(spec_obj.get("assumptions") or []),
        "metrics": _jsonable(metrics),
        "objects": [str(p.get("id")) for p in asm.get("parts") or []],
        "parts": _jsonable(asm.get("parts") or []),
        "placements": _jsonable(built.get("placements") or {}),
        "assembly": _jsonable({k: v for k, v in asm.items()
                               if k != "part_meshes"}),
        "created": _now(),
        "units": "mm",
        "exact": False,
        "artifacts": {},
    }
    facts = _facts(mesh, record, metrics)
    facts.update({
        "assembly": {
            "part_count": asm.get("part_count"),
            "mates": len(asm.get("mates") or []),
            "conflicts": len(asm.get("conflicts") or []),
            "dof": (asm.get("dof") or {}).get("dof"),
            "collisions": len((asm.get("collision") or {}).get("collisions") or []),
            "min_clearance_mm": (asm.get("clearance") or {}).get("min_mm"),
        },
        "one_line": "%d-part assembly, dof=%s, %d collision(s), %d mate(s)"
        % (int(asm.get("part_count") or 0),
           (asm.get("dof") or {}).get("dof"),
           len((asm.get("collision") or {}).get("collisions") or []),
           len(asm.get("mates") or [])),
    })
    record["facts"] = _jsonable(facts)
    record = _jsonable(record)
    state.put(record)

    conflicts = asm.get("conflicts") or []
    status = "conflict" if conflicts else (
        "under_constrained" if (asm.get("dof") or {}).get("under_constrained")
        else "ok")
    issues = [{
        "severity": "error",
        "code": "mate_conflict",
        "mate": m.get("id"),
        "residual_mm": m.get("residual_mm"),
        "message": "mate %s cannot be satisfied" % m.get("id"),
        "fix": "loosen its offset, change the port pair, or remove the mate",
    } for m in conflicts]
    return {
        "status": status,
        "model_id": record["model_id"],
        "model": _summary(record),
        "metrics": _jsonable(metrics),
        "facts": record["facts"],
        "assembly": record["assembly"],
        "placements": record.get("placements") or {},
        "issues": _jsonable(issues),
        "next": [
            "atta_describe model_id=%s" % record["model_id"],
            "atta_verify model_id=%s" % record["model_id"],
            "atta_preview model_id=%s" % record["model_id"],
        ],
    }


# ---------------------------------------------------------------- build

def _build_from_recipe(recipe_id: str, params: dict | None, patches: dict | None,
                       preset: str, quality: str, name: str | None,
                       material: str | None, finish: dict | None) -> dict:
    recipe = recipes.get(recipe_id)
    patch = dict(params or {})
    patch.update(patches or {})
    resolved = recipe.resolve(patch=patch, preset=preset)
    if getattr(resolved, "blocked", False):
        raise AttaError(
            "recipe_blocked",
            "recipe %s cannot build with these parameters" % recipe.id,
            path="params",
            fix="fix the first issue's fix (clamped/typed values are in applied_patches)",
            details=_jsonable(resolved.issues),
        )
    mesh, base_finish = recipe.build_shape(resolved.values, quality)
    mat = material or recipe.material
    finish_final = _apply_finish(base_finish, finish, material=mat)
    spec_obj = _recipe_spec(recipe, resolved.display, preset, quality,
                            finish_final.get("material") or mat, finish_final,
                            name or recipe.id)
    return _commit(
        mesh, spec_obj, finish_final,
        name=name or recipe.id, recipe_id=recipe.id, kind="recipe",
        quality=quality, params_display=resolved.display, preset=preset,
        expected_genus=recipe.expected_genus, issues=resolved.issues,
        applied=resolved.applied_patches,
    )


def _build_from_spec(spec: dict, params: dict | None, patches: dict | None,
                     quality: str | None, name: str | None,
                     material: str | None, finish: dict | None) -> dict:
    validated = specmod.validate_spec(spec)
    if not validated.get("ok"):
        raise AttaError(
            "spec_invalid",
            "spec validation failed",
            path="$",
            fix="fix errors[] or call atta_spec op=propose to fill design defaults",
            details=_jsonable(validated.get("errors")),
        )
    s = validated["spec"]
    if params or patches:
        if s["model"]["kind"] != "recipe":
            raise AttaError(
                "patches_not_supported",
                "params/patches only apply to recipe specs",
                path="patches",
                fix="edit graph feature args directly, or use a recipe spec",
            )
        merged = dict(s["model"].get("params") or {})
        merged.update(params or {})
        merged.update(patches or {})
        s["model"]["params"] = merged
    if quality:
        s["quality"] = _q(quality)
    if name:
        s["name"] = str(name)
    if material:
        s["material"] = str(material)
    if finish:
        s["finish"] = dict(s.get("finish") or {})
        s["finish"].update(finish)

    compiled = specmod.compile_spec(s)
    if not compiled.get("ok"):
        raise AttaError(
            "spec_compile_failed",
            "spec could not be compiled",
            path="$",
            fix="fix errors[]; recipe constraints may block this parameter set",
            details=_jsonable(compiled.get("errors")),
        )
    c = compiled["compiled"]
    if c["kind"] == "assembly":
        built = assemblymod.build(s, quality=c["quality"])
        finish_final = _apply_finish({}, c.get("finish"),
                                     material=c["material"])
        s["finish"] = _public_finish(finish_final)
        s["material"] = finish_final.get("material") or c["material"]
        return _commit_assembly(built, s, name=s["name"], quality=c["quality"])
    if c["kind"] == "recipe":
        recipe = recipes.get(c["recipe"])
        resolved = recipe.resolve(patch=c["params_display"], preset=c["preset"])
        if getattr(resolved, "blocked", False):
            raise AttaError(
                "recipe_blocked",
                "recipe %s cannot build with these parameters" % recipe.id,
                path="spec.model.params",
                fix="fix the first issue's fix",
                details=_jsonable(resolved.issues),
            )
        mesh, base_finish = recipe.build_shape(c["params_m"], c["quality"])
        expected = recipe.expected_genus
        params_display = c["params_display"]
        recipe_id = recipe.id
        issues = resolved.issues
        applied = resolved.applied_patches
        mat = c["material"]
    else:
        mesh = features.evaluate(
            c["features"], params=c.get("params_m") or {},
            units=c.get("units", "mm"), output=c.get("output"),
        )
        base_finish = {}
        expected = (c.get("constraints") or {}).get("genus")
        params_display = {}
        recipe_id = None
        issues = []
        applied = []
        mat = c["material"]

    finish_final = _apply_finish(base_finish, c.get("finish"), material=mat)
    s["finish"] = _public_finish(finish_final)
    s["material"] = finish_final.get("material") or mat
    return _commit(
        mesh, s, finish_final,
        name=s["name"], recipe_id=recipe_id, kind=c["kind"],
        quality=c["quality"], params_display=params_display,
        preset=c.get("preset"), expected_genus=expected,
        issues=issues, applied=applied,
    )


# ---------------------------------------------------------------- verbs

def doctor(deep: bool = False) -> dict:
    t0 = time.perf_counter()
    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    deps = {
        "numpy": version("numpy"),
        "scipy": version("scipy"),
        "trimesh": version("trimesh"),
        "manifold3d": version("manifold3d"),
    }
    try:
        import numpy as _np
        deps["numpy"] = deps["numpy"] or _np.__version__
    except Exception:  # noqa: BLE001
        pass

    blender = {"available": False, "path": None, "version": None,
               "version_tuple": None, "supported": None, "error": None,
               "what_works_without_blender": [
                   "draft geometry (atta_model, atta_spec)",
                   "measurement and facts (atta_describe)",
                   "verify verdicts (atta_verify)",
                   "STEP export (atta_deliver --formats step)",
               ]}
    try:
        from .blender import available as blender_available, find_blender
        from .blender import blender_version
        blender["path"] = find_blender()
        blender["available"] = bool(blender_available())
        if blender["available"]:
            # Version drives behaviour (export operators, EEVEE identity,
            # shading APIs), so doctor must report it, not just "found".
            found = blender_version()
            if found:
                blender["version"] = found.get("version")
                blender["version_tuple"] = found.get("version_tuple")
                blender["supported"] = bool(
                    tuple(found.get("version_tuple") or (0,)) >= (3, 6))
    except Exception as exc:  # noqa: BLE001
        blender["error"] = "%s: %s" % (type(exc).__name__, exc)

    workspace = state.workspace()
    checks = []

    def check(cid, ok, severity="error", message=None, fix=None, value=None,
              expected=None):
        checks.append({
            "id": cid, "ok": bool(ok), "severity": severity,
            "message": message or ("ok" if ok else cid + " failed"),
            "value": value, "expected": expected, "fix": fix,
        })

    check("recipes", len(recipes.all_recipes()) > 0, value=len(recipes.all_recipes()),
          message="recipe registry is populated",
          fix="import atta.recipes and check registration errors")
    check("features", len(features.FEATURE_OPS) > 0, value=len(features.FEATURE_OPS),
          message="feature graph ops are registered")
    check("workspace_writable", os.access(workspace, os.W_OK), value=str(workspace),
          message="workspace is writable",
          fix="set ATTA_HOME to a writable directory")
    check("blender", blender["available"], severity="warn",
          value=blender["path"],
          message="Blender is available for finish/render/export" if blender["available"]
          else "Blender not found; draft geometry still works, preview/deliver will not",
          fix="install Blender or set ATTA_BLENDER to its executable")
    check("numpy", bool(deps.get("numpy")), value=deps.get("numpy"),
          message="numpy is available")
    check("trimesh", bool(deps.get("trimesh")), value=deps.get("trimesh"),
          message="trimesh is available", severity="warn")

    smoke = None
    if deep:
        t1 = time.perf_counter()
        try:
            m = features.evaluate([
                {"id": "cube", "op": "box", "args": {"size": [10, 10, 10]}}
            ])
            topo = m.topology()
            smoke = {
                "ok": bool(topo["closed"]),
                "closed": topo["closed"],
                "volume_mm3": round(m.volume() * 1e9, 3),
                "ms": round((time.perf_counter() - t1) * 1000.0, 1),
            }
            check("smoke_cube", smoke["ok"], value=smoke,
                  message="10 mm cube builds and is closed")
        except Exception as exc:  # noqa: BLE001
            smoke = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            check("smoke_cube", False, value=smoke,
                  message="10 mm cube build failed",
                  fix="inspect the traceback in atta/features.py")

    failed = [c for c in checks if not c["ok"]]
    errors = [c for c in failed if c["severity"] == "error"]
    return {
        "ok": not errors,
        "version": __version__ if "__version__" in globals() else _version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "deps": deps,
        "blender": blender,
        "workspace": str(workspace),
        "models": len(state.load()["models"]),
        "recipes": [r.id for r in recipes.all_recipes()],
        "features": len(features.FEATURE_OPS),
        "materials": len(materials.names()),
        "checks": checks,
        "smoke": smoke,
        "ms": _ms(t0),
    }


def catalog(verbose: bool = False) -> dict:
    from .blender import EXPORT_FORMATS, VIEWS
    out = {
        "version": _version(),
        "recipes": [r.to_dict(verbose=verbose) for r in recipes.all_recipes()],
        "features": {
            op: {
                "required": list(meta.get("required") or ()),
                "optional": list(meta.get("optional") or ()),
                "arity": meta.get("arity"),
                "doc": meta.get("doc"),
            }
            for op, meta in sorted(features.FEATURE_OPS.items())
        },
        "materials": materials.catalog(),
        "qualities": list(QUALITY),
        "quality_profiles": QUALITY,
        "perception": {
            "tool": "atta_describe",
            "args": ["perceive", "views", "res", "sections", "compare_to", "trace"],
            "views": ["front", "top", "right", "left", "back", "iso"],
            "default_res": 24,
            "traits": ["size_mm", "aspect", "sphericity", "convexity",
                       "symmetry", "holes_est", "bbox_fill", "wall", "curvature"],
            "note": "pure numpy ASCII/viewfinder; no Blender render; use before preview",
        },
        "solve": {
            "tool": "atta_variant",
            "args": ["targets", "constraints", "objective", "strategy", "count",
                     "max_evaluations", "tolerance", "initial", "pareto",
                     "free", "fixed", "solve"],
            "strategies": ["auto", "lhs", "compass", "nelder-mead",
                           "differential-evolution", "local", "pareto"],
            "targets": "metric -> value | [min,max] | {target,tol} | {min,max}",
            "metrics": ["bbox_x", "bbox_y", "bbox_z", "volume_mm3", "area_mm2",
                        "min_wall_mm", "genus", "closed", "n_components",
                        "dihedral_max_deg", "curvature_p95_deg"],
            "note": "builds real draft meshes; returns feasible solution + "
                    "sensitivity + closest achievable when infeasible",
        },
        "assembly": {
            "spec_kind": "assembly",
            "mates": list(specmod.MATE_TYPES),
            "ports": "declared frame {origin_mm, axis, normal, radius_mm} or "
                     "auto center/axis_x/y/z/face_*",
            "solver": "SE(3) least squares + weak prior; DOF from Jacobian rank",
            "checks": ["per-part closed/volume", "mate residual", "DOF",
                       "AABB broadphase", "boolean interference",
                       "sampled clearance"],
        },
        "views": list(VIEWS),
        "formats": list(EXPORT_FORMATS) + ["step"],
        "constraints": dict(specmod.CONSTRAINT_DOCS),
        "finish": dict(specmod.FINISH_DOCS),
        "verbs": ["doctor", "catalog", "spec", "model", "variant", "describe",
                  "verify", "preview", "deliver", "run"],
        "units": {"spec": "mm", "kernel": "m", "angles": "deg"},
        "loops": {
            "fast": "spec -> model(draft) -> describe -> model(patches)",
            "slow": "verify -> preview -> deliver",
        },
    }
    if verbose:
        out["spec_schema"] = specmod.schema()
        out["how"] = {
            "recipe": "atta_model(recipe='product.mug', params={...})",
            "graph": "atta_model(spec={schema, name, model:{kind:'graph', features:[...]}})",
            "assumptions": "atta_spec(spec=..., op='propose')",
        }
    return out


def spec(spec: dict | None = None, op: str = "validate",
         assume: bool = False) -> dict:
    op = str(op or "validate").strip().lower()
    if op == "schema":
        return {"ok": True, "schema": specmod.schema()}
    if op not in {"validate", "propose", "compile"}:
        raise AttaError("bad_arguments", "unknown spec op %r" % op, path="op",
                        fix="use validate | propose | compile | schema")
    if spec is None:
        raise AttaError("bad_arguments", "spec is required for op=%s" % op,
                        path="spec", fix="pass the Spec JSON object")
    if op == "validate":
        if assume:
            proposed = specmod.propose_spec(spec)
            checked = specmod.validate_spec(proposed.get("spec"))
            return {
                "ok": bool(proposed.get("ok") and checked.get("ok")),
                "op": "validate",
                "spec": checked.get("spec"),
                "errors": checked.get("errors") or proposed.get("errors") or [],
                "assumption_ledger": proposed.get("assumption_ledger") or [],
                "assumption_summary": proposed.get("assumption_summary") or {},
                "unresolved": proposed.get("unresolved") or [],
            }
        out = specmod.validate_spec(spec)
        out["op"] = "validate"
        return out
    if op == "propose":
        out = specmod.propose_spec(spec)
        out["op"] = "propose"
        return out
    out = specmod.compile_spec(spec)
    out["op"] = "compile"
    return out


def model(recipe: str | None = None, spec: dict | None = None,
          params: dict | None = None, patches: dict | None = None,
          preset: str = "nominal", quality: str = "draft",
          name: str | None = None, material: str | None = None,
          finish: dict | None = None) -> dict:
    t0 = time.perf_counter()
    q = _q(quality)
    if recipe and spec:
        raise AttaError("bad_arguments", "pass recipe or spec, not both",
                        path="recipe", fix="choose one input")
    if not recipe and not spec:
        raise AttaError("bad_arguments", "atta_model needs recipe or spec",
                        path="recipe",
                        fix="pass recipe='product.mug' or a Spec JSON")
    from .locks import project_lock
    with project_lock(exclusive=True):
        if recipe:
            out = _build_from_recipe(recipe, params, patches, preset, q, name,
                                     material, finish)
        else:
            out = _build_from_spec(spec, params, patches, q, name, material, finish)
    out["ms"] = _ms(t0)
    return out


def _mentions(value, key: str) -> bool:
    """True when a nested target/constraint document names ``key``."""
    if isinstance(value, dict):
        return any(str(k) == key or _mentions(v, key) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_mentions(v, key) for v in value)
    return False


def _range_of(rng, base: float, ps) -> tuple[float, float]:
    if isinstance(rng, dict) and ("min" in rng or "max" in rng):
        lo = float(rng.get("min", base))
        hi = float(rng.get("max", base))
    elif isinstance(rng, (list, tuple)) and len(rng) >= 2:
        lo, hi = float(rng[0]), float(rng[1])
    else:
        lo = float(ps.min) if ps.min is not None else base * 0.5
        hi = float(ps.max) if ps.max is not None else base * 1.5
    if ps.min is not None:
        lo = max(lo, float(ps.min))
    if ps.max is not None:
        hi = min(hi, float(ps.max))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _solve_variables(r, base_display: dict, ranges: dict | None,
                     free=None, fixed=None) -> list[dict]:
    """Design variables for the solver.

    Without ``ranges`` a solve uses each parameter's full declared bounds: a
    target such as ``volume_mm3=500000`` is often outside ±20% of nominal.
    Sampling (the old variant path) keeps the narrower ±20% default.
    """
    free_set = None
    if free:
        free_set = {str(x) for x in (free.split(",") if isinstance(free, str) else free)}
    fixed_set = {str(x) for x in (fixed.split(",") if isinstance(fixed, str) else (fixed or []))}
    out: list[dict] = []
    for name, ps in sorted(r.params.items()):
        if ps.type not in ("float", "int"):
            continue
        if free_set is not None and name not in free_set:
            continue
        if name in fixed_set:
            continue
        if ps.advanced and not (ranges and name in ranges):
            continue
        base = float(base_display.get(name, ps.default if ps.default is not None else 0.0))
        rng = ranges.get(name) if isinstance(ranges, dict) and name in ranges else None
        lo, hi = _range_of(rng, base, ps)
        row = {"name": name, "min": lo, "max": hi,
               "type": ps.type, "unit": getattr(ps, "unit", None)}
        if rng is None:
            # A named solve target should be reachable; the declared bounds are
            # the contract, not an arbitrary ±20% neighbourhood.
            row["min"] = float(ps.min) if ps.min is not None else lo
            row["max"] = float(ps.max) if ps.max is not None else hi
        if row["type"] == "int":
            row["min"], row["max"] = int(round(row["min"])), int(round(row["max"]))
        out.append(row)
    return out


def _recipe_metrics(recipe, params: dict, preset: str, quality: str, *,
                    wall: bool = False, curvature: bool = False) -> dict:
    """Build a draft mesh without committing a model, then measure it.

    Solver evaluations must not pollute ``models.json`` with hundreds of
    candidates.  The returned metrics are the same ones ``verify`` sees.
    """
    try:
        resolved = recipe.resolve(patch=dict(params), preset=preset)
        if getattr(resolved, "blocked", False):
            first = (resolved.issues or [{}])[0]
            return {
                "ok": False,
                "metrics": {},
                "params": _jsonable(resolved.display),
                "error": {
                    "code": "recipe_blocked",
                    "message": str(first.get("message") or "recipe constraints blocked"),
                    "fix": first.get("fix"),
                    "issues": _jsonable(resolved.issues),
                },
            }
        mesh, _ = recipe.build_shape(resolved.values, quality)
        metrics = metrology.measure(mesh, wall=wall, dihedral=True,
                                    curvature=curvature)
        metrics["expected_genus"] = recipe.expected_genus
        return {
            "ok": True,
            "metrics": _jsonable(metrics),
            "params": _jsonable(resolved.display),
            "issues": _jsonable(resolved.issues),
        }
    except AttaError as exc:
        return {"ok": False, "metrics": {}, "error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 - a failed candidate is data
        return {"ok": False, "metrics": {},
                "error": {"code": "build_failed",
                          "message": "%s: %s" % (type(exc).__name__, exc)}}


def _solve_strategy(strategy) -> str:
    """In solve mode "lhs" means the default solve, not sampling-only.

    Sampling-only lhs cannot hit an equality target, and the tool/CLI default
    strategy is lhs. Treating lhs as auto (lhs + compass + local polish) is the
    only useful reading here; pure sampling stays available with solve=False.
    """
    mode = str(strategy or "lhs").strip().lower()
    return "auto" if mode in {"", "none", "lhs", "sample", "random"} else mode


def _variant_solve(recipe, base_rec, count: int, ranges: dict | None,
                   strategy: str, seed: int, objective, constraints,
                   quality: str, *, targets: dict | None,
                   max_evaluations: int | None, tolerance: float,
                   initial: dict | None, pareto: bool,
                   free=None, fixed=None) -> dict:
    t0 = time.perf_counter()
    r = recipes.get(recipe)
    preset = (base_rec or {}).get("preset") or "nominal"
    base_display = dict((base_rec or {}).get("params") or {})
    if not base_display:
        base_display = dict(r.resolve({}, preset).display)

    variables = _solve_variables(r, base_display, ranges, free=free, fixed=fixed)
    if not variables:
        raise AttaError(
            "no_numeric_params",
            "recipe %s has no solvable numeric parameters" % recipe,
            path="ranges",
            fix="name float/int parameters in ranges/free, or choose another recipe",
        )

    base_constraints = dict((base_rec or {}).get("constraints") or {})
    cons = dict(base_constraints)
    cons.update(constraints or {})
    need_wall = _mentions(targets, "min_wall_mm") or _mentions(cons, "min_wall_mm")
    need_curv = (_mentions(targets, "curvature_p95_deg")
                 or _mentions(cons, "curvature_p95_deg"))

    def evaluate(params):
        return _recipe_metrics(r, params, preset, quality,
                               wall=need_wall, curvature=need_curv)

    budget = int(max_evaluations) if max_evaluations else max(48, min(240, 12 * count))
    report = solvemod.solve(
        evaluate,
        variables=variables,
        targets=targets,
        constraints=cons or None,
        objective=objective,
        strategy=("pareto" if pareto else _solve_strategy(strategy)),
        count=count,
        max_evaluations=budget,
        seed=seed,
        tolerance=tolerance,
        initial=initial,
    )
    report.update({
        "status": report.get("status") or ("ok" if report.get("feasible") else "infeasible"),
        "recipe": recipe,
        "preset": preset,
        "requested_strategy": strategy,
        "variables": variables,
        "count": len(report.get("pareto") or []),
        "ms": _ms(t0),
    })

    solution = report.get("solution")
    candidates: list[dict] = []
    if solution:
        built = model(recipe=recipe, params=solution["params"], preset=preset,
                      quality=quality, name=None, material=None, finish=None)
        mid = built["model_id"]
        solution.update({
            "model_id": mid,
            "model": built.get("model"),
            "facts": built.get("facts"),
            "verdict": None,
        })
        try:
            ver = verify(model_id=mid, level="basic")
            solution["verdict"] = ver.get("verdict")
            solution["checks"] = ver.get("checks")
            solution["issues"] = ver.get("issues")
        except AttaError as exc:
            solution["verdict"] = "error"
            solution["error"] = exc.to_dict()
        report["solution"] = solution
        report["best"] = solution
        report["model_id"] = mid
        report["model"] = built.get("model")
        report["facts"] = built.get("facts")
        candidates.append({
            "ok": True, "model_id": mid, "params": solution["params"],
            "metrics": solution["metrics"], "facts": built.get("facts"),
            "verdict": solution.get("verdict"), "score": solution.get("score"),
        })
    else:
        report["best"] = report.get("closest")
    for row in report.get("pareto") or []:
        row["verdict"] = None
    report["candidates"] = candidates
    report["pareto"] = report.get("pareto") or []
    report["strategy"] = report.get("strategy") or strategy
    report["ms"] = _ms(t0)
    return report


def _assembly_variables(spec_obj: dict, ranges: dict | None,
                        free=None, fixed=None) -> list[dict]:
    model = spec_obj.get("model") or {}
    free_set = None
    if free:
        free_set = {str(x) for x in (free.split(",") if isinstance(free, str) else free)}
    fixed_set = {str(x) for x in (fixed.split(",") if isinstance(fixed, str)
                                  else (fixed or []))}
    out: list[dict] = []
    for part in model.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        pid = str(part.get("id") or "")
        if not pid:
            continue
        if part.get("recipe"):
            try:
                recipe = recipes.get(str(part["recipe"]))
            except Exception:  # noqa: BLE001 - validation already ran
                continue
            base = dict(part.get("params") or {})
            for name, ps in sorted(recipe.params.items()):
                if ps.type not in ("float", "int"):
                    continue
                dotted = "%s.%s" % (pid, name)
                if free_set is not None and dotted not in free_set:
                    continue
                if dotted in fixed_set:
                    continue
                rng = ranges.get(dotted) if isinstance(ranges, dict) else None
                if ps.advanced and rng is None:
                    continue
                if rng is None and not isinstance(ranges, dict):
                    rng = None
                if rng is None and name in (ranges or {}):
                    rng = ranges.get(name)
                value = float(base.get(name, ps.default
                                       if ps.default is not None else 0.0))
                if rng is not None:
                    lo, hi = _range_of(rng, value, ps)
                else:
                    lo = float(ps.min) if ps.min is not None else value * 0.5
                    hi = float(ps.max) if ps.max is not None else value * 1.5
                row = {"name": dotted, "min": lo, "max": hi,
                       "type": ps.type, "unit": getattr(ps, "unit", None),
                       "part": pid, "param": name, "initial": value}
                if row["type"] == "int":
                    row["min"], row["max"] = int(round(lo)), int(round(hi))
                out.append(row)
        elif part.get("params") and isinstance(part["params"], Mapping):
            for name, value in sorted(part["params"].items()):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                dotted = "%s.%s" % (pid, name)
                if free_set is not None and dotted not in free_set:
                    continue
                if dotted in fixed_set:
                    continue
                rng = (ranges or {}).get(dotted)
                if rng is None:
                    continue
                lo, hi = _range_of(rng, float(value), _NullParam())
                out.append({"name": dotted, "min": lo, "max": hi,
                            "type": "float", "unit": None,
                            "part": pid, "param": name,
                            "initial": float(value)})
    return out


class _NullParam:
    min = None
    max = None


def _patch_assembly(spec_obj: dict, params: dict) -> dict:
    out = copy.deepcopy(spec_obj)
    for dotted, value in (params or {}).items():
        if "." not in str(dotted):
            continue
        pid, name = str(dotted).split(".", 1)
        for part in (out.get("model") or {}).get("parts") or []:
            if str(part.get("id")) != pid:
                continue
            part.setdefault("params", {})
            part["params"][name] = (int(round(float(value)))
                                    if float(value).is_integer() else float(value))
            break
    return out


def _assembly_metrics(built: dict) -> dict:
    flat = solvemod.flatten_metrics(built.get("metrics") or {})
    asm = built.get("assembly") or {}
    dof = (asm.get("dof") or {}).get("dof")
    flat["dof"] = float(dof) if dof is not None else 999.0
    collisions = (asm.get("collision") or {}).get("collisions") or []
    flat["collisions"] = float(len(collisions))
    flat["interference_mm3"] = float(max(
        (float(c.get("volume_mm3") or 0.0) for c in collisions), default=0.0))
    cl = (asm.get("clearance") or {}).get("clearances") or []
    threshold = float((asm.get("clearance") or {}).get("threshold_mm") or 5.0)
    flat["min_clearance_mm"] = float(min(
        (float(r.get("min_mm") or 0.0) for r in cl), default=max(threshold, 1000.0)))
    flat["mate_conflicts"] = float(len(asm.get("conflicts") or []))
    flat["worst_residual_mm"] = float(asm.get("worst_residual_mm") or 0.0)
    flat["part_count"] = float(asm.get("part_count") or 0)
    return flat


def _assembly_variant(base_rec: dict, count: int, ranges: dict | None,
                      strategy: str, seed: int, objective, constraints,
                      quality: str, *, targets: dict | None,
                      max_evaluations: int | None, tolerance: float,
                      initial: dict | None, pareto: bool,
                      free=None, fixed=None) -> dict:
    t0 = time.perf_counter()
    spec_obj = base_rec.get("spec") or {}
    variables = _assembly_variables(spec_obj, ranges, free=free, fixed=fixed)
    if not variables:
        raise AttaError(
            "no_numeric_params",
            "this assembly has no solvable part parameters",
            path="ranges",
            fix="use a recipe part with float/int params, or give a graph part "
                "a params dict plus a ranges entry for part.param",
        )
    tgt = dict(targets or {})
    cons = dict(spec_obj.get("constraints") or {})
    cons.update(constraints or {})
    if cons.get("fully_constrained") is True:
        tgt.setdefault("dof", 0)
    clearance_rows = cons.get("clearance") or []
    if isinstance(clearance_rows, Mapping):
        clearance_rows = [clearance_rows]
    needs = [float(r.get("min", r.get("min_mm", 0.0)))
             for r in clearance_rows if isinstance(r, Mapping)]
    if needs:
        tgt.setdefault("min_clearance_mm", {"min": max(needs)})
    inter_rows = cons.get("interference") or []
    if isinstance(inter_rows, Mapping):
        inter_rows = [inter_rows]
    caps = [float(r.get("max", r.get("max_mm3", 1e-6)))
            for r in inter_rows if isinstance(r, Mapping)]
    if caps:
        tgt.setdefault("interference_mm3", {"max": min(caps)})
    env = cons.get("envelope")
    if isinstance(env, Mapping):
        for i, axis in enumerate("xyz"):
            mn = list(env.get("min") or [None, None, None])
            mx = list(env.get("max") or [None, None, None])
            if i < len(mn) and mn[i] is not None:
                tgt.setdefault("bbox_min_" + axis, {"min": float(mn[i])})
            if i < len(mx) and mx[i] is not None:
                tgt.setdefault("bbox_max_" + axis, {"max": float(mx[i])})

    cache: dict = {}
    init = {}
    for var in variables:
        if initial and var["name"] in initial:
            init[var["name"]] = initial[var["name"]]
        elif initial and var["param"] in initial:
            init[var["name"]] = initial[var["param"]]
        else:
            # Current part value is the natural starting point.
            init[var["name"]] = var.get(
                "initial", var["min"] + 0.5 * (var["max"] - var["min"]))

    def evaluate(params):
        try:
            patched = _patch_assembly(spec_obj, params)
            built = assemblymod.build(patched, quality=quality, cache=cache)
            return {"ok": True, "metrics": _assembly_metrics(built)}
        except AttaError as exc:
            return {"ok": False, "metrics": {}, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "metrics": {},
                    "error": {"code": "assembly_build_failed",
                              "message": "%s: %s" % (type(exc).__name__, exc)}}

    budget = (int(max_evaluations) if max_evaluations
              else max(48, min(200, 10 * count)))
    report = solvemod.solve(
        evaluate, variables=variables, targets=tgt or None,
        constraints=cons, objective=objective,
        strategy=("pareto" if pareto else _solve_strategy(strategy)),
        count=count, max_evaluations=budget, seed=seed,
        tolerance=tolerance, initial=init,
    )
    report.update({"base": base_rec.get("model_id"), "kind": "assembly",
                   "recipe": None, "requested_strategy": strategy,
                   "variables": variables})
    solution = report.get("solution")
    if solution:
        patched = _patch_assembly(spec_obj, solution["params"])
        built = assemblymod.build(patched, quality=quality, cache=cache)
        from .locks import project_lock
        with project_lock(exclusive=True):
            out = _commit_assembly(built, patched,
                                   name=base_rec.get("name") or "assembly",
                                   quality=quality)
        solution.update({
            "model_id": out["model_id"],
            "model": out["model"],
            "facts": out["facts"],
            "assembly": out["assembly"],
            "placements": out["placements"],
            "verdict": None,
        })
        try:
            ver = verify(model_id=out["model_id"], level="basic")
            solution["verdict"] = ver.get("verdict")
            solution["issues"] = ver.get("issues")
        except AttaError as exc:
            solution["verdict"] = "error"
            solution["error"] = exc.to_dict()
        report["solution"] = solution
        report["best"] = solution
        report["model_id"] = out["model_id"]
        report["model"] = out["model"]
        report["facts"] = out["facts"]
        report["assembly"] = out["assembly"]
        report["placements"] = out["placements"]
        report["candidates"] = [solution]
    else:
        report["candidates"] = []
    report["strategy"] = report.get("strategy") or strategy
    report["ms"] = _ms(t0)
    return report


def variant(recipe: str | None = None, base: str | None = None,
            count: int = 6, ranges: dict | None = None,
            strategy: str = "lhs", seed: int = 42,
            objective: dict | str | None = None,
            constraints: dict | None = None,
            quality: str = "draft",
            targets: dict | None = None,
            max_evaluations: int | None = None,
            tolerance: float = 1e-6,
            initial: dict | None = None,
            pareto: bool = False,
            free=None, fixed=None,
            solve: bool = False) -> dict:
    t0 = time.perf_counter()
    base_rec = None
    if base:
        base_rec = _require_models(base)
        if base_rec.get("kind") == "assembly":
            return _assembly_variant(
                base_rec, count, ranges, strategy, seed, objective, constraints,
                quality, targets=targets, max_evaluations=max_evaluations,
                tolerance=tolerance, initial=initial, pareto=pareto,
                free=free, fixed=fixed,
            )
        recipe = recipe or base_rec.get("recipe")
    if not recipe:
        raise AttaError("bad_arguments", "atta_variant needs a recipe or base model_id",
                        path="recipe", fix="pass recipe='product.mug'")
    mode = str(strategy or "lhs").strip().lower()
    if bool(solve or targets or pareto or mode not in {"lhs", "sample", "random"}):
        return _variant_solve(recipe, base_rec, count, ranges, strategy, seed,
                              objective, constraints, quality,
                              targets=targets,
                              max_evaluations=max_evaluations,
                              tolerance=tolerance, initial=initial,
                              pareto=pareto, free=free, fixed=fixed)

    r = recipes.get(recipe)
    preset = (base_rec or {}).get("preset") or "nominal"
    base_display = dict((base_rec or {}).get("params") or {})
    if not base_display:
        base_display = dict(r.resolve({}, preset).display)

    names, lows, highs = _sample_space(r, base_display, ranges)
    if not names:
        raise AttaError("no_numeric_params", "recipe %s has no numeric parameters" % recipe,
                        path="ranges", fix="choose a recipe with float/int params")
    samples = _lhs(int(count), len(names), int(seed))
    candidates = []
    for row in samples:
        patch = {}
        for name, lo, hi, t in zip(names, lows, highs, row):
            value = lo + (hi - lo) * t
            patch[name] = int(round(value)) if r.params[name].type == "int" else float(value)
        try:
            built = model(recipe=recipe, params=patch, preset=preset,
                          quality=quality)
        except AttaError as exc:
            candidates.append({"ok": False, "params": patch,
                               "error": exc.to_dict()})
            continue
        mid = built["model_id"]
        try:
            ver = verify(model_id=mid, level="basic", constraints=constraints)
            verdict = ver.get("verdict")
            metrics = ver.get("metrics") or built.get("metrics")
        except AttaError as exc:
            verdict, metrics = "error", built.get("metrics")
            ver = {"verdict": verdict, "error": exc.to_dict()}
        candidates.append({
            "ok": True,
            "model_id": mid,
            "params": built["model"].get("params") or patch,
            "metrics": metrics,
            "facts": built.get("facts"),
            "verdict": verdict,
            "checks": ver.get("checks") if isinstance(ver, dict) else None,
            "issues": ver.get("issues") if isinstance(ver, dict) else None,
            "score": 1.0 if verdict == "pass" else 0.5 if verdict == "degraded" else 0.0,
        })

    ok_rows = [c for c in candidates if c.get("ok")]
    if objective and ok_rows:
        metric, direction = _objective(objective)
        vals = [float(c["metrics"].get(metric) or 0.0) for c in ok_rows]
        lo, hi = min(vals), max(vals)
        for c, v in zip(ok_rows, vals):
            norm = 1.0 if hi <= lo else (v - lo) / (hi - lo)
            if str(direction).lower() in ("min", "minimize", "small"):
                norm = 1.0 - norm
            c["score"] = round(0.7 * norm + 0.3 * c.get("score", 0.0), 4)
            c["objective"] = {metric: v, "direction": direction}
    ok_rows.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    return {
        "status": "ok",
        "recipe": recipe,
        "strategy": strategy,
        "seed": int(seed),
        "count": len(candidates),
        "candidates": candidates,
        "best": ok_rows[0] if ok_rows else None,
        "ms": _ms(t0),
    }


def _objective(objective) -> tuple[str, str]:
    if isinstance(objective, str):
        return objective, "min"
    if isinstance(objective, dict) and objective:
        key, value = next(iter(objective.items()))
        if isinstance(value, str):
            return str(key), value
        if isinstance(value, (int, float)):
            return str(key), "max" if float(value) > 0 else "min"
    raise AttaError("bad_arguments", "objective must be a metric name or {metric: min|max}",
                    path="objective", fix='use {"volume_mm3": "min"} or "volume_mm3"')


def _sample_space(r, base_display: dict, ranges: dict | None):
    names, lows, highs = [], [], []
    for key, ps in sorted(r.params.items()):
        if ps.type not in ("float", "int"):
            continue
        # Resolution knobs (segments/profile_samples) are not design variables:
        # vary them only when the caller names them in ranges.
        if ps.advanced and not (ranges and key in ranges):
            continue
        base = float(base_display.get(key, ps.default if ps.default is not None else 0.0))
        rng = ranges.get(key) if isinstance(ranges, dict) and key in ranges else None
        if isinstance(rng, dict) and ("min" in rng or "max" in rng):
            lo = float(rng.get("min", base))
            hi = float(rng.get("max", base))
        elif isinstance(rng, (list, tuple)) and len(rng) >= 2:
            lo, hi = float(rng[0]), float(rng[1])
        else:
            span = abs(base) * 0.2 if base else (ps.max or 1.0) * 0.2
            lo, hi = base - span, base + span
        if ps.min is not None:
            lo = max(lo, float(ps.min))
        if ps.max is not None:
            hi = min(hi, float(ps.max))
        if ps.type == "int":
            lo, hi = math.ceil(lo), math.floor(hi)
        if hi < lo:
            lo, hi = hi, lo
        names.append(key)
        lows.append(lo)
        highs.append(hi)
    return names, lows, highs


def _lhs(n: int, d: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(int(seed))
    out = np.empty((max(1, int(n)), max(1, int(d))), dtype=np.float64)
    for j in range(d):
        cuts = (np.arange(n) + rng.random(n)) / max(1, n)
        out[:, j] = cuts[rng.permutation(n)]
    return out


def describe(model_id: str | None = None, probe: Any = None,
             wall: bool = False, curvature: bool = False,
             perceive: bool = False, views=None, res: int = 24,
             sections=None, compare_to: str | None = None,
             trace: bool = False) -> dict:
    t0 = time.perf_counter()
    rec, mesh, finish = _load(model_id)
    f = factsmod.facts(mesh, rec, wall=wall, curvature=curvature)
    probes = None
    if probe is not None:
        probes = _probes(mesh, probe)
    view_list = ([v.strip() for v in views.split(",") if v.strip()]
                 if isinstance(views, str) else list(views or perception.DEFAULT_VIEWS))
    if isinstance(sections, (int, float)):
        sec_list = [float(sections)]
    elif isinstance(sections, str):
        sec_list = [float(s) for s in sections.split(",") if s.strip()]
    elif sections is None:
        sec_list = list(perception.DEFAULT_SECTIONS)
    else:
        sec_list = [float(s) for s in sections]
    pack = (perception.perceive(mesh, rec, views=view_list, res=int(res),
                                sections=sec_list, wall=wall,
                                curvature=curvature)
            if perceive else None)
    trace_pack = perception.trace(rec, mesh) if trace else None
    diff_pack = None
    if compare_to:
        _, other_mesh, _ = _load(compare_to)
        diff_pack = perception.diff(other_mesh, mesh, views=view_list,
                                    res=int(res))
    return {
        "status": "ok",
        "model_id": rec["model_id"],
        "model": _summary(rec),
        "facts": f,
        "probes": probes,
        "perception": pack,
        "trace": trace_pack,
        "diff": diff_pack,
        "metrics": rec.get("metrics"),
        "mesh_source": "draft",
        "evaluated": False,
        "assumptions": rec.get("assumptions") or [],
        "assembly": _assembly_summary(rec),
        "next": ["atta_verify model_id=%s" % rec["model_id"],
                 "atta_preview model_id=%s" % rec["model_id"]],
        "ms": _ms(t0),
    }


def _assembly_summary(rec: dict) -> dict | None:
    if rec.get("kind") != "assembly":
        return None
    asm = rec.get("assembly") or {}
    return {
        "part_count": asm.get("part_count"),
        "parts": [p.get("id") for p in asm.get("parts") or []],
        "mates": [{k: m.get(k) for k in ("id", "type", "a", "b", "ok",
                                         "residual_mm")}
                  for m in asm.get("mates") or []],
        "dof": asm.get("dof"),
        "conflicts": asm.get("conflicts"),
        "collision": asm.get("collision"),
        "clearance": asm.get("clearance"),
        "placements": rec.get("placements") or asm.get("placements"),
    }


def _probes(mesh: Mesh, spec: Any) -> Any:
    if isinstance(spec, str):
        rows = [(spec, {})]
    elif isinstance(spec, dict):
        op = spec.get("op") or spec.get("name")
        rows = [(op, {k: v for k, v in spec.items() if k not in ("op", "name")})]
    elif isinstance(spec, (list, tuple)):
        rows = []
        for item in spec:
            if isinstance(item, str):
                rows.append((item, {}))
            elif isinstance(item, dict):
                op = item.get("op") or item.get("name")
                rows.append((op, {k: v for k, v in item.items() if k not in ("op", "name")}))
            else:
                raise AttaError("bad_arguments", "probe list rows must be strings or objects",
                                path="probe", fix='use ["bbox","raycast"]')
    else:
        raise AttaError("bad_arguments", "probe must be a string, object or array",
                        path="probe", fix='use "bbox" or {"op":"raycast","axis":"z"}')

    out = {}
    for i, (op, args) in enumerate(rows):
        key = str(op or "probe")
        if key in out:
            key = "%s_%d" % (key, i)
        out[key] = _probe_one(mesh, key, args)
    return out


def _probe_one(mesh: Mesh, op: str, args: dict):
    op = str(op).lower()
    if op in ("bbox", "bounding_box"):
        return metrology.bbox_mm(mesh)
    if op in ("topology", "topo"):
        return mesh.topology()
    if op in ("metrology", "measure"):
        return metrology.measure(mesh, wall=True, dihedral=True, curvature=True,
                                 density_kg_m3=args.get("density_kg_m3"))
    if op in ("wall", "thickness"):
        return metrology.wall_thickness(mesh, max_samples=int(args.get("max_samples", 600)),
                                        seed=int(args.get("seed", 0)))
    if op == "dihedral":
        return metrology.dihedral(mesh)
    if op == "curvature":
        return metrology.curvature(mesh)
    if op in ("mass", "mass_properties"):
        return metrology.mass_properties(mesh, float(args.get("density_kg_m3", 7850.0)))
    if op in ("raycast", "ray"):
        return probe.raycast_grid(mesh, axis=args.get("axis", "z"),
                                  res=tuple(args.get("res", (32, 32))),
                                  bounds=args.get("bounds"),
                                  direction=int(args.get("direction", 1)))
    if op in ("silhouette", "sil"):
        return probe.silhouette(mesh, views=tuple(args.get("views", ("front", "top", "left", "iso"))),
                                res=int(args.get("res", 32)))
    if op in ("section", "slice"):
        return probe.section(mesh, plane=args.get("plane", "z"),
                             offset_mm=float(args.get("offset_mm", 0.0)),
                             res=int(args.get("res", 64)))
    if op in ("clearance", "interference"):
        other_id = args.get("other") or args.get("other_model_id") or args.get("model_id")
        if not other_id:
            raise AttaError("bad_arguments", "%s needs other model_id" % op,
                            path="probe.other",
                            fix='pass {"op":"%s","other":"mdl_..."}' % op)
        _, other_mesh, _ = _load(other_id)
        if op == "clearance":
            return probe.clearance(mesh, other_mesh,
                                   max_samples=int(args.get("max_samples", 2000)))
        return probe.interference(mesh, other_mesh)
    if op == "nearest":
        point = args.get("point_mm") or args.get("point")
        if not point:
            raise AttaError("bad_arguments", "nearest needs point_mm",
                            path="probe.point_mm", fix="pass [x,y,z] in mm")
        return probe.nearest(mesh, point)
    if op == "inside":
        point = args.get("point_mm") or args.get("point")
        if not point:
            raise AttaError("bad_arguments", "inside needs point_mm",
                            path="probe.point_mm", fix="pass [x,y,z] in mm")
        return {"inside": bool(probe.inside(mesh, point))}
    raise AttaError(
        "unknown_probe",
        "unknown probe %r" % op,
        path="probe",
        fix="use bbox/topology/metrology/wall/dihedral/curvature/mass/"
            "raycast/silhouette/section/clearance/interference/nearest/inside",
    )


def _evaluated(mesh: Mesh, finish: dict):
    if float((finish.get("bevel") or {}).get("width") or 0.0) <= 0.0:
        return mesh, {"evaluated": False, "reason": "no bevel; raw mesh is the evaluated geometry"}
    try:
        from .blender import available as blender_available, runtime as get_runtime
        if not blender_available():
            return mesh, {"evaluated": False, "reason": "Blender unavailable; verified raw mesh"}
        ev, info = get_runtime(root=str(state.workspace())).evaluate(mesh, finish)
        return ev, {"evaluated": True, **info}
    except Exception as exc:  # noqa: BLE001 - verify should still run, visibly degraded
        return mesh, {"evaluated": False,
                      "reason": "%s: %s" % (type(exc).__name__, exc)}


def _verify_assembly(rec: dict, level: str, constraints: dict | None,
                     reference: str | None) -> dict:
    t0 = time.perf_counter()
    built = assemblymod.build(rec.get("spec") or {},
                              quality=rec.get("quality") or "draft")
    cons = dict(rec.get("constraints") or {})
    if constraints:
        cons.update(constraints)
    receipt = assemblymod.verify(built, constraints=cons, level=level)
    card = _facts(built["mesh"], rec, built.get("metrics"))
    asm = built.get("assembly") or {}
    card.update({
        "model_id": rec.get("model_id"),
        "name": rec.get("name"),
        "quality": rec.get("quality"),
        "material": rec.get("material"),
        "assembly": {
            "part_count": asm.get("part_count"),
            "mates": len(asm.get("mates") or []),
            "conflicts": len(asm.get("conflicts") or []),
            "dof": (asm.get("dof") or {}).get("dof"),
            "collisions": len((asm.get("collision") or {}).get("collisions") or []),
            "min_clearance_mm": (asm.get("clearance") or {}).get("min_mm"),
        },
        "one_line": "%d-part assembly, dof=%s, %d collision(s), %d mate(s)"
        % (int(asm.get("part_count") or 0),
           (asm.get("dof") or {}).get("dof"),
           len((asm.get("collision") or {}).get("collisions") or []),
           len(asm.get("mates") or [])),
    })
    receipt["facts"] = _jsonable(card)
    receipt.update({
        "model_id": rec["model_id"],
        "recipe": None,
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": built["mesh"].hash(),
        "mesh_source": {"evaluated": False, "assembly": True},
        "evaluated": False,
        "reference": reference,
        "artifacts": rec.get("artifacts") or {},
        "ms": _ms(t0),
    })
    path = state.out_dir(rec["model_id"]) / "verify.json"
    path.write_text(json.dumps(_jsonable(receipt), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    artifacts = dict(rec.get("artifacts") or {})
    artifacts["verify"] = str(path)
    state.touch(rec, artifacts=artifacts, last_verify=receipt.get("verdict"))
    return receipt


def verify(model_id: str | None = None, level: str = "standard",
           constraints: dict | None = None, reference: str | None = None) -> dict:
    t0 = time.perf_counter()
    rec0 = _require_models(model_id)
    if rec0.get("kind") == "assembly":
        return _verify_assembly(rec0, level, constraints, reference)
    rec, mesh, finish = _load(model_id)
    ev, source = _evaluated(mesh, finish)
    cons = dict(rec.get("constraints") or {})
    if constraints:
        cons.update(constraints)
    receipt = verifymod.verify(
        ev, constraints=cons,
        expected_genus=rec.get("expected_genus"),
        level=level,
        assumptions=rec.get("assumptions") or [],
    )
    # verify() only sees the mesh; attach the model identity so the fact card
    # in the receipt reads like the model card, not an anonymous mesh.
    card = dict(receipt.get("facts") or {})
    card.update({
        "model_id": rec.get("model_id"),
        "recipe": rec.get("recipe"),
        "name": rec.get("name"),
        "quality": rec.get("quality"),
        "material": rec.get("material"),
    })
    card["one_line"] = factsmod.one_line(card)
    receipt["facts"] = card
    receipt.update({
        "model_id": rec["model_id"],
        "recipe": rec.get("recipe"),
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": ev.hash(),
        "mesh_source": source,
        "evaluated": bool(source.get("evaluated")),
        "artifacts": rec.get("artifacts") or {},
        "ms": _ms(t0),
    })
    path = state.out_dir(rec["model_id"]) / "verify.json"
    path.write_text(json.dumps(_jsonable(receipt), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    artifacts = dict(rec.get("artifacts") or {})
    artifacts["verify"] = str(path)
    state.touch(rec, artifacts=artifacts, last_verify=receipt.get("verdict"))
    return receipt


def _preview_mode_defaults(mode: str) -> dict:
    base = {
        "beauty": {"lighting": "studio3", "background": "transparent",
                   "ground": False, "lens_mm": 50.0, "look": "None",
                   "exposure": 2.0},
        "studio": {"lighting": "studio", "background": "gradient",
                   "ground": True, "lens_mm": 85.0,
                   "look": "AgX - Medium High Contrast", "exposure": 1.5,
                   "aspect": "auto"},
        "catalog": {"lighting": "catalog", "background": "gray",
                    "ground": True, "lens_mm": 85.0,
                    "look": "AgX - Medium High Contrast", "exposure": 1.0,
                    "aspect": "auto"},
        "dark": {"lighting": "dark", "background": "dark",
                 "ground": False, "lens_mm": 85.0, "look": "AgX - Punchy",
                 "exposure": 0.0, "aspect": "auto"},
        "clay": {"lighting": "clay", "background": "gray",
                 "ground": True, "lens_mm": 85.0,
                 "look": "AgX - Medium High Contrast", "exposure": 1.0,
                 "aspect": "auto"},
        "diagnostic": {"lighting": "none", "background": "dark",
                       "ground": False, "lens_mm": 50.0, "look": "None",
                       "exposure": 0.0,
                       "passes": ["normal", "depth", "id", "curvature"]},
    }
    out = dict(base.get(mode, base["beauty"]))
    out["passes"] = list(out.get("passes") or ["beauty"])
    return out


def _preview_parts(rec: dict, mesh, finish, quality: str, materials_override):
    """Return (parts, assembly_build) for the named-part preview scene."""
    if rec.get("kind") == "assembly":
        spec_obj = rec.get("spec") or {}
        built = assemblymod.build(spec_obj, quality=quality,
                                  root=str(state.workspace()))
        parts = []
        for pid, wm in built.get("part_meshes", {}).items():
            part = (built.get("parts") or {}).get(pid)
            base = (part.material if part is not None else None) \
                or rec.get("material") or "ceramic"
            mat = _preview_material(pid, base, materials_override)
            f = dict(finish or {})
            f["material"] = mat
            parts.append({"id": pid, "mesh": wm, "finish": f, "material": mat})
        return parts, built
    pid = str(rec.get("name") or "model")
    base = (finish or {}).get("material") or rec.get("material") or "ceramic"
    mat = _preview_material(pid, base, materials_override)
    f = dict(finish or {})
    f["material"] = mat
    return [{"id": pid, "mesh": mesh, "finish": f, "material": mat}], None


def _preview_material(pid: str, base: str, override) -> str:
    if isinstance(override, str) and override:
        return override
    if isinstance(override, Mapping):
        if pid in override:
            return str(override[pid])
        if "*" in override:
            return str(override["*"])
    return str(base or "ceramic")


def _preview_analysis(images, *, top_n: int = 3) -> dict:
    warnings = []
    rows = []
    for im in images:
        analysis = im.get("analysis") or {}
        rows.append({
            "view": im.get("view"),
            "pass": im.get("pass"),
            "warnings": analysis.get("warnings") or [],
            "mean_zone": (analysis.get("zone_system") or {}).get("mean_zone"),
            "colorfulness": (analysis.get("color") or {}).get("colorfulness"),
        })
        for warning in analysis.get("warnings") or []:
            warnings.append("%s/%s: %s" % (im.get("view"), im.get("pass"), warning))
    return {
        "method": "atta.vision/v1",
        "images": rows,
        "warnings": warnings[:top_n * 4],
        "note": ("visual flags only; verify is the geometry gate. Lighting, "
                 "material and tone mapping are not traceable measurements."),
    }


def preview(model_id: str | None = None, views=None, res: int | None = None,
            samples: int | None = None, quality: str | None = None,
            mode: str = "beauty", outdir: str | None = None,
            *, lighting: str | None = None, background: str | None = None,
            ground: bool | None = None, lens_mm: float | None = None,
            exposure: float | None = None, look: str | None = None,
            materials=None, passes=None, ortho: bool | None = None,
            distance_factor: float | None = None,
            reference: str | None = None,
            deviation_range_mm: float | None = None,
            focus: str | None = None,
            aspect=None, focus_padding: float | None = None) -> dict:
    t0 = time.perf_counter()
    rec, mesh, finish = _load(model_id)
    mode = str(mode or "beauty").lower()
    if mode not in ("beauty", "studio", "catalog", "dark", "clay", "diagnostic"):
        raise AttaError(
            "unsupported_mode",
            "preview mode %r is not implemented" % mode,
            path="mode",
            fix="use beauty | studio | catalog | dark | clay | diagnostic",
        )
    q = _q(quality or rec.get("quality") or "draft")
    profile = QUALITY[q]
    defaults = _preview_mode_defaults(mode)
    default_views = {
        "beauty": profile["views"],
        "studio": ["hero"],
        "catalog": ["iso", "front"],
        "dark": ["hero"],
        "clay": ["iso"],
        "diagnostic": ["iso"],
    }[mode]
    view_list = list(views or default_views)
    if mode == "beauty":
        res = int(res or profile["res"])
        samples = int(samples or profile["samples"])
    else:
        res = int(res or {"studio": 1200, "catalog": 1400, "dark": 1400,
                          "clay": 1024, "diagnostic": 900}[mode])
        samples = int(samples or {"studio": 48, "catalog": 64, "dark": 64,
                                  "clay": 32, "diagnostic": 8}[mode])
    settings = dict(defaults)
    for key, value in (("lighting", lighting), ("background", background),
                       ("ground", ground), ("lens_mm", lens_mm),
                       ("exposure", exposure), ("look", look),
                       ("ortho", ortho), ("distance_factor", distance_factor)):
        if value is not None:
            settings[key] = value
    if deviation_range_mm is not None:
        settings["deviation_range_mm"] = float(deviation_range_mm)
    if focus:
        settings["focus"] = str(focus)
    if aspect is not None:
        settings["aspect"] = aspect
    if focus_padding is not None:
        settings["focus_padding"] = float(focus_padding)
    if passes is not None:
        settings["passes"] = [str(x) for x in passes]
    if mode == "diagnostic":
        settings["lighting"] = "none"
        settings["background"] = "dark"
        settings["look"] = "None"

    parts, _built = _preview_parts(rec, mesh, finish, q, materials)
    reference_mesh = None
    if reference:
        _ref_rec, reference_mesh, _ref_finish = _load(reference)
    if mode == "clay":
        for part in parts:
            part["material"] = "clay"
            part["finish"]["material"] = "clay"

    try:
        from .blender import available as blender_available, runtime as get_runtime
        if not blender_available():
            raise AttaError("blender_unavailable", "Blender is not installed or not on PATH",
                            path="blender",
                            fix="install Blender or set ATTA_BLENDER to its executable")
        work = Path(outdir).expanduser().resolve() if outdir else state.out_dir(
            rec["model_id"], sub="preview")
        rendered = get_runtime(root=str(state.workspace())).render_parts(
            parts, settings=settings, views=view_list, res=res,
            samples=samples, workdir=work, reference=reference_mesh,
        )
    except AttaError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AttaError("preview_failed", "Blender render failed: %s" % exc,
                        path="preview",
                        fix="run atta_doctor; check _atta/blender/blender.log; retry at lower res") from exc

    from . import vision as visionmod
    images = []
    for im in rendered.get("images") or []:
        row = {k: im.get(k) for k in (
            "view", "pass", "path", "abs_path", "width", "height", "coverage",
            "content_bbox_px", "worst_ndc", "empty_frame", "render_ms",
            "bytes", "sha256", "res", "resolution", "aspect", "samples",
            "engine", "exact", "cached", "range_m", "range_mm", "note",
            "skipped",
        )}
        row = _jsonable(row)
        if row.get("pass") == "beauty":
            try:
                row["analysis"] = visionmod.analyze_image(row.get("abs_path"))
            except Exception as exc:  # noqa: BLE001 - analysis is not the gate
                row["analysis"] = {"ok": False, "code": "analysis_failed",
                                   "error": "%s: %s" % (type(exc).__name__, exc)}
        else:
            row["analysis"] = {"ok": False, "code": "diagnostic_pass",
                               "note": "colour/light analysis is for beauty renders"}
        images.append(row)
    beauty = [i for i in images if i.get("pass") == "beauty"]
    subject = [float((i.get("analysis") or {}).get("subject_fraction") or 0.0)
               for i in beauty if (i.get("analysis") or {}).get("ok")]
    gate = {
        "pass": (bool(images)
                 and all(not i.get("empty_frame") for i in images)
                 and (not subject or min(subject) > 0.005)),
        "empty_frames": ["%s/%s" % (i.get("view"), i.get("pass"))
                         for i in images if i.get("empty_frame")],
        "min_coverage": min([float(i.get("coverage") or 0.0) for i in images], default=0.0),
        "min_subject_fraction": min(subject) if subject else None,
    }
    analysis = _preview_analysis(images)
    f = factsmod.facts(mesh, rec)
    receipt = {
        "schema": "atta.preview/v3",
        "created_at": _now(),
        "model_id": rec["model_id"],
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": rec.get("mesh_hash"),
        "quality": q,
        "mode": mode,
        "settings": settings,
        "res": res,
        "samples": samples,
        "views": view_list,
        "parts": [{"id": p["id"], "material": p.get("material")}
                  for p in parts],
        "images": images,
        "analysis": analysis,
        "degraded": rendered.get("degraded") or [],
        "facts": f,
        "gate": gate,
        "judgment": True,
        "note": ("preview is a judgement image, not acceptance; the analysis "
                 "flags lighting/colour, it does not verify geometry"),
        "exact": False,
        "method": "atta.preview/v4",
        "ms": _ms(t0),
    }
    path = Path(work) / "preview.json"
    path.write_text(json.dumps(_jsonable(receipt), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    artifacts = dict(rec.get("artifacts") or {})
    artifacts["preview"] = str(path)
    state.touch(rec, artifacts=artifacts)
    return {
        "status": "ok",
        "model_id": rec["model_id"],
        "quality": q,
        "mode": mode,
        "settings": settings,
        "images": images,
        "outdir": str(work),
        "receipt_path": str(path),
        "facts": f,
        "analysis": analysis,
        "degraded": rendered.get("degraded") or [],
        "gate": gate,
        "metrics": rec.get("metrics"),
        "ms": _ms(t0),
    }


def _deliver_assembly(rec: dict, formats, outdir, allow_degraded: bool,
                      name: str | None) -> dict:
    t0 = time.perf_counter()
    ver = _verify_assembly(rec, "standard", None, None)
    rec = state.get(rec["model_id"]) or rec
    if ver.get("verdict") == "fail":
        return {"status": "blocked", "reason": "verify fail", "verify": ver,
                "fix": "fix the first issue in verify.issues, then verify again",
                "ms": _ms(t0)}
    if ver.get("verdict") == "degraded" and not allow_degraded:
        return {
            "status": "blocked",
            "reason": "verify degraded; pass only",
            "verify": ver,
            "fix": "fix verify.issues, or set allow_degraded=true with a reason",
            "ms": _ms(t0),
        }
    wanted = formats or ["glb", "obj"]
    if isinstance(wanted, str):
        wanted = [x.strip() for x in wanted.split(",") if x.strip()]
    wanted = [str(x).lower() for x in wanted]
    stem = (name or rec.get("name") or rec["model_id"]).replace("/", "_")
    out = (Path(outdir).expanduser().resolve() if outdir
           else state.out_dir(rec["model_id"], sub="deliver"))
    built = assemblymod.build(rec.get("spec") or {},
                              quality=rec.get("quality") or "draft")
    exported = assemblymod.export_scene(built, formats=wanted, outdir=out,
                                        stem=stem)
    files = [_jsonable(f) for f in exported.get("files") or []]
    errors = [_jsonable(f) for f in exported.get("errors") or []]
    manifest = {
        "schema": "atta.deliver/v3",
        "created_at": _now(),
        "model_id": rec["model_id"],
        "name": stem,
        "kind": "assembly",
        "spec": rec.get("spec"),
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": rec.get("mesh_hash"),
        "units": "mm",
        "material": rec.get("material"),
        "parts": rec.get("parts"),
        "placements": built.get("placements"),
        "assembly": _jsonable(built.get("assembly")),
        "verify": {
            "verdict": ver.get("verdict"),
            "score": ver.get("score"),
            "level": ver.get("level"),
            "checks": len(ver.get("checks") or []),
            "issues": ver.get("issues") or [],
            "receipt": (rec.get("artifacts") or {}).get("verify"),
        },
        "files": files,
        "errors": errors,
        "exact": False,
        "note": "per-part tessellated derivatives; the spec rebuilds the assembly",
        "method": "atta.deliver/v3",
    }
    mpath = Path(out) / ("%s_manifest.json" % stem)
    mpath.write_text(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    artifacts = dict(rec.get("artifacts") or {})
    artifacts["deliver"] = str(mpath)
    state.touch(rec, artifacts=artifacts)
    status = "ok" if files and not errors else ("partial" if files else "failed")
    return {
        "status": status,
        "model_id": rec["model_id"],
        "files": files,
        "errors": errors,
        "manifest": manifest,
        "manifest_path": str(mpath),
        "verify": ver,
        "assembly": built.get("assembly"),
        "placements": built.get("placements"),
        "ms": _ms(t0),
    }


def deliver(model_id: str | None = None, formats=None, outdir: str | None = None,
            allow_degraded: bool = False, name: str | None = None) -> dict:
    t0 = time.perf_counter()
    rec0 = _require_models(model_id)
    if rec0.get("kind") == "assembly":
        return _deliver_assembly(rec0, formats, outdir, allow_degraded, name)
    rec, mesh, finish = _load(model_id)
    ver = verify(model_id=rec["model_id"], level="standard")
    rec = state.get(rec["model_id"]) or rec
    if ver.get("verdict") == "fail":
        return {"status": "blocked", "reason": "verify fail", "verify": ver,
                "fix": "fix the first issue in verify.issues, then rebuild and verify again",
                "ms": _ms(t0)}
    if ver.get("verdict") == "degraded" and not allow_degraded:
        return {
            "status": "blocked",
            "reason": "verify degraded; pass only",
            "verify": ver,
            "fix": "fix verify.issues, or set allow_degraded=true with a written reason",
            "ms": _ms(t0),
        }
    wanted = formats or ["glb", "obj"]
    if isinstance(wanted, str):
        wanted = [x.strip() for x in wanted.split(",") if x.strip()]
    wanted = [str(x).lower() for x in wanted]
    step_wanted = "step" in wanted
    mesh_formats = [f for f in wanted if f != "step"]
    stem = (name or rec.get("name") or rec["model_id"]).replace("/", "_")
    out = Path(outdir).expanduser().resolve() if outdir else state.out_dir(rec["model_id"], sub="deliver")
    ex = {"files": []}
    if mesh_formats:
        try:
            from .blender import available as blender_available, runtime as get_runtime
            if not blender_available():
                # Blender is optional (doctor rates it "warn").  A missing
                # backend must not explode: report which formats still work
                # and let STEP through, so the user gets a usable file.
                ex = {"files": [_jsonable({
                    "format": fmt, "ok": False, "backend": "blender",
                    "error": {
                        "code": "blender_unavailable",
                        "message": "Blender is not installed or not on PATH",
                        "fix": ("install Blender, set ATTA_BLENDER to its "
                                "executable, or request formats that do not "
                                "need it: step is available without Blender"),
                    },
                }) for fmt in mesh_formats]}
            else:
                ex = get_runtime(root=str(state.workspace())).export(
                    mesh, finish, formats=mesh_formats, stem=stem, workdir=out,
                )
        except AttaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AttaError("deliver_failed", "export failed: %s" % exc,
                            path="deliver",
                            fix="run atta_doctor; inspect _atta/blender/blender.log") from exc

    files = [_jsonable(f) for f in (ex.get("files") or [])]
    brep_report = None
    if step_wanted:
        from . import brep as brepmod
        step_path = Path(out) / ("%s.step" % stem)
        try:
            brep_report = brepmod.export_step(rec.get("spec") or {}, str(step_path))
            files.append(_jsonable({
                "format": "step", "ok": True, "path": str(step_path),
                "bytes": brep_report.get("bytes"), "backend": "brep",
                "sha256": _sha256_file(step_path),
            }))
        except AttaError as exc:
            files.append(_jsonable({
                "format": "step", "ok": False, "path": str(step_path),
                "error": exc.to_dict(),
            }))
    errors = [f for f in files if f.get("ok") is False]
    manifest = {
        "schema": "atta.deliver/v3",
        "created_at": _now(),
        "model_id": rec["model_id"],
        "name": stem,
        "spec": rec.get("spec"),
        "spec_hash": rec.get("spec_hash"),
        "mesh_hash": rec.get("mesh_hash"),
        "units": "mm",
        "material": rec.get("material"),
        "finish": rec.get("finish"),
        "params": rec.get("params"),
        "assumptions": rec.get("assumptions") or [],
        "verify": {
            "verdict": ver.get("verdict"),
            "score": ver.get("score"),
            "level": ver.get("level"),
            "checks": len(ver.get("checks") or []),
            "issues": ver.get("issues") or [],
            "receipt": (rec.get("artifacts") or {}).get("verify"),
        },
        "files": files,
        "errors": errors,
        "exact": False,
        "backend": "brep+mesh" if step_wanted else "mesh",
        "brep": ({k: v for k, v in brep_report.items() if k != "shape"}
                 if brep_report else None),
        "note": ("GLB/OBJ/STL are tessellated derivatives; STEP is an analytic "
                 "B-rep for the supported graph subset, while the verdict still "
                 "comes from the mesh verify receipt; the spec rebuilds the scene"),
        "method": "atta.deliver/v3",
    }
    mpath = Path(out) / ("%s_manifest.json" % stem)
    mpath.write_text(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    artifacts = dict(rec.get("artifacts") or {})
    artifacts["deliver"] = str(mpath)
    state.touch(rec, artifacts=artifacts)
    status = "ok" if files and not errors else ("partial" if files else "failed")
    return {
        "status": status,
        "model_id": rec["model_id"],
        "files": files,
        "errors": errors,
        "manifest": manifest,
        "manifest_path": str(mpath),
        "verify": ver,
        "backend": manifest["backend"],
        "brep": manifest["brep"],
        "ms": _ms(t0),
    }


# ---------------------------------------------------------------- batch

_TOOL_FUNCS = {
    "atta_doctor": "doctor", "atta_catalog": "catalog", "atta_spec": "spec",
    "atta_model": "model", "atta_variant": "variant", "atta_describe": "describe",
    "atta_verify": "verify", "atta_preview": "preview", "atta_deliver": "deliver",
}


def run(steps, stop_on_error: bool = True) -> dict:
    t0 = time.perf_counter()
    if not isinstance(steps, list) or not steps:
        raise AttaError("bad_arguments", "steps must be a non-empty array",
                        path="steps", fix='pass [{"tool":"atta_model","args":{...}}]')
    results: dict[int, Any] = {}
    rows = []
    failed = False
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise AttaError("bad_arguments", "steps[%d] must be an object" % index,
                            path="steps[%d]" % index,
                            fix='use {"tool":"atta_model","args":{...}}')
        tool = str(step.get("tool") or step.get("op") or "")
        if tool.startswith("atta_"):
            func_name = _TOOL_FUNCS.get(tool)
        else:
            func_name = tool if tool in _TOOL_FUNCS.values() else None
        if not func_name:
            rows.append({"index": index, "tool": tool, "ok": False,
                         "error": {"code": "unknown_tool", "message": tool,
                                   "fix": "use one of: " + ", ".join(sorted(_TOOL_FUNCS))}})
            failed = True
            if stop_on_error:
                break
            continue
        prev = results.get(index - 1) if index > 0 else None
        try:
            args = _resolve_refs(step.get("args") or {}, results, prev)
            fn = globals()[func_name]
            data = fn(**args)
            results[index] = data
            rows.append({"index": index, "tool": tool, "ok": True, "data": data})
            if isinstance(data, dict) and data.get("status") in ("blocked", "failed"):
                failed = True
                if stop_on_error:
                    break
        except AttaError as exc:
            failed = True
            rows.append({"index": index, "tool": tool, "ok": False,
                         "error": exc.to_dict()})
            if stop_on_error:
                break
        except Exception as exc:  # noqa: BLE001 - a batch is a value
            failed = True
            rows.append({"index": index, "tool": tool, "ok": False,
                         "error": {"code": type(exc).__name__, "message": str(exc),
                                   "fix": "inspect the step args; run atta_doctor"}})
            if stop_on_error:
                break
    return {
        "status": "failed" if failed else "ok",
        "steps": rows,
        "results": results,
        "ms": _ms(t0),
    }


def _resolve_refs(value, results: dict, prev):
    if isinstance(value, dict):
        if "$prev" in value:
            return _get_path(prev, value.get("$prev"))
        if "$step" in value:
            idx = int(value.get("$step"))
            return _get_path(results.get(idx), value.get("path"))
        if "$ref" in value:
            key = str(value.get("$ref"))
            for idx in sorted(results, reverse=True):
                found = _get_path(results[idx], key)
                if found is not None:
                    return found
            return None
        return {k: _resolve_refs(v, results, prev) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, results, prev) for v in value]
    return value


def _get_path(data, path):
    if not path:
        return data
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and str(part).isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


def _version() -> str:
    from . import __version__
    return __version__


__all__ = [
    "QUALITY", "doctor", "catalog", "spec", "model", "variant", "describe",
    "verify", "preview", "deliver", "run",
]
