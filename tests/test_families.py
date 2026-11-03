# -*- coding: utf-8 -*-
"""Every shipped shape family must load, resolve and build a closed solid.

This is the gate for the ``atta/families/`` extension modules: adding a file
is the whole registration story, so the test discovers whatever is there and
holds it to the same bar as the built-in recipes.
"""

from __future__ import annotations

import os

import pytest

os.environ["ATTA_FAMILIES_STRICT"] = "1"

from atta import families, features, recipes  # noqa: E402
from atta.errors import AttaError  # noqa: E402
from atta.mesh import topology  # noqa: E402

FAMILY_IDS = [
    rid for rid in sorted(recipes.REGISTRY)
    if rid.split(".")[0] in ("mechanical", "architecture", "pattern", "assembly",
                             "text", "lattice", "organic", "tolerance",
                             "primitive", "material_finish")
    or rid.startswith(("mechanism.", "surface.", "product.", "part."))
]


def test_family_modules_load_strictly():
    """A syntax error or bad registration in one family fails the suite."""
    loaded = families.load(strict=True)
    assert families.load_errors() == {}
    for name in families.modules():
        assert name in loaded, name


def test_family_metadata_is_complete():
    assert FAMILY_IDS, "no families registered"
    for rid in FAMILY_IDS:
        rec = recipes.get(rid)
        assert {"safe", "nominal", "hero"} <= set(rec.presets), rid
        assert len(rec.constraints) >= 2, (rid, "needs >= 2 constraints")
        assert rec.name and rec.description and rec.tags, rid
        assert isinstance(rec.expected_genus, int), rid
        for key, spec in rec.params.items():
            assert spec.type in ("float", "int", "bool", "enum", "str"), (rid, key)
            if spec.type in ("float", "int"):
                assert spec.min is not None and spec.max is not None, (rid, key)
                assert spec.min <= spec.default <= spec.max, (rid, key)


def test_every_family_resolves_all_presets():
    for rid in FAMILY_IDS:
        for preset in ("safe", "nominal", "hero"):
            resolved = recipes.get(rid).resolve(preset=preset)
            errors = [i for i in resolved.issues if i.get("severity") == "error"]
            assert not errors, (rid, preset, errors)


def test_every_family_builds_a_closed_solid():
    """Draft-quality build must be a closed, single, non-degenerate solid."""
    for rid in FAMILY_IDS:
        rec = recipes.get(rid)
        resolved = rec.resolve(preset="nominal")
        mesh = rec.build_shape(resolved.values, quality="draft")
        if isinstance(mesh, tuple):
            mesh = mesh[0]
        topo = topology(mesh)
        assert not topo["empty"], rid
        assert topo["closed"], (rid, topo)
        assert topo["non_manifold_edges"] == 0, (rid, topo)
        assert topo["n_components"] == 1, (rid, topo["n_components"])


def test_extra_ops_are_validated_like_builtins():
    """An op registered by a family is subject to the same arg validation."""
    names = features.extra_ops()
    for name, fn in names.items():
        spec = features.FEATURE_OPS[name]
        assert callable(fn), name
        assert isinstance(spec["required"], tuple), name
    if names:  # a missing required arg must raise, not TypeError
        name = sorted(names)[0]
        required = features.FEATURE_OPS[name]["required"]
        if required:
            with pytest.raises(AttaError):
                features.evaluate([{"id": "a", "op": name, "args": {}}])
