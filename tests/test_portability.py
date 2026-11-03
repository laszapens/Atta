# -*- coding: utf-8 -*-
"""Portability contract: the things that break on a stranger's machine.

We develop on Linux (POSIX locks, utf-8 default, ``/tmp``, a Blender on PATH).
The v1.2 audience is Windows, macOS and Linux, some with no Blender at all.
These tests pin the *invariants* that make the difference, and grep-audit for
the POSIX-only constructs we cannot execute here.

Everything asserted below is platform-neutral: it either exercises the real
code path on this machine or asserts a property of the source tree.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ATTA = REPO / "atta"


def _py_files():
    return [p for p in sorted(ATTA.rglob("*.py")) if "__pycache__" not in str(p)]


def _source(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- imports

def test_atta_imports_without_blender_installed(monkeypatch):
    """Importing atta must never require Blender.

    ``atta.blender`` is imported at engine level, so a module-scope ``bpy``
    import anywhere in the chain would make the whole package unimportable on
    a machine without Blender.
    """
    for path in _py_files():
        text = _source(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # a module-scope "import bpy" / "from bpy ..." is a hard failure
            if re.match(r"^(import bpy\b|from bpy\b)", stripped) and not line.startswith((" ", "\t")):
                rel = path.relative_to(REPO)
                if rel.as_posix() == "atta/blender/worker.py":
                    continue  # runs *inside* Blender by design
                pytest.fail("%s:%d imports bpy at module scope; a Blender-less "
                            "machine could not import atta" % (rel, lineno))


def test_bpy_is_never_imported_at_import_time(monkeypatch):
    """A Blender-less import of the public API must succeed."""
    code = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'bpy' or name.startswith('bpy.'):\n"
        "            raise ImportError('bpy blocked for the portability test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import atta, atta.engine, atta.blender, atta.solve, atta.state\n"
        "print(atta.blender.available())\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() in ("True", "False")


# ------------------------------------------------------------------ locks

def test_lock_backend_is_platform_appropriate():
    from atta import locks
    kind = locks.backend()
    assert kind in ("fcntl", "msvcrt", "none")
    if os.name == "nt":
        assert kind != "fcntl", "Windows must not depend on POSIX fcntl"
    else:
        assert kind == "fcntl", "POSIX should use the real advisory lock"


def test_locks_module_imports_without_fcntl(monkeypatch):
    """The original bug: module-scope ``import fcntl`` made every build fail
    on Windows with ImportError.  Simulate by blocking fcntl and importing."""
    code = (
        "import sys\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in ('fcntl', 'msvcrt'):\n"
        "            raise ImportError('blocked for the portability test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from atta import locks\n"
        "print(locks.backend())\n"
        "with locks.project_lock(exclusive=True, timeout=5):\n"
        "    with locks.project_lock(exclusive=True, timeout=5):\n"
        "        print('reentrant-ok')\n"
    )
    env = dict(os.environ)
    env["ATTA_HOME"] = "/tmp/atta-lock-portability-test"
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "none" in proc.stdout
    assert "reentrant-ok" in proc.stdout


def test_lock_is_reentrant_in_one_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    from atta.locks import project_lock
    with project_lock(exclusive=True, timeout=10):
        with project_lock(exclusive=True, timeout=10):
            pass  # the original code would have deadlocked / re-locked here


def test_lock_excludes_other_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    from atta.locks import project_lock
    events = []
    t0 = time.perf_counter()

    def work(tag, hold):
        with project_lock(exclusive=True, timeout=30):
            events.append((tag, "in", time.perf_counter() - t0))
            time.sleep(hold)
            events.append((tag, "out", time.perf_counter() - t0))

    threads = [threading.Thread(target=work, args=(i, 0.15)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ins = [e for e in events if e[1] == "in"]
    outs = [e for e in events if e[1] == "out"]
    assert len(ins) == 3 and len(outs) == 3
    for i in range(len(ins) - 1):
        assert outs[i][2] <= ins[i + 1][2] + 1e-6, "critical sections overlapped"


# --------------------------------------------------------------- encoding

def test_models_json_roundtrips_non_ascii_names(tmp_path, monkeypatch):
    """Windows defaults to cp1252; a name like 测试 must survive a round-trip."""
    monkeypatch.setenv("ATTA_HOME", str(tmp_path))
    from atta import state
    rec = {"model_id": "mdl_nonascii", "name": "Müg-测试-Ω-日本語",
           "spec": {"note": "naïve café"}, "created": "2026-01-01T00:00:00"}
    state.put(rec)
    raw = state.models_path().read_bytes()
    # proof it was written as utf-8, not escaped or mangled
    assert "Müg-测试-Ω-日本語".encode("utf-8") in raw
    loaded = state.get("mdl_nonascii")
    assert loaded["name"] == "Müg-测试-Ω-日本語"
    assert loaded["spec"]["note"] == "naïve café"


def test_every_text_write_states_utf8():
    """An explicit encoding= on every text write/read is the Windows contract."""
    offenders = []
    for path in _py_files():
        lines = _source(path).splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # widen the window enough to cover a multi-line json.dumps(...) call
            window = " ".join(lines[i:i + 6])
            if re.search(r"\.(write_text|read_text)\s*\(", line):
                if "encoding=" not in window:
                    offenders.append("%s:%d" % (path.relative_to(REPO), i + 1))
            elif re.search(r"(?<![\w.])open\s*\(", line):
                if "encoding=" in window:
                    continue
                # binary modes need no encoding: bytes are not decoded
                if re.search(r"open\s*\([^)]*['\"][rwa]?[+]?[bt][+]?['\"]", window):
                    continue
                if "Image.open" in line:
                    continue
                offenders.append("%s:%d" % (path.relative_to(REPO), i + 1))
    assert not offenders, "text I/O without encoding= (cp1252 corruption on Windows): %s" % offenders


# ------------------------------------------------------------- no-POSIX

def test_no_hardcoded_tmp_or_shell_in_logic():
    """'/tmp' and '/bin/sh' in runtime *code* are POSIX assumptions.

    Docstrings and CLI examples may legitimately mention ``/tmp/args.json``;
    what matters is that no runtime path construction hardcodes it.
    """
    offenders = []
    path_use = re.compile(r"(open\(|Path\(|makedirs|mkdir|write_text|read_text)")
    tmp_literal = re.compile(r"['\"]/tmp([/'\"\\\\]|$)")
    for path in _py_files():
        for lineno, line in enumerate(_source(path).splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith(('"""', "'''")):
                continue
            if tmp_literal.search(line) and path_use.search(line):
                offenders.append("%s:%d" % (path.relative_to(REPO), lineno))
            if re.search(r"['\"]/bin/sh['\"]", line) and "shell" not in line:
                offenders.append("%s:%d" % (path.relative_to(REPO), lineno))
    assert not offenders, "POSIX-only literal in runtime logic: %s" % offenders


