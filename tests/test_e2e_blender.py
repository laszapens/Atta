# -*- coding: utf-8 -*-
"""Golden path through the real Blender backend: model -> verify -> preview -> deliver."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from atta import engine

pytestmark = pytest.mark.skipif(shutil.which("blender") is None,
                                reason="Blender is not installed")


def test_mug_full_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    built = engine.model("product.mug", quality="draft")
    mid = built["model_id"]
    ver = engine.verify(mid, level="standard")
    assert ver["verdict"] == "pass", ver["issues"]
    assert ver["mesh_hash"]

    prev = engine.preview(mid, views=["iso"], res=160, samples=4,
                          outdir=str(tmp_path / "preview"))
    assert prev["status"] == "ok"
    assert prev["gate"]["pass"] is True
    assert Path(prev["images"][0]["abs_path"]).is_file()

    out = engine.deliver(mid, formats=["glb"], outdir=str(tmp_path / "deliver"))
    assert out["status"] == "ok", out
    assert out["files"][0]["format"] == "glb"
    assert Path(out["files"][0]["abs_path"]).stat().st_size > 0
    assert Path(out["manifest_path"]).is_file()
    assert out["manifest"]["verify"]["verdict"] == "pass"
    assert out["manifest"]["spec_hash"] == built["model"]["spec_hash"]
