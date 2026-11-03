# -*- coding: utf-8 -*-
"""Manifests for OpenAI, Anthropic, MCP and generic hosts.

All four are projections of :data:`atta.agent.tools.TOOLS`; there is no second
list to maintain.
"""

from __future__ import annotations

from atta.version import VERSION

from .tools import DEFAULT_TIMEOUTS, TOOLS, tool_schemas


def _openai() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in TOOLS
    ]


def _anthropic() -> list[dict]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["inputSchema"],
        }
        for tool in TOOLS
    ]


def _generic() -> dict:
    return {
        "schema": "atta.agent/v1",
        "name": "atta",
        "version": VERSION,
        "tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["inputSchema"],
                "long": tool["name"] in DEFAULT_TIMEOUTS,
                "timeout_s": DEFAULT_TIMEOUTS.get(tool["name"]),
            }
            for tool in TOOLS
        ],
        "call": {
            "module": "atta.agent.tools:call_tool",
            "python": "python3 -m atta.agent call <tool> --args-file <json>",
            "cli": "python3 -m atta --json tool <tool> --args-file <json>",
            "mcp_stdio": "python3 -m atta.agent.mcp",
            "http": "POST /call {tool, args} | POST /mcp <json-rpc>",
        },
    }


def build_manifest(fmt: str = "generic"):
    """Return the tool list in the requested host format."""
    key = str(fmt or "generic").strip().lower()
    if key in {"openai", "function", "functions"}:
        return _openai()
    if key in {"anthropic", "claude"}:
        return _anthropic()
    if key in {"mcp", "raw"}:
        return tool_schemas()
    if key in {"generic", "json"}:
        return _generic()
    raise ValueError(
        "unknown manifest format %r (supported: openai, anthropic, mcp, generic)"
        % (fmt,)
    )


__all__ = ["build_manifest"]
