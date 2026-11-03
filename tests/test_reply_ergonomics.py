# -*- coding: utf-8 -*-
"""Reply-ergonomics invariants for the ten agent tools.

These pin the properties that make a tool reply usable by a model rather than
merely correct:

* ``atta_catalog`` stays small by default and stays *complete through paging*;
* the targeted ``recipe=`` / ``feature=`` lookups do not disturb that default;
* payload compression never rewrites ``images`` into a non-list (the DSH plugin's
  ``collectImages`` and the MCP content-block path both require a list);
* oversized nested arrays inside ``atta_run`` steps still get an explicit
  truncation marker instead of flooding the context.

Everything here goes through ``call_tool`` so it exercises the real envelope,
the real ergonomics pass and the real engine.  No Blender needed: the model is a
tiny numpy feature graph.
"""

from __future__ import annotations

import json

import pytest

from atta.agent import tools

#: Default catalogue must stay well inside a small context budget.  The measured
#: figure is ~9.6 KB; 12 KB leaves room for a recipe or two of growth without
#: letting the payload silently double.
CATALOG_MAX_BYTES = 12000
#: Total recipes in the shipped catalogue (used to prove paging is lossless).
EXPECTED_RECIPES = 23


def _size(value) -> int:
    """Byte size as a host would receive it (UTF-8, no indentation)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    """One workspace for the whole module: builds are cached across tests."""
    import os

    path = tmp_path_factory.mktemp("ergonomics-home")
    previous = os.environ.get("ATTA_HOME")
    os.environ["ATTA_HOME"] = str(path)
    yield path
    if previous is None:
        os.environ.pop("ATTA_HOME", None)
    else:
        os.environ["ATTA_HOME"] = previous


def _ring_spec(segments: int = 120) -> dict:
    """A feature graph whose spec embeds a long point list."""
    return {
        "schema": "atta.spec/v3",
        "name": "ring",
        "model": {
            "kind": "graph",
            "output": "out",
            "features": [{
                "id": "out",
                "op": "revolve",
                "args": {
                    "profile_rz": [[i * 0.5, (i % 7) * 3.0] for i in range(segments)],
                    "segments": 64,
                },
            }],
        },
    }


# --------------------------------------------------------------------------- #
# 1. catalogue: small by default, complete through paging
# --------------------------------------------------------------------------- #


def test_catalog_default_payload_stays_small(home):
    out = tools.call_tool("atta_catalog", {})
    assert out["ok"] is True, out
    assert _size(out) < CATALOG_MAX_BYTES, (
        "default catalog is %d bytes; it must stay under %d"
        % (_size(out), CATALOG_MAX_BYTES)
    )


def test_catalog_default_is_explicitly_truncated(home):
    data = tools.call_tool("atta_catalog", {})["data"]
    assert data["truncated"] is True
    assert data["total_recipes"] == EXPECTED_RECIPES
    assert data["returned_recipes"] == len(data["recipes"])
    assert data["returned_recipes"] < data["total_recipes"]
    # Nothing may be dropped silently: the reply says what is missing and how
    # to get it back.
    assert data["omitted"]
    assert data["hint"]
    assert data["next_offset"] == data["returned_recipes"]


def test_catalog_paging_reaches_every_recipe(home):
    seen: list[str] = []
    offset = 0
    for _ in range(EXPECTED_RECIPES + 5):  # hard stop if the loop never ends
        args = {} if offset == 0 else {"offset": offset}
        out = tools.call_tool("atta_catalog", args)
        assert out["ok"] is True, out
        data = out["data"]
        assert data["total_recipes"] == EXPECTED_RECIPES
        page = [r["id"] for r in data["recipes"]]
        assert page, "page at offset %d is empty" % offset
        seen.extend(page)
        if "next_offset" not in data:
            break
        nxt = data["next_offset"]
        assert isinstance(nxt, int) and nxt > offset, (
            "next_offset must advance, got %r after %d" % (nxt, offset)
        )
        offset = nxt
    else:
        pytest.fail("paging did not terminate")

    assert len(seen) == len(set(seen)), "a recipe was returned twice"
    assert len(seen) == EXPECTED_RECIPES, (
        "paging reached %d of %d recipes" % (len(seen), EXPECTED_RECIPES)
    )


def test_catalog_recipe_lookup_returns_full_schema(home):
    out = tools.call_tool("atta_catalog", {"recipe": "product.mug"})
    assert out["ok"] is True, out
    recipes = out["data"]["recipes"]
    assert len(recipes) == 1
    mug = recipes[0]
    assert mug["id"] == "product.mug"
    # The index omits params; this path is the one that must contain them.
    assert isinstance(mug["params"], dict) and mug["params"]
    assert "height" in mug["params"]


def test_catalog_recipe_lookup_accepts_a_list(home):
    out = tools.call_tool("atta_catalog",
                          {"recipe": ["product.mug", "product.bowl"]})
    assert out["ok"] is True, out
    ids = [r["id"] for r in out["data"]["recipes"]]
    assert ids == ["product.mug", "product.bowl"]


def test_catalog_unknown_recipe_is_reported_not_raised(home):
    out = tools.call_tool("atta_catalog", {"recipe": "no.such.recipe"})
    assert out["ok"] is True, out
    data = out["data"]
    assert data["recipes"] == []
    assert data["missing"] == ["no.such.recipe"]
    assert data["missing_hint"]


def test_catalog_feature_lookup_is_tiny_and_specific(home):
    out = tools.call_tool("atta_catalog", {"feature": "revolve"})
    assert out["ok"] is True, out
    assert _size(out) < 2000
    schema = out["data"]["schema"]
    assert "profile_rz" in schema["required"]


def test_catalog_unknown_feature_is_actionable(home):
    out = tools.call_tool("atta_catalog", {"feature": "not_an_op"})
    assert out["ok"] is False
    assert out["error"]["code"] == "bad_arguments"
    assert out["error"]["path"] == "feature"
    # The fix must enumerate the legal ops so the model can self-correct.
    assert "revolve" in out["error"]["fix"]


def test_catalog_lookups_do_not_disturb_the_default_index(home):
    """A targeted call must not mutate what the next default call returns."""
    first = tools.call_tool("atta_catalog", {})["data"]
    assert first["returned_recipes"] < first["total_recipes"]
    tools.call_tool("atta_catalog", {"recipe": "product.mug"})
    tools.call_tool("atta_catalog", {"feature": "revolve"})
    again = tools.call_tool("atta_catalog", {})["data"]
    assert again["returned_recipes"] == first["returned_recipes"]
    assert again["total_recipes"] == first["total_recipes"]
    assert [r["id"] for r in again["recipes"]] == [r["id"] for r in first["recipes"]]
    assert _size(again) < CATALOG_MAX_BYTES


# --------------------------------------------------------------------------- #
# 2. images must survive the ergonomics pass as a list
# --------------------------------------------------------------------------- #


def test_ergonomic_pass_keeps_images_a_list():
    """``_compact`` must not turn ``images`` into a truncation dict.

    The DSH plugin's ``collectImages`` and the MCP image content blocks both
    iterate this list; a dict in its place is a silent attachment failure.
    """
    payload = {
        "model_id": "mdl_test",
        "images": [{"view": "iso", "path": "a.png"}],
    }
    out = tools._ergonomic("atta_preview", payload)
    assert isinstance(out["images"], list)
    assert out["images"][0]["path"] == "a.png"


def test_ergonomic_pass_keeps_a_long_images_list_intact():
    """Even past the truncation threshold, ``images`` stays a plain list."""
    payload = {"images": [{"path": "p%d.png" % i} for i in range(tools.MAX_ARRAY + 10)]}
    out = tools._ergonomic("atta_preview", payload)
    assert isinstance(out["images"], list)
    assert len(out["images"]) == tools.MAX_ARRAY + 10


def test_ergonomic_pass_keeps_files_a_list():
    payload = {"files": [{"path": "m.glb"}, {"path": "m.obj"}]}
    out = tools._ergonomic("atta_deliver", payload)
    assert isinstance(out["files"], list)
    assert len(out["files"]) == 2


# --------------------------------------------------------------------------- #
# 3. oversized nested arrays get an explicit marker
# --------------------------------------------------------------------------- #


def test_compact_truncates_a_long_array_with_a_marker():
    payload = {"profile": [[i, i * 2.0] for i in range(tools.MAX_ARRAY + 56)]}
    out = tools._compact(payload)
    marker = out["profile"]
    assert isinstance(marker, dict), "a long array must become a marker, not a list"
    assert marker["truncated"] is True
    assert marker["n_total"] == tools.MAX_ARRAY + 56
    assert marker["n_shown"] == tools.MAX_ARRAY
    assert marker["note"]
    assert len(marker["head"]) + len(marker["tail"]) == tools.MAX_ARRAY


def test_compact_leaves_a_small_array_alone():
    payload = {"profile": [[0, 0], [1, 2]]}
    out = tools._compact(payload)
    assert out["profile"] == [[0, 0], [1, 2]]


def test_compact_truncates_a_huge_string():
    payload = {"text": "x" * (tools.MAX_STRING + 500)}
    out = tools._compact(payload)
    assert "truncated" in out["text"]
    assert len(out["text"]) < tools.MAX_STRING + 200


def test_atta_run_step_contents_are_truncated(home):
    """A model spec embedded in a run step must not bypass compression.

    ``steps``/``checks``/``issues`` are exempt from *being* truncated (they are
    small record tables the model reads directly), but their contents are still
    walked — a feature graph can carry thousands of profile points.
    """
    out = tools.call_tool("atta_run", {"steps": [
        {"tool": "atta_model", "args": {"spec": _ring_spec(120)}},
        {"tool": "atta_describe",
         "args": {"model_id": {"$prev": "model_id"}, "trace": True}},
    ]})
    assert out["ok"] is True, out
    data = out["data"]
    assert isinstance(data["steps"], list)
    assert data["status"] == "ok"

    # The describe step carries the construction trace with the full profile.
    describe_step = data["steps"][1]
    assert describe_step["ok"] is True, describe_step
    trace = describe_step["data"]["trace"]
    profile = trace["steps"][0]["args"]["profile_rz"]
    assert isinstance(profile, dict), (
        "a 120-point profile inside atta_run was not compressed"
    )
    assert profile["truncated"] is True
    assert profile["n_total"] == 120


# --------------------------------------------------------------------------- #
# 4. failures always carry an actionable fix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,args", [
    ("atta_model", {}),                                  # no recipe/spec
    ("atta_run", {"steps": []}),                         # empty steps
    ("atta_verify", {"level": "nonsense"}),              # bad enum
    ("atta_catalog", {"feature": "not_an_op"}),          # unknown op
    ("atta_nope", {}),                                   # unknown tool
])
def test_every_failure_carries_an_actionable_fix(name, args, home):
    out = tools.call_tool(name, args)
    assert out["ok"] is False, out
    error = out["error"]
    assert error["code"]
    assert error["message"]
    assert isinstance(error.get("fix"), str) and error["fix"].strip(), (
        "%s returned a failure with no usable fix: %r" % (name, error)
    )


def test_attaerror_without_fix_gets_a_fallback(home, monkeypatch):
    """An engine error that forgot its fix must still reach the model actionable."""
    from atta.errors import AttaError

    def boom(**kwargs):
        raise AttaError("bad_arguments", "the engine rejected something")

    monkeypatch.setattr(tools, "_engine", lambda: type("E", (), {"doctor": boom})())
    out = tools.call_tool("atta_doctor", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "bad_arguments"
    assert out["error"]["fix"].strip()


# --------------------------------------------------------------------------- #
# 5. the readable text leads the payload
# --------------------------------------------------------------------------- #


def test_describe_exposes_one_line_ahead_of_the_blobs(home):
    tools.call_tool("atta_model", {"recipe": "product.mug", "quality": "draft"})
    out = tools.call_tool("atta_describe", {"perceive": True, "res": 16})
    assert out["ok"] is True, out
    data = out["data"]
    assert isinstance(data.get("one_line"), str) and data["one_line"].strip()
    # The one-liner is hoisted near the front, not buried past the big blobs.
    keys = list(data)
    assert keys.index("one_line") <= 3, keys


def test_verify_verdict_leads_the_payload(home):
    tools.call_tool("atta_model", {"recipe": "product.mug", "quality": "draft"})
    out = tools.call_tool("atta_verify", {"level": "basic"})
    assert out["ok"] is True, out
    keys = list(out["data"])
    assert keys.index("verdict") <= 2, keys
    assert out["data"]["verdict"] in {"pass", "degraded", "fail"}
