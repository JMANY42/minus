"""The socket a running MINUS listens on.

Knows nothing about conversations, models or memory. It is handed a dispatch
table of `command -> handler(params)` and a RuntimeState to broadcast, which
keeps every decision about *what* a command means in the composition root
where the object graph lives, and leaves this module testable with a table of
lambdas.

Threads rather than selectors: there is one dashboard, occasionally two, and
a thread each is a great deal easier to read than a poll loop. What matters
is the direction of blocking -- see the writer queue below. Nothing on the
conversation thread may ever wait on a socket.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

from minus.control import protocol
from minus.errors import MinusError

logger = logging.getLogger(__name__)

# Per-connection outbound buffer. Deep enough that a dashboard doing something
# slow does not lose events, shallow enough that one which has stopped reading
# entirely cannot make us hold its backlog.
WRITE_QUEUE_DEPTH = 256

_CLOSE = object()


class ControlServerBusy(MinusError):
    """Another MINUS already owns the socket."""


class ControlServer:
    """Accepts control connections and answers them."""

    def __init__(
        self,
        path: Path,
        handlers: dict[str, Callable[[dict], Any]],
        state: Any | None = None,
    ) -> None:
        self.path = Path(path)
        self.handlers = handlers
        self.state = state

        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._connections: list[_Connection] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._seq = 0

    # ---- Lifecycle ----

    def start(self) -> None:
        """Bind and begin accepting. Raises if another MINUS is already up."""
        self._claim_socket_path()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(8)
        # Best effort: the 0700 parent directory is the real protection, and
        # there is no way to bind atomically with a mode.
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

        self._server = server
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="control-accept", daemon=True
        )
        self._accept_thread.start()

        if self.state is not None:
            self.state.subscribe(self._broadcast_status)

        logger.info("Control socket listening on %s", self.path)

    def _claim_socket_path(self) -> None:
        """Take ownership of the path, or refuse to start.

        A socket file left by a crashed process is indistinguishable from a
        live one by inspection -- so ask it. Anything that answers owns the
        microphone, and a second MINUS fighting it for the device is a failure
        mode worth making impossible.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)

        if not self.path.exists():
            return

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(self.path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            logger.info("Removing a stale control socket at %s", self.path)
            with contextlib.suppress(OSError):
                self.path.unlink()
            return
        else:
            raise ControlServerBusy(
                f"Another MINUS is already listening on {self.path}. "
                "Stop it first, or pass --no-control to run without one."
            )
        finally:
            probe.close()

    def close(self) -> None:
        """Stop accepting, drop every connection, and unlink the socket."""
        if self._closed.is_set():
            return
        self._closed.set()

        server, self._server = self._server, None
        if server is not None:
            # Shut down before closing: a thread parked in accept() is not
            # woken by close() alone on every platform.
            with contextlib.suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                server.close()

        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)

        with contextlib.suppress(OSError):
            self.path.unlink()

    # ---- Accepting ----

    def _accept_loop(self) -> None:
        server = self._server
        while not self._closed.is_set() and server is not None:
            try:
                client, _ = server.accept()
            except OSError:
                if not self._closed.is_set():
                    logger.debug("Control socket accept ended", exc_info=True)
                return

            connection = _Connection(client, self)
            with self._lock:
                self._connections.append(connection)
            connection.start()

    def _forget(self, connection: _Connection) -> None:
        with self._lock:
            if connection in self._connections:
                self._connections.remove(connection)

    # ---- Dispatch ----

    def handle(self, payload: dict) -> dict:
        """Turn one decoded request into one response frame."""
        request_id = payload.get("id")
        command = payload.get("command")
        params = payload.get("params") or {}

        if not isinstance(command, str):
            return protocol.error(request_id, protocol.BAD_PARAMS, "command must be a string")
        if not isinstance(params, dict):
            return protocol.error(request_id, protocol.BAD_PARAMS, "params must be an object")

        handler = self.handlers.get(command)
        if handler is None:
            return protocol.error(
                request_id, protocol.UNKNOWN_COMMAND, f"Unknown command {command!r}"
            )

        try:
            return protocol.response(request_id, handler(params))
        except protocol.ProtocolError as exc:
            return protocol.error(request_id, exc.code, str(exc))
        except (TypeError, ValueError, KeyError) as exc:
            return protocol.error(request_id, protocol.BAD_PARAMS, str(exc))
        except Exception as exc:
            # A failing command must not take the connection, or the
            # assistant, down with it.
            logger.exception("Control command %r failed", command)
            return protocol.error(request_id, protocol.INTERNAL, f"{type(exc).__name__}: {exc}")

    # ---- Events ----

    def _broadcast_status(self) -> None:
        if self.state is None:
            return
        self.broadcast("status", self.state.snapshot())

    def broadcast(self, name: str, data: Any = None) -> None:
        """Send an event to every subscribed connection."""
        with self._lock:
            self._seq += 1
            frame = protocol.event(name, data, self._seq)
            connections = list(self._connections)

        for connection in connections:
            connection.send_event(frame)


