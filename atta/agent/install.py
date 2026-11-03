# -*- coding: utf-8 -*-
"""Install helpers for agent hosts.

    python3 -m atta.agent.install zcode            # merge into ~/.zcode/cli/config.json
    python3 -m atta.agent.install zcode --print    # show the atta entry only
    python3 -m atta.agent.install dsh --create     # symlink the DSH plugin
    python3 -m atta.agent.install claude           # print Claude Desktop JSON
    python3 -m atta.agent.install cursor           # print Cursor JSON

Rules:
* every emitted path is absolute;
* ``${VAR}`` is **never** expanded (a literal ``$`` stays a literal ``$``);
* the ZCode merge preserves every unrelated key;
* invalid JSON is refused instead of overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from atta.errors import AttaError

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugin"
DEFAULT_ZCODE_CONFIG = Path.home() / ".zcode" / "cli" / "config.json"
DEFAULT_DSH_LINK = Path.home() / ".dsh" / "plugins" / "atta"


def _abs(value) -> str:
    """Absolute path without ``~`` or ``${VAR}`` expansion."""
    path = Path(str(value))
    if not path.is_absolute():
        path = Path.cwd() / path
    return os.path.abspath(str(path))


def _python(python=None) -> str:
    return _abs(python or sys.executable or "python3")


def mcp_entry(root=None, python=None) -> dict:
    """The stdio MCP server entry shared by every host config."""
    root_dir = _abs(root or ROOT)
    interpreter = _python(python)
    return {
        "type": "stdio",
        "command": interpreter,
        "args": ["-m", "atta.agent.mcp"],
        "cwd": root_dir,
        "env": {
            "ATTA_ROOT": root_dir,
            "PYTHONPATH": root_dir,
            "PYTHONUNBUFFERED": "1",
        },
        # A long preview/deliver may exceed the host's default MCP timeout.
        "timeoutMs": 3600000,
        "enabled": True,
    }


# --------------------------------------------------------------------------- #
# ZCode
# --------------------------------------------------------------------------- #


def load_config(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AttaError(
            "bad_config",
            "refusing to overwrite invalid JSON: %s" % exc,
            path=str(path),
            fix="fix the JSON or move the file away, then re-run the installer",
        ) from exc
    if not isinstance(data, dict):
        raise AttaError(
            "bad_config",
            "refusing to overwrite: top-level value is %s, not an object"
            % type(data).__name__,
            path=str(path),
            fix="make the config a JSON object or move the file away",
        )
    return data


def merge_zcode(data: dict, entry: dict) -> dict:
    merged = dict(data)
    mcp = merged.get("mcp")
    if mcp is None:
        mcp = {}
    if not isinstance(mcp, dict):
        raise AttaError("bad_config", "mcp field is not an object",
                        path="mcp", fix="make mcp an object")
    servers = mcp.get("servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise AttaError("bad_config", "mcp.servers field is not an object",
                        path="mcp.servers", fix="make mcp.servers an object")
    servers = dict(servers)
    servers["atta"] = entry
    mcp = dict(mcp)
    mcp["servers"] = servers
    merged["mcp"] = mcp
    return merged


def install_zcode(config_path=None, root=None, python=None,
                  dry_run: bool = False) -> dict:
    path = Path(_abs(config_path or DEFAULT_ZCODE_CONFIG))
    entry = mcp_entry(root=root, python=python)
    merged = merge_zcode(load_config(path), entry)

    result = {
        "ok": True,
        "host": "zcode",
        "config_path": str(path),
        "server": "atta",
        "entry": entry,
        "written": False,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        result["config"] = merged
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    result["written"] = True
    result["restart_required"] = True
    result["note"] = "restart ZCode (or the session) to load mcp__atta__* tools"
    return result


def install_zcode_plugin(root=None, python=None, dry_run: bool = False) -> dict:
    """Write the ZCode-native plugin's .mcp.json for this checkout."""
    root_dir = _abs(root or ROOT)
    entry = mcp_entry(root=root_dir, python=python)
    server = {k: entry[k] for k in ("type", "command", "args", "cwd",
                                    "env", "timeoutMs")}
    path = Path(root_dir) / "plugins" / "atta" / ".mcp.json"
    market = Path(root_dir) / "plugins" / "marketplace.json"
    payload = {"mcpServers": {"atta": server}}
    result = {
        "ok": True,
        "host": "zcode-plugin",
        "plugin": str(Path(root_dir) / "plugins" / "atta"),
        "mcp_config": str(path),
        "marketplace": str(market),
        "marketplace_root": str(Path(root_dir) / "plugins"),
        "marketplace_exists": market.is_file(),
        "written": False,
        "dry_run": bool(dry_run),
        "next": ("Plugin Marketplace -> Add -> Add Plugin Marketplace -> "
                 "paste marketplace_root; then Personal -> dev-atta-local -> "
                 "Atta -> Install"),
    }
    if dry_run:
        result["config"] = payload
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    result["written"] = True
    return result


def zcode_snippet(root=None, python=None) -> dict:
    return {"mcp": {"servers": {"atta": mcp_entry(root=root, python=python)}}}


# --------------------------------------------------------------------------- #
# DSH plugin
# --------------------------------------------------------------------------- #


def _dsh_profiles() -> list[tuple[Path, dict]]:
    """Profiles that already reference @atta/dsh-atta (dependency or bundle)."""
    base = Path.home() / ".dsh" / "profiles"
    out = []
    if not base.is_dir():
        return out
    for pkg in sorted(base.glob("*/package.json")):
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        deps = data.get("dependencies") or {}
        profile = ((data.get("dsh") or {}).get("profile") or {})
        bundles = profile.get("bundles") or []
        if "@atta/dsh-atta" in deps or "@atta/dsh-atta" in bundles:
            out.append((pkg, data))
    return out


