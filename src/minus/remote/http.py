"""Loopback HTTP in front of a `DownloadService`.

Everything here is transport: parsing a query string, checking the token,
choosing a status code. The search/grab/job semantics live in `service.py`, so
the only thing this module adds to an in-process caller is the wire format the
browser client is written against.

The asset route serves one file from the resolved frontend, prefixed with the
`window.__MINUS_REMOTE__` config object. That prefix is why the same immutable
JS can talk to any bridge: the page learns the base URL, the per-process token
and which frontend it is from the same response that loads it.

`/api/job/<id>` serializes the service's job dict verbatim: progress, ETA,
timing and the event trace are part of that frozen shape, and the transport
neither invents nor filters any of them.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from minus.remote.frontends import Frontend, get_frontend
from minus.remote.service import DownloadError, DownloadService

logger = logging.getLogger(__name__)

# Only a page served from the machine itself may talk to the bridge; a random
# website in the same browser must not be able to read the API, even though it
# could still hit loopback. The token is the real boundary; this keeps the
# browser from blocking the legitimate caller during preflight.
_ORIGIN = re.compile(r"\Ahttp://(?:127\.0\.0\.1|localhost):\d+\Z")

_MAX_BODY = 64 * 1024


def _resolve_frontend(frontend: Frontend | str | None) -> Frontend:
    if frontend is None:
        return get_frontend("jellyfin")
    if isinstance(frontend, str):
        return get_frontend(frontend)
    return frontend


class HttpBridge:
    """Serve one `DownloadService` on loopback.

    The service is a constructor argument rather than something built here,
    because that is the seam: the same object can back an in-process caller and
    this socket at once, and tests can hand in a service with fake search and
    pipeline. The listening socket, token and frontend belong to the transport.
    """

    def __init__(
        self,
        service: DownloadService,
        host: str = "127.0.0.1",
        port: int = 8099,
        token: str | None = None,
        frontend: Frontend | str | None = None,
    ) -> None:
        self.service = service
        self.host = host
        self.token = token or secrets.token_urlsafe(32)
        self.frontend = _resolve_frontend(frontend)
        self._thread: threading.Thread | None = None
        self._closed = False
        # Proof the asset exists, at construction rather than on the first page
        # load: a broken checkout should fail where the operator is looking.
        self._asset = self.frontend.asset_path().read_text(encoding="utf-8")
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._httpd.daemon_threads = True
        # Port 0 asks the kernel for a free one; report what it picked.
        self.port = int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the service's workers and serve in the background (tests, embedding)."""
        self.service.start()
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve_loop,
            name="minus-remote-bridge",
            daemon=True,
        )
        self._thread.start()

    def _serve_loop(self) -> None:
        # A short poll interval keeps stop() from waiting half a second per
        # test; the useful work happens on request threads either way.
        self._httpd.serve_forever(poll_interval=0.05)

    def serve_forever(self) -> None:
        """Own the calling thread, for `minus bridge serve`."""
        self.service.start()
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread is not None and self._thread.is_alive():
            self._httpd.shutdown()
            self._thread.join(timeout=5.0)
        self._thread = None
        self._httpd.server_close()
        self.service.stop()

    # ---- Routing ----

    def _authorized(self, handler: Any) -> bool:
        supplied = handler.headers.get("X-Minus-Token") or ""
        if hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8")):
            return True
        handler.reply_json(401, {"error": "unauthorized"})
        return False

    def _dispatch(self, handler: Any, method: str) -> None:
        try:
            if method == "GET":
                self._handle_get(handler)
            else:
                self._handle_post(handler)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Remote bridge client disconnected", exc_info=True)
        except Exception as exc:
            logger.exception("Remote bridge request failed")
            try:
                handler.reply_json(500, {"error": str(exc) or "internal error"})
            except Exception:
                logger.debug("Could not send the error response", exc_info=True)

    def _handle_get(self, handler: Any) -> None:
        parsed = urlsplit(handler.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            handler.reply_json(200, {"ok": True, "frontend": self.frontend.name})
            return
        if path == "/minus.js":
            handler.reply_js(self._script())
            return
        if path == "/api/search":
            if not self._authorized(handler):
                return
            params = parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            raw_limit = (params.get("limit") or ["8"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                handler.reply_json(400, {"error": "limit must be an integer"})
                return
            try:
                envelope = self.service.search(query, limit)
            except DownloadError as exc:
                handler.reply_json(exc.status, {"error": str(exc)})
                return
            handler.reply_json(200, envelope)
            return
        if path.startswith("/api/job/"):
            if not self._authorized(handler):
                return
            try:
                job = self.service.job(path[len("/api/job/") :])
            except DownloadError as exc:
                handler.reply_json(exc.status, {"error": str(exc)})
                return
            handler.reply_json(200, job)
            return
        handler.reply_json(404, {"error": "not found"})

    def _handle_post(self, handler: Any) -> None:
        path = urlsplit(handler.path).path.rstrip("/") or "/"
        if path != "/api/grab":
            handler.reply_json(404, {"error": "not found"})
            return
        if not self._authorized(handler):
            return
        raw = handler.read_body()
        if len(raw) > _MAX_BODY:
            # The rest of the body is unread, so this connection cannot be
            # reused for another request.
            handler.close_connection = True
            handler.reply_json(413, {"error": "body too large"})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            handler.reply_json(400, {"error": "body must be JSON"})
            return
        if not isinstance(body, dict):
            handler.reply_json(400, {"error": "body must be a JSON object"})
            return
        query = str(body.get("query") or "")
        infohash = str(body.get("infohash") or "")
        try:
            job_id = self.service.grab(query, infohash)
        except DownloadError as exc:
            handler.reply_json(exc.status, {"error": str(exc)})
            return
        handler.reply_json(202, {"job": job_id})

    def _script(self) -> str:
        prefix = (
            f"window.__MINUS_REMOTE__ = {{base: {json.dumps(self.base_url)}, "
            f"token: {json.dumps(self.token)}, "
            f"frontend: {json.dumps(self.frontend.name)}}};\n"
        )
        return prefix + self._asset

    # ---- Request handler ----

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("remote bridge: " + fmt, *args)

            def do_GET(self) -> None:  # the base class names this hook
                bridge._dispatch(self, "GET")

            def do_POST(self) -> None:  # the base class names this hook
                bridge._dispatch(self, "POST")

            def do_OPTIONS(self) -> None:  # the base class names this hook
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _cors(self) -> None:
                origin = self.headers.get("Origin") or ""
                if not _ORIGIN.match(origin):
                    return
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "content-type, x-minus-token")

            def reply_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def reply_js(self, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def read_body(self) -> bytes:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return b""
                if length <= 0:
                    return b""
                return self.rfile.read(min(length, _MAX_BODY + 1))

        return _Handler
