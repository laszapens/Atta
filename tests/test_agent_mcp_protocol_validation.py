from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from atta.agent import mcp


def test_missing_or_non_string_method_is_invalid_request():
    for request in (
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "method": 7},
        {"jsonrpc": "2.0", "id": 1, "method": ""},
    ):
        response = mcp.process_request(request)
        assert response["error"]["code"] == -32600
        assert "method" in response["error"]["message"]


def test_non_object_params_are_invalid_params():
    for params in ([], "wrong", 0):
        response = mcp.process_request({
            "jsonrpc": "2.0", "id": 1, "method": "ping", "params": params,
        })
        assert response["error"]["code"] == -32602


def test_null_params_is_equivalent_to_absent():
    """§4.2: params MAY be omitted; explicit null carries no params either."""
    for request in (
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None},
    ):
        assert mcp.process_request(request)["result"] == {}


def test_object_params_and_notifications_keep_existing_contract():
    assert mcp.process_request({
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
    })["result"] == {}
    assert mcp.process_request({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }) is None


def test_malformed_notifications_never_produce_a_response():
    """§4.1: a notification must never be answered, even when it is malformed.

    Returning an error for a request the client did not ask a reply for puts an
    unsolicited message on the wire and desynchronises strict clients.
    """
    for request in (
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": []},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": None},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": "x"},
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "method": ""},
        {"jsonrpc": "2.0", "method": 7},
        {"jsonrpc": "1.0", "method": "notifications/initialized"},
    ):
        assert mcp.process_request(request) is None, request


def test_malformed_notifications_write_nothing_to_the_wire(monkeypatch):
    emitted = []
    monkeypatch.setattr(mcp, "_write", emitted.append)
    with ThreadPoolExecutor(max_workers=1) as executor:
        for line in (
            '{"jsonrpc":"2.0","method":"notifications/initialized","params":[]}',
            '{"jsonrpc":"2.0"}',
            '[{"jsonrpc":"2.0","method":"notifications/initialized","params":[]}]',
        ):
            mcp._serve_line(line, executor, [])
    assert emitted == []


def test_empty_batch_emits_invalid_request_error(monkeypatch):
    emitted = []
    monkeypatch.setattr(mcp, "_write", emitted.append)
    with ThreadPoolExecutor(max_workers=1) as executor:
        mcp._serve_line("[]", executor, [])
    assert emitted == [{
        "jsonrpc": "2.0", "id": None,
        "error": {"code": -32600, "message": "invalid request: empty batch"},
    }]


def test_notification_only_batch_still_has_no_response(monkeypatch):
    emitted = []
    monkeypatch.setattr(mcp, "_write", emitted.append)
    with ThreadPoolExecutor(max_workers=1) as executor:
        mcp._serve_line(
            '[{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}]',
            executor, [],
        )
    assert emitted == []
