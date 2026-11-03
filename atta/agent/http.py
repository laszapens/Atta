# -*- coding: utf-8 -*-
"""HTTP bridge for the ten tools.

    python3 -m atta.agent.http --port 8765

    GET  /health
    GET  /tools
    GET  /manifest?format=openai|anthropic|mcp|generic
    POST /call   {"tool": "...", "args": {...}}
                 {"name": "...", "arguments": {...}}
    POST /mcp    one JSON-RPC 2.0 request

The listener is always 127.0.0.1 and has no authentication: it is a local
agent bridge, not a network service.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from atta.version import VERSION

from .manifest import build_manifest
from .mcp import process_request
from .tools import call_tool, tool_schemas

MAX_BODY = 32 * 1024 * 1024
LOOPBACK_HOST = "127.0.0.1"


def _error(code: str, message: str, *, path: str | None = None,
           fix: str | None = None) -> dict:
    return {"code": code, "message": message, "path": path, "fix": fix}


class Handler(BaseHTTPRequestHandler):
    server_version = "atta-agent/" + VERSION
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - keep stdout clean
        print("[atta.http] " + (fmt % args), file=sys.stderr, flush=True)

    def _send(self, status: int, payload, *, content_type: str =
              "application/json; charset=utf-8") -> None:
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(int(status))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self):
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if length <= 0:
            raise ValueError("empty body")
        if length > MAX_BODY:
            raise ValueError("body too large")
        data = self.rfile.read(length)
        value = json.loads(data.decode("utf-8"))
        return value

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(200, {
                "ok": True,
                "name": "atta",
                "version": VERSION,
                "tools": len(tool_schemas()),
            })
            return
        if url.path == "/tools":
            self._send(200, tool_schemas())
            return
        if url.path == "/manifest":
            fmt = (parse_qs(url.query).get("format") or ["generic"])[0]
            try:
                self._send(200, build_manifest(fmt))
            except ValueError as exc:
                self._send(400, {"ok": False,
                                 "error": _error("bad_format", str(exc),
                                                 path="format",
                                                 fix="openai|anthropic|mcp|generic")})
            return
        self._send(404, {"ok": False,
                         "error": _error("not_found", "not found: %s" % url.path,
                                         path="path", fix="GET /health /tools /manifest")})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        try:
            body = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"ok": False,
                             "error": _error("bad_request", "bad JSON body: %s" % exc,
                                             path="body",
                                             fix="send {tool,args} or {name,arguments}")})
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False,
                             "error": _error("bad_request",
                                             "body must be a JSON object",
                                             path="body",
                                             fix='send {"tool":"atta_doctor","args":{}}')})
            return

        if url.path == "/call":
            tool = body.get("tool") or body.get("name")
            args = body.get("args")
            if args is None:
                args = body.get("arguments")
            if not isinstance(tool, str) or not tool:
                self._send(400, {
                    "ok": False,
                    "tool": None,
                    "data": None,
                    "error": _error("bad_request", "tool/name required",
                                    path="tool",
                                    fix='send {"tool":"atta_doctor","args":{}}'),
                    "ms": 0.0,
                })
                return
            if args is None:
                args = {}
            if not isinstance(args, dict):
                self._send(400, {
                    "ok": False,
                    "tool": tool,
                    "data": None,
                    "error": _error("bad_arguments", "args/arguments must be an object",
                                    path="args", fix='send {"args":{}}'),
                    "ms": 0.0,
                })
                return
            self._send(200, call_tool(tool, args))
            return

        if url.path == "/mcp":
            try:
                response = process_request(body)
            except Exception as exc:  # noqa: BLE001
                self._send(200, {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32603,
                              "message": "internal error: %s" % type(exc).__name__},
                })
                return
            if response is None:
                self._send_empty(202)
            else:
                self._send(200, response)
            return

        self._send(404, {"ok": False,
                         "error": _error("not_found", "not found: %s" % url.path,
                                         path="path", fix="POST /call or POST /mcp")})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Atta HTTP/JSON agent bridge")
    parser.add_argument("--host", default=LOOPBACK_HOST,
                        help="only 127.0.0.1/localhost is accepted")
    parser.add_argument("--port", type=int, default=8765,
                        help="0 picks a free port and prints the real address")
    args = parser.parse_args(argv)

    if args.host not in {LOOPBACK_HOST, "localhost"}:
        raise SystemExit(
            "refusing to bind %r: the Atta bridge is loopback-only" % args.host)

    server = ThreadingHTTPServer((LOOPBACK_HOST, int(args.port)), Handler)
    server.daemon_threads = True
    host, port = server.server_address[0], server.server_address[1]
    print("http://%s:%d" % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
