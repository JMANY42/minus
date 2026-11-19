"""A Model Context Protocol server over stdio, exposing MINUS's tool registry.

The point is portability: every tool MINUS already builds -- filesystem reads,
alarms, Google, torrent triage, AirVPN -- becomes callable by any MCP client
(Claude Code, an editor, another agent) without that client learning MINUS's
Python API. There is no second tool definition here; `ToolRegistry.schemas()`
is the source of truth and `ToolRegistry.dispatch()` is the implementation, so
a tool added to the registry appears here on the next launch.

The transport is deliberately dependency-free. MCP's stdio transport is
newline-delimited JSON-RPC 2.0, and the whole of what this server needs is
`initialize`, `tools/list`, `tools/call` and `ping`; pulling in the SDK would
add a dependency to the core install for a few hundred lines of framing. It
also means the server can run from a bare checkout: `python -m minus.mcp`.

Rules that matter for a stdio server:

* stdout is the wire. Nothing else may write there -- logs go to stderr, and a
  tool that prints is not a tool this server can offer correctly. Tool bodies
  already return values rather than printing.
* a malformed line is answered with a parse error, not a traceback;
* a tool failure is a JSON-RPC *result* with `isError: true`, because MCP says
  so and because the model should be able to read and correct it;
* unknown methods get `-32601`, not a crash.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from minus.errors import MinusError
from minus.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SERVER_NAME = "minus"
SERVER_VERSION = "0.1.0"

# Newest first. A client that asks for one of these gets it echoed; anything
# else gets the newest, and the client decides whether it can proceed.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "MINUS tools: filesystem reads are confined to MINUS_PROJECT_ROOT. Torrent "
    "tools only see torrents already added to qBittorrent; downloads are staged "
    "in quarantine, statically inspected, reviewed by the model, and only a "
    "clean verdict can be promoted. vpn_connect and vpn_disconnect drive the "
    "user's AirVPN account."
)


@dataclass
class McpServer:
    """One registry, one request handler, no global state."""

    registry: ToolRegistry
    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    initialized: bool = False
    _tools: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Built once: `schemas()` sorts and copies, and the list is stable for
        # the process lifetime (a tool is not added while a request is served).
        self._tools = {entry["function"]["name"]: entry for entry in self.registry.schemas()}

    # ---- JSON-RPC plumbing ----

    @staticmethod
    def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle(self, message: Any) -> dict | None:
        """Handle one decoded JSON-RPC message. Notifications return None."""
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "message must be an object")
        if message.get("jsonrpc") != "2.0":
            return self._error(message.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")

        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            return (
                None
                if is_notification
                else self._error(request_id, INVALID_REQUEST, "method must be a string")
            )

        raw_params = message.get("params")
        if raw_params is not None and not isinstance(raw_params, dict):
            return (
                None
                if is_notification
                else self._error(request_id, INVALID_PARAMS, "params must be an object")
            )
        params: dict[str, Any] = raw_params or {}

        try:
            result = self._dispatch_method(method, params)
        except Exception as exc:  # a server bug must not kill the session
            logger.exception("MCP method %s failed", method)
            return None if is_notification else self._error(request_id, INTERNAL_ERROR, str(exc))

        if is_notification:
            return None
        if result is _NO_RESULT:
            return self._error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
        return self._result(request_id, result)

    def _dispatch_method(self, method: str, params: dict) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            if requested in PROTOCOL_VERSIONS:
                self.protocol_version = requested
            self.initialized = True
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }

        if method == "notifications/initialized" or method.startswith("notifications/"):
            return {}

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": [self._to_mcp_tool(entry) for entry in self._tools.values()]}

        if method == "tools/call":
            return self._call_tool(params)

        if method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {
                "resources/list": "resources",
                "resources/templates/list": "resourceTemplates",
                "prompts/list": "prompts",
            }[method]
            return {key: []}

        if method == "logging/setLevel":
            return {}

        return _NO_RESULT

    # ---- Tool surface ----

    @staticmethod
    def _to_mcp_tool(entry: dict) -> dict:
        """OpenAI's function schema to MCP's `Tool` object.

        The arguments object is already JSON Schema; MCP wants it as
        `inputSchema` rather than under `function.parameters`, which is the
        only difference.
        """
        function = entry["function"]
        return {
            "name": function["name"],
            "description": function.get("description", ""),
            "inputSchema": function.get("parameters") or {"type": "object", "properties": {}},
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return {
                "content": [{"type": "text", "text": "tools/call needs a tool name"}],
                "isError": True,
            }

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "content": [{"type": "text", "text": "tools/call arguments must be an object"}],
                "isError": True,
            }

        try:
            text = self.registry.dispatch(name, arguments)
        except MinusError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # dispatch already wraps most of these
            logger.exception("Tool %s failed", name)
            return {"content": [{"type": "text", "text": f"{name}: {exc}"}], "isError": True}

        return {"content": [{"type": "text", "text": text}], "isError": False}


class _NoResult:
    """Sentinel: method unknown, as distinct from a method returning {}."""


_NO_RESULT = _NoResult()


def build_registry(settings: Any) -> ToolRegistry:
    """Every portable tool, built without a model or a conversation.

    `escalate` is absent on purpose: it needs a live conversation to snapshot
    and a queue to answer on, which is not a capability a stateless MCP call
    can offer. The coding tools are absent too: they are an approval-gated
    workspace shell, and an MCP client has no MINUS approval UI behind it.
    """
    from minus.assembly import (
        build_alarm_tools,
        build_google_tools,
        build_triage_tools,
        build_vpn_tools,
    )
    from minus.tools import registry as builtin

    registry = ToolRegistry()
    for name in builtin.names():
        registry.add_tool(builtin.get(name))

    groups = [
        *build_google_tools(settings),
        build_alarm_tools(settings),
        *build_triage_tools(settings),
        *build_vpn_tools(settings),
    ]
    for group in groups:
        group.register(registry)
    return registry


def serve(server: McpServer, stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC until EOF, writing one line per reply."""
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout

    for line in input_stream:
        line = line.strip()
        if not line:
            continue
        reply: dict | None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            reply = McpServer._error(None, PARSE_ERROR, f"Invalid JSON: {exc.msg}")
        else:
            reply = server.handle(message)
        if reply is not None:
            output_stream.write(json.dumps(reply, ensure_ascii=False) + "\n")
            output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    """Start the server on stdin/stdout. Any argument is ignored for now."""
    from minus.config import load_settings
    from minus.logging_config import setup_logging

    # Log to stderr, never stdout: stdout is the protocol.
    setup_logging(level="WARNING", console_level="WARNING", retention=5)
    settings = load_settings()
    registry = build_registry(settings)
    logger.info("MCP server offering %d tools", len(registry.schemas()))
    serve(McpServer(registry))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