class _Connection:
    """One dashboard, with a reader thread and a writer thread.

    Two threads rather than one because the halves block on different things:
    the reader on the socket, the writer on the queue. That separation is what
    lets `send_event` be non-blocking, which is the whole point -- it is
    called from the conversation thread by way of RuntimeState, and that
    thread must never wait on a peer.
    """

    def __init__(self, sock: socket.socket, server: ControlServer) -> None:
        self._socket = sock
        self._server = server
        self._outbox: Queue[Any] = Queue(maxsize=WRITE_QUEUE_DEPTH)
        self._closed = threading.Event()
        self._subscribed = False
        self._dropped = 0

    def start(self) -> None:
        threading.Thread(target=self._write_loop, name="control-writer", daemon=True).start()
        threading.Thread(target=self._read_loop, name="control-reader", daemon=True).start()

    # ---- Outbound ----

    def send(self, frame: dict) -> None:
        """Queue a frame. Never blocks."""
        if self._closed.is_set():
            return
        try:
            self._outbox.put_nowait(frame)
        except Full:
            # A peer this far behind is not going to catch up on a response,
            # and holding the backlog for it helps nobody.
            logger.warning("Control connection is not draining; dropping it")
            self.close()

    def send_event(self, frame: dict) -> None:
        """Queue an event, discarding the oldest if the peer is behind.

        Events are a stream of the current truth, and `status` carries a full
        snapshot rather than a delta, so the newest one makes every older one
        redundant. Losing the stale end of the queue is the right thing to
        lose.
        """
        if not self._subscribed or self._closed.is_set():
            return
        try:
            self._outbox.put_nowait(frame)
        except Full:
            self._dropped += 1
            with contextlib.suppress(Empty):
                self._outbox.get_nowait()
            with contextlib.suppress(Full):
                self._outbox.put_nowait(frame)

    def _write_loop(self) -> None:
        while True:
            frame = self._outbox.get()
            if frame is _CLOSE:
                break
            try:
                self._socket.sendall(protocol.encode(frame))
            except OSError:
                break
        self.close()

    # ---- Inbound ----

    def _read_loop(self) -> None:
        buffer = b""
        try:
            while not self._closed.is_set():
                try:
                    chunk = self._socket.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return

                buffer += chunk
                if len(buffer) > protocol.MAX_LINE_BYTES:
                    self.send(
                        protocol.error(
                            None, protocol.OVERSIZED, "Frame exceeds the maximum line length"
                        )
                    )
                    return

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._handle_line(line)
        finally:
            self.close()

    def _handle_line(self, line: bytes) -> None:
        try:
            payload = protocol.decode(line)
        except protocol.ProtocolError as exc:
            self.send(protocol.error(None, exc.code, str(exc)))
            return

        # `subscribe` is the connection's own business rather than a handler's:
        # it is about this socket, not about the assistant.
        if payload.get("command") == "subscribe":
            self._subscribed = True
            state = self._server.state
            self.send(protocol.response(payload.get("id"), {"subscribed": True}))
            if state is not None:
                self.send_event(protocol.event("status", state.snapshot()))
            return

        self.send(self._server.handle(payload))

    # ---- Teardown ----

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()

        if self._dropped:
            logger.info("Dropped %d events to a slow control connection", self._dropped)

        with contextlib.suppress(Full):
            self._outbox.put_nowait(_CLOSE)
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._socket.close()

        self._server._forget(self)