def test_no_fcntl_outside_locks_module():
    offenders = []
    for path in _py_files():
        if path.name == "locks.py":
            continue
        if "fcntl" in _source(path) and "fcntl" in "\n".join(
                ln for ln in _source(path).splitlines()
                if not ln.strip().startswith("#")):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, "fcntl imported outside atta/locks.py: %s" % offenders


def test_subprocess_calls_are_argument_lists_not_shell_strings():
    """shell=True with a POSIX string is how quoting bugs reach Windows."""
    offenders = []
    for path in _py_files():
        text = _source(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "shell=True" in line:
                offenders.append("%s:%d" % (path.relative_to(REPO), lineno))
    assert not offenders, "shell=True is not portable: %s" % offenders


def test_windows_subprocess_flags_are_guarded():
    """CREATE_NO_WINDOW / start_new_session must not be applied blindly."""
    text = _source(ATTA / "blender" / "runtime.py")
    if "CREATE_NO_WINDOW" in text:
        assert "os.name == \"nt\"" in text or "os.name == 'nt'" in text, (
            "CREATE_NO_WINDOW must be gated on os.name == 'nt'")


# --------------------------------------------------------------- blender

def test_blender_absence_is_a_value_not_an_exception(monkeypatch):
    """With no Blender, every entry point must report, not raise."""
    rt = importlib.import_module("atta.blender.runtime")
    monkeypatch.setattr(rt, "find_blender", lambda: None)
    monkeypatch.setattr(rt, "_VERSION_CACHE", {})
    from atta import blender
    assert blender.available() is False
    assert blender.blender_version() is None
    # doctor still answers
    from atta import engine
    report = engine.doctor()
    assert report["blender"]["available"] is False
    assert report["blender"]["version"] is None
    check = [c for c in report["checks"] if c["id"] == "blender"][0]
    assert check["ok"] is False and check["severity"] == "warn"


def test_blender_version_is_reported_when_present():
    from atta import blender
    if not blender.available():
        pytest.skip("Blender not installed on this machine")
    info = blender.blender_version()
    assert info and info.get("version")
    assert isinstance(info["version_tuple"], list)
    assert len(info["version_tuple"]) == 3
    assert all(isinstance(n, int) for n in info["version_tuple"])


def test_doctor_reports_blender_version_not_null():
    """Regression: doctor reported version=null even with Blender present."""
    from atta import blender, engine
    if not blender.available():
        pytest.skip("Blender not installed on this machine")
    report = engine.doctor()
    assert report["blender"]["available"] is True
    assert report["blender"]["version"], "doctor must surface the detected version"
    assert re.match(r"^\d+\.\d+\.\d+", report["blender"]["version"])


def test_blender_worker_only_imports_bpy_inside_blender():
    """worker.py is the one file allowed to import bpy (it runs in Blender)."""
    text = _source(ATTA / "blender" / "worker.py")
    imports = [ln for ln in text.splitlines()
               if re.match(r"^(import bpy|from bpy)", ln.strip())]
    assert imports, "worker.py is expected to import bpy"


def test_deliver_reports_what_works_without_blender(monkeypatch):
    """The no-Blender error must name the formats that still work."""
    from atta import engine
    from atta.errors import AttaError

    rt = importlib.import_module("atta.blender.runtime")
    monkeypatch.setattr(rt, "find_blender", lambda: None)
    monkeypatch.setattr(rt, "_VERSION_CACHE", {})
    out = engine.model(recipe="product.mug", quality="draft")
    result = engine.deliver(model_id=out["model_id"], formats=["glb"])
    # deliver returns a structured failure value for a missing backend
    assert result.get("ok") is False or result.get("status") in ("failed", "partial")
    blob = json.dumps(result, ensure_ascii=False).lower()
    assert "blender_unavailable" in blob
    assert "glb" in blob
