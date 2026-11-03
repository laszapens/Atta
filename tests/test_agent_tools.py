# -*- coding: utf-8 -*-
"""Contract tests for the ten agent tools (no engine required)."""

from __future__ import annotations

import json
import sys
import time
import types

import pytest

from atta.agent import tools
from atta.errors import AttaError

EXPECTED = [
    "atta_doctor",
    "atta_catalog",
    "atta_spec",
    "atta_model",
    "atta_variant",
    "atta_describe",
    "atta_verify",
    "atta_preview",
    "atta_deliver",
    "atta_run",
]


ENGINE_NAMES = [name[len("atta_"):] for name in EXPECTED]


def _fake_engine(calls, *, error=None):
    """A stand-in atta.engine whose calls are recorded."""
    def make(tool_name):
        def handler(**kwargs):
            calls.append((tool_name, kwargs))
            if error is not None:
                raise error
            return {"tool": tool_name, "kwargs": kwargs}
        return handler

    return types.SimpleNamespace(**{
        engine_name: make(tool_name)
        for engine_name, tool_name in zip(ENGINE_NAMES, EXPECTED)
    })


def test_exactly_ten_tools_in_order():
    assert [tool["name"] for tool in tools.TOOLS] == EXPECTED
    assert tuple(tools.TOOL_NAMES) == tuple(EXPECTED)
    assert list(tools.TOOL_BY_NAME) == EXPECTED


def test_every_tool_has_a_usable_json_schema():
    for tool in tools.TOOLS:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["name"].startswith("atta_")
        assert isinstance(tool["description"], str) and len(tool["description"]) > 20

        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert schema["additionalProperties"] is False
        assert isinstance(schema.get("required", []), list)
        for key, prop in schema["properties"].items():
            assert isinstance(key, str) and key
            assert isinstance(prop, dict) and "type" in prop
        # Must be JSON serialisable as-is (MCP/manifest contracts).
        json.dumps(tool)

    assert tools.TOOL_BY_NAME["atta_run"]["inputSchema"]["required"] == ["steps"]


def test_unknown_tool_is_a_structured_error():
    out = tools.call_tool("atta_nope", {})
    assert out["ok"] is False
    assert out["tool"] == "atta_nope"
    assert out["data"] is None
    assert set(out) == {"ok", "tool", "data", "error", "ms"}
    error = out["error"]
    assert set(error) >= {"code", "message", "path", "fix"}
    assert error["code"] == "unknown_tool"
    assert "atta_doctor" in error["message"]
    assert isinstance(out["ms"], float)


def test_bad_arguments_are_a_value_not_a_crash():
    out = tools.call_tool("atta_doctor", ["not", "an", "object"])
    assert out["ok"] is False
    assert out["error"]["code"] == "bad_arguments"
    assert out["error"]["path"] == "arguments"
    assert out["error"]["fix"]
    assert "Traceback" not in json.dumps(out)


def test_success_envelope_and_lazy_engine_monkeypatch(monkeypatch):
    calls = []
    fake = _fake_engine(calls)
    monkeypatch.setitem(sys.modules, "atta.engine", fake)

    out = tools.call_tool("atta_model", {
        "recipe": "product.mug",
        "params": {"height": 95},
        "bogus_field": "must be dropped",
        "name": None,
    })

    assert out["ok"] is True
    assert out["tool"] == "atta_model"
    assert out["error"] is None
    assert out["data"]["tool"] == "atta_model"
    assert out["ms"] >= 0

    assert len(calls) == 1
    name, kwargs = calls[0]
    assert name == "atta_model"
    assert kwargs["recipe"] == "product.mug"
    assert kwargs["params"] == {"height": 95}
    assert kwargs["preset"] == "nominal"
    assert kwargs["quality"] == "draft"
    assert "bogus_field" not in kwargs


@pytest.mark.parametrize("name,args", [
    ("atta_doctor", {}),
    ("atta_catalog", {}),
    ("atta_spec", {"op": "validate"}),
    ("atta_model", {"recipe": "product.mug"}),
    ("atta_variant", {"recipe": "product.mug", "count": 3}),
    ("atta_describe", {"wall": True}),
    ("atta_verify", {"level": "strict"}),
    ("atta_preview", {"views": ["iso"]}),
    ("atta_deliver", {"formats": ["glb"]}),
    ("atta_run", {"steps": [{"tool": "atta_doctor"}]}),
])
def test_all_ten_handlers_dispatch(monkeypatch, name, args):
    calls = []
    monkeypatch.setitem(sys.modules, "atta.engine", _fake_engine(calls))
    out = tools.call_tool(name, args)
    assert out["ok"] is True, out
    assert calls and calls[0][0] == name
    assert out["data"]["tool"] == name


def test_attaerror_becomes_code_message_path_fix(monkeypatch):
    calls = []
    err = AttaError("blender_missing", "Blender was not found",
                    path="blender", fix="install Blender or set ATTA_BLENDER")
    monkeypatch.setitem(sys.modules, "atta.engine", _fake_engine(calls, error=err))

    out = tools.call_tool("atta_doctor", {})
    assert out["ok"] is False
    assert out["data"] is None
    assert out["error"]["code"] == "blender_missing"
    assert out["error"]["message"] == "Blender was not found"
    assert out["error"]["path"] == "blender"
    assert out["error"]["fix"].startswith("install Blender")
    assert "traceback" not in out
    assert "Traceback" not in json.dumps(out)


def test_unexpected_exception_is_redacted(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "atta.engine",
                        _fake_engine(calls, error=RuntimeError("kaboom")))

    out = tools.call_tool("atta_catalog", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "internal_error"
    assert "kaboom" in out["error"]["message"]
    assert set(out["error"]) >= {"code", "message", "path", "fix"}
    assert "traceback" not in out
    assert "Traceback" not in json.dumps(out)


def test_engine_unavailable_is_structured(monkeypatch):
    real_import = tools.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "atta.engine":
            raise ModuleNotFoundError("No module named 'atta.engine'",
                                      name="atta.engine")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(tools.importlib, "import_module", fake_import)
    out = tools.call_tool("atta_doctor", {})
    assert out["ok"] is False
    assert out["error"]["code"] == "engine_unavailable"
    assert out["error"]["path"] == "atta.engine"
    assert "atta/engine.py" in out["error"]["fix"]


def test_timeout_is_enforced_when_requested(monkeypatch):
    def slow(**kwargs):
        time.sleep(0.4)
        return {"too": "late"}

    fake = types.SimpleNamespace(**{name: slow for name in ENGINE_NAMES})
    monkeypatch.setitem(sys.modules, "atta.engine", fake)

    started = time.perf_counter()
    out = tools.call_tool("atta_model", {"recipe": "product.mug"}, timeout=0.05)
    elapsed = time.perf_counter() - started

    assert out["ok"] is False
    assert out["error"]["code"] == "timeout"
    assert elapsed < 0.35


def test_manifests_have_ten_tools():
    from atta.agent import manifest

    assert len(manifest("openai")) == 10
    assert len(manifest("anthropic")) == 10
    assert len(manifest("mcp")) == 10
    generic = manifest("generic")
    assert generic["name"] == "atta"
    assert len(generic["tools"]) == 10
    with pytest.raises(ValueError):
        manifest("bogus")
