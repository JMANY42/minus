"""A loopback HTTP bridge between jellyfin-web and MINUS's torrent search.

jellyfin-web is a static bundle with no server-side hook, so the only way a
"download this" control can appear in it is a script loaded from somewhere
else. This module serves that script and the small JSON API behind it, over a
loopback-only server started by `minus jellyfin serve`.

Three constraints shape it. It is not a second pipeline: search is
`tools.discovery.DiscoveryTools.torrent_search` and a grab replays the harness
sequence (`ensure_running`, `ingest(detonate=True, release=True)`, then
`promote` only for a clean, non-low-confidence pick), so ranking, gating and
quarantine cannot drift. The browser is not trusted: it names an infohash and
the bridge re-runs the search to find that candidate's URL, instead of
downloading whatever URL the request carried. And the token is minted per
process and only ever published in the script body, so a page that did not
load the script cannot reach the API.

The pipeline is built by `assembly.build_triage_pipeline` and injected in
tests, as is discovery: no test here touches a tracker or qBittorrent.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import re
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

# Only a page served from the Jellyfin host itself may talk to the bridge; a
# random website in the same browser must not be able to read the API, even
# though it could still hit loopback. The token is the real boundary; this
# keeps the browser from blocking the legitimate caller during preflight.
_ORIGIN = re.compile(r"\Ahttp://(?:127\.0\.0\.1|localhost):\d+\Z")

# Two grabs in flight is all the pipeline can usefully run at once (one
# qBittorrent add/download/scan at a time); the third request is told to wait
# rather than queued behind arbitrary work.
_MAX_OUTSTANDING = 2
_MAX_BODY = 64 * 1024
# Finished jobs are kept so the UI can still poll after the fact, but the dict
# must not grow for the lifetime of a service that runs for weeks.
_JOBS_KEPT = 64

# The browser client ships under `web/` beside this module, and that is the
# only copy: a second, degraded implementation living in this file would drift
# from the one actually served, and a missing asset is a broken checkout rather
# than a reason to hand the browser something else.
_ASSET = Path(__file__).with_name("web") / "minus.js"


def _asset_js() -> str:
    """The browser client, read once at startup so a missing asset fails there."""
    return _ASSET.read_text(encoding="utf-8")


def _assembly_pipeline(settings: Any) -> Any | None:
    from minus.assembly import build_triage_pipeline

    return build_triage_pipeline(settings)


class Bridge:
    """Serve search/grab for one MINUS process, on loopback only.

    `discovery` and `pipeline_factory` are the two seams the tests use: a fake
    search and a fake pipeline keep every assertion on the wire format rather
    than on a tracker being reachable.
    """

    def __init__(
        self,
        settings: Any,
        host: str = "127.0.0.1",
        port: int = 8099,
        discovery: Any | None = None,
        pipeline_factory: Callable[[], Any | None] | None = None,
        *,
        token: str | None = None,
    ) -> None:
        self.settings = settings
        self.host = host
        self.token = token or secrets.token_urlsafe(32)
        self._discovery = discovery
        self._default_discovery: Any | None = None
        self._pipeline_factory = pipeline_factory or (lambda: _assembly_pipeline(settings))
        self._pipeline_cache: Any | None = None
        self._build_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._active_job: str | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._thread: threading.Thread | None = None
        self._closed = False
        # Proof the asset exists, at construction rather than on the first page
        # load: a broken checkout should fail where the operator is looking.
        self._asset = _asset_js()
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._httpd.daemon_threads = True
        # Port 0 asks the kernel for a free one; report what it picked.
        self.port = int(self._httpd.server_address[1])

    # ---- Lifecycle ----

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        """Start the workers and serve in the background (tests, embedding)."""
        self._start_workers()
        self._thread = threading.Thread(
            target=self._serve_loop,
            name="minus-jellyfin",
            daemon=True,
        )
        self._thread.start()

    def _serve_loop(self) -> None:
        # A short poll interval keeps stop() from waiting half a second per
        # test; the useful work happens on request threads either way.
        self._httpd.serve_forever(poll_interval=0.05)

    def serve_forever(self) -> None:
        """Own the calling thread, for `minus jellyfin serve`."""
        self._start_workers()
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
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=2.0)
        self._workers.clear()

    def _start_workers(self) -> None:
        if self._workers:
            return
        for index in range(_MAX_OUTSTANDING):
            worker = threading.Thread(
                target=self._work,
                name=f"minus-jellyfin-grab-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    # ---- Search and pipeline (the harness's functions, not copies) ----

    def _discovery_tools(self) -> Any:
        if self._discovery is not None:
            return self._discovery
        with self._build_lock:
            if self._default_discovery is None:
                from minus.tools.discovery import DiscoveryTools

                self._default_discovery = DiscoveryTools(self.settings)
            return self._default_discovery

    def _search(self, query: str, limit: int) -> dict[str, Any]:
        payload = self._discovery_tools().torrent_search(query, limit)
        threshold = float(getattr(self.settings, "discovery_min_score", 0.0) or 0.0)
        return {
            "query": str(payload.get("query") or query),
            "kind": str(payload.get("kind") or ""),
            "errors": list(payload.get("errors") or ()),
            "threshold": threshold,
            "candidates": list(payload.get("candidates") or ()),
            "pick": payload.get("pick"),
        }

    def _pipeline(self) -> Any | None:
        with self._build_lock:
            if self._pipeline_cache is None:
                pipeline = self._pipeline_factory()
                if pipeline is None:
                    return None
                # The harness guards this the same way: a pipeline that refuses
                # the hook must not cost us the search half of the bridge.
                try:
                    pipeline.notify = self._notify
                except Exception:
                    logger.debug("Triage pipeline would not take a notify hook", exc_info=True)
                self._pipeline_cache = pipeline
            return self._pipeline_cache

    def _notify(self, kind: str, payload: Any) -> None:
        """Route a real pipeline milestone to the job currently being run."""
        job_id = self._active_job
        if not job_id:
            return
        detail = ""
        try:
            data = dict(payload or {})
        except (TypeError, ValueError):
            data = {}
        verdict = data.get("verdict")
        if isinstance(verdict, dict):
            detail = str(verdict.get("label") or "")
        if not detail:
            for key in ("name", "title", "torrent_hash"):
                if data.get(key):
                    detail = str(data[key])
                    break
        self._update(job_id, stage=str(kind or "working"), detail=detail)

    # ---- Jobs ----

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _public_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "id": job["id"],
                "state": job["state"],
                "stage": job["stage"],
                "detail": job["detail"],
                "result": job["result"],
            }

    def _submit(self, job: dict[str, Any]) -> bool:
        with self._lock:
            outstanding = sum(
                1 for item in self._jobs.values() if item["state"] in ("queued", "running")
            )
            if outstanding >= _MAX_OUTSTANDING:
                return False
            self._jobs[job["id"]] = job
            self._prune_locked()
        self._queue.put(job["id"])
        return True

    def _prune_locked(self) -> None:
        if len(self._jobs) <= _JOBS_KEPT:
            return
        for job_id, item in list(self._jobs.items()):
            if len(self._jobs) <= _JOBS_KEPT:
                break
            if item["state"] in ("done", "error"):
                del self._jobs[job_id]

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                self._run_job(job_id)
            except Exception:  # pragma: no cover - _run_job records its own errors
                logger.exception("Jellyfin bridge worker failed")

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            snapshot = dict(self._jobs.get(job_id) or {})
        if not snapshot:
            return
        with self._run_lock:
            self._active_job = job_id
            try:
                self._update(job_id, state="running", stage="starting")
                pipeline = self._pipeline()
                if pipeline is None:
                    self._update(
                        job_id,
                        state="error",
                        stage="unavailable",
                        detail="qBittorrent is not configured (set MINUS_QBITTORRENT_URL)",
                    )
                    return
                # Starting the downloader belongs in the worker: it waits for
                # the WebUI, and that wait must not hold an HTTP response open.
                if getattr(self.settings, "qbittorrent_url", ""):
                    from minus.scripts.qbt import ensure_running

                    ensure_running(self.settings)
                self._update(job_id, stage="ingest")
                result = pipeline.ingest(
                    snapshot["download_url"],
                    detonate=True,
                    release=True,
                    expected_title=snapshot["query"],
                )
                outcome = {
                    "status": str(result.get("status") or ""),
                    "torrent_hash": str(result.get("torrent_hash") or ""),
                }
                auto_promote = (
                    bool(getattr(self.settings, "discovery_auto_promote", False))
                    and not snapshot["low_confidence"]
                )
                if outcome["status"] == "clean" and auto_promote:
                    self._update(job_id, stage="promote")
                    try:
                        promoted = pipeline.promote(
                            result["torrent_hash"],
                            name=snapshot["title"],
                        )
                    except Exception as exc:
                        outcome["promote_error"] = str(exc)
                    else:
                        outcome["moved_to"] = str(promoted.get("moved_to") or "")
                self._update(
                    job_id,
                    state="done",
                    stage="done",
                    detail=outcome["status"],
                    result=outcome,
                )
            except Exception as exc:
                logger.exception("Jellyfin bridge grab failed")
                self._update(job_id, state="error", stage="failed", detail=str(exc))
            finally:
                self._active_job = None

    # ---- HTTP ----

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
            logger.debug("Jellyfin bridge client disconnected", exc_info=True)
        except Exception as exc:
            logger.exception("Jellyfin bridge request failed")
            try:
                handler.reply_json(500, {"error": str(exc) or "internal error"})
            except Exception:
                logger.debug("Could not send the error response", exc_info=True)

    def _handle_get(self, handler: Any) -> None:
        parsed = urlsplit(handler.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            handler.reply_json(200, {"ok": True})
            return
        if path == "/minus.js":
            handler.reply_js(self._script())
            return
        if path == "/api/search":
            if not self._authorized(handler):
                return
            query = (parse_qs(parsed.query).get("q") or [""])[0]
            query = " ".join(query.split())
            if not query:
                handler.reply_json(400, {"error": "q is required"})
                return
            raw_limit = (parse_qs(parsed.query).get("limit") or ["8"])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                handler.reply_json(400, {"error": "limit must be an integer"})
                return
            handler.reply_json(200, self._search(query, max(1, min(limit, 50))))
            return
        if path.startswith("/api/job/"):
            if not self._authorized(handler):
                return
            job = self._public_job(path[len("/api/job/") :])
            if job is None:
                handler.reply_json(404, {"error": "no such job"})
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
        query = " ".join(str(body.get("query") or "").split())
        infohash = str(body.get("infohash") or "").strip().lower()
        if not query or not infohash:
            handler.reply_json(400, {"error": "query and infohash are required"})
            return
        if self._pipeline() is None:
            handler.reply_json(
                503,
                {"error": "qBittorrent is not configured (set MINUS_QBITTORRENT_URL)"},
            )
            return
        # The request contributes an identity, never a URL: search again and
        # let the authoritative shortlist decide what would be downloaded.
        found = self._search(query, 20)
        candidate = next(
            (
                item
                for item in found["candidates"]
                if str(item.get("infohash") or "").lower() == infohash
            ),
            None,
        )
        if candidate is None:
            handler.reply_json(409, {"error": f"no search result matches infohash {infohash}"})
            return
        download_url = str(candidate.get("download_url") or "")
        if not download_url:
            handler.reply_json(
                409, {"error": "the selected candidate has no magnet or .torrent URL"}
            )
            return
        # The same rule `DiscoveryTools._pick` publishes: below the threshold a
        # pick is a guess, so it is downloaded for back-checking and never
        # auto-promoted.
        low_confidence = float(candidate.get("score") or 0.0) < found["threshold"]
        job = {
            "id": secrets.token_hex(8),
            "state": "queued",
            "stage": "queued",
            "detail": "",
            "result": None,
            "query": query,
            "title": str(candidate.get("title") or query),
            "download_url": download_url,
            "low_confidence": low_confidence,
        }
        if not self._submit(job):
            handler.reply_json(429, {"error": "the grab queue is full; try again shortly"})
            return
        handler.reply_json(202, {"job": job["id"]})

    def _script(self) -> str:
        prefix = (
            f"window.__MINUS_JELLYFIN__ = {{base: {json.dumps(self.base_url)}, "
            f"token: {json.dumps(self.token)}}};\n"
        )
        return prefix + self._asset

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("jellyfin bridge: " + fmt, *args)

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
