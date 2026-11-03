# -*- coding: utf-8 -*-
"""``python3 -m atta.agent`` — the generic agent adapter entry point.

    python3 -m atta.agent manifest --format openai|anthropic|mcp|generic
    python3 -m atta.agent call <tool> --args '<json>' | --args-file <json>
    python3 -m atta.agent mcp
    python3 -m atta.agent http --port 8765
"""

from __future__ import annotations

import argparse
import json


def _load_json(args) -> dict:
    if args.args_file:
        if args.args_file == "-":
            import sys
            value = json.load(sys.stdin)
        else:
            with open(args.args_file, "r", encoding="utf-8") as fh:
                value = json.load(fh)
    elif args.args:
        value = json.loads(args.args)
    else:
        value = {}
    if not isinstance(value, dict):
        raise SystemExit("--args/--args-file must contain a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atta.agent", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("manifest", help="print a tool manifest")
    p.add_argument("--format", default="generic",
                   choices=["openai", "anthropic", "mcp", "generic"])
    p.add_argument("--out", default=None, help="write to a file instead of stdout")

    p = sub.add_parser("call", help="call one tool synchronously")
    p.add_argument("tool")
    p.add_argument("--args", default=None)
    p.add_argument("--args-file", default=None)
    p.add_argument("--timeout", type=float, default=None)

    sub.add_parser("mcp", help="MCP stdio server")

    p = sub.add_parser("http", help="HTTP JSON bridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "manifest":
        from .manifest import build_manifest
        text = json.dumps(build_manifest(args.format), ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 0

    if args.cmd == "call":
        from .tools import call_tool
        out = call_tool(args.tool, _load_json(args), timeout=args.timeout)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    if args.cmd == "mcp":
        from .mcp import main as mcp_main
        return mcp_main([])

    if args.cmd == "http":
        from .http import main as http_main
        return http_main(["--host", args.host, "--port", str(args.port)])

    raise SystemExit("unknown command: %r" % (args.cmd,))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
