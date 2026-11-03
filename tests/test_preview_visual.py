# -*- coding: utf-8 -*-
"""Studio preview, per-part materials and diagnostic passes (real Blender)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from atta import engine

pytestmark = pytest.mark.skipif(shutil.which("blender") is None,
                                reason="Blender is not installed")

SPEC = {
    "schema": "atta.spec/v3", "name": "preview_assembly",
    "model": {
        "kind": "assembly",
        "parts": [
            {"id": "plate", "fixed": True, "material": "stainless",
             "graph": {"features": [{"id": "p", "op": "box",
                                      "args": {"size": [40, 30, 6],
                                               "center": [0, 0, 3]}}]}},
            {"id": "pin", "material": "gold",
             "graph": {"features": [{"id": "p", "op": "cylinder",
                                      "args": {"radius": 4, "height": 20,
                                               "center": [0, 0, 13]}}]}},
        ],
        "mates": [{"id": "m1", "type": "concentric", "a": "plate", "b": "pin",
                   "port_a": "axis_z", "port_b": "axis_z", "offset_mm": 10}],
    },
}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def test_studio_preview_uses_per_part_materials(home, tmp_path):
    built = engine.model(spec=SPEC, quality="draft")
    out = engine.preview(built["model_id"], mode="studio", views=["hero"],
                         res=140, samples=2, outdir=str(tmp_path / "studio"))
    assert out["status"] == "ok"
    assert out["mode"] == "studio"
    assert len(out["images"]) == 1
    image = out["images"][0]
    assert image["pass"] == "beauty"
    assert image["width"] == image["resolution"][0]
    assert image["height"] == image["resolution"][1]
    assert image["aspect"] > 0
    assert image["aspect"] == pytest.approx(
        image["resolution"][0] / image["resolution"][1], abs=0.02)
    assert Path(image["abs_path"]).is_file()
    assert image["analysis"]["ok"] is True
    assert "zone_system" in image["analysis"]
    receipt = out["receipt_path"]
    import json
    data = json.loads(Path(receipt).read_text(encoding="utf-8"))
    mats = {p["id"]: p["material"] for p in data["parts"]}
    assert mats["plate"] == "stainless"
    assert mats["pin"] == "gold"


def test_diagnostic_passes_write_normal_id_and_curvature(home, tmp_path):
    built = engine.model(spec=SPEC, quality="draft")
    out = engine.preview(built["model_id"], mode="diagnostic", views=["iso"],
                         res=120, samples=1, passes=["diagnostic"],
                         outdir=str(tmp_path / "diag"))
    passes = [im["pass"] for im in out["images"]]
    assert passes == ["normal", "depth", "id", "curvature"]
    for image in out["images"]:
        assert Path(image["abs_path"]).is_file()
        assert image["coverage"] > 0.0
        assert image["analysis"]["code"] == "diagnostic_pass"
