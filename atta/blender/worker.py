# -*- coding: utf-8 -*-
"""The bpy half of the Blender backend — runs *inside* Blender.

Started by :mod:`atta.blender.runtime` as::

    blender -b --factory-startup --python atta/blender/worker.py -- <args>

Protocol is JSON lines on stdin/stdout.  Blender is chatty (render progress,
addon notices), so every protocol message is prefixed:

    ATTA_READY                     # once, after startup

    ATTA_JSON:{"id":1,"ok":true}   # one line per response

Anything else on stdout is plain Blender noise; the host logs and skips it.

Requests carry a host-written job directory::

    mesh.npz      verts (n,3 float, metres), faces (m,3 int triangles)
    finish.json   normalised finish (see atta.blender.finish)
    material.json resolved BSDF parameters (see atta.materials)

Ops: ``ping``, ``render``, ``eval``, ``export``, ``shutdown``.

This module deliberately imports nothing from the ``atta`` package: Blender's
bundled Python does not have the repo on ``sys.path`` and a missing import at
startup would be fatal.  Data crosses the boundary as files and JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

#: Camera direction from the model centre; +Z is up.
VIEWS = {
    "iso": (1.0, -1.0, 0.75),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "iso_l": (-1.0, -1.0, 0.75),
    "iso_r": (1.0, 1.0, 0.75),
    "hero": (1.15, -1.55, 0.85),
    "hero_l": (-1.35, -1.35, 0.70),
    "hero_r": (1.35, 1.35, 0.70),
}

#: Fallback only — the host always ships material.json.  Kept tiny on purpose:
#: the preset vocabulary lives in atta/materials.py, never here.
FALLBACK_MATERIAL = {
    "name": "ceramic", "base_color": (0.62, 0.62, 0.62), "metallic": 0.0,
    "roughness": 0.42, "ior": 1.5, "alpha": 1.0, "transmission": 0.0,
    "coat_weight": 0.0, "coat_roughness": 0.08, "sheen_weight": 0.0,
    "sheen_roughness": 0.3, "anisotropic": 0.0, "subsurface_weight": 0.0,
    "emission_color": (0.0, 0.0, 0.0), "emission_strength": 0.0,
}


# --------------------------------------------------------------------- utils

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(job: Path):
    data = np.load(job / "mesh.npz")
    verts = np.asarray(data["verts"], dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(data["faces"], dtype=np.int64).reshape(-1, 3)
    finish = _read_json(job / "finish.json", {})
    material = _read_json(job / "material.json", dict(FALLBACK_MATERIAL))
    return verts, faces, finish, material


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default) if isinstance(default, dict) else default


def _bounds(verts: np.ndarray):
    if len(verts) == 0:
        zero = np.zeros(3)
        return zero, zero, zero, zero, 0.0
    lo = verts.min(axis=0)
    hi = verts.max(axis=0)
    size = hi - lo
    return lo, hi, size, (lo + hi) * 0.5, float(np.linalg.norm(size))


# -------------------------------------------------------------------- scene

def _set_engine(scene) -> tuple[str, str | None]:
    """Pick EEVEE under whichever identifier this Blender uses."""
    try:
        valid = {item.identifier
                 for item in scene.render.bl_rna.properties["engine"].enum_items}
    except Exception:  # noqa: BLE001
        valid = set()
    note = None
    for candidate in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        if candidate in valid or not valid:
            try:
                scene.render.engine = candidate
                return scene.render.engine, note
            except Exception:  # noqa: BLE001
                continue
    try:
        scene.render.engine = "BLENDER_WORKBENCH"
        note = "EEVEE unavailable; fell back to BLENDER_WORKBENCH"
        return "BLENDER_WORKBENCH", note
    except Exception:  # noqa: BLE001
        note = "no render engine could be set; using '%s'" % scene.render.engine
        return scene.render.engine, note


def _reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    engine, note = _set_engine(scene)
    degraded = [note] if note else []
    try:
        scene.view_settings.view_transform = "AgX"
    except Exception:  # noqa: BLE001
        try:
            scene.view_settings.view_transform = "Filmic"
        except Exception:  # noqa: BLE001
            degraded.append("AgX view transform unavailable")
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = True
    scene.render.resolution_percentage = 100
    scene.render.use_file_extension = True
    return scene, engine, degraded


def _make_material(spec: dict):
    name = str(spec.get("name") or "material")
    mat = bpy.data.materials.new("M_%s" % name)
    try:
        mat.use_nodes = True
    except Exception:  # noqa: BLE001
        pass
    tree = getattr(mat, "node_tree", None)
    bsdf = None
    if tree is not None:
        for node in tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break
    if bsdf is None:
        return mat, []

    missing = []

    def put(key: str, value) -> None:
        sock = bsdf.inputs.get(key)
        if sock is None:
            missing.append(key)
            return
        try:
            if isinstance(value, (tuple, list)):
                sock.default_value = (*[float(v) for v in value], 1.0)
            else:
                sock.default_value = float(value)
        except Exception:  # noqa: BLE001
            missing.append(key)

    color = spec.get("base_color", (0.62, 0.62, 0.62))
    put("Base Color", color)
    put("Metallic", spec.get("metallic", 0.0))
    put("Roughness", spec.get("roughness", 0.42))
    put("IOR", spec.get("ior", 1.5))
    put("Alpha", spec.get("alpha", 1.0))
    put("Transmission Weight", spec.get("transmission", 0.0))
    put("Coat Weight", spec.get("coat_weight", 0.0))
    put("Coat Roughness", spec.get("coat_roughness", 0.08))
    put("Sheen Weight", spec.get("sheen_weight", 0.0))
    put("Sheen Roughness", spec.get("sheen_roughness", 0.3))
    put("Anisotropic", spec.get("anisotropic", 0.0))
    put("Subsurface Weight", spec.get("subsurface_weight", 0.0))
    put("Emission Color", spec.get("emission_color", (0.0, 0.0, 0.0)))
    put("Emission Strength", spec.get("emission_strength", 0.0))

    try:
        mat.diffuse_color = (*[float(v) for v in color], float(spec.get("alpha", 1.0)))
    except Exception:  # noqa: BLE001
        pass
    if float(spec.get("alpha", 1.0)) < 0.999:
        for attr, value in (("surface_render_method", "BLENDED"),
                            ("blend_method", "BLEND")):
            if hasattr(mat, attr):
                try:
                    setattr(mat, attr, value)
                except Exception:  # noqa: BLE001
                    pass
    return mat, missing


def _shade_smooth(obj, angle_deg: float) -> str:
    """Smooth-by-angle, with graceful version fallbacks. Returns method used."""
    mesh = obj.data
    for poly in mesh.polygons:
        poly.use_smooth = True
    for op in ("shade_smooth_by_angle", "shade_auto_smooth"):
        func = getattr(bpy.ops.object, op, None)
        if func is None:
            continue
        try:
            func(angle=math.radians(float(angle_deg)))
            return op
        except Exception:  # noqa: BLE001
            continue
    try:
        mesh.set_sharp_from_angle(angle=math.radians(float(angle_deg)))
        return "set_sharp_from_angle"
    except Exception:  # noqa: BLE001
        pass
    try:
        mesh.shade_smooth()
        return "mesh.shade_smooth"
    except Exception:  # noqa: BLE001
        return "polygon_use_smooth"


def _shade_flat(obj) -> None:
    for poly in obj.data.polygons:
        poly.use_smooth = False
    try:
        bpy.ops.object.shade_flat()
    except Exception:  # noqa: BLE001
        pass


def _build(verts, faces, finish: dict, material_spec: dict):
    """Create the object and apply bevel + smoothing. Returns (obj, info)."""
    scene = bpy.context.scene
    mesh = bpy.data.meshes.new("ATTAMESH")
    raw_verts, raw_faces = len(verts), len(faces)
    mesh.from_pydata([tuple(float(x) for x in v) for v in verts], [],
                     [tuple(int(i) for i in f) for f in faces])
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new("ATTAMODEL", mesh)
    scene.collection.objects.link(obj)

    view_layer = bpy.context.view_layer
    for other in list(bpy.context.selected_objects):
        try:
            other.select_set(False)
        except Exception:  # noqa: BLE001
            pass
    view_layer.objects.active = obj
    obj.select_set(True)

    info = {"applied": [], "degraded": [], "smooth_method": None,
            "bevel": None, "material": None, "missing_material_inputs": []}
    if len(mesh.vertices) != raw_verts or len(mesh.polygons) != raw_faces:
        info["degraded"].append(
            "mesh.validate dropped %d verts / %d faces"
            % (raw_verts - len(mesh.vertices), raw_faces - len(mesh.polygons)))

    bevel = finish.get("bevel") or {}
    width = float(bevel.get("width") or 0.0)
    segments = int(bevel.get("segments") or 0)
    if width > 0.0 and segments > 0:
        modifier = obj.modifiers.new("bevel", "BEVEL")
        modifier.width = width
        modifier.segments = segments
        modifier.limit_method = "ANGLE"
        modifier.angle_limit = math.radians(float(bevel.get("angle_deg", 40.0)))
        modifier.use_clamp_overlap = True
        try:
            bpy.ops.object.modifier_apply(modifier=modifier.name)
            info["applied"].append("bevel")
            info["bevel"] = {"width": width, "segments": segments,
                             "angle_deg": float(bevel.get("angle_deg", 40.0))}
        except Exception as exc:  # noqa: BLE001
            info["degraded"].append("bevel modifier left live: %s" % exc)
            info["bevel"] = {"width": width, "segments": segments,
                             "angle_deg": float(bevel.get("angle_deg", 40.0)),
                             "live_modifier": True}

    material, missing = _make_material(material_spec or dict(FALLBACK_MATERIAL))
    mesh.materials.append(material)
    info["material"] = material.name
    info["missing_material_inputs"] = missing

    if bool(finish.get("smooth", True)):
        angle = float(finish.get("smooth_angle_deg", 35.0))
        if angle <= 0.0:
            _shade_flat(obj)
            info["smooth_method"] = "flat"
        else:
            info["smooth_method"] = _shade_smooth(obj, angle)
    else:
        _shade_flat(obj)
        info["smooth_method"] = "flat"

    view_layer.update()
    return obj, info


def _object_verts(obj) -> np.ndarray:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3).astype(np.float64)
    finally:
        evaluated.to_mesh_clear()


# ---------------------------------------------------------- diagnostic maps

def _mesh_arrays(obj):
    """Final mesh vertices, triangles and vertex normals (modifiers applied)."""
    mesh = obj.data
    n = len(mesh.vertices)
    verts = np.empty(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3).astype(np.float64)
    mesh.calc_loop_triangles()
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(-1, 3).astype(np.int64)
    normals = np.empty(n * 3, dtype=np.float32)
    try:
        mesh.vertex_normals.foreach_get("vector", normals)
        normals = normals.reshape(-1, 3).astype(np.float64)
    except Exception:  # noqa: BLE001 - older Blender
        normals = np.zeros_like(verts)
    return verts, tris, normals


def _vertex_curvature(verts, tris, diag):
    """Uniform-Laplacian mean curvature magnitude, scaled to dimensionless."""
    n = len(verts)
    if n == 0 or len(tris) == 0:
        return np.zeros(n)
    i, j, k = tris[:, 0], tris[:, 1], tris[:, 2]
    edges = np.concatenate([
        np.stack([i, j], axis=1), np.stack([j, k], axis=1),
        np.stack([k, i], axis=1),
    ], axis=0)
    d = verts[edges[:, 1]] - verts[edges[:, 0]]
    acc = np.zeros_like(verts)
    cnt = np.zeros(n)
    np.add.at(acc, edges[:, 0], d)
    np.add.at(acc, edges[:, 1], -d)
    np.add.at(cnt, edges[:, 0], 1.0)
    np.add.at(cnt, edges[:, 1], 1.0)
    lap = acc / np.maximum(cnt, 1.0)[:, None]
    area = np.linalg.norm(np.cross(verts[j] - verts[i], verts[k] - verts[i]),
                          axis=1)
    va = np.zeros(n)
    np.add.at(va, i, area / 3.0)
    np.add.at(va, j, area / 3.0)
    np.add.at(va, k, area / 3.0)
    h = np.linalg.norm(lap, axis=1) / np.maximum(2.0 * va, 1e-18)
    h = np.nan_to_num(h, nan=0.0, posinf=0.0)
    return np.log1p(h * max(diag, 1e-9))


def _normalize01(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    lo, hi = np.percentile(v, 2), np.percentile(v, 98)
    if hi - lo < 1e-12:
        return np.zeros_like(v)
    return np.clip((v - lo) / (hi - lo), 0.0, 1.0)


def _ramp(values, stops):
    """Piecewise-linear colormap; stops = [(t, (r,g,b)), ...]."""
    v = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
    out = np.zeros((v.size, 3))
    for idx in range(len(stops) - 1):
        t0, c0 = stops[idx]
        t1, c1 = stops[idx + 1]
        m = (v >= t0) & (v <= t1)
        if not m.any():
            continue
        f = (v[m] - t0) / max(t1 - t0, 1e-12)
        out[m] = (np.asarray(c0)[None, :] * (1 - f)[:, None]
                  + np.asarray(c1)[None, :] * f[:, None])
    out[v <= stops[0][0]] = stops[0][1]
    out[v >= stops[-1][0]] = stops[-1][1]
    return out


def _id_palette(count):
    import colorsys
    out = []
    for i in range(max(1, count)):
        h = (i * 0.61803398875) % 1.0
        out.append(colorsys.hsv_to_rgb(h, 0.62, 0.92))
    return out


def _display_to_linear(c):
    c = np.clip(np.asarray(c, dtype=np.float64), 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92,
                    ((c + 0.055) / 1.055) ** 2.4)


def _set_vertex_colors(obj, colors):
    mesh = obj.data
    n = len(mesh.vertices)
    if n == 0:
        return
    cols = np.asarray(colors, dtype=np.float64)
    if cols.ndim != 2 or cols.shape[0] != n:
        return
    # Color attributes are scene-linear; the map values are display-referred.
    cols = _display_to_linear(cols)
    rgba = np.concatenate([cols, np.ones((n, 1))], axis=1).astype(np.float32)
    attr = mesh.color_attributes.get("atta_diag")
    if attr is None:
        attr = mesh.color_attributes.new(name="atta_diag", type="FLOAT_COLOR",
                                         domain="POINT")
    attr.data.foreach_set("color", rgba.ravel())
    try:
        mesh.color_attributes.active_color = attr
    except Exception:  # noqa: BLE001
        pass


def _workbench(scene, samples=8):
    scene.render.engine = "BLENDER_WORKBENCH"
    try:
        aa = min((5, 8, 11, 16, 32), key=lambda v: abs(v - int(samples)))
        scene.display.render_aa = str(aa)
    except Exception:  # noqa: BLE001
        pass
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
    except Exception:  # noqa: BLE001
        pass
    sh = scene.display.shading
    sh.light = "FLAT"
    sh.color_type = "VERTEX"
    sh.show_object_outline = False
    sh.show_cavity = False
    sh.show_shadows = False
    sh.show_specular_highlight = False
    sh.show_xray = False
    sh.background_type = "WORLD"


def _reference_grid(reference):
    """Voxel-hash nearest-surface samples for a deviation map (numpy only)."""
    verts, tris, _ = reference
    if len(verts) == 0:
        return None
    if len(tris):
        centroids = verts[tris].mean(axis=1)
        samples = np.concatenate([verts, centroids], axis=0)
    else:
        samples = verts
    lo, hi = samples.min(axis=0), samples.max(axis=0)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    cell = max(diag / 64.0, 1e-6)
    keys = np.floor((samples - lo) / cell).astype(np.int64)
    grid = {}
    for key, point in zip(map(tuple, keys), samples):
        grid.setdefault(key, []).append(point)
    return {"grid": grid, "lo": lo, "cell": cell, "samples": samples,
            "diag": diag}


def _nearest_from_grid(ref, points):
    grid, lo, cell = ref["grid"], ref["lo"], ref["cell"]
    out = np.full(len(points), np.inf)
    for idx, point in enumerate(points):
        base = np.floor((point - lo) / cell).astype(np.int64)
        best = np.inf
        for radius in (0, 1, 2):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    for dz in range(-radius, radius + 1):
                        bucket = grid.get((int(base[0] + dx),
                                           int(base[1] + dy),
                                           int(base[2] + dz)))
                        if bucket:
                            arr = np.asarray(bucket)
                            d = float(np.min(np.linalg.norm(arr - point, axis=1)))
                            if d < best:
                                best = d
            if best < np.inf:
                break
        if not np.isfinite(best):
            best = float(np.min(np.linalg.norm(ref["samples"] - point, axis=1)))
        out[idx] = best
    return out


# ------------------------------------------------------------------- render

def _setup_world_and_lights(scene, lo, hi, size, center, diag, settings=None):
    """Studio presets.  ``settings`` is optional so the old single-mesh path
    keeps its original look exactly."""
    s = dict(settings or {})
    lighting = str(s.get("lighting") or "studio3")
    background = str(s.get("background") or "transparent")
    diag = max(diag, 1e-3)

    world = bpy.data.worlds.new("AttaWorld")
    world.use_nodes = True
    tree = world.node_tree
    bg = tree.nodes.get("Background")
    world_color = {
        "transparent": (0.020, 0.024, 0.030, 1.0),
        "dark": (0.006, 0.007, 0.009, 1.0),
        "white": (0.900, 0.900, 0.900, 1.0),
        "gray": (0.180, 0.180, 0.180, 1.0),
        "gradient": (0.080, 0.090, 0.105, 1.0),
    }.get(background, (0.045, 0.050, 0.060, 1.0))
    strength = {
        "transparent": 0.45, "dark": 1.0, "white": 0.85,
        "gray": 1.0, "gradient": 1.0,
    }.get(background, 1.0)
    if bg is not None:
        bg.inputs[0].default_value = world_color
        bg.inputs[1].default_value = float(strength)
    if background == "gradient":
        try:
            coord = tree.nodes.new("ShaderNodeTexCoord")
            grad = tree.nodes.new("ShaderNodeTexGradient")
            grad.gradient_type = "SPHERICAL"
            ramp = tree.nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.elements[0].position = 0.10
            ramp.color_ramp.elements[0].color = (0.012, 0.015, 0.020, 1.0)
            ramp.color_ramp.elements[1].position = 0.90
            ramp.color_ramp.elements[1].color = (0.16, 0.18, 0.22, 1.0)
            tree.links.new(coord.outputs["Generated"], grad.inputs["Vector"])
            tree.links.new(grad.outputs["Color"], ramp.inputs["Fac"])
            tree.links.new(ramp.outputs["Color"], bg.inputs["Color"])
        except Exception:  # noqa: BLE001
            pass
    scene.world = world
    scene.render.film_transparent = background == "transparent"

    presets = {
        # Energies are calibrated so a 0.18-linear ceramic reads near Zone V
        # at exposure 0; the old 110 W key was ~6 stops hot.
        "studio3": [((1.2, -1.4, 1.8), 1.72, 1.4),
                    ((-1.6, -1.0, 0.9), 0.70, 1.8),
                    ((-0.2, 1.8, 1.2), 1.41, 1.2)],
        "studio": [((1.4, -1.6, 1.9), 3.44, 1.8),
                   ((-1.7, -1.1, 0.8), 1.09, 2.4),
                   ((-0.3, 1.9, 1.3), 2.50, 1.5)],
        "catalog": [((1.0, -1.8, 2.0), 4.06, 2.6),
                    ((-1.8, -1.2, 1.0), 2.97, 3.0),
                    ((0.0, 1.6, 1.4), 0.78, 2.0)],
        "dark": [((1.5, -1.3, 1.6), 1.25, 1.2),
                 ((-1.6, -0.8, 0.6), 0.28, 2.0),
                 ((-0.2, 1.8, 1.2), 3.75, 0.9)],
        "clay": [((1.2, -1.4, 1.8), 2.50, 2.0),
                 ((-1.6, -1.0, 0.9), 1.72, 2.4),
                 ((-0.2, 1.8, 1.2), 1.09, 1.6)],
        "none": [],
    }
    lights = presets.get(lighting, presets["studio3"])

    def area(name, offset, base_energy, size_factor):
        data = bpy.data.lights.new(name, "AREA")
        data.energy = float(base_energy * (diag / 0.2) ** 2)
        data.size = float(diag * size_factor)
        data.shape = "DISK"
        light = bpy.data.objects.new(name, data)
        scene.collection.objects.link(light)
        light.location = Vector(center + np.asarray(offset, dtype=np.float64) * diag)
        light.rotation_euler = (Vector(center) - light.location).to_track_quat(
            "-Z", "Y").to_euler()
        return light

    for i, row in enumerate(lights):
        area("atta_light_%d" % i, *row)

    if bool(s.get("ground")):
        z = float(lo[2] - 0.002 * diag)
        bpy.ops.mesh.primitive_plane_add(size=float(diag * 30.0),
                                         location=(float(center[0]),
                                                   float(center[1]), z))
        plane = bpy.context.object
        plane.name = "ATTA_GROUND"
        tone = 0.62 if background == "white" else 0.16
        gm, _missing = _make_material({
            "name": "ground", "base_color": (tone, tone, tone),
            "metallic": 0.0, "roughness": 0.78, "ior": 1.45,
        })
        plane.data.materials.append(gm)

    camera_data = bpy.data.cameras.new("AttaCam")
    camera_data.lens = float(s.get("lens_mm") or 50.0)
    camera_data.sensor_width = 36.0
    camera_data.clip_start = max(1e-4, diag * 0.01)
    camera_data.clip_end = max(10.0, diag * 200.0)
    if bool(s.get("ortho")):
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = float(diag * 1.45)
    camera = bpy.data.objects.new("AttaCam", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera, diag


def _configure_render(scene, settings, samples):
    s = dict(settings or {})
    if "exposure" in s:
        try:
            scene.view_settings.exposure = float(s.get("exposure") or 0.0)
        except Exception:  # noqa: BLE001
            pass
    look = str(s.get("look") or "None")
    try:
        scene.view_settings.look = look
    except Exception:  # noqa: BLE001
        pass
    eevee = getattr(scene, "eevee", None)
    if eevee is not None:
        for attr, value in (("taa_render_samples", int(samples)),
                            ("use_gtao", True),
                            ("use_raytracing", True),
                            ("use_shadows", True)):
            if hasattr(eevee, attr):
                try:
                    setattr(eevee, attr, value)
                except Exception:  # noqa: BLE001
                    pass
    if hasattr(scene, "render") and hasattr(scene.render, "use_file_extension"):
        scene.render.use_file_extension = True


def _aim(camera, center, direction, distance):
    vec = np.asarray(direction, dtype=np.float64)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        vec = np.array([0.0, -1.0, 0.0])
        norm = 1.0
    vec = vec / norm
    camera.location = Vector(np.asarray(center) + vec * distance)
    camera.rotation_euler = (Vector(center) - camera.location).to_track_quat(
        "-Z", "Y").to_euler()
    bpy.context.view_layer.update()


def _worst_ndc(lo, hi) -> float:
    from bpy_extras.object_utils import world_to_camera_view

    scene = bpy.context.scene
    camera = scene.camera
    worst = 0.0
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                view = world_to_camera_view(scene, camera, Vector((x, y, z)))
                worst = max(worst, abs(view.x - 0.5) * 2.0, abs(view.y - 0.5) * 2.0)
    return float(worst)


def _png_stats(path: Path) -> dict:
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            return {"width": 0, "height": 0, "coverage": 0.0,
                    "content_bbox_px": None, "empty_frame": True}
        buf = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buf)
        alpha = buf.reshape(height, width, 4)[:, :, 3]
        mask = alpha > 0.02
        coverage = float(mask.mean()) if mask.size else 0.0
        bbox = None
        if mask.any():
            ys, xs = np.nonzero(mask)
            # Blender pixel rows run bottom-up; report top-left origin.
            bbox = [int(xs.min()), int(height - 1 - ys.max()),
                    int(xs.max()), int(height - 1 - ys.min())]
        return {"width": width, "height": height,
                "coverage": round(coverage, 6), "content_bbox_px": bbox,
                "empty_frame": coverage < 0.005}
    finally:
        try:
            bpy.data.images.remove(image)
        except Exception:  # noqa: BLE001
            pass


def _render(job: Path, req: dict):
    verts, faces, finish, material_spec = _load(job)
    scene, engine, degraded = _reset()
    res = max(16, int(req.get("res") or 512))
    samples = max(1, int(req.get("samples") or 8))
    obj, build_info = _build(verts, faces, finish, material_spec)
    degraded.extend(build_info["degraded"])

    scene.render.resolution_x = res
    scene.render.resolution_y = res
    eevee = getattr(scene, "eevee", None)
    if eevee is not None:
        try:
            eevee.taa_render_samples = samples
        except Exception:  # noqa: BLE001
            pass
        if float((material_spec or {}).get("transmission", 0.0) or 0.0) > 0.05:
            for attr in ("use_raytracing",):
                if hasattr(eevee, attr):
                    try:
                        setattr(eevee, attr, True)
                    except Exception:  # noqa: BLE001
                        pass

    world_verts = _object_verts(obj)
    lo, hi, size, center, diag = _bounds(world_verts)
    camera, diag = _setup_world_and_lights(scene, lo, hi, size, center, diag)

    outdir = job / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    images = []
    for view in [str(v) for v in (req.get("views") or ["iso"])]:
        direction = VIEWS.get(view)
        if direction is None:
            raise ValueError("unknown view %r (known: %s)"
                             % (view, ", ".join(sorted(VIEWS))))
        distance = max(diag * 1.9, 0.02)
        ndc = 0.0
        for _ in range(6):
            _aim(camera, center, direction, distance)
            ndc = _worst_ndc(lo, hi) if len(world_verts) else 0.0
            if ndc <= 0.92:
                break
            distance *= max(1.02, ndc / 0.85)
        _aim(camera, center, direction, distance)

        target = outdir / ("%s.png" % view)
        scene.render.filepath = str(target)
        started = time.perf_counter()
        render_error = None
        try:
            bpy.ops.render.render(write_still=True)
        except Exception as exc:  # noqa: BLE001 - degrade once, then report
            if not str(engine).startswith("BLENDER_WORKBENCH"):
                degraded.append("render failed on %s (%s); retried with "
                                "BLENDER_WORKBENCH" % (engine, exc))
                try:
                    scene.render.engine = "BLENDER_WORKBENCH"
                    engine = "BLENDER_WORKBENCH"
                    bpy.ops.render.render(write_still=True)
                except Exception as retry_exc:  # noqa: BLE001
                    render_error = retry_exc
            else:
                render_error = exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if render_error is not None:
            raise render_error
        stats = _png_stats(target)
        images.append({
            "view": view,
            "path": str(target),
            "abs_path": str(target.resolve()),
            "width": stats["width"],
            "height": stats["height"],
            "coverage": stats["coverage"],
            "content_bbox_px": stats["content_bbox_px"],
            "worst_ndc": round(float(ndc), 4),
            "empty_frame": bool(stats["empty_frame"]),
            "render_ms": round(elapsed_ms, 1),
            "sha256": _sha256(target),
            "bytes": target.stat().st_size,
            "res": res,
            "samples": samples,
            "engine": engine,
            "exact": False,
        })

    return {"ok": True, "blender": bpy.app.version_string, "engine": engine,
            "images": images, "views": [img["view"] for img in images],
            "res": res, "samples": samples, "degraded": degraded,
            "finish_applied": build_info, "exact": False}


# --------------------------------------------------------------- multi-part

def _bbox_aspect(camera, lo, hi):
    """Width/height of the bbox projected on the camera's right/up axes."""
    R = camera.matrix_world.to_3x3()
    right = np.array(R @ Vector((1.0, 0.0, 0.0)), dtype=np.float64)
    up = np.array(R @ Vector((0.0, 1.0, 0.0)), dtype=np.float64)
    pts = np.array([[x, y, z] for x in (lo[0], hi[0])
                    for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    w = float(np.ptp(pts @ right))
    h = float(np.ptp(pts @ up))
    if w <= 1e-9 or h <= 1e-9:
        return 1.0
    return w / h


def _load_parts(job: Path):
    spec = json.loads((job / "parts.json").read_text(encoding="utf-8"))
    parts = []
    for row in spec.get("parts") or []:
        data = np.load(job / str(row["mesh"]))
        parts.append({
            "id": str(row.get("id") or "part"),
            "verts": np.asarray(data["verts"], dtype=np.float64).reshape(-1, 3),
            "faces": np.asarray(data["faces"], dtype=np.int64).reshape(-1, 3),
            "finish": row.get("finish") or {},
            "material": row.get("material") or dict(FALLBACK_MATERIAL),
        })
    reference = None
    ref_name = spec.get("reference")
    if ref_name:
        data = np.load(job / str(ref_name))
        reference = (np.asarray(data["verts"], dtype=np.float64).reshape(-1, 3),
                     np.asarray(data["faces"], dtype=np.int64).reshape(-1, 3),
                     None)
    return parts, dict(spec.get("settings") or {}), reference


def _render_parts(job: Path, req: dict):
    """Render a named-part scene with per-part materials, studio presets and
    diagnostic passes (normal / depth / id / curvature / deviation)."""
    parts, settings, reference = _load_parts(job)
    settings = dict(settings)
    settings.update({k: v for k, v in req.items()
                     if k in ("lighting", "background", "ground", "lens_mm",
                              "exposure", "look", "ortho", "passes",
                              "distance_factor", "deviation_range_mm",
                              "focus", "aspect", "focus_padding")})
    scene, engine, degraded = _reset()
    base_engine = engine
    res = max(16, int(req.get("res") or 512))
    samples = max(1, int(req.get("samples") or 8))
    scene.render.resolution_x = res
    scene.render.resolution_y = res
    _configure_render(scene, settings, samples)

    objs = []
    applied = []
    for part in parts:
        obj, info = _build(part["verts"], part["faces"],
                           part["finish"], part["material"])
        obj.name = "ATTA_" + str(part["id"])[:48]
        objs.append(obj)
        applied.append({"id": part["id"], **info})
        degraded.extend(info.get("degraded") or [])

    world_verts = (np.concatenate([_object_verts(o) for o in objs], axis=0)
                   if objs else np.zeros((0, 3)))
    lo, hi, size, center, diag = _bounds(world_verts)
    focus = str(settings.get("focus") or "")
    if focus:
        target = next((o for o in objs if o.name == "ATTA_" + focus[:48]), None)
        if target is None:
            raise ValueError("focus part %r not found; parts: %s"
                             % (focus, ", ".join(o.name[5:] for o in objs)))
        flo, fhi, fsize, fcenter, fdiag = _bounds(_object_verts(target))
        if fdiag > 0:
            pad = float(settings.get("focus_padding", 0.3)) * max(fsize.max(), 1e-9)
            flo = flo - pad
            fhi = fhi + pad
            lo, hi = flo, fhi
            size = fhi - flo
            center = (flo + fhi) * 0.5
            diag = float(np.linalg.norm(size))
    camera, diag = _setup_world_and_lights(
        scene, lo, hi, size, center, diag, settings)
    factor = float(settings.get("distance_factor") or 1.0)
    aspect_setting = settings.get("aspect", "square")
    allowed = ("beauty", "normal", "depth", "id", "curvature", "deviation")
    passes = []
    for raw in (settings.get("passes") or ["beauty"]):
        name = str(raw).strip().lower()
        seq = (["normal", "depth", "id", "curvature"]
               if name == "diagnostic" else [name])
        for item in seq:
            if item not in allowed:
                raise ValueError("unknown render pass %r" % item)
            if item not in passes:
                passes.append(item)
    meshes = [_mesh_arrays(o) for o in objs]
    curv = [_vertex_curvature(v, t, diag) for v, t, _ in meshes]
    curv_all = np.concatenate(curv) if curv else np.zeros(0)
    curv_t = _normalize01(curv_all)
    curv_slices = []
    _off = 0
    for h in curv:
        curv_slices.append(curv_t[_off:_off + len(h)])
        _off += len(h)
    palette = _id_palette(len(objs))
    ref_grid = _reference_grid(reference) if reference is not None else None

    outdir = job / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    images = []
    for view in [str(v) for v in (req.get("views") or ["iso"])]:
        direction = VIEWS.get(view)
        if direction is None:
            raise ValueError("unknown view %r (known: %s)"
                             % (view, ", ".join(sorted(VIEWS))))
        distance = max(diag * 1.9, 0.02)
        for _ in range(6):
            _aim(camera, center, direction, distance)
            ndc = _worst_ndc(lo, hi) if len(world_verts) else 0.0
            if ndc <= 0.92:
                break
            distance *= max(1.02, ndc / 0.85)

        # Orientation-aware auto aspect, independent of the current resolution
        # (world_to_camera_view depends on the camera frame, so it cannot be
        # used here without feedback).
        aspect = 1.0
        if isinstance(aspect_setting, (int, float)):
            aspect = float(aspect_setting)
        elif str(aspect_setting).lower() == "auto":
            aspect = _bbox_aspect(camera, lo, hi)
        elif str(aspect_setting).lower() != "square":
            try:
                aspect = float(aspect_setting)
            except (TypeError, ValueError):
                aspect = 1.0
        aspect = min(2.2, max(0.45, float(aspect)))
        if aspect >= 1.0:
            rx, ry = int(res), max(16, int(round(res / aspect)))
        else:
            rx, ry = max(16, int(round(res * aspect))), int(res)
        scene.render.resolution_x = rx
        scene.render.resolution_y = ry

        # Re-fit with the final frame, then apply the explicit distance
        # multiplier so a close-up is not undone by the fit loop.
        for _ in range(4):
            _aim(camera, center, direction, distance)
            ndc = _worst_ndc(lo, hi) if len(world_verts) else 0.0
            if ndc <= 0.92:
                break
            distance *= max(1.02, ndc / 0.85)
        distance *= factor
        _aim(camera, center, direction, distance)
        cam_loc = np.array(camera.location, dtype=np.float64)
        forward = np.asarray(center, dtype=np.float64) - cam_loc
        forward /= max(float(np.linalg.norm(forward)), 1e-12)
        depths = [((v - cam_loc) @ forward) for v, _, _ in meshes]
        dmin = min((float(d.min()) for d in depths if len(d)), default=0.0)
        dmax = max((float(d.max()) for d in depths if len(d)), default=1.0)

        for pass_name in passes:
            if pass_name == "beauty":
                scene.render.engine = base_engine
                scene.render.film_transparent = (
                    str(settings.get("background") or "transparent") == "transparent")
                try:
                    scene.view_settings.view_transform = "AgX"
                except Exception:  # noqa: BLE001
                    pass
                _configure_render(scene, settings, samples)
                for obj in objs:
                    obj.hide_render = False
                target = outdir / ("%s.png" % view)
            elif pass_name in ("normal", "depth", "id", "curvature", "deviation"):
                _workbench(scene, samples)
                scene.render.film_transparent = True
                target = outdir / ("%s_%s.png" % (view, pass_name))
                if pass_name == "normal":
                    for obj, (_, _, normals) in zip(objs, meshes):
                        _set_vertex_colors(obj, np.clip(normals * 0.5 + 0.5, 0, 1))
                elif pass_name == "depth":
                    for obj, depth in zip(objs, depths):
                        t = (depth - dmin) / max(dmax - dmin, 1e-12)
                        gray = np.clip(1.0 - t, 0.0, 1.0)
                        _set_vertex_colors(obj, np.stack([gray, gray, gray], axis=1))
                elif pass_name == "curvature":
                    for obj, t in zip(objs, curv_slices):
                        _set_vertex_colors(obj, _ramp(t, [
                            (0.0, (0.03, 0.10, 0.35)),
                            (0.35, (0.05, 0.55, 0.65)),
                            (0.7, (0.95, 0.80, 0.20)),
                            (1.0, (0.75, 0.08, 0.06)),
                        ]))
                elif pass_name == "id":
                    for obj, color in zip(objs, palette):
                        _set_vertex_colors(obj, np.tile(color, (len(obj.data.vertices), 1)))
                else:  # deviation
                    if ref_grid is None:
                        images.append({
                            "view": view, "pass": "deviation", "ok": False,
                            "skipped": "no reference mesh",
                            "path": None, "abs_path": None,
                        })
                        continue
                    dist = _nearest_from_grid(ref_grid, np.concatenate(
                        [v for v, _, _ in meshes], axis=0))
                    offset = 0
                    range_mm = settings.get("deviation_range_mm")
                    p95 = float(np.percentile(dist, 95)) * 1000.0 if len(dist) else 0.0
                    scale = float(range_mm) if range_mm else max(1.0, p95 * 2.0)
                    for obj, (v, _, _) in zip(objs, meshes):
                        d = dist[offset:offset + len(v)] * 1000.0
                        offset += len(v)
                        t = np.clip(d / max(scale, 1e-9), 0.0, 1.0)
                        _set_vertex_colors(obj, _ramp(t, [
                            (0.0, (0.04, 0.10, 0.55)),
                            (0.25, (0.05, 0.55, 0.85)),
                            (0.5, (0.10, 0.75, 0.35)),
                            (0.75, (0.95, 0.80, 0.15)),
                            (1.0, (0.80, 0.06, 0.05)),
                        ]))
            else:
                raise ValueError("unknown render pass %r" % pass_name)

            scene.render.filepath = str(target)
            started = time.perf_counter()
            render_error = None
            try:
                bpy.ops.render.render(write_still=True)
            except Exception as exc:  # noqa: BLE001
                if not str(scene.render.engine).startswith("BLENDER_WORKBENCH"):
                    degraded.append("render failed on %s (%s); retried with "
                                    "BLENDER_WORKBENCH" % (scene.render.engine, exc))
                    try:
                        scene.render.engine = "BLENDER_WORKBENCH"
                        bpy.ops.render.render(write_still=True)
                    except Exception as retry_exc:  # noqa: BLE001
                        render_error = retry_exc
                else:
                    render_error = exc
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if render_error is not None:
                raise render_error
            stats = _png_stats(target)
            row = {
                "view": view,
                "pass": pass_name,
                "path": str(target),
                "abs_path": str(target.resolve()),
                "width": stats["width"],
                "height": stats["height"],
                "coverage": stats["coverage"],
                "content_bbox_px": stats["content_bbox_px"],
                "worst_ndc": round(float(ndc), 4),
                "empty_frame": bool(stats["empty_frame"]),
                "render_ms": round(elapsed_ms, 1),
                "sha256": _sha256(target),
                "bytes": target.stat().st_size,
                "res": res,
                "resolution": [rx, ry],
                "aspect": round(float(aspect), 4),
                "samples": samples,
                "engine": scene.render.engine,
                "exact": False,
            }
            if pass_name == "depth":
                row["range_m"] = [round(dmin, 9), round(dmax, 9)]
                row["note"] = "near = white, far = black"
            elif pass_name == "curvature":
                row["note"] = "log mean-curvature magnitude (blue low, red high)"
            elif pass_name == "normal":
                row["note"] = "world-space normal encoded as RGB = n*0.5+0.5"
            elif pass_name == "deviation":
                row["range_mm"] = round(scale, 6)
                row["note"] = "absolute nearest-surface distance (blue=0, red=range)"
            images.append(row)

    return {"ok": True, "blender": bpy.app.version_string, "engine": base_engine,
            "images": images, "views": [img["view"] for img in images],
            "passes": passes,
            "res": res, "samples": samples, "degraded": degraded,
            "parts": [{"id": p["id"], "material": (p["material"] or {}).get("name")}
                      for p in parts],
            "finish_applied": applied, "settings": settings, "exact": False}


# ----------------------------------------------------------- eval / export

def _eval(job: Path, req: dict):
    verts, faces, finish, material_spec = _load(job)
    _reset()
    obj, build_info = _build(verts, faces, finish, material_spec)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    depsgraph.update()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        n_vertices = len(mesh.vertices)
        n_tris = len(mesh.loop_triangles)
        positions = np.empty(n_vertices * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", positions)
        indices = np.empty(n_tris * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", indices)
    finally:
        evaluated.to_mesh_clear()

    out = job / "evaluated.npz"
    np.savez_compressed(out,
                        verts=positions.reshape(-1, 3).astype(np.float64),
                        faces=indices.reshape(-1, 3).astype(np.int64))
    return {"ok": True, "path": str(out), "abs_path": str(out.resolve()),
            "n_vertices": int(n_vertices), "n_tris": int(n_tris),
            "live_modifiers": [m.type for m in obj.modifiers],
            "finish_applied": build_info, "exact": False,
            "degraded": build_info["degraded"]}


def _export(job: Path, req: dict):
    verts, faces, finish, material_spec = _load(job)
    _reset()
    obj, build_info = _build(verts, faces, finish, material_spec)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    outdir = job / "export"
    outdir.mkdir(parents=True, exist_ok=True)
    stem = str(req.get("stem") or "model")
    formats = [str(f).lower() for f in (req.get("formats") or ["glb"])]
    files = []
    for fmt in formats:
        suffix = "blend" if fmt == "blend" else fmt
        target = outdir / ("%s.%s" % (stem, suffix))
        entry = {"format": fmt, "path": str(target),
                 "abs_path": str(target.resolve()),
                 "exact": fmt == "blend"}
        try:
            if fmt == "glb":
                bpy.ops.export_scene.gltf(
                    filepath=str(target), export_format="GLB",
                    use_selection=True, export_apply=True, export_yup=True)
            elif fmt == "obj":
                bpy.ops.wm.obj_export(
                    filepath=str(target), export_selected_objects=True,
                    apply_modifiers=True, export_materials=True,
                    export_normals=True)
            elif fmt == "stl":
                bpy.ops.wm.stl_export(
                    filepath=str(target), export_selected_objects=True,
                    apply_modifiers=True, ascii_format=False)
            elif fmt == "blend":
                bpy.ops.wm.save_as_mainfile(filepath=str(target), copy=True)
            else:
                raise ValueError("unsupported format %r (use glb, obj, stl, blend)"
                                 % (fmt,))
            entry.update({"ok": True, "bytes": target.stat().st_size,
                          "sha256": _sha256(target)})
        except Exception as exc:  # noqa: BLE001
            entry.update({"ok": False,
                          "error": "%s: %s" % (type(exc).__name__, exc)})
        files.append(entry)

    return {"ok": all(f.get("ok", False) for f in files), "files": files,
            "dir": str(outdir), "n_vertices": int(len(obj.data.vertices)),
            "n_tris": int(len(obj.data.polygons)),
            "finish_applied": build_info, "exact": False,
            "degraded": build_info["degraded"]}


# ------------------------------------------------------------------- protocol

def _handle(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {"ok": True, "pong": True, "blender": bpy.app.version_string,
                "pid": os.getpid(), "engine": bpy.context.scene.render.engine}
    if op == "shutdown":
        return {"ok": True, "shutdown": True}
    job = Path(str(req.get("dir") or ".")).expanduser().resolve()
    if op == "render":
        return _render(job, req)
    if op in ("render_parts", "render_scene"):
        return _render_parts(job, req)
    if op in ("eval", "evaluate"):
        return _eval(job, req)
    if op == "export":
        return _export(job, req)
    return {"ok": False, "error": "unknown op %r" % (op,),
            "fix": "use ping, render, render_parts, eval, export or shutdown"}


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=None)
    parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    sys.stdout.write("ATTA_READY\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            req_id = req.get("id")
            response = _handle(req)
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                        "traceback": traceback.format_exc(limit=8)}
        response["id"] = req_id
        sys.stdout.write("ATTA_JSON:" + json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if response.get("shutdown"):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
