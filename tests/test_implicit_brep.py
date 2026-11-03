# -*- coding: utf-8 -*-
"""Composable implicit fields and the optional B-rep/STEP delivery backend."""

from __future__ import annotations

import pytest

from atta import brep, engine
from atta.errors import AttaError


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def _graph(features, name="graph", output="out"):
    return {"schema": "atta.spec/v3", "name": name,
            "model": {"kind": "graph", "units": "mm", "output": output,
                      "features": features}}


@pytest.mark.parametrize("field", ["gyroid", "schwarz_p", "neovius",
                                   "mandelbulb", "sphere", "box", "torus"])
def test_sdf_field_builds_closed_solid(home, field):
    spec = _graph([{"id": "out", "op": "sdf",
                    "args": {"size": [30, 30, 30], "field": field,
                             "period": 30, "thickness": 3.0, "resolution": 14}}])
    out = engine.model(spec=spec)
    facts = out["facts"]
    assert facts["closed"] is True
    assert facts["volume_mm3"] > 0
    assert facts["n_components"] == 1


def test_sdf_gyroid_is_composable_and_has_handles(home):
    spec = _graph([{"id": "out", "op": "sdf",
                    "args": {"size": [30, 30, 30], "field": "gyroid",
                             "period": 30, "thickness": 3.6}}])
    out = engine.model(spec=spec)
    assert out["facts"]["genus"] and out["facts"]["genus"] > 0


def test_sdf_rejects_bad_field(home):
    spec = _graph([{"id": "out", "op": "sdf",
                    "args": {"size": [20, 20, 20], "field": "nope"}}])
    with pytest.raises(AttaError) as exc:
        engine.model(spec=spec)
    assert exc.value.code in ("bad_arg", "bad_spec")


def test_brep_step_export_and_delivery(home, tmp_path):
    pytest.importorskip("build123d")
    spec = _graph([
        {"id": "b", "op": "box", "args": {"size": [40, 30, 10], "center": [0, 0, 5]}},
        {"id": "c", "op": "cylinder",
         "args": {"radius": 5, "height": 20, "center": [0, 0, 5]}},
        {"id": "out", "op": "difference", "inputs": ["b", "c"]},
    ], name="plate_hole")
    out = engine.model(spec=spec)
    assert engine.verify(out["model_id"])["verdict"] == "pass"

    delivered = engine.deliver(out["model_id"], formats=["step"],
                               outdir=str(tmp_path / "export"))
    assert delivered["status"] == "ok"
    assert delivered["backend"] == "brep+mesh"
    step = [f for f in delivered["files"] if f["format"] == "step"][0]
    assert step["ok"] is True and step["bytes"] > 100
    head = open(step["path"], "rb").read(64)
    assert b"ISO-10303-21" in head
    assert delivered["brep"]["volume_mm3"] == pytest.approx(11214.6, abs=1.0)


def test_brep_refuses_mesh_only_ops_with_a_fix():
    spec = _graph([{"id": "out", "op": "coons", "args": {
        "edges": [[[0, 0, 0], [10, 0, 0]], [[10, 0, 0], [10, 10, 0]],
                  [[10, 10, 0], [0, 10, 0]], [[0, 10, 0], [0, 0, 0]]]}}])
    with pytest.raises(AttaError) as exc:
        brep.build_graph(spec)
    assert exc.value.code in ("brep_unavailable", "brep_unsupported")
    assert exc.value.fix
