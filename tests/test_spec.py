# -*- coding: utf-8 -*-
"""Spec v3 tests: recipe and graph, defaults, hashing, provenance.

The neighbours ``atta.recipes`` and ``atta.features`` are intentionally
injected as fake modules so these tests do not depend on those workstreams.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from atta import spec as S


# --------------------------------------------------------------------- fakes

def _param(type="float", default=None, min=None, max=None, unit="none", enum=None):
    return SimpleNamespace(type=type, default=default, min=min, max=max,
                           unit=unit, enum=enum, description="", group="",
                           advanced=False)


class FakeRecipe:
    def __init__(self, rid="product.mug", params=None, presets=None,
                 material="ceramic"):
        self.id = rid
        self.name = rid
        self.description = "fake recipe"
        self.params = params or {}
        self.presets = {"nominal": {}} if presets is None else presets
        self.constraints = []
        self.expected_genus = 0
        self.material = material
        self.tags = []

    def resolve(self, patch=None, preset="nominal"):
        raw = {k: p.default for k, p in self.params.items()}
        raw.update(self.presets.get("nominal", {}))
        if preset and preset != "nominal":
            if preset not in self.presets:
                raise ValueError("unknown preset %r" % preset)
            raw.update(self.presets[preset])
        raw.update(patch or {})
        display, values = {}, {}
        for name, ps in self.params.items():
            value = raw.get(name, ps.default)
            if ps.type == "int":
                value = int(value)
            elif ps.type == "float":
                value = float(value)
            elif ps.type == "bool":
                value = bool(value)
            display[name] = value
            values[name] = value * 0.001 if ps.unit == "mm" else value
        return SimpleNamespace(values=values, display=display,
                               issues=[], blocked=False)


def install_recipes(monkeypatch, recipe=None):
    recipe = recipe or FakeRecipe(params={
        "height": _param(default=95.0, min=40, max=400, unit="mm"),
        "wall": _param(default=3.0, min=1, max=20, unit="mm"),
    })
    mod = types.ModuleType("atta.recipes")

    def get(recipe_id):
        if recipe_id != recipe.id:
            raise KeyError(recipe_id)
        return recipe

    mod.get = get
    mod.all_recipes = lambda: [recipe]
    monkeypatch.setitem(sys.modules, "atta.recipes", mod)
    return recipe


FEATURE_OPS = {
    "box": {"required": ["size"], "optional": ["center"], "doc": "box"},
    "cylinder": {"required": ["radius", "height"], "optional": ["center"],
                 "doc": "cylinder"},
    "difference": {"required": ["inputs"], "optional": [], "doc": "difference"},
    "union": {"required": ["inputs"], "optional": [], "doc": "union"},
    # No "hole" op: Atta expresses a hole as cylinder + difference (see
    # atta/features.py).  The fake used to invent one, which is how a dead
    # `op == "hole"` branch in propose_spec stayed green for a whole release.
    # The arg names below are the real ones; a fake registry that disagrees
    # with atta.features is what hid DEFECT 1.
    "revolve": {"required": ["profile_rz"], "optional": ["segments"],
                "doc": "revolve"},
    "loft": {"required": ["rings"], "optional": ["caps"], "doc": "loft"},
    "sweep": {"required": ["path"], "optional": ["closed"], "doc": "sweep"},
}


#: The real arg-kind table, captured before the fake module replaces
#: ``atta.features``.  The fake supplies the *shape* (which ops exist, which
#: args they take) but deliberately reuses the real kinds, so a test can catch
#: a type regression in the shipped table rather than in a copy of it.
from atta import features as _real_features  # noqa: E402

_REAL_ARG_KINDS = _real_features.arg_kinds


def install_features(monkeypatch):
    mod = types.ModuleType("atta.features")
    mod.FEATURE_OPS = dict(FEATURE_OPS)
    # expose the real kinds for the ops this fake declares
    mod.arg_kinds = lambda op: (_REAL_ARG_KINDS(op)
                                if op in FEATURE_OPS else {})
    monkeypatch.setitem(sys.modules, "atta.features", mod)
    return mod


def recipe_spec(params=None, **over):
    spec = {
        "schema": S.SCHEMA,
        "name": "mug-01",
        "model": {"kind": "recipe", "id": "product.mug",
                  "preset": "nominal", "params": dict(params or {})},
        "quality": "draft",
        "material": "ceramic",
        "finish": {"bevel_mm": 0.8, "smooth_deg": 35},
        "constraints": {"bbox": {"x": [60, 140]}, "min_wall_mm": 2.5},
        "assumptions": [],
    }
    spec.update(over)
    return spec


def graph_spec(features=None, output="cut", **over):
    spec = {
        "schema": S.SCHEMA,
        "name": "bracket",
        "model": {
            "kind": "graph",
            "units": "mm",
            "segments": 64,
            "output": output,
            "features": features if features is not None else [
                {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
                {"id": "holes", "op": "cylinder",
                 "args": {"radius": 4, "height": 20, "center": [15, 25, 3]}},
                {"id": "cut", "op": "difference", "args": {},
                 "inputs": ["plate", "holes"]},
            ],
        },
        "quality": "draft",
        "material": "steel",
        "finish": {"bevel_mm": 0.5, "smooth_deg": 30},
        "constraints": {},
        "assumptions": [],
    }
    spec.update(over)
    return spec


def _codes(result):
    return [e["code"] for e in result["errors"]]


# --------------------------------------------------------------------- recipe

def test_valid_recipe(monkeypatch):
    install_recipes(monkeypatch)
    out = S.validate_spec(recipe_spec({"height": 95}))
    assert out["ok"], out["errors"]
    assert out["spec"]["model"]["params"]["height"] == 95.0
    assert out["spec"]["model"]["preset"] == "nominal"
    assert out["spec"]["quality"] == "draft"
    assert out["spec"]["material"] == "ceramic"


def test_unknown_recipe_and_unknown_param(monkeypatch):
    install_recipes(monkeypatch)
    bad_id = recipe_spec()
    bad_id["model"]["id"] = "product.nope"
    out = S.validate_spec(bad_id)
    assert not out["ok"]
    assert "unknown_recipe" in _codes(out)

    out = S.validate_spec(recipe_spec({"bogus": 1}))
    assert not out["ok"]
    assert "unknown_param" in _codes(out)
    err = next(e for e in out["errors"] if e["code"] == "unknown_param")
    assert err["path"] == "$.model.params.bogus"
    assert err["fix"]


def test_param_type_and_range(monkeypatch):
    install_recipes(monkeypatch)
    out = S.validate_spec(recipe_spec({"height": "tall"}))
    assert "bad_param" in _codes(out)

    out = S.validate_spec(recipe_spec({"height": 5000}))
    assert "param_out_of_range" in _codes(out)


def test_unknown_preset(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["model"]["preset"] = "slim"
    out = S.validate_spec(spec)
    assert "unknown_preset" in _codes(out)


def test_propose_fills_recipe_param(monkeypatch):
    install_recipes(monkeypatch)
    out = S.propose_spec(recipe_spec({"height": 95}))
    assert out["ok"], out["errors"]
    assert out["spec"]["model"]["params"]["wall"] == 3.0
    paths = [e["path"] for e in out["assumption_ledger"]]
    assert "$.model.params.wall" in paths
    entry = next(e for e in out["assumption_ledger"]
                 if e["path"] == "$.model.params.wall")
    assert entry["value"] == 3.0
    assert entry["unit"] == "mm"
    assert entry["basis"] == "design_default"
    assert entry["status"] == "assumed"
    assert entry["requires_confirmation"] is True
    assert entry["why"]
    assert out["assumption_summary"]["assumed"] >= 1
    assert out["assumption_summary"]["unresolved"] == 0
    assert all(isinstance(a, str) for a in out["spec"]["assumptions"])
    # The proposed spec is itself valid.
    assert S.validate_spec(out["spec"])["ok"]


def test_propose_reports_missing_param_without_default(monkeypatch):
    recipe = FakeRecipe(params={
        "height": _param(default=None, min=40, max=400, unit="mm"),
    })
    install_recipes(monkeypatch, recipe)
    out = S.propose_spec(recipe_spec({}))
    assert not out["ok"]
    assert out["spec"]["model"].get("params", {}).get("height") is None
    assert [u["code"] for u in out["unresolved"]] == ["missing_param"]


# ---------------------------------------------------------------------- graph

def test_graph_valid(monkeypatch):
    install_features(monkeypatch)
    out = S.validate_spec(graph_spec())
    assert out["ok"], out["errors"]
    assert out["spec"]["model"]["output"] == "cut"
    assert [f["id"] for f in out["spec"]["model"]["features"]] == \
        ["plate", "holes", "cut"]


def test_graph_cycle(monkeypatch):
    install_features(monkeypatch)
    features = [
        {"id": "a", "op": "box", "args": {"size": [10, 10, 10]}},
        {"id": "b", "op": "union", "args": {}, "inputs": ["a", "c"]},
        {"id": "c", "op": "difference", "args": {}, "inputs": ["b"]},
    ]
    out = S.validate_spec(graph_spec(features, output="c"))
    assert not out["ok"]
    assert "cycle" in _codes(out)


def test_graph_unknown_input(monkeypatch):
    install_features(monkeypatch)
    features = [
        {"id": "a", "op": "box", "args": {"size": [10, 10, 10]}},
        {"id": "b", "op": "difference", "args": {}, "inputs": ["a", "nope"]},
    ]
    out = S.validate_spec(graph_spec(features, output="b"))
    assert "unknown_input" in _codes(out)


def test_graph_duplicate_and_unknown_op(monkeypatch):
    install_features(monkeypatch)
    features = [
        {"id": "a", "op": "box", "args": {"size": [10, 10, 10]}},
        {"id": "a", "op": "box", "args": {"size": [1, 1, 1]}},
        {"id": "b", "op": "teleport", "args": {}},
    ]
    out = S.validate_spec(graph_spec(features, output="a"))
    assert "duplicate_id" in _codes(out)
    assert "unknown_feature" in _codes(out)


def test_graph_missing_required_arg(monkeypatch):
    install_features(monkeypatch)
    features = [{"id": "a", "op": "box", "args": {}}]
    out = S.validate_spec(graph_spec(features, output="a"))
    assert "bad_arg" in _codes(out)


def test_graph_output_defaults_to_last(monkeypatch):
    install_features(monkeypatch)
    spec = graph_spec()
    del spec["model"]["output"]
    out = S.validate_spec(spec)
    assert out["ok"], out["errors"]
    assert out["spec"]["model"]["output"] == "cut"


def test_graph_propose_defaults_and_unresolved(monkeypatch):
    install_features(monkeypatch)
    features = [
        {"id": "plate", "op": "box", "args": {"size": [80, 50, 6]}},
        {"id": "body", "op": "revolve", "args": {}},
        {"id": "neck", "op": "loft", "args": {}},
        {"id": "wire", "op": "sweep", "args": {}},
    ]
    spec = graph_spec(features, output="plate")
    for key in ("units", "segments"):
        spec["model"].pop(key, None)
    spec.pop("quality", None)
    spec.pop("material", None)
    out = S.propose_spec(spec)
    assert not out["ok"]
    assert {u["code"] for u in out["unresolved"]} == {
        "missing_profile", "missing_sections", "missing_path"}
    assert out["spec"]["model"]["units"] == "mm"
    assert out["spec"]["model"]["segments"] == 64
    assert out["spec"]["quality"] == "draft"
    assert out["spec"]["material"] == "ceramic"
    assumed = {e["path"] for e in out["assumption_ledger"]}
    assert {"$.model.units", "$.model.segments", "$.quality",
            "$.material"} <= assumed


def test_propose_accepts_real_revolve_and_loft_arg_names(monkeypatch):
    """DEFECT 1 regression: propose must read the arg names the ops declare.

    v1.1 checked ``profile`` (revolve's arg is ``profile_rz``) and ``sections``
    (loft's arg is ``rings``), so a *legal* spec validated ok=True but propose
    reported ok=False with a bogus missing_profile/missing_sections.
    """
    install_features(monkeypatch)
    features = [
        {"id": "body", "op": "revolve",
         "args": {"profile_rz": [[0, 0], [30, 0], [30, 90], [0, 90]]}},
        {"id": "neck", "op": "loft",
         "args": {"rings": [[[0, 0, 0], [10, 0, 0], [10, 10, 0]],
                            [[0, 0, 20], [12, 0, 20], [12, 12, 20]]]}},
    ]
    spec = graph_spec(features, output="neck")
    assert S.validate_spec(spec)["ok"]
    out = S.propose_spec(spec)
    assert out["ok"], (out["errors"], out["unresolved"])
    assert out["unresolved"] == []


def test_propose_intent_args_match_the_live_feature_registry():
    """The design-intent arg names must exist in the real FEATURE_OPS.

    This is the root cause of DEFECT 1: the names were hand-written and drifted
    from the registry.  Reading the registry makes drift a test failure.
    """
    from atta import features

    for op, (key, _code, _what, _fix) in S._DESIGN_INTENT.items():
        assert op in features.FEATURE_OPS, op
        spec = features.FEATURE_OPS[op]
        allowed = set(spec["required"]) | set(spec["optional"])
        assert key in allowed, (op, key, sorted(allowed))


# ------------------------------------------------------------------ constraints

def test_unknown_constraint(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["constraints"]["mood"] = "happy"
    out = S.validate_spec(spec)
    assert not out["ok"]
    assert "unknown_constraint" in _codes(out)
    err = next(e for e in out["errors"] if e["code"] == "unknown_constraint")
    assert err["path"] == "$.constraints.mood"


def test_unknown_finish_key(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["finish"]["sparkle"] = True
    out = S.validate_spec(spec)
    assert "unknown_finish_key" in _codes(out)


def test_bad_quality_and_material(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["quality"] = "ultra"
    spec["material"] = "unobtainium"
    out = S.validate_spec(spec)
    assert "bad_quality" in _codes(out)
    assert "bad_material" in _codes(out)


# ------------------------------------------------------------------------- hash

def test_spec_hash_is_order_independent(monkeypatch):
    install_features(monkeypatch)
    a = graph_spec()
    b = graph_spec()
    # Rebuild b with a different insertion order at every level.
    b = {"assumptions": b["assumptions"], "constraints": b["constraints"],
         "finish": b["finish"], "material": b["material"],
         "quality": b["quality"], "model": b["model"],
         "name": b["name"], "schema": b["schema"]}
    assert S.spec_hash(a) == S.spec_hash(b)
    assert S.spec_hash(a) != S.spec_hash({**a, "name": "other"})


def test_assumptions_participate_in_hash(monkeypatch):
    install_recipes(monkeypatch)
    a = recipe_spec()
    b = recipe_spec()
    b["assumptions"] = ["wall thickness chosen by hand"]
    assert S.spec_hash(a) != S.spec_hash(b)
    # Canonical JSON has no insignificant whitespace.
    assert " " not in S.canonical_json(a)


# ---------------------------------------------------------------------- compile

def test_compile_recipe(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec({"height": 95})
    out = S.compile_spec(spec)
    assert out["ok"], out["errors"]
    compiled = out["compiled"]
    assert compiled["kind"] == "recipe"
    assert compiled["recipe"] == "product.mug"
    assert compiled["preset"] == "nominal"
    assert compiled["params_display"]["height"] == 95.0
    assert compiled["params_m"]["height"] == pytest.approx(0.095)
    assert len(compiled["spec_hash"]) == 64
    assert compiled["spec_hash"] == S.spec_hash(
        S.validate_spec(spec)["spec"])
    assert compiled["assumptions"] == []
    # Stable across repeated compiles.
    assert S.compile_spec(spec)["compiled"] == compiled


def test_compile_graph_does_not_evaluate(monkeypatch):
    install_features(monkeypatch)
    spec = graph_spec()
    out = S.compile_spec(spec)
    assert out["ok"], out["errors"]
    compiled = out["compiled"]
    assert compiled["kind"] == "graph"
    assert compiled["output"] == "cut"
    assert [f["id"] for f in compiled["features"]] == \
        ["plate", "holes", "cut"]
    assert compiled["params_display"] == {}
    assert compiled["params_m"] == {}
    assert compiled["units"] == "mm"
    assert compiled["segments"] == 64


def test_compile_rejects_invalid(monkeypatch):
    install_recipes(monkeypatch)
    out = S.compile_spec(recipe_spec({"height": -1}))
    assert not out["ok"]
    assert out["compiled"] is None
    assert "param_out_of_range" in _codes(out)


# ------------------------------------------------------- assumptions provenance

def test_assumptions_are_opaque_strings(monkeypatch):
    """Assumptions must stay provenance text, not verify output."""
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["assumptions"] = ["the client asked for a 95 mm mug"]
    out = S.validate_spec(spec)
    assert set(out) == {"ok", "spec", "errors"}
    assert out["spec"]["assumptions"] == ["the client asked for a 95 mm mug"]
    assert "verdict" not in out and "checks" not in out
    assert all(isinstance(a, str) for a in out["spec"]["assumptions"])

    compiled = S.compile_spec(spec)["compiled"]
    assert compiled["assumptions"] == ["the client asked for a 95 mm mug"]
    assert "verdict" not in compiled and "checks" not in compiled


def test_bad_assumption_is_an_error(monkeypatch):
    install_recipes(monkeypatch)
    spec = recipe_spec()
    spec["assumptions"] = [{"verdict": "pass"}]
    out = S.validate_spec(spec)
    assert "bad_assumption" in _codes(out)


# ----------------------------------------------------------------------- schema

def test_schema_documents_all_models():
    doc = S.schema()
    assert doc["schema"] == S.SCHEMA
    assert doc["model_kinds"] == ["recipe", "graph", "assembly"]
    assert set(doc["examples"]) == {"recipe", "graph", "assembly"}
    assert doc["examples"]["recipe"]["model"]["kind"] == "recipe"
    assert doc["examples"]["graph"]["model"]["kind"] == "graph"
    assert doc["examples"]["assembly"]["model"]["kind"] == "assembly"
    assert "bbox" in doc["constraints"]
    assert "bevel_mm" in doc["finish"]
    assert "draft" in doc["quality"]
    assert doc["assumption_ledger_entry"]["basis"] == "design_default"


def test_graph_params_are_validated_and_compiled():
    spec = {
        "schema": S.SCHEMA, "name": "param_box",
        "model": {"kind": "graph", "units": "mm", "params": {"w": 40, "h": 20},
                  "features": [{"id": "b", "op": "box", "args": {
                      "size": [{"$expr": "w*2"}, {"$expr": "h*2"},
                               {"$param": "w"}]}}]},
        "quality": "draft", "material": "steel",
    }
    v = S.validate_spec(spec)
    assert v["ok"], v["errors"]
    assert v["spec"]["model"]["params"] == {"w": 40, "h": 20}
    c = S.compile_spec(spec)
    assert c["ok"], c["errors"]
    assert c["compiled"]["params_m"] == {"w": 40, "h": 20}


def test_graph_params_must_be_finite_numbers():
    spec = {
        "schema": S.SCHEMA, "name": "bad",
        "model": {"kind": "graph", "params": {"w": "wide"},
                  "features": [{"id": "b", "op": "box", "args": {"size": [1, 1, 1]}}]},
        "quality": "draft", "material": "steel",
    }
    v = S.validate_spec(spec)
    assert not v["ok"]
    assert any(e["code"] in ("bad_param", "bad_params") for e in v["errors"])


# ------------------------------------------------------- feature arg contracts

def test_validate_rejects_wrong_arg_types(monkeypatch):
    """DEFECT 4: bad arg *types* must fail validate, not explode in evaluate."""
    install_features(monkeypatch)
    cases = [
        ({"op": "box", "args": {"size": "big"}}, "args.size"),
        ({"op": "box", "args": {"size": [10, 10]}}, "args.size"),
        ({"op": "box", "args": {"size": [10, 10, "tall"]}}, "args.size"),
        ({"op": "cylinder", "args": {"radius": "wide", "height": 10}},
         "args.radius"),
        ({"op": "cylinder", "args": {"radius": 5, "height": 10,
                                     "segments": 3.5}}, "args.segments"),
        ({"op": "cylinder", "args": {"radius": 5, "height": 10,
                                     "segments": 2}}, "args.segments"),
        ({"op": "cylinder", "args": {"radius": 5, "height": 10,
                                     "caps": "maybe"}}, "args.caps"),
        ({"op": "cylinder", "args": {"radius": 5, "height": 10,
                                     "axis": "w"}}, "args.axis"),
        ({"op": "revolve", "args": {"profile_rz": [[0, 0], [10, "up"]]}},
         "args.profile_rz"),
        ({"op": "revolve", "args": {"profile_rz": [[0, 0]]}},
         "args.profile_rz"),
        ({"op": "loft", "args": {"rings": [[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]}},
         "args.rings"),
    ]
    for feat, where in cases:
        feat = dict(feat, id="f")
        out = S.validate_spec(graph_spec([feat], output="f"))
        assert not out["ok"], feat
        assert any(where in e["path"] for e in out["errors"]), (feat,
                                                               out["errors"])
        err = next(e for e in out["errors"] if where in e["path"])
        assert err["fix"], feat


def test_validate_rejects_unknown_feature_arg(monkeypatch):
    """An unknown arg used to validate ok and fail later inside evaluate."""
    install_features(monkeypatch)
    feat = {"id": "f", "op": "box",
            "args": {"size": [10, 10, 10], "colour": "red"}}
    out = S.validate_spec(graph_spec([feat], output="f"))
    assert not out["ok"]
    err = next(e for e in out["errors"] if e["code"] == "unknown_arg")
    assert err["path"] == "$.model.features[0].args.colour"
    assert "size" in err["fix"]  # lists the args that are legal


def test_validate_accepts_deferred_param_args(monkeypatch):
    """A $param/$expr arg has no type until evaluate, so validation defers."""
    install_features(monkeypatch)
    feats = [{"id": "f", "op": "box",
              "args": {"size": [{"$param": "w"}, {"$expr": "h*2"}, 10]}}]
    spec = graph_spec(feats, output="f")
    spec["model"]["params"] = {"w": 20, "h": 5}
    out = S.validate_spec(spec)
    assert out["ok"], out["errors"]


def test_arg_kinds_cover_every_declared_kind_entry():
    """A kind table entry must name only args the op really declares.

    Catches both drift directions: a dead kind for a removed arg, and a
    required arg left without a type (which is how ``size: "big"`` slipped
    through).  Family ops may legitimately declare no kinds, so only ops with
    a non-empty kind table are checked for completeness.
    """
    from atta import features

    for op, spec in features.FEATURE_OPS.items():
        declared = set(spec["required"]) | set(spec["optional"])
        kinds = features.arg_kinds(op)
        extra = set(kinds) - declared
        assert not extra, (op, "kinds for undeclared args", sorted(extra))
        if kinds and op in features._ARG_TYPES:
            missing = declared - set(kinds)
            assert not missing, (op, "declared args without a kind",
                                 sorted(missing))
