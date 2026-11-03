# -*- coding: utf-8 -*-
"""Text/ASCII perception: no render, no Blender, deterministic."""

from __future__ import annotations

import pytest

from atta import engine
from atta.agent.tools import call_tool


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def test_perceive_returns_text_traits_views_and_sections(home):
    mid = engine.model("product.mug", quality="draft")["model_id"]
    out = engine.describe(mid, perceive=True, views=["front", "top", "right"],
                          res=16, sections=[0.5])
    pack = out["perception"]
    assert pack["schema"] == "atta.perceive/v1"
    assert "mm" in pack["text"] and "closed=yes" in pack["text"]
    assert pack["traits"]["closed"] is True
    assert pack["traits"]["genus"] == 1
    assert pack["traits"]["size_mm"][2] == pytest.approx(95.0, abs=0.5)
    assert set(pack["views"]) == {"front", "top", "right"}
    for view in pack["views"].values():
        assert len(view["ascii"]) == 16
        assert all(len(row) == 16 for row in view["ascii"])
        assert view["coverage"] > 0.0
    assert pack["sections"][0]["n_loops"] >= 1
    assert pack["sections"][0]["occupancy"] > 0.0
    assert len(pack["signature"]["sha256"]) == 64


def test_trace_recipe_and_graph(home):
    mug = engine.model("product.mug")["model_id"]
    tr = engine.describe(mug, trace=True)["trace"]
    assert tr["kind"] == "recipe"
    assert tr["steps"][0]["id"] == "product.mug"
    assert "revolve" in tr["steps"][0]["tags"]

    spec = {
        "schema": "atta.spec/v3",
        "name": "trace-bracket",
        "model": {
            "kind": "graph",
            "output": "cut",
            "features": [
                {"id": "plate", "op": "box",
                 "args": {"size": [80, 50, 6], "center": [40, 25, 3]}},
                {"id": "hole", "op": "cylinder",
                 "args": {"radius": 4, "height": 20, "center": [40, 25, 3]}},
                {"id": "cut", "op": "difference", "inputs": ["plate", "hole"]},
            ],
        },
    }
    mid = engine.model(spec=spec)["model_id"]
    tr = engine.describe(mid, trace=True)["trace"]
    assert tr["kind"] == "graph"
    assert [s["id"] for s in tr["steps"]] == ["plate", "hole", "cut"]
    vols = {s["id"]: s["volume_mm3"] for s in tr["steps"]}
    assert vols["cut"] < vols["plate"]
    assert tr["steps"][1]["bbox_mm"][2] == pytest.approx(20.0, abs=0.01)


def test_diff_returns_iou_and_ascii(home):
    mug = engine.model("product.mug")["model_id"]
    bowl = engine.model("product.bowl")["model_id"]
    out = engine.describe(bowl, compare_to=mug, views=["front", "top"], res=16)
    diff = out["diff"]
    assert set(diff["views"]) == {"front", "top"}
    for row in diff["views"].values():
        assert 0.0 <= row["iou"] <= 1.0
        assert len(row["ascii"]) == 16
        assert any(c in "".join(row["ascii"]) for c in "#+-")
    assert diff["volume_delta_mm3"] != 0.0
    assert len(diff["bbox_delta_mm"]) == 3


def test_describe_tool_exposes_perception(home):
    mid = engine.model("product.mug")["model_id"]
    env = call_tool("atta_describe", {
        "model_id": mid, "perceive": True, "res": 12,
        "views": ["front"], "sections": 0.5,
    })
    assert env["ok"] is True
    pack = env["data"]["perception"]
    assert pack["views"]["front"]["coverage"] > 0.0
    assert len(pack["sections"]) == 1
