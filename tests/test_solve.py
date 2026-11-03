# -*- coding: utf-8 -*-
"""Constrained parameter solving: accuracy, semantics, diagnostics, Pareto."""

from __future__ import annotations

import pytest

from atta import engine
from atta import solve as S


def _synthetic(p):
    x, y = float(p["x"]), float(p["y"])
    return {"ok": True, "metrics": {
        "volume_mm3": x * x * y,
        "bbox_z": y,
        "min_wall_mm": 0.1 * y,
        "closed": 1.0,
        "genus": 0.0,
    }}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    return tmp_path


def test_solve_hits_equality_targets():
    out = S.solve(
        _synthetic, ranges={"x": [1, 10], "y": [1, 10]},
        targets={"volume_mm3": 500, "bbox_z": 5},
        strategy="compass", count=10, max_evaluations=120,
    )
    assert out["status"] == "ok"
    assert out["feasible"] is True
    assert out["solution"]["params"]["y"] == pytest.approx(5.0, abs=1e-6)
    assert out["solution"]["metrics"]["volume_mm3"] == pytest.approx(500.0, abs=1e-2)
    assert out["diagnostics"]["violation"] < 1e-5


def test_min_wall_constraint_is_a_lower_bound_not_an_equality():
    out = S.solve(
        _synthetic, ranges={"x": [1, 10], "y": [1, 10]},
        targets={"bbox_z": 5},
        constraints={"min_wall_mm": 0.3},
        strategy="compass", count=8, max_evaluations=80,
    )
    assert out["feasible"] is True
    # y=5 gives wall 0.5 >= 0.3; an equality interpretation would reject it.
    assert out["solution"]["metrics"]["min_wall_mm"] == pytest.approx(0.5, abs=1e-6)


def test_infeasible_reports_closest_and_side_effects():
    # An impossible equality: y cannot be both 5 and 8.
    out = S.solve(
        _synthetic, ranges={"x": [1, 10], "y": [1, 10]},
        targets={"bbox_z": 5, "min_wall_mm": 0.8},
        strategy="compass", count=8, max_evaluations=80,
    )
    assert out["status"] == "infeasible"
    assert out["feasible"] is False
    assert out["closest"]["metrics"]
    assert out["recommendations"]
    moves = out["recommendations"][0]["moves"]
    assert moves
    assert "side_effects" in moves[0]


def test_pareto_front_is_non_dominated():
    out = S.solve(
        _synthetic, ranges={"x": [1, 10], "y": [1, 10]},
        objectives=[{"metric": "volume_mm3", "direction": "min"},
                    {"metric": "bbox_z", "direction": "max"}],
        strategy="pareto", count=12, max_evaluations=100,
    )
    assert out["pareto"]
    rows = out["pareto"]
    for i, a in enumerate(rows):
        for j, b in enumerate(rows):
            if i == j:
                continue
            dominated = (b["metrics"]["volume_mm3"] <= a["metrics"]["volume_mm3"]
                         and b["metrics"]["bbox_z"] >= a["metrics"]["bbox_z"]
                         and (b["metrics"]["volume_mm3"] < a["metrics"]["volume_mm3"]
                              or b["metrics"]["bbox_z"] > a["metrics"]["bbox_z"]))
            assert not dominated


def test_variable_screening_drops_irrelevant_parameters():
    def ev(p):
        return {"ok": True, "metrics": {"bbox_z": p["y"], "volume_mm3": p["y"] * 10}}

    out = S.solve(
        ev, variables=[{"name": "x", "min": 1, "max": 10},
                       {"name": "y", "min": 1, "max": 10},
                       {"name": "z", "min": 1, "max": 10}],
        targets={"bbox_z": 6.0}, strategy="compass", count=6,
        max_evaluations=80,
    )
    assert out["feasible"] is True
    assert out["solution"]["params"]["y"] == pytest.approx(6.0, abs=1e-5)
    assert "x" in out["diagnostics"]["screened_out"]
    assert "z" in out["diagnostics"]["screened_out"]
    assert out["diagnostics"]["variables"] == ["y"]


def test_engine_variant_solves_real_recipe_and_verifies(home):
    out = engine.variant(
        "product.mug", count=6, strategy="compass",
        targets={"bbox_z": 100.0},
        constraints={"min_wall_mm": 2.5},
        max_evaluations=120, tolerance=1e-4,
    )
    assert out["status"] == "ok"
    assert out["feasible"] is True
    assert out["model_id"]
    assert out["solution"]["verdict"] in ("pass", "degraded")
    assert out["solution"]["metrics"]["bbox_z"] == pytest.approx(100.0, abs=1e-3)
    assert out["sensitivity"]


def test_engine_variant_infeasible_does_not_commit_a_model(home):
    out = engine.variant(
        "product.mug", count=4, strategy="compass",
        targets={"bbox_z": 999.0},
        max_evaluations=50, tolerance=1e-5,
    )
    assert out["status"] == "infeasible"
    assert out.get("model_id") is None
    assert out["closest"]["metrics"]
    assert out["recommendations"]


def test_engine_default_strategy_refines_lhs_into_a_solution(home):
    """The tool/CLI default is strategy=lhs; in solve mode that must not stop
    at sampling, or an exactly reachable target is reported infeasible."""
    out = engine.variant(
        "product.mug", count=6, max_evaluations=120,
        targets={"bbox_z": 100.0},
        constraints={"min_wall_mm": 2.5},
    )
    assert out["requested_strategy"] == "lhs"
    assert out["status"] == "ok"
    assert out["feasible"] is True
    assert out["model_id"]
    assert out["solution"]["metrics"]["bbox_z"] == pytest.approx(100.0, abs=1e-3)

