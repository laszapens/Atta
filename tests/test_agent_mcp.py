# -*- coding: utf-8 -*-
"""End-to-end MCP stdio handshake tests (real subprocess, real protocol)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = [
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


def _run_session(protocol: str = "2024-11-05", *, doctor: bool = True,
                 extra=None, env=None):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}},
        {"jsonrpc": "2.0", "id": 6, "method": "prompts/list", "params": {}},
        {"jsonrpc": "2.0", "id": 7, "method": "logging/setLevel",
         "params": {"level": "info"}},
        {"jsonrpc": "2.0", "id": 8, "method": "bogus/method", "params": {}},
    ]
    if doctor:
        requests.insert(3, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "atta_doctor", "arguments": {}},
        })
    if extra:
        requests.extend(extra)
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "atta.agent.mcp"],
        cwd=str(ROOT),
        input=payload,
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    responses = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)  # stdout must be protocol JSON only
        assert message.get("jsonrpc") == "2.0"
        if "id" in message:
            responses[message["id"]] = message
    return responses, proc


def test_full_handshake_tools_list_and_doctor():
    responses, proc = _run_session("2025-06-18")

    # Notifications must not produce a response; every request must.
    assert set(responses) == {1, 2, 3, 4, 5, 6, 7, 8}

    init = responses[1]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "atta"
    assert "tools" in init["capabilities"]
    assert init["capabilities"]["tools"]["listChanged"] is False

    listed = responses[2]["result"]["tools"]
    assert [tool["name"] for tool in listed] == TOOL_NAMES
    for tool in listed:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["inputSchema"]["type"] == "object"

    assert responses[4]["result"] == {}
    assert responses[5]["result"] == {"resources": []}
    assert responses[6]["result"] == {"prompts": []}
    assert responses[7]["result"] == {}
    assert responses[8]["error"]["code"] == -32601

    call = responses[3]["result"]
    assert isinstance(call["content"], list) and call["content"]
    assert call["content"][0]["type"] == "text"
    envelope = json.loads(call["content"][0]["text"])
    assert envelope["tool"] == "atta_doctor"
    assert set(envelope) == {"ok", "tool", "data", "error", "ms"}
    assert call["isError"] is (not envelope["ok"])
    assert "Traceback" not in call["content"][0]["text"]
    if not envelope["ok"]:
        # The engine may not exist yet: the handshake is still real.
        assert set(envelope["error"]) >= {"code", "message", "path", "fix"}
        assert envelope["error"]["code"]

    # stderr is for humans; it must not leak into the protocol stream.
    assert "[atta.mcp]" in proc.stderr


def test_model_then_verify_keeps_arrival_order(tmp_path):
    """A verify must not overtake the model that created the workspace."""
    extra = [
        {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
         "params": {"name": "atta_model",
                    "arguments": {"recipe": "product.mug", "quality": "draft"}}},
        {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
         "params": {"name": "atta_verify", "arguments": {"level": "basic"}}},
    ]
    responses, _ = _run_session("2025-06-18", doctor=False, extra=extra,
                                env={"ATTA_HOME": str(tmp_path)})
    model = json.loads(responses[20]["result"]["content"][0]["text"])
    verify = json.loads(responses[21]["result"]["content"][0]["text"])
    assert model["ok"] is True
    assert model["data"]["model_id"]
    assert verify["ok"] is True
    assert verify["data"]["verdict"] in ("pass", "degraded")


@pytest.mark.parametrize("protocol,expected", [
    ("2024-11-05", "2024-11-05"),
    ("2025-03-26", "2025-03-26"),
    ("2025-06-18", "2025-06-18"),
    ("1999-01-01", "2024-11-05"),
])
def test_protocol_version_echo_or_default(protocol, expected):
    responses, _ = _run_session(protocol, doctor=False)
    assert responses[1]["result"]["protocolVersion"] == expected
    assert len(responses[2]["result"]["tools"]) == 10
    assert 3 not in responses  # no tools/call requested
