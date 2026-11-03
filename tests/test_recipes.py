# -*- coding: utf-8 -*-
"""Recipe registry tests: metadata, resolution and closed builds."""

import pytest

from atta.errors import AttaError
from atta.mesh import topology, volume
from atta.recipes import (ParamSpec, Recipe, Resolved, all_recipes, get, register)

RECIPE_IDS = [
    "product.mug", "product.bowl", "product.vase", "product.bottle",
    "mechanism.gear", "surface.panel", "surface.dome", "part.bracket",
    "part.enclosure",
]


def test_registry_contains_all_recipes():
    ids = {rec.id for rec in all_recipes()}
    for rid in RECIPE_IDS:
        assert rid in ids
        assert isinstance(get(rid), Recipe)
    with pytest.raises(AttaError) as ei:
        get("product.nope")
    assert ei.value.code == "unknown_recipe" and ei.value.fix


def test_recipe_metadata():
    for rid in RECIPE_IDS:
        rec = get(rid)
        assert {"safe", "nominal", "hero"} <= set(rec.presets)
        assert len(rec.constraints) >= 2
        assert isinstance(rec.expected_genus, int)
        assert rec.material
        assert rec.name and rec.description
        for key, spec in rec.params.items():
            assert isinstance(spec, ParamSpec)
            assert spec.type in ("float", "int", "bool", "enum", "str")
            if spec.type in ("float", "int"):
                assert spec.min is not None and spec.max is not None, (rid, key)
                assert spec.min <= spec.default <= spec.max, (rid, key)


def test_presets_resolve_without_errors():
    for rid in RECIPE_IDS:
        for preset in ("safe", "nominal", "hero"):
            resolved = get(rid).resolve(preset=preset)
            assert not resolved.blocked, (rid, preset, resolved.issues)
            assert not [i for i in resolved.issues if i.get("severity") == "error"]


def test_resolve_units_display_mm_values_m():
    resolved = get("product.mug").resolve()
    assert resolved.display["height"] == pytest.approx(95.0)
    assert resolved.values["height"] == pytest.approx(0.095)
    assert resolved.display["wall"] == pytest.approx(3.5)
    assert resolved.values["wall"] == pytest.approx(0.0035)
    assert isinstance(resolved.values["handle_enabled"], bool)

    vase = get("product.vase").resolve()
    assert vase.display["twist_deg"] == pytest.approx(75.0)
    assert vase.values["twist_deg"] == pytest.approx(75.0)
    assert isinstance(resolved, Resolved)


def test_unknown_param_is_warned_and_ignored():
    resolved = get("product.mug").resolve({"not_a_param": 3})
    codes = [i["code"] for i in resolved.issues]
    assert "unknown_param" in codes
    assert not resolved.blocked
    assert resolved.display["height"] == pytest.approx(95.0)


def test_out_of_range_is_clamped():
    resolved = get("product.mug").resolve({"height": 100000.0})
    assert resolved.display["height"] == pytest.approx(140.0)
    assert resolved.values["height"] == pytest.approx(0.140)
    assert any(i["code"] == "param_out_of_range" for i in resolved.issues)
    assert any(p["op"] == "clamp" for p in resolved.applied_patches)
    assert not resolved.blocked


def test_bad_type_blocks_and_falls_back():
    resolved = get("mechanism.gear").resolve({"teeth": 12.5})
    assert any(i["code"] == "bad_param" for i in resolved.issues)
    assert resolved.blocked
    assert resolved.display["teeth"] == 20


def test_unknown_preset_raises():
    with pytest.raises(AttaError) as ei:
        get("product.mug").resolve(preset="giant")
    assert ei.value.code == "unknown_preset"
    assert ei.value.path == "preset" and ei.value.fix


def test_constraint_can_block():
    resolved = get("product.vase").resolve({"wall": 5.5, "neck_radius": 10.0})
    assert resolved.blocked
    assert any(i["code"] == "wall_fits" for i in resolved.issues)


def test_every_recipe_builds_closed():
    for rid in RECIPE_IDS:
        rec = get(rid)
        resolved = rec.resolve()
        mesh, finish = rec.build_shape(resolved.values, "draft")
        facts = topology(mesh)
        assert facts["closed"], rid
        assert facts["genus"] == rec.expected_genus, (rid, facts["genus"])
        assert volume(mesh) > 0.0, rid
        assert isinstance(finish, dict) and finish.get("material")


def test_mug_presets_and_handle_toggle():
    rec = get("product.mug")
    for preset in ("safe", "nominal", "hero"):
        mesh, _ = rec.build_shape(rec.resolve(preset=preset).values, preset)
        assert topology(mesh)["closed"]
        assert topology(mesh)["genus"] == 1

    no_handle = rec.resolve({"handle_enabled": False})
    mesh, _ = rec.build_shape(no_handle.values, "draft")
    assert topology(mesh)["closed"]
    assert topology(mesh)["genus"] == 0


def test_register_rejects_duplicate():
    with pytest.raises(AttaError) as ei:
        register(get("product.mug"))
    assert ei.value.code == "duplicate_recipe"
