"""The MCP server's wire behaviour, without spawning a process.

`handle` is the whole protocol surface: if these pass, a client that speaks
JSON-RPC over stdio gets the same answers. The last test drives `serve` through
StringIO so framing, blank lines and parse errors are covered too.
"""

from __future__ import annotations

import io
import json

from minus.config import Settings
from minus.mcp.server import McpServer, build_registry, serve


def _server() -> McpServer:
    settings = Settings(
        qbittorrent_url="",
        airvpn_username="",
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
    )
    return McpServer(build_registry(settings))


def _request(server: McpServer, method: str, params: dict | None = None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return server.handle(payload)


def test_initialize_negotiates_a_supported_version():
    server = _server()

    reply = _request(
        server,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )

    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "minus"
    assert "tools" in reply["result"]["capabilities"]
    assert "quarantine" in reply["result"]["instructions"]


def test_unknown_protocol_version_falls_back_to_the_newest():
    server = _server()
    reply = _request(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_is_the_registry_in_mcp_shape():
    server = _server()

    reply = _request(server, "tools/list")
    tools = {tool["name"]: tool for tool in reply["result"]["tools"]}

    assert "get_current_time" in tools
    schema = tools["get_current_time"]["inputSchema"]
    assert schema["type"] == "object"
    assert "properties" in schema
    assert "function" not in tools["get_current_time"]


def test_a_tool_call_returns_text_content():
    server = _server()

    reply = _request(server, "tools/call", {"name": "get_current_time", "arguments": {}})

    assert reply["result"]["isError"] is False
    text = reply["result"]["content"][0]["text"]
    assert json.loads(text)


def test_unknown_tool_is_an_error_result_not_a_crash():
    server = _server()

    reply = _request(server, "tools/call", {"name": "no_such_tool", "arguments": {}})

    assert reply["result"]["isError"] is True
    assert "no_such_tool" in reply["result"]["content"][0]["text"]


def test_bad_tool_arguments_are_an_error_result():
    server = _server()

    reply = _request(
        server,
        "tools/call",
        {"name": "get_current_time", "arguments": {"timezone": 42}},
    )

    assert reply["result"]["isError"] is True


def test_unknown_method_is_method_not_found():
    reply = _request(_server(), "tools/teleport")

    assert reply["error"]["code"] == -32601


def test_notifications_get_no_reply():
    assert _server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_and_empty_resource_lists():
    server = _server()
    assert _request(server, "ping")["result"] == {}
    assert _request(server, "resources/list")["result"] == {"resources": []}
    assert _request(server, "prompts/list")["result"] == {"prompts": []}


def test_serve_frames_replies_and_survives_bad_json():
    stdin = io.StringIO(
        "\n"
        '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n'
        "not json\n"
        '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n'
    )
    stdout = io.StringIO()

    serve(_server(), stdin=stdin, stdout=stdout)

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == [1, None, 2]
    assert lines[1]["error"]["code"] == -32700
    assert "tools" in lines[2]["result"]
