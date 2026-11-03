# -*- coding: utf-8 -*-
"""Host-independent agent adapter for Atta.

The ten tool contracts in :mod:`atta.agent.tools` are the single source of
truth; MCP, HTTP and every manifest are generated from them.
"""

from __future__ import annotations

from .manifest import build_manifest as manifest
from .tools import (
    DEFAULT_TIMEOUTS,
    TOOL_BY_NAME,
    TOOL_NAMES,
    TOOLS,
    call_tool,
    images_for_result,
    tool_schemas,
)

__all__ = [
    "TOOLS",
    "TOOL_NAMES",
    "TOOL_BY_NAME",
    "DEFAULT_TIMEOUTS",
    "tool_schemas",
    "call_tool",
    "images_for_result",
    "manifest",
]
