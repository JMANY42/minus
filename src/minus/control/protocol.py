"""The wire format between MINUS and whatever is managing it.

Newline-delimited JSON, one object per line. That works because
`json.dumps(indent=None)` never emits a literal newline -- every newline
inside a string is escaped as `\\n` -- so a raw `\\n` is an unambiguous frame
terminator and framing needs no length prefix and no escaping pass.

Three envelope types, distinguished by `type`:

    request   dashboard -> MINUS, carries an `id`
    response  MINUS -> dashboard, echoes that `id`
    event     MINUS -> dashboard, unsolicited, no `id`

Pure functions and constants only. Nothing here opens a socket, so a test can
exercise the codec without one, and the dashboard and the assistant share the
definition rather than each writing their own half.
"""

from __future__ import annotations

import json
from typing import Any

from minus.errors import MinusError

PROTOCOL_VERSION = 1

# A frame longer than this is refused rather than buffered. Nothing legitimate
# comes close -- the largest real frame is a status snapshot at a few hundred
# bytes -- so a line this long means a peer that has lost framing, and reading
# it to its end would mean letting that peer choose our memory use.
MAX_LINE_BYTES = 1 << 20

REQUEST = "request"
RESPONSE = "response"
EVENT = "event"

# Error codes. A closed set, so the dashboard can branch on them without
# matching on human-readable text that is free to change.
UNSUPPORTED_VERSION = "unsupported_version"
MALFORMED = "malformed"
OVERSIZED = "oversized"
UNKNOWN_COMMAND = "unknown_command"
BAD_PARAMS = "bad_params"
INTERNAL = "internal"


class ProtocolError(MinusError):
    """A frame that could not be understood.

    Carries a `code` from the set above so the receiving end can answer with a
    structured error rather than dropping the connection and leaving the peer
    to guess why.
    """

    def __init__(self, message: str, code: str = MALFORMED) -> None:
        super().__init__(message)
        self.code = code


def encode(payload: dict) -> bytes:
    """One frame, ready to write."""
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return line.encode("utf-8") + b"\n"


def decode(line: bytes | str) -> dict:
    """Parse one frame, or raise ProtocolError.

    Enforces the version here rather than at each call site: a peer speaking a
    future dialect is a single failure with one clear message, not a scatter of
    KeyErrors deeper in.
    """
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError(f"Frame exceeds {MAX_LINE_BYTES} bytes", OVERSIZED)
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"Frame is not UTF-8: {exc}") from exc

    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"Frame is not JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("Frame is not a JSON object")

    version = payload.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"Unsupported protocol version {version!r}; this build speaks {PROTOCOL_VERSION}",
            UNSUPPORTED_VERSION,
        )

    return payload


def request(request_id: str, command: str, params: dict | None = None) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": REQUEST,
        "id": request_id,
        "command": command,
        "params": params or {},
    }


def response(request_id: str | None, result: Any = None) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": RESPONSE,
        "id": request_id,
        "ok": True,
        "result": result,
    }


def error(request_id: str | None, code: str, message: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": RESPONSE,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def event(name: str, data: Any = None, seq: int = 0) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": EVENT,
        "event": name,
        "seq": seq,
        "data": data,
    }
