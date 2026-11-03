# -*- coding: utf-8 -*-
"""Installer contract: absolute paths and a ZCode-plugin dry run."""

from __future__ import annotations

from atta.agent import install


def test_mcp_entry_uses_absolute_paths():
    entry = install.mcp_entry()
    assert entry["command"].startswith("/")
    assert entry["cwd"].startswith("/")
    assert entry["env"]["ATTA_ROOT"].startswith("/")
    assert entry["args"] == ["-m", "atta.agent.mcp"]


def test_zcode_plugin_dry_run_reports_real_paths():
    out = install.install_zcode_plugin(dry_run=True)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["marketplace_exists"] is True
    assert out["marketplace"].endswith("plugins/marketplace.json")
    server = out["config"]["mcpServers"]["atta"]
    assert server["command"].startswith("/")
    assert server["cwd"].startswith("/")
    assert server["args"] == ["-m", "atta.agent.mcp"]


def test_claude_and_cursor_snippets_are_absolute():
    for fn in (install.claude_config, install.cursor_config):
        cfg = fn()
        text = str(cfg)
        assert "atta.agent.mcp" in text
