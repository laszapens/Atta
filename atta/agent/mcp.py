# -*- coding: utf-8 -*-
"""MCP stdio server (JSON-RPC 2.0, one message per line).

    python3 -m atta.agent.mcp

stdout carries protocol JSON only.  Diagnostics go to stderr.  Tool calls run
on a small thread pool so a long ``atta_preview`` does not block ``ping`` or
``tools/list``.
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from atta.version import VERSION

from .tools import call_tool, images_for_result, tool_schemas

SERVER_NAME = "atta"
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL = "2024-11-05"
MAX_IMAGES = 3

_WRITE_LOCK = threading.Lock()


def _log(*parts) -> None:
    print("[atta.mcp]", *parts, file=sys.stderr, flush=True)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _write(message: dict) -> None:
    line = _dumps(message)
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _ok(req_id, result) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id, code: int, message: str, data=None) -> None:
    error = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    _write({"jsonrpc": "2.0", "id": req_id, "error": error})


def _initialize(params: dict) -> dict:
    requested = (params or {}).get("protocolVersion")
    version = requested if requested in PROTOCOLS else DEFAULT_PROTOCOL
    return {
        "protocolVersion": version,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {},
            "prompts": {},
            "logging": {},
        },
        "serverInfo": {"name": SERVER_NAME, "version": VERSION},
    }


def _tool_result(params: dict) -> dict:
    name = (params or {}).get("name")
    arguments = (params or {}).get("arguments")
    if not isinstance(name, str) or not name:
        return {
            "content": [{"type": "text", "text": "tools/call requires a name"}],
            "isError": True,
        }
    result = call_tool(name, arguments if isinstance(arguments, dict) else {})
    content = [{
        "type": "text",
        "text": json.dumps(result, ensure_ascii=False, indent=2, default=str),
    }]
    for image in images_for_result(result, MAX_IMAGES):
        content.append({
            "type": "image",
            "data": image["data_base64"],
            "mimeType": image["mime_type"],
        })
    return {"content": content, "isError": not bool(result.get("ok"))}


def process_request(req):
    """Pure JSON-RPC dispatch.  Returns a response dict or ``None``.

    ``None`` means "no response": notifications, including
    ``notifications/initialized``.
    """
    if not isinstance(req, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "invalid request: expected object"},
        }

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if req.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "invalid request: jsonrpc must be '2.0'"},
        }

    is_notification = "id" not in req
    if is_notification:
        # JSON-RPC notifications never receive a response, including
        # notifications/initialized and notifications/cancelled.
        return None

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": _initialize(params)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"tools": tool_schemas()}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": req_id, "result": _tool_result(params)}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    if method == "logging/setLevel":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "method not found: %r" % (method,)},
    }


def _serve_line(line: str, executor: ThreadPoolExecutor, futures: list) -> None:
    line = line.strip()
    if not line:
        return
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        _err(None, -32700, "parse error: %s" % exc)
        return

    if isinstance(req, list):
        responses = []
        for item in req:
            try:
                resp = process_request(item)
            except Exception as exc:  # noqa: BLE001
                _log("batch item failed:", type(exc).__name__, exc)
                resp = {
                    "jsonrpc": "2.0",
                    "id": item.get("id") if isinstance(item, dict) else None,
                    "error": {"code": -32603, "message": "internal error"},
                }
            if resp is not None:
                responses.append(resp)
        if responses:
            _write(responses)
        return

    if not isinstance(req, dict):
        _err(None, -32600, "invalid request")
        return

    if req.get("method") == "tools/call" and "id" in req:
        def worker(payload=req):
            try:
                resp = process_request(payload)
            except Exception as exc:  # noqa: BLE001
                _log("tools/call failed:", type(exc).__name__, exc)
                resp = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": "tool failed: %s: %s"
                                    % (type(exc).__name__, exc),
                        }],
                        "isError": True,
                    },
                }
            if resp is not None:
                _write(resp)

        futures.append(executor.submit(worker))
        return

    resp = process_request(req)
    if resp is not None:
        _write(resp)


def serve() -> int:
    # One worker preserves JSON-RPC arrival order. Modeling is stateful:
    # a verify that overtakes its model would read an older workspace.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atta-mcp")
    futures: list = []
    try:
        for line in sys.stdin:
            _serve_line(line, executor, futures)
    except KeyboardInterrupt:
        pass
    finally:
        # EOF may arrive before a concurrently submitted tool call finishes;
        # drain it so batch clients (and tests) see the response.
        executor.shutdown(wait=True)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(VERSION, file=sys.stderr)
        return 0
    if "--help" in argv or "-h" in argv:
        print(__doc__, file=sys.stderr)
        return 0
    _log("starting stdio server v%s" % VERSION)
    return serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
