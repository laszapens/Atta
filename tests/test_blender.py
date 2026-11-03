# -*- coding: utf-8 -*-
"""Blender backend contract tests.

Pure-Python checks (finish/materials) always run.  Backend checks spawn the
resident Blender worker once per session and are skipped when Blender is not
installed; on a machine with Blender they must pass for real, not silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from atta import materials
from atta.blender import available, find_blender, normalize_finish, runtime
from atta.blender.finish import FINISH_DEFAULTS, finish_key
from atta.mesh import Mesh, topology

needs_blender = pytest.mark.skipif(not available(),
                                   reason="Blender is not installed")


# --------------------------------------------------------------------- meshes

def box_mesh(size: float = 0.08) -> Mesh:
    """A closed, outward-wound cube in metres (12 triangles)."""
    s = size / 2.0
    verts = np.array([
        (-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
        (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s),
    ], dtype=np.float64)
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    faces = []
    for a, b, c, d in quads:
        faces.extend([(a, b, c), (a, c, d)])
    return Mesh(verts, np.array(faces, dtype=np.int64))


@pytest.fixture(scope="session")
def rt(tmp_path_factory):
    inst = runtime(root=str(tmp_path_factory.mktemp("atta-blender")))
    try:
        yield inst
    finally:
        inst.close()


# ------------------------------------------------------------------ materials

def test_materials_have_at_least_ten_presets():
    names = materials.names()
    assert len(names) >= 10
    for required in ("ceramic", "quartz", "glass_frosted", "brushed_steel",
                     "powder_blue", "gold", "dark", "clay", "resin",
                     "emissive"):
        assert required in names


def test_materials_carry_bsdf_parameters():
    spec = materials.get("glass_frosted")
    for key in ("base_color", "metallic", "roughness", "ior", "transmission"):
        assert key in spec
    assert spec["transmission"] > 0.5
    assert materials.get("brushed_steel")["metallic"] == pytest.approx(1.0)
    assert materials.get("emissive")["emission_strength"] > 0.0
    assert len(materials.get("gold")["base_color"]) == 3


def test_materials_reject_unknown_name():
    ok, errors = materials.validate("unobtainium")
    assert not ok and errors and errors[0]["code"] == "unknown_material"


# -------------------------------------------------------------------- finish

def test_finish_defaults_are_metres_and_degrees():
    norm = normalize_finish(None)
    assert norm["smooth"] is True
    assert norm["smooth_angle_deg"] == FINISH_DEFAULTS["smooth_angle_deg"]
    assert norm["bevel"]["width"] == 0.0          # metres, bevel off by default
    assert norm["material"] == "ceramic"


def test_finish_converts_millimetres_to_metres():
    norm = normalize_finish({"bevel_mm": 0.8, "smooth_deg": 42,
                             "material": "quartz"})
    assert norm["bevel"]["width"] == pytest.approx(0.0008)
    assert norm["smooth_angle_deg"] == pytest.approx(42.0)
    assert norm["material"] == "quartz"

    nested = normalize_finish({"bevel": {"width_mm": 500, "segments": 3},
                               "smooth": False})
    assert nested["bevel"]["width"] == pytest.approx(0.5)
    assert nested["bevel"]["segments"] == 3
    assert nested["smooth"] is False


def test_finish_validates_material():
    with pytest.raises(ValueError, match="unknown material"):
        normalize_finish({"material": "not_a_material"})
    with pytest.raises(ValueError):
        normalize_finish({"bevel_mm": "thick"})


def test_finish_key_is_stable():
    a = finish_key({"bevel_mm": 0.8, "material": "gold"})
    b = finish_key({"material": "gold", "bevel": {"width": 0.0008}})
    assert a == b
    assert json.loads(a)["bevel"]["width"] == pytest.approx(0.0008)


# ------------------------------------------------------------------- backend

@needs_blender
def test_find_blender_is_executable():
    path = find_blender()
    assert path and Path(path).is_file()


@needs_blender
def test_ping(rt):
    response = rt.ping()
    assert response["ok"] is True
    assert response["pong"] is True
    assert response["blender"] and response["blender"][0].isdigit()


@needs_blender
def test_render_iso_256(rt, tmp_path):
    response = rt.render(box_mesh(), {"material": "ceramic"},
                         views=("iso",), res=256, samples=8, workdir=tmp_path)
    assert response["ok"] is True
    assert response["engine"].startswith("BLENDER_")
    assert response["degraded"] == []
    assert len(response["images"]) == 1

    image = response["images"][0]
    path = Path(image["path"])
    assert path.is_file()
    assert image["width"] == 256 and image["height"] == 256
    assert image["coverage"] > 0.0
    assert image["empty_frame"] is False
    assert image["bytes"] > 0 and path.stat().st_size == image["bytes"]
    assert len(image["sha256"]) == 64
    assert image["content_bbox_px"] is not None
    assert image["worst_ndc"] < 1.0
    assert image["render_ms"] > 0
    assert image["exact"] is False


@needs_blender
def test_evaluate_returns_closed_beveled_mesh(rt):
    source = box_mesh()
    evaluated, receipt = rt.evaluate(source, {"bevel_mm": 0.8,
                                              "material": "ceramic"})
    facts = topology(evaluated)
    assert facts["closed"] is True
    assert facts["boundary_edges"] == 0
    assert facts["n_vertices"] > len(source.verts)
    assert facts["n_tris"] > len(source.faces)
    assert evaluated.bbox()["size"][0] == pytest.approx(0.08, abs=1e-4)
    assert receipt["exact"] is False
    assert receipt["degraded"] == []
    assert "bevel" in receipt["finish_applied"]["applied"]


@needs_blender
def test_export_glb_is_non_empty(rt, tmp_path):
    response = rt.export(box_mesh(), {"material": "brushed_steel"},
                         formats=("glb",), stem="box", workdir=tmp_path)
    files = response["files"]
    assert len(files) == 1 and files[0]["ok"] is True
    target = Path(files[0]["path"])
    assert target.is_file() and target.stat().st_size > 0
    assert files[0]["bytes"] == target.stat().st_size
    assert files[0]["sha256"] == files[0]["sha256"].lower()


@needs_blender
def test_render_dedupes_by_mesh_finish_view_res_samples(rt):
    mesh = box_mesh()
    finish = {"bevel_mm": 0.4, "material": "quartz"}
    first = rt.render(mesh, finish, views=("iso",), res=128, samples=4)
    second = rt.render(mesh, finish, views=("iso",), res=128, samples=4)
    assert first["rendered_views"] == ["iso"]
    assert second["cached_views"] == ["iso"]
    assert second["images"][0]["sha256"] == first["images"][0]["sha256"]
    assert second["images"][0]["cached"] is True
