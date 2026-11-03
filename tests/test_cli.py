# -*- coding: utf-8 -*-
"""CLI contract: JSON envelopes, tool bridge, version."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from atta.version import VERSION

ROOT = Path(__file__).resolve().parents[1]


def run(args, home, timeout=120):
    env = dict(os.environ)
    env["ATTA_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "atta", *args],
        cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=timeout,
    )


def test_version(tmp_path):
    p = run(["--version"], tmp_path)
    assert p.returncode == 0
    # Derived from the single source of truth, never a hardcoded literal:
    # a release bump must not be able to break this test.
    assert p.stdout.strip() == VERSION


def test_doctor_json_envelope(tmp_path):
    p = run(["--json", "doctor"], tmp_path)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["ok"] is True
    assert data["data"]["recipes"]


def test_tool_bridge(tmp_path):
    p = run(["--json", "tool", "atta_catalog", "--args", "{}"], tmp_path)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["ok"] is True
    assert data["tool"] == "atta_catalog"
    assert data["data"]["recipes"]


def test_model_and_describe(tmp_path):
    p = run(["--json", "model", "--recipe", "product.mug", "--quality", "draft"], tmp_path)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    mid = data["data"]["model_id"]
    assert data["data"]["facts"]["closed"] is True
    p2 = run(["--json", "describe", "--model-id", mid, "--probe", "bbox"], tmp_path)
    assert p2.returncode == 0, p2.stderr
    d2 = json.loads(p2.stdout)
    assert "bbox" in d2["data"]["probes"]


def test_perceive_cli(tmp_path):
    p = run(["--json", "model", "--recipe", "product.mug", "--quality", "draft"], tmp_path)
    assert p.returncode == 0, p.stderr
    mid = json.loads(p.stdout)["data"]["model_id"]
    p2 = run(["--json", "perceive", "--model-id", mid, "--res", "12"], tmp_path)
    assert p2.returncode == 0, p2.stderr
    data = json.loads(p2.stdout)["data"]
    pack = data["perception"]
    assert "mm" in pack["text"]
    assert pack["views"]["front"]["coverage"] > 0.0
    assert len(pack["sections"]) == 1


def test_perceive_human_viewer(tmp_path):
    p = run(["--json", "model", "--recipe", "product.mug", "--quality", "draft"], tmp_path)
    assert p.returncode == 0, p.stderr
    mid = json.loads(p.stdout)["data"]["model_id"]
    p2 = run(["perceive", "--model-id", mid, "--res", "10",
              "--views", "front,top"], tmp_path)
    assert p2.returncode == 0, p2.stderr
    assert "[front]" in p2.stdout
    assert "[top]" in p2.stdout
    assert "#" in p2.stdout
    assert not p2.stdout.lstrip().startswith("{")


def test_error_is_structured(tmp_path):
    p = run(["--json", "model", "--recipe", "does.not.exist"], tmp_path)
    assert p.returncode == 1
    data = json.loads(p.stdout)
    assert data["ok"] is False
    assert data["error"]["code"]
    assert data["error"]["fix"]
