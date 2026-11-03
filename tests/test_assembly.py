# -*- coding: utf-8 -*-
"""Multi-part assembly: spec, mates, DOF, collision, receipt and export."""

from __future__ import annotations

import json

import pytest

from atta import assembly, engine
from atta import spec as S


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def _box(pid, size, center, *, fixed=True):
    return {
        "id": pid,
        "fixed": fixed,
        "graph": {"features": [
            {"id": pid, "op": "box",
             "args": {"size": list(size), "center": list(center)}},
        ]},
    }


def _example():
    return S.schema()["examples"]["assembly"]


def test_assembly_spec_validates_proposes_and_compiles():
    raw = _example()
    v = S.validate_spec(raw)
    assert v["ok"], v["errors"]
    assert v["spec"]["model"]["kind"] == "assembly"

    c = S.compile_spec(raw)
    assert c["ok"]
    assert c["compiled"]["kind"] == "assembly"
    assert len(c["compiled"]["parts"]) == 2

    p = S.propose_spec(raw)
    # Declared mates but auto ports: propose must surface that as unresolved,
    # not silently claim the bbox frame is a real mating feature.
    assert any(u["code"] == "auto_ports" for u in p["unresolved"])
    assert p["assumption_summary"]["assumed"] >= 1


def test_assembly_build_solves_mate_and_counts_dof():
    built = assembly.build(S.validate_spec(_example())["spec"])
    assert built["status"] in ("ok", "under_constrained")
    mate = built["assembly"]["mates"][0]
    assert mate["ok"] is True
    assert mate["residual_mm"] <= 0.01
    # concentric leaves rotation about the shared z axis free.
    assert built["assembly"]["dof"]["dof"] == 1
    assert built["assembly"]["dof"]["per_part"]["pin"] == 1
    assert built["assembly"]["collision"]["collisions"] == []

    receipt = assembly.verify(built)
    assert receipt["verdict"] == "pass"
    assert receipt["score"] == 1.0
    assert {c["id"] for c in receipt["checks"]} >= {
        "part.plate.closed", "part.pin.closed", "mate.m1", "dof", "collision"}


def test_overlapping_parts_fail_verification():
    raw = {
        "schema": "atta.spec/v3", "name": "overlap",
        "model": {"kind": "assembly",
                  "parts": [_box("a", [20, 20, 20], [10, 10, 10]),
                            _box("b", [20, 20, 20], [15, 10, 10])],
                  "mates": []},
        "quality": "draft", "material": "steel",
    }
    v = S.validate_spec(raw)
    assert v["ok"], v["errors"]
    built = assembly.build(v["spec"])
    collisions = built["assembly"]["collision"]["collisions"]
    assert collisions
    assert collisions[0]["volume_mm3"] > 1000
    receipt = assembly.verify(built)
    assert receipt["verdict"] == "fail"
    assert any(i["check"] == "collision" for i in receipt["issues"])


def test_conflicting_mates_are_reported():
    raw = {
        "schema": "atta.spec/v3", "name": "conflict",
        "model": {"kind": "assembly", "parts": [
            _box("plate", [60, 60, 10], [30, 30, 5]),
            {"id": "pin", "graph": {"features": [
                {"id": "pin", "op": "cylinder",
                 "args": {"radius": 4, "height": 30, "center": [30, 30, 25]}}]}},
        ], "mates": [
            {"id": "m1", "type": "concentric", "a": "plate", "b": "pin",
             "offset_mm": 20},
            {"id": "m2", "type": "concentric", "a": "plate", "b": "pin",
             "offset_mm": 40},
        ]},
        "quality": "draft", "material": "steel",
    }
    v = S.validate_spec(raw)
    assert v["ok"], v["errors"]
    built = assembly.build(v["spec"])
    assert built["status"] == "conflict"
    assert len(built["assembly"]["conflicts"]) == 2
    receipt = assembly.verify(built)
    assert receipt["verdict"] == "fail"
    assert any(i["check"].startswith("mate.") for i in receipt["issues"])


def test_fully_constrained_is_a_gate():
    raw = S.validate_spec(_example())["spec"]
    raw["constraints"] = {"fully_constrained": True}
    built = assembly.build(raw)
    receipt = assembly.verify(built, constraints={"fully_constrained": True})
    # dof=1, so the strict gate must fail rather than call it pass.
    assert receipt["verdict"] == "fail"
    assert any(i["check"] == "dof" for i in receipt["issues"])


def test_allow_interference_suppresses_the_collision():
    raw = {
        "schema": "atta.spec/v3", "name": "press_fit",
        "model": {"kind": "assembly",
                  "parts": [_box("a", [20, 20, 20], [10, 10, 10]),
                            _box("b", [20, 20, 20], [15, 10, 10])],
                  "mates": [],
                  "allow_interference": [{"a": "a", "b": "b"}]},
        "quality": "draft", "material": "steel",
    }
    built = assembly.build(S.validate_spec(raw)["spec"])
    assert built["assembly"]["collision"]["collisions"] == []
    assert assembly.verify(built)["verdict"] == "pass"


def test_engine_model_describe_verify_assembly(home):
    out = engine.model(spec=_example())
    assert out["model_id"]
    assert out["model"]["kind"] == "assembly"
    assert out["facts"]["assembly"]["part_count"] == 2
    mid = out["model_id"]

    described = engine.describe(mid)
    assert described["assembly"]["part_count"] == 2
    assert described["assembly"]["dof"]["dof"] == 1

    receipt = engine.verify(mid)
    assert receipt["verdict"] == "pass"
    assert (home / "_atta" / "out" / mid / "verify.json").is_file()


def test_engine_assembly_deliver_writes_scene_and_parts(home, tmp_path):
    out = engine.model(spec=_example())
    delivered = engine.deliver(out["model_id"], formats=["glb", "obj"],
                               outdir=str(tmp_path / "export"))
    assert delivered["status"] == "ok"
    formats = [f["format"] for f in delivered["files"]]
    assert "glb" in formats
    assert formats.count("obj") == 2
    assert delivered["verify"]["verdict"] == "pass"
    manifest = json.loads(open(delivered["manifest_path"], encoding="utf-8").read())
    assert manifest["kind"] == "assembly"
    assert len(manifest["parts"]) == 2


def test_assembly_parameter_solve_hits_a_composite_target(home):
    raw = {
        "schema": "atta.spec/v3", "name": "one_part",
        "model": {"kind": "assembly", "parts": [
            {"id": "block", "fixed": True, "recipe": "primitive.chamfer_box",
             "params": {"length": 40, "width": 30, "height": 25}},
        ], "mates": []},
        "quality": "draft", "material": "steel",
    }
    built = engine.model(spec=raw)
    solved = engine.variant(
        base=built["model_id"], targets={"bbox_z": 50.0},
        strategy="compass", count=5, max_evaluations=60, tolerance=1e-4,
    )
    assert solved["status"] == "ok"
    assert solved["feasible"] is True
    assert solved["model_id"]
    assert solved["solution"]["metrics"]["bbox_z"] == pytest.approx(50.0, abs=1e-3)
    assert solved["solution"]["verdict"] in ("pass", "degraded")
