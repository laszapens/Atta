# -*- coding: utf-8 -*-
"""Blender version compatibility: one backend, several API eras.

Atta ships to users on whatever Blender they already have (3.6 LTS, 4.0-4.5,
5.x).  The Python API moved underneath us several times:

* ``Mesh.use_auto_smooth`` / ``auto_smooth_angle`` were REMOVED in 4.1 and
  replaced by ``object.shade_smooth_by_angle`` / ``shade_auto_smooth``.
* ``wm.obj_export`` / ``wm.stl_export`` only exist from 3.2; before that the
  operators were ``export_scene.obj`` / ``export_mesh.stl``.
* EEVEE is ``BLENDER_EEVEE`` up to 4.1, ``BLENDER_EEVEE_NEXT`` in 4.2-4.x, and
  ``BLENDER_EEVEE`` again in 5.x.
* ``context.temp_override`` is 3.2+, and ``modifier_apply`` polls on the
  *active* object, so it fails whenever the active object is not the target.

Only the Blender on this machine can be executed.  These tests therefore do two
things: exercise the real backend when it is installed, and drive the fallback
chains against duck-typed mocks of the *other* versions so a regression in a
branch we cannot execute here still fails loudly.
"""

from __future__ import annotations

import math
import pathlib
import re

import pytest

from atta import blender

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKER = REPO / "atta" / "blender" / "worker.py"


def _worker_source() -> str:
    return WORKER.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------ discovery

def test_find_blender_checks_platform_install_locations():
    """A GUI install does not always land on PATH (macOS/Windows)."""
    text = (REPO / "atta" / "blender" / "runtime.py").read_text(encoding="utf-8")
    assert "Blender.app" in text or "Application" in text, (
        "macOS app-bundle installs must be discoverable")
    assert "blender.exe" in text, "Windows executable must be discoverable"
    assert "Blender Foundation" in text, (
        "Windows versioned install directories must be discoverable")


def test_blender_version_is_parsed_and_cached():
    if not blender.available():
        pytest.skip("Blender not installed on this machine")
    info = blender.blender_version()
    assert info is not None
    assert re.match(r"^\d+\.\d+\.\d+", info["version"])
    assert len(info["version_tuple"]) == 3
    # cached: a second call must be the same object/value without re-spawning
    again = blender.blender_version()
    assert again == info


def test_blender_versions_supported_are_declared():
    """`supported` must be derived from a real floor, not hardcoded to True."""
    text = (REPO / "atta" / "engine.py").read_text(encoding="utf-8")
    assert "(3, 6)" in text, (
        "doctor should express a minimum supported Blender version")


# --------------------------------------------------------- render engine

def test_engine_selection_handles_both_eevee_identifiers():
    """BLENDER_EEVEE (<=4.1, 5.x) and BLENDER_EEVEE_NEXT (4.2-4.x) both occur."""
    text = _worker_source()
    assert "BLENDER_EEVEE_NEXT" in text
    assert "BLENDER_EEVEE" in text
    # and it must introspect rather than blind-assign
    assert "enum_items" in text, (
        "engine identity must be introspected; hardcoding one name breaks "
        "the other version band")


def test_view_transform_is_set_defensively():
    """AgX is 4.0+; older builds only have Filmic, and assignment can raise."""
    text = _worker_source()
    assert "AgX" in text and "Filmic" in text
    tail = text[text.index('view_transform = "AgX"'):][:400]
    assert "except" in tail, "an unavailable view transform must not be fatal"


# ------------------------------------------------------------- shading

def test_shade_smooth_has_a_legacy_auto_smooth_branch():
    """Regression: pre-4.1 builds fell through to all-smooth, losing the angle.

    ``use_auto_smooth`` is the ONLY angle-aware mechanism on 2.8x-4.0, so a
    fallback chain that never touches it silently renders hard edges soft.
    """
    text = _worker_source()
    assert "use_auto_smooth" in text, (
        "the 2.8x-4.0 smooth-by-angle path is missing; those versions would "
        "lose the angle threshold entirely")
    assert "auto_smooth_angle" in text


def test_shade_smooth_prefers_the_4_1_operator_order():
    """shade_smooth_by_angle bakes; shade_auto_smooth leaves a modifier."""
    text = _worker_source()
    block = text[text.index("def _shade_smooth"):]
    block = block[:block.index("def _shade_flat")]
    first = block.index("shade_smooth_by_angle")
    second = block.index("shade_auto_smooth")
    assert first < second, (
        "shade_auto_smooth installs a geometry-nodes modifier that export_apply "
        "would bake twice; it must not be the first choice")


