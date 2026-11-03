# -*- coding: utf-8 -*-
"""Make the coverage matrix executable.

`docs/coverage_matrix.json` says what each category must accept.  This test
applies the mechanically-checkable part of that to every shipped family, so a
family cannot be declared "covered" while violating its own category's rule.

The mapping is deliberately conservative: only rules that are decidable from a
single build are enforced here (closed, min_wall, genus, single component,
positive volume).  Rules that need a reference model or an external standard
(`bbox_tolerance_mm`, `param_recovery_mm`, `continuity`, `porosity_tolerance`,
`fit_class`) are recorded in the family report instead — see
`work/reports/family-*.md` — and are not silently pretended here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ["ATTA_FAMILIES_STRICT"] = "1"

from atta import families, recipes, verify as verify_mod  # noqa: E402
from atta.mesh import topology  # noqa: E402
import atta.metrology as metrology  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "docs" / "coverage_matrix.json").read_text(encoding="utf-8"))

#: category -> the rules from the matrix that a single build can decide
CATEGORY_RULES = {c["id"]: c for c in MATRIX["categories"]}

#: recipe id prefix -> coverage category (the recipe id already encodes it)
PREFIX_CATEGORY = {
    "mechanical": "mechanical", "gear": "mechanical", "tolerance": "tolerance",
    "product": "product", "architecture": "architecture", "organic": "organic",
    "pattern": "pattern", "assembly": "assembly", "text": "text",
    "lattice": "lattice", "surface": "surface", "primitive": "primitive",
    "material_finish": "material_finish",
}
# built-in recipes whose prefix is not the category
LEGACY_CATEGORY = {
    "mechanism.gear": "mechanical", "part.bracket": "mechanical",
    "part.enclosure": "product", "product.mug": "product",
    "product.bowl": "product", "product.vase": "product",
    "product.bottle": "product", "surface.panel": "surface",
    "surface.dome": "surface",
}


def category_of(rid: str) -> str:
    if rid in LEGACY_CATEGORY:
        return LEGACY_CATEGORY[rid]
    return PREFIX_CATEGORY.get(rid.split(".")[0], "other")


def families_to_check():
    return [(r.id, category_of(r.id)) for r in recipes.all_recipes()]


def constraints_for(category: str, recipe) -> dict:
    """Translate the category's acceptance block into verify constraints."""
    acc = (CATEGORY_RULES.get(category) or {}).get("acceptance") or {}
    cons: dict = {}
    if acc.get("closed"):
        cons["closed"] = True
    if "min_wall_mm" in acc:
        cons["min_wall_mm"] = {"min": float(acc["min_wall_mm"])}
    # more than one part is only legitimate for a declared assembly
    cons["n_components"] = 1
    if getattr(recipe, "expected_genus", None) is not None:
        cons["genus"] = recipe.expected_genus
    return cons


@pytest.mark.parametrize("rid,category", families_to_check())
def test_family_passes_its_category_acceptance(rid, category):
    recipe = recipes.get(rid)
    resolved = recipe.resolve(preset="nominal")
    assert not resolved.blocked, (rid, [i for i in resolved.issues
                                        if i.get("severity") == "error"])
    mesh = recipe.build_shape(resolved.values, quality="draft")
    if isinstance(mesh, tuple):
        mesh = mesh[0]
    cons = constraints_for(category, recipe)
    receipt = verify_mod.verify(mesh, constraints=cons, level="standard",
                                expected_genus=recipe.expected_genus)
    verdict = receipt.get("verdict")
    failed = [c for c in receipt.get("checks", []) if not c.get("ok")]
    assert verdict == "pass", (
        "%s (%s) verdict=%s; failing checks: %s"
        % (rid, category, verdict,
           [(c["id"], c.get("value"), c.get("expected")) for c in failed][:6]))


def test_every_category_with_a_target_has_at_least_one_family():
    """The matrix's `target_families: 0` categories are exempt (material_finish)."""
    by_cat: dict[str, list[str]] = {}
    for rid, cat in families_to_check():
        by_cat.setdefault(cat, []).append(rid)
    missing = []
    for cat in MATRIX["categories"]:
        need = int(cat.get("target_families") or 0)
        have = len(by_cat.get(cat["id"], []))
        if need and have < need:
            missing.append((cat["id"], have, need))
    assert not missing, "categories short of target_families: %s" % missing
