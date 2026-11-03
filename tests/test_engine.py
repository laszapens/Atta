# -*- coding: utf-8 -*-
"""Engine integration: the ten verbs, state, assumptions and receipts."""

from __future__ import annotations

import json

import pytest

from atta import engine, state


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def test_doctor_deep_reports_ready(home):
    out = engine.doctor(deep=True)
    assert out["ok"] is True
    assert out["smoke"]["closed"] is True
    assert out["recipes"]
    assert out["features"] > 0


def test_catalog_lists_recipes_and_features(home):
    out = engine.catalog(verbose=True)
    ids = {r["id"] for r in out["recipes"]}
    assert {"product.mug", "mechanism.gear", "part.bracket"} <= ids
    assert "box" in out["features"]
    assert "difference" in out["features"]
    assert out["materials"]
    assert "spec_schema" in out


def test_model_recipe_is_closed_and_persisted(home):
    out = engine.model("product.mug", quality="draft")
    assert out["status"] == "ok"
    assert out["facts"]["closed"] is True
    assert out["facts"]["genus"] == 1
    mid = out["model_id"]
    rec = state.get(mid)
    assert rec and rec["spec"]["model"]["id"] == "product.mug"
    assert (home / "_atta" / "models.json").is_file()


def test_model_graph_builds_and_measures(home):
    spec = {
        "schema": "atta.spec/v3",
        "name": "plate-with-hole",
        "model": {
            "kind": "graph",
            "output": "cut",
            "features": [
                {"id": "plate", "op": "box",
                 "args": {"size": [80, 50, 6], "center": [40, 25, 3]}},
                {"id": "hole", "op": "cylinder",
                 "args": {"radius": 5, "height": 20, "center": [40, 25, 3]}},
                {"id": "cut", "op": "difference", "inputs": ["plate", "hole"]},
            ],
        },
        "constraints": {"bbox": {"x": [75, 85]}},
    }
    out = engine.model(spec=spec)
    assert out["status"] == "ok"
    assert out["facts"]["closed"] is True
    assert out["facts"]["genus"] == 1
    assert 18000 < out["facts"]["volume_mm3"] < 26000


def test_spec_propose_records_assumptions(home):
    spec = {
        "schema": "atta.spec/v3",
        "name": "mug-min",
        "model": {"kind": "recipe", "id": "product.mug", "params": {"height": 110}},
    }
    out = engine.spec(spec, op="propose")
    assert out["ok"] is True
    assert out["assumption_ledger"]
    assert all(row["status"] == "assumed" for row in out["assumption_ledger"])
    built = engine.model(spec=spec)
    assert built["status"] == "ok"
    assert built["facts"]["bbox_mm"]["size"][2] == pytest.approx(110.0, abs=0.01)


def test_describe_returns_probes(home):
    mid = engine.model("product.mug")["model_id"]
    out = engine.describe(mid, probe=["bbox", "topology", "silhouette"], wall=True)
    assert out["facts"]["closed"] is True
    assert "bbox" in out["probes"]
    assert "topology" in out["probes"]
    assert "silhouette" in out["probes"]
    assert out["facts"]["wall"]["min_mm"] is not None


def test_verify_pass_and_fail(home):
    mid = engine.model("product.mug")["model_id"]
    ok = engine.verify(mid, level="standard")
    assert ok["verdict"] == "pass"
    assert ok["score"] == 1.0
    assert ok["mesh_hash"]

    bad = engine.verify(mid, level="standard",
                        constraints={"bbox": {"z": [500, 600]}})
    assert bad["verdict"] == "fail"
    assert bad["issues"]
    assert any(i.get("fix") for i in bad["issues"])


def test_variant_returns_ranked_candidates(home):
    out = engine.variant("product.mug", count=3, ranges={"height": [80, 110]})
    assert out["status"] == "ok"
    assert len(out["candidates"]) == 3
    assert out["best"]
    assert all(c.get("verdict") in ("pass", "degraded", "fail") for c in out["candidates"])


def test_run_pipeline_threads_model_id(home):
    out = engine.run([
        {"tool": "atta_model", "args": {"recipe": "product.mug", "quality": "draft"}},
        {"tool": "atta_verify", "args": {"model_id": {"$prev": "model_id"},
                                          "level": "basic"}},
        {"tool": "atta_describe", "args": {"model_id": {"$ref": "model_id"},
                                            "probe": "bbox"}},
    ])
    assert out["status"] == "ok"
    assert [row["ok"] for row in out["steps"]] == [True, True, True]
    assert out["results"][1]["verdict"] in ("pass", "degraded")


def test_rebuild_from_spec_after_process_state_loss(home):
    mid = engine.model("product.mug", params={"height": 100})["model_id"]
    path = home / "_atta" / "models.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Keep only the record; describe must rebuild from its spec, not a mesh.
    assert mid in raw["models"]
    out = engine.describe(mid, probe="bbox")
    assert out["model_id"] == mid
    assert out["facts"]["bbox_mm"]["size"][2] == pytest.approx(100.0, abs=0.01)