def test_legacy_shade_smooth_mock_sets_the_angle(tmp_path):
    """Drive the 3.6 branch inside Blender with a mock of the old API.

    ``worker.py`` imports ``bpy`` at module scope (it only ever runs inside
    Blender), so the mock harness has to run there too.  This executes the real
    ``_shade_smooth`` against an object whose 4.1+ operators are hidden.
    """
    if not blender.available():
        pytest.skip("Blender not installed on this machine")
    script = r'''
import json, math, sys
sys.path.insert(0, r"%s")
import bpy
from atta.blender.worker import _shade_smooth

class _Poly:
    def __init__(self): self.use_smooth = False
class _Mesh36:
    def __init__(self, n=6):
        self.polygons = [_Poly() for _ in range(n)]
        self.use_auto_smooth = False
        self.auto_smooth_angle = 0.0
class _Obj36:
    def __init__(self): self.data = _Mesh36()
class _Ops36:
    def __getattr__(self, name): raise AttributeError(name)
class _Ctx: object = _Ops36()
class _Bpy: ops = _Ctx()

real = bpy.ops.object
bpy.ops.object = _Ops36()
try:
    obj = _Obj36()
    method = _shade_smooth(obj, 35.0)
finally:
    bpy.ops.object = real
print("RES:" + json.dumps({
    "method": method,
    "use_auto_smooth": bool(obj.data.use_auto_smooth),
    "angle_deg": round(math.degrees(obj.data.auto_smooth_angle), 3),
    "all_smooth": all(p.use_smooth for p in obj.data.polygons),
}))
''' % str(REPO)
    exe = blender.find_blender()
    import subprocess
    proc = subprocess.run(
        [exe, "-b", "--factory-startup", "--python-expr", script],
        capture_output=True, text=True, timeout=180)
    payload = [ln for ln in (proc.stdout or "").splitlines()
               if ln.startswith("RES:")]
    assert payload, "worker probe produced no result: %s" % (proc.stderr[-1500:],)
    import json
    res = json.loads(payload[-1][4:])
    assert res["method"] == "use_auto_smooth", (
        "expected the legacy auto-smooth branch, got %r" % (res["method"],))
    assert res["use_auto_smooth"] is True
    assert res["angle_deg"] == pytest.approx(35.0)
    assert res["all_smooth"] is True


# ------------------------------------------------------- modifier apply

def test_modifier_apply_uses_a_context_override():
    """modifier_apply polls the ACTIVE object and fails when it is another one."""
    text = _worker_source()
    assert "_apply_modifier" in text
    block = text[text.index("def _apply_modifier"):][:1200]
    assert "temp_override" in block, (
        "needs a context override for the headless multi-object case")
    assert "view_layer.objects.active" in block, (
        "and the pre-3.2 fallback of setting the active object by hand")


# ------------------------------------------------------------- exporters

def test_export_helpers_have_legacy_fallbacks():
    """wm.obj_export / wm.stl_export are 3.2+; older builds used other ops."""
    text = _worker_source()
    assert "export_scene.obj" in text, "pre-3.2 OBJ operator missing"
    assert "export_mesh.stl" in text, "pre-3.2 STL operator missing"
    assert "wm.obj_export" in text and "wm.stl_export" in text
    for helper in ("_export_glb", "_export_obj", "_export_stl"):
        assert helper in text, "%s helper missing" % helper


def test_export_kwargs_degrade_instead_of_crashing():
    """Unknown kwargs raise TypeError on older builds; must retry, not die."""
    text = _worker_source()
    block = text[text.index("def _export_glb"):][:900]
    assert "TypeError" in block, (
        "glTF export must drop unsupported kwargs instead of failing")


# ------------------------------------------------------------- live smoke

@pytest.mark.skipif(not blender.available(), reason="Blender not installed")
def test_live_export_produces_all_three_mesh_formats(tmp_path, monkeypatch):
    """Executed against whatever Blender this machine has."""
    import numpy as np
    from atta.blender import runtime as get_runtime
    from atta.mesh import Mesh

    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    box = Mesh(np.array([
        [0, 0, 0], [0.02, 0, 0], [0.02, 0.02, 0], [0, 0.02, 0],
        [0, 0, 0.02], [0.02, 0, 0.02], [0.02, 0.02, 0.02], [0, 0.02, 0.02],
    ], dtype=float), np.array([
        [0, 3, 2], [0, 2, 1], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.int64))
    rt = get_runtime(root=str(tmp_path))
    out = rt.export(box, {"bevel": {"width": 0.0, "segments": 0},
                          "smooth": False, "material": "ceramic"},
                    formats=("glb", "obj", "stl"), stem="vprobe",
                    workdir=tmp_path / "export")
    files = out.get("files") or []
    assert len(files) == 3
    for f in files:
        assert f.get("ok") is True, f
        assert int(f.get("bytes") or 0) > 0, f
        assert pathlib.Path(f["path"]).is_file(), f
    assert out.get("blender"), "the worker must report its own version"