def _link_plugin(link: Path, src: str, force: bool) -> bool:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if os.path.realpath(str(link)) == os.path.realpath(src):
            return False
        if not force:
            raise AttaError(
                "link_exists",
                "symlink already points elsewhere: %s" % link,
                path=str(link),
                fix="remove it or pass --force",
            )
        link.unlink()
    elif link.exists():
        if link.is_dir():
            raise AttaError(
                "link_exists",
                "target is a directory; refusing to delete it: %s" % link,
                path=str(link),
                fix="remove the directory yourself, then re-run with --create",
            )
        if not force:
            raise AttaError(
                "link_exists",
                "target already exists: %s" % link,
                path=str(link),
                fix="remove it or pass --force",
            )
        link.unlink()
    os.symlink(src, str(link), target_is_directory=True)
    return True


def install_dsh(link=None, source=None, create: bool = False,
                force: bool = False) -> dict:
    """Point every DSH profile at this plugin, not at an old Atta directory.

    DSH profiles load a bundle from ``<profile>/node_modules/@atta/dsh-atta``.
    The old helper only created ``~/.dsh/plugins/atta``, which DSH does not
    load.  This updates the profile dependency, bundle list and symlink.
    """
    src = _abs(source or PLUGIN_DIR)
    profiles = _dsh_profiles()
    result = {
        "ok": True,
        "host": "dsh",
        "source": src,
        "created": False,
        "profiles": [str(pkg) for pkg, _ in profiles],
        "profiles_updated": [],
        "commands": ["dsh plugin add %s" % src],
    }
    if not profiles:
        target = _abs(link or DEFAULT_DSH_LINK)
        result["link"] = target
        if not create:
            result["note"] = ("no DSH profile references @atta/dsh-atta; "
                              "pass --create to make the fallback link at %s" % target)
            return result
        result["created"] = _link_plugin(Path(target), src, force)
        return result

    if not create:
        result["note"] = "pass --create to update every DSH profile"
        return result

    for pkg, data in profiles:
        deps = data.setdefault("dependencies", {})
        deps["@atta/dsh-atta"] = "link:%s" % src
        profile = data.setdefault("dsh", {}).setdefault("profile", {})
        bundles = profile.setdefault("bundles", [])
        if "@atta/dsh-atta" not in bundles:
            bundles.append("@atta/dsh-atta")
        tmp = pkg.with_name("%s.tmp-%d" % (pkg.name, os.getpid()))
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(pkg))
        link_path = pkg.parent / "node_modules" / "@atta" / "dsh-atta"
        if _link_plugin(link_path, src, force):
            result["created"] = True
        result["profiles_updated"].append(str(pkg))
    result["note"] = "restart DSH to load the new plugin"
    return result


# --------------------------------------------------------------------------- #
# Claude / Cursor snippets
# --------------------------------------------------------------------------- #


def claude_config(root=None, python=None) -> dict:
    entry = mcp_entry(root=root, python=python)
    return {
        "mcpServers": {
            "atta": {
                "command": entry["command"],
                "args": list(entry["args"]),
                "env": dict(entry["env"]),
            },
        },
    }


def cursor_config(root=None, python=None) -> dict:
    entry = mcp_entry(root=root, python=python)
    return {
        "mcpServers": {
            "atta": {
                "command": entry["command"],
                "args": list(entry["args"]),
                "env": dict(entry["env"]),
            },
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atta.agent.install",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("zcode", help="merge mcp.servers.atta into ZCode config")
    p.add_argument("--config", default=None,
                   help="default: ~/.zcode/cli/config.json")
    p.add_argument("--root", default=None, help="Atta repo root")
    p.add_argument("--python", default=None, help="Python interpreter")
    p.add_argument("--print", action="store_true", dest="dry_run",
                   help="print the resulting config without writing")

    p = sub.add_parser("zcode-plugin",
                       help="write plugins/atta/.mcp.json for this checkout")
    p.add_argument("--root", default=None, help="Atta repo root")
    p.add_argument("--python", default=None, help="Python interpreter")
    p.add_argument("--print", action="store_true", dest="dry_run",
                   help="print the resulting config without writing")

    p = sub.add_parser("dsh", help="print or create the DSH plugin symlink")
    p.add_argument("--link", default=None,
                   help="default: ~/.dsh/plugins/atta")
    p.add_argument("--source", default=None,
                   help="default: <repo>/plugin")
    p.add_argument("--create", action="store_true",
                   help="actually create the symlink")
    p.add_argument("--force", action="store_true",
                   help="replace an existing file/symlink")

    for name in ("claude", "cursor"):
        p = sub.add_parser(name, help="print the %s MCP config snippet" % name)
        p.add_argument("--root", default=None)
        p.add_argument("--python", default=None)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "zcode":
            result = install_zcode(config_path=args.config, root=args.root,
                                   python=args.python, dry_run=args.dry_run)
        elif args.cmd == "zcode-plugin":
            result = install_zcode_plugin(root=args.root, python=args.python,
                                          dry_run=args.dry_run)
        elif args.cmd == "dsh":
            result = install_dsh(link=args.link, source=args.source,
                                 create=args.create, force=args.force)
        elif args.cmd == "claude":
            result = claude_config(root=args.root, python=args.python)
        else:
            result = cursor_config(root=args.root, python=args.python)
    except AttaError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
