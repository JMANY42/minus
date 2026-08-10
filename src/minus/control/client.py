"""Talking to a running MINUS.

Used by `minus say` and `minus status`, which are one request each, and by the
dashboard, which holds a connection open for events. Both go through this so
there is one place that knows how a response is correlated to its request and
what a missing socket means.

The client never unlinks the socket file. It does not own it, and a client
that tidies up after what it assumes is a dead server will eventually delete a
live one's socket during a slow start.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minus.control import protocol
from minus.errors import MinusError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


class NotRunning(MinusError):
    """Nothing is listening on the control socket."""


class ControlError(MinusError):
    """The assistant answered, and the answer was a refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ControlClient:
    """A connection to the assistant's control socket."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.on_event = on_event

        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, dict] = {}
        self._arrived = threading.Condition()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None

    # ---- Lifecycle ----

    def connect(self) -> ControlClient:
        """Open the connection, or raise NotRunning with a usable reason."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.path))
        except FileNotFoundError as exc:
            sock.close()
            raise NotRunning(f"MINUS is not running (no socket at {self.path})") from exc
        except ConnectionRefusedError as exc:
            sock.close()
            raise NotRunning(f"MINUS is not running (stale socket at {self.path})") from exc
        except PermissionError as exc:
            sock.close()
            raise NotRunning(f"The control socket at {self.path} belongs to another user") from exc
        except OSError as exc:
            sock.close()
            raise NotRunning(f"Cannot reach MINUS at {self.path}: {exc}") from exc

        # Cleared once connected: the reader blocks indefinitely by design, and
        # per-request deadlines are enforced on the condition variable instead.
        sock.settimeout(None)
        self._socket = sock
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, name="control-client", daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        sock, self._socket = self._socket, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

        # Wake anything waiting on a reply that is never coming.
        with self._arrived:
            self._arrived.notify_all()

    def __enter__(self) -> ControlClient:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- Requests ----

    def request(self, command: str, **params: Any) -> Any:
        """Send one command and wait for its answer."""
        if self._socket is None:
            raise NotRunning("Not connected")

        request_id = str(next(self._ids))
        frame = protocol.request(request_id, command, params)

        with self._lock:
            sock = self._socket
            if sock is None:
                raise NotRunning("Not connected")
            try:
                sock.sendall(protocol.encode(frame))
            except OSError as exc:
                raise NotRunning(f"Lost the connection to MINUS: {exc}") from exc

        return self._await(request_id)

    def _await(self, request_id: str) -> Any:
        with self._arrived:
            deadline_passed = not self._arrived.wait_for(
                lambda: request_id in self._pending or self._closed.is_set(),
                timeout=self.timeout,
            )
            payload = self._pending.pop(request_id, None)

        if payload is None:
            if deadline_passed:
                raise NotRunning(f"MINUS did not answer {request_id} within {self.timeout}s")
            raise NotRunning("The connection to MINUS closed before it answered")

        if payload.get("ok"):
            return payload.get("result")

        failure = payload.get("error") or {}
        raise ControlError(failure.get("code", "unknown"), failure.get("message", ""))

    def subscribe(self) -> Any:
        """Ask to receive events as well as responses."""
        return self.request("subscribe")

    # ---- Reading ----

    def _read_loop(self) -> None:
        buffer = b""
        sock = self._socket
        try:
            while not self._closed.is_set() and sock is not None:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return

                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._dispatch(line)
        finally:
            self.close()

    def _dispatch(self, line: bytes) -> None:
        try:
            payload = protocol.decode(line)
        except protocol.ProtocolError:
            logger.warning("Discarding an unreadable frame from MINUS", exc_info=True)
            return

        if payload.get("type") == protocol.EVENT:
            if self.on_event is not None:
                try:
                    self.on_event(payload)
                except Exception:
                    logger.exception("Control event handler raised")
            return

        request_id = payload.get("id")
        if request_id is None:
            # An error with no id -- a malformed frame we sent, most likely.
            logger.warning("MINUS reported: %s", payload.get("error"))
            return

        with self._arrived:
            self._pending[str(request_id)] = payload
            self._arrived.notify_all()
