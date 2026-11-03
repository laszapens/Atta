# -*- coding: utf-8 -*-
"""The shape-corpus schema, matrix and validator must stay usable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "docs" / "shape_corpus.schema.json"
MATRIX = ROOT / "docs" / "coverage_matrix.json"
VALIDATOR = ROOT / "scripts" / "validate_corpus.py"


def _sample():
    return {
        "id": "gh_bosl2_gears_001",
        "source": {
            "url": "https://github.com/example/gears.scad",
            "repo": "https://github.com/example",
            "commit": "abc1234",
            "path": "gears.scad",
            "license": "BSD-2-Clause",
            "license_spdx": "BSD-2-Clause",
            "license_url": "https://example.com/LICENSE",
            "author": "example",
            "retrieved_at": "2026-11-02T12:00:00Z",
        },
        "kind": "openscad",
        "title": "Involute spur gear module",
        "description": "Parametric involute gear.",
        "language": "openscad",
        "shape": {
            "family": "gear.spur.involute",
            "category": "mechanical",
            "tags": ["gear"],
            "units": "mm",
            "symmetry": ["radial"],
            "expected": {"closed": True, "genus": 1, "components": 1},
        },
        "parameters": [
            {"name": "module", "type": "float", "unit": "mm",
             "default": 2.0, "min": 0.5, "max": 6.0},
        ],
        "operations": ["extrude", "difference", "pattern_polar"],
        "constraints": [],
        "tests": [{"name": "nominal", "expect": {"closed": True}}],
        "quality": {
            "license_tier": "A", "license_ok": True, "has_params": True,
            "runnable": True, "has_tests": True, "shape_confidence": 0.9,
        },
        "coverage": {
            "category": "mechanical", "family_gap": 0.4,
            "reuse_score": 0.8, "priority": "P1",
        },
        "nomination": {
            "family": "gear.spur.involute", "reason": "missing",
            "suggested_recipe": "mechanism.gear.involute",
        },
    }


def test_schema_and_matrix_are_json():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert len(matrix["categories"]) == 12


def test_sample_record_validates(tmp_path):
    pytest.importorskip("jsonschema")
    import jsonschema

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(_sample(), schema)


def test_validator_cli(tmp_path):
    pytest.importorskip("jsonschema")
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(_sample()) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(path), "--coverage"],
        cwd=str(ROOT), text=True, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "valid: 1" in proc.stdout
    assert "mechanical" in proc.stdout
    assert "P0" in proc.stdout
