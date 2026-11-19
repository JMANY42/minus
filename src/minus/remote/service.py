"""Search, grab and job bookkeeping for MINUS's remote bridge.

This is everything the old HTTP handler knew how to do, minus the HTTP. That
split exists because the bridge is no longer only for Jellyfin: another agent
in this process may want the same search/grab machinery, and an in-process
caller should not have to open a loopback socket (or mint a token) to get it.

The transport is `http.py`; nothing here imports it. The seam is an exception
that carries the status code the transport should print, so a bad request
means the same thing whether it arrived over a socket or from a local caller.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from minus.errors import MinusError

logger = logging.getLogger(__name__)

# Two grabs in flight is all the pipeline can usefully run at once (one
# qBittorrent add/download/scan at a time); the third request is told to wait
# rather than queued behind arbitrary work.
_MAX_OUTSTANDING = 2
# Finished jobs are kept so the UI can still poll after the fact, but the dict
# must not grow for the lifetime of a service that runs for weeks.
_JOBS_KEPT = 64

# Progress is a single 0..1 number shared by the job dict and the UI; the
# pipeline's stages are made visible at fixed points so a toast moves even
# when qBittorrent is slow to report a fraction. The download span is capped
# below the staged milestone so a late probe can never walk the bar backwards.
_PROGRESS_STARTED = 0.02
_PROGRESS_ADDED = 0.05
_PROGRESS_DOWNLOAD_SPAN = 0.35
_PROGRESS_STAGED = 0.45
_PROGRESS_SCANNING = 0.50
_PROGRESS_VERDICT = 0.80
_PROGRESS_PROMOTE = 0.90
_PROGRESS_DONE = 1.0

# The sampler adds a trace row only when progress has moved this far since the
# previous row; otherwise a live detail line would grow the event list every
# poll without telling the operator anything new.
_EVENT_STEP = 0.05
_EVENT_CAP = 100
_SAMPLE_SECONDS = 2.0

# qBittorrent's "unknown" ETA sentinel, not a duration (it is 100 days).
_ETA_UNKNOWN = 8640000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _download_progress(fraction: float) -> float:
    """Map a qBittorrent fraction onto the download slice of the job bar."""
    return _PROGRESS_ADDED + _PROGRESS_DOWNLOAD_SPAN * max(0.0, min(1.0, fraction))


def _eta_seconds(value: Any) -> int | None:
    """qBittorrent's ETA, or None when it does not know yet."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds >= _ETA_UNKNOWN:
        return None
    return int(seconds)


def _speed_text(value: Any) -> str:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return ""
    if rate <= 0:
        return ""
    if rate >= 1024**2:
        return f"{rate / 1024**2:.1f} MB/s"
    if rate >= 1024:
        return f"{rate / 1024:.0f} KB/s"
    return f"{rate:.0f} B/s"


class DownloadError(MinusError):
    """A request the bridge cannot serve, with the HTTP status to report.

    Search validation lives here too, not in the transport: an empty query is
    just as wrong when a sibling agent calls `search()` directly.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _assembly_pipeline(settings: Any) -> Any | None:
    from minus.assembly import build_triage_pipeline

    return build_triage_pipeline(settings)


class DownloadService:
    """Run search and grabs for one MINUS process.

    `discovery` and `pipeline_factory` are the two seams the tests use: a fake
    search and a fake pipeline keep every assertion on the envelope and the
    job dict rather than on a tracker being reachable. `torrent_probe` is the
    third: it replaces the qBittorrent read the sampler would do, so the
    progress curve can be checked without a daemon.

    The job dict is the second wire contract in this module. It carries the
    pipeline's verdict (`state`, `stage`, `result`) and the browser's live
    view of one transfer (`progress`, `eta_seconds`, `events`), frozen because
    the toast renders it without a version check. A failed ingest is a failed
    job, not a completed one whose detail happens to say "failed": the state
    and the error text must agree with what the pipeline reported, or the
    operator sees a green toast for a download that never happened.
    """

    def __init__(
        self,
        settings: Any,
        discovery: Any | None = None,
        pipeline_factory: Callable[[], Any | None] | None = None,
        torrent_probe: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.settings = settings
        self._discovery = discovery
        self._default_discovery: Any | None = None
        self._pipeline_factory = pipeline_factory or (lambda: _assembly_pipeline(settings))
        self._pipeline_cache: Any | None = None
        self._torrent_probe = torrent_probe
        self._build_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._active_job: str | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._closed = False

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the grab workers and the probe; safe to call more than once."""
        if self._workers or self._closed:
            return
        for index in range(_MAX_OUTSTANDING):
            worker = threading.Thread(
                target=self._work,
                name=f"minus-remote-grab-{index}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        if self._sampler is None:
            self._sampler_stop.clear()
            self._sampler = threading.Thread(
                target=self._sample_loop,
                name="minus-remote-probe",
                daemon=True,
            )
            self._sampler.start()

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sampler_stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2.0)
            self._sampler = None
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=2.0)
        self._workers.clear()

    # ---- Public API ----

    def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Search, returning the envelope the browser client is written against."""
        text = " ".join(str(query or "").split())
        if not text:
            raise DownloadError(400, "q is required")
        tools = self._discovery_tools()
        payload = tools.torrent_search(text, max(1, min(int(limit), 50)))
        threshold = float(getattr(self.settings, "discovery_min_score", 0.0) or 0.0)
        return {
            "query": str(payload.get("query") or text),
            "kind": str(payload.get("kind") or ""),
            "errors": list(payload.get("errors") or ()),
            "threshold": threshold,
            "candidates": list(payload.get("candidates") or ()),
            "pick": payload.get("pick"),
        }

    def grab(self, query: str, infohash: str) -> str:
        """Queue a download for a candidate identity; return the job id.

        The caller contributes an identity, never a URL: the authoritative
        shortlist is searched again here and its candidate supplies the magnet,
        so a request cannot name a download the UI never showed.
        """
        text = " ".join(str(query or "").split())
        digest = str(infohash or "").strip().lower()
        if not text or not digest:
            raise DownloadError(400, "query and infohash are required")
        if self._pipeline() is None:
            raise DownloadError(503, "qBittorrent is not configured (set MINUS_QBITTORRENT_URL)")
        found = self.search(text, 20)
        candidate = next(
            (
                item
                for item in found["candidates"]
                if str(item.get("infohash") or "").lower() == digest
            ),
            None,
        )
        if candidate is None:
            raise DownloadError(409, f"no search result matches infohash {digest}")
        download_url = str(candidate.get("download_url") or "")
        if not download_url:
            raise DownloadError(409, "the selected candidate has no magnet or .torrent URL")
        # The same rule `DiscoveryTools._pick` publishes: below the threshold a
        # pick is a guess, so it is downloaded for back-checking and never
        # auto-promoted.
        low_confidence = float(candidate.get("score") or 0.0) < found["threshold"]
        title = str(candidate.get("title") or text)
        started_at = _now_iso()
        job = {
            "id": secrets.token_hex(8),
            "state": "queued",
            "stage": "queued",
            "detail": "",
            "result": None,
            "query": text,
            "title": title,
            "download_url": download_url,
            "low_confidence": low_confidence,
            # The frozen wire fields. They live beside the internal ones so
            # `_public_job` can project one view without knowing which is which.
            "progress": 0.0,
            "eta_seconds": None,
            "torrent_hash": "",
            "name": title,
            "error": "",
            "started_at": started_at,
            "updated_at": started_at,
            "started_mono": time.monotonic(),
            "events": [
                {"at": started_at, "stage": "queued", "detail": "", "progress": 0.0},
            ],
        }
        if not self._submit(job):
            raise DownloadError(429, "the grab queue is full; try again shortly")
        return job["id"]

    def job(self, job_id: str) -> dict[str, Any]:
        """The public view of one job, or a 404-shaped error."""
        job = self._public_job(job_id)
        if job is None:
            raise DownloadError(404, "no such job")
        return job

    # ---- Discovery and pipeline (the harness's functions, not copies) ----

    def _discovery_tools(self) -> Any:
        if self._discovery is not None:
            return self._discovery
        with self._build_lock:
            if self._default_discovery is None:
                from minus.tools.discovery import DiscoveryTools

                self._default_discovery = DiscoveryTools(self.settings)
            return self._default_discovery

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
        """Route a real pipeline milestone to the job currently being run.

        The pipeline's kind names are transport, not the UI's stage names: the
        toast shows a single progress curve, so `added` means the download has
        begun and `staged` means qBittorrent's fraction has stopped being the
        right denominator. The hash from `added` is what lets the sampler ask
        qBittorrent about a transfer that is still running.
        """
        job_id = self._active_job
        if not job_id:
            return
        stage = str(kind or "working")
        detail = ""
        try:
            data = dict(payload or {})
        except (TypeError, ValueError):
            data = {}
        verdict = data.get("verdict")
        if isinstance(verdict, dict):
            detail = str(verdict.get("label") or "")
        if not detail:
            for key in ("name", "title"):
                if data.get(key):
                    detail = str(data[key])
                    break
        fields: dict[str, Any] = {"stage": stage, "detail": detail}
        torrent_hash = str(data.get("torrent_hash") or "")
        if torrent_hash:
            fields["torrent_hash"] = torrent_hash
        if stage == "added":
            fields["stage"] = "download"
            fields["progress"] = _download_progress(0.0)
            fields["eta_seconds"] = None
        elif stage == "staged":
            # Two steps in one milestone: the transfer is complete (0.45)
            # before the scanner owns the bar (0.50). Keeping them distinct
            # leaves both points in the trace.
            self._update(
                job_id,
                stage="staged",
                progress=_PROGRESS_STAGED,
                eta_seconds=None,
            )
            fields["stage"] = "scanning"
            fields["progress"] = _PROGRESS_SCANNING
            fields["eta_seconds"] = None
        elif stage == "verdict":
            fields["progress"] = _PROGRESS_VERDICT
            fields["eta_seconds"] = None
        elif stage == "promoted":
            fields["stage"] = "promote"
            fields["progress"] = _PROGRESS_PROMOTE
            fields["eta_seconds"] = None
        elif stage == "malicious_deleted":
            fields["stage"] = "verdict"
        self._update(job_id, **fields)

    # ---- Jobs ----

    def _update(self, job_id: str, **fields: Any) -> None:
        """Apply a milestone and append a trace row when one is warranted.

        Progress only ever moves forward: a sampler and the pipeline worker
        can race, and a late qBittorrent read must not drag the bar back past
        a stage the pipeline already announced. Stage, detail and state
        changes always make a row; plain progress carries one only per 0.05
        so the trace stays a trace.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            now = _now_iso()
            was_stage = job["stage"]
            was_detail = job["detail"]
            was_state = job["state"]
            previous_progress = float(job["progress"])
            if "progress" in fields:
                try:
                    value = float(fields["progress"])
                except (TypeError, ValueError):
                    value = previous_progress
                fields["progress"] = max(previous_progress, max(0.0, min(1.0, value)))
            job.update(fields)
            events = job["events"]
            last_progress = float(events[-1]["progress"]) if events else 0.0
            changed = (
                job["stage"] != was_stage
                or job["detail"] != was_detail
                or job["state"] != was_state
            )
            if changed or abs(float(job["progress"]) - last_progress) >= _EVENT_STEP:
                events.append(
                    {
                        "at": now,
                        "stage": str(job["stage"]),
                        "detail": str(job["detail"]),
                        "progress": float(job["progress"]),
                    }
                )
                if len(events) > _EVENT_CAP:
                    del events[: len(events) - _EVENT_CAP]
            job["updated_at"] = now

    def _public_job(self, job_id: str) -> dict[str, Any] | None:
        """The frozen job shape the API and the browser agree on."""
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
                "progress": float(job["progress"]),
                "eta_seconds": job["eta_seconds"],
                "elapsed_seconds": int(max(0.0, time.monotonic() - job["started_mono"])),
                "started_at": job["started_at"],
                "updated_at": job["updated_at"],
                "torrent_hash": job["torrent_hash"],
                "name": job["name"],
                "error": job["error"],
                "events": [dict(event) for event in job["events"]],
            }

    # ---- Live qBittorrent sampling ----

    def _sample_loop(self) -> None:
        while not self._sampler_stop.wait(_SAMPLE_SECONDS):
            try:
                self._sample_active_job()
            except Exception:  # pragma: no cover - a probe must never kill the thread
                logger.debug("Remote bridge torrent probe failed", exc_info=True)

    def _sample_active_job(self) -> None:
        """Copy qBittorrent's fraction and ETA onto the running job, once."""
        job_id = self._active_job
        if not job_id:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            torrent_hash = str(job["torrent_hash"] or "")
            progress = float(job["progress"])
            state = str(job["state"])
        if not torrent_hash or state in ("done", "error") or progress >= _PROGRESS_STAGED:
            return
        try:
            info = self._probe_torrent(torrent_hash)
        except Exception:
            # A torrent can vanish between the add and the first sample; the
            # pipeline will report the real failure on its own.
            logger.debug("Torrent %s could not be probed", torrent_hash, exc_info=True)
            return
        if not isinstance(info, dict):
            return
        try:
            fraction = float(info.get("progress") or 0.0)
        except (TypeError, ValueError):
            return
        fraction = max(0.0, min(1.0, fraction))
        target = _download_progress(fraction)
        percent = round(fraction * 100)
        speed = _speed_text(info.get("dlspeed"))
        detail = f"download {percent}%" + (f" · {speed}" if speed else "")
        with self._lock:
            live = self._jobs.get(job_id)
            if (
                live is None
                or self._active_job != job_id
                or live["state"] in ("done", "error")
                or float(live["progress"]) >= _PROGRESS_STAGED
            ):
                return
        self._update(
            job_id,
            progress=target,
            eta_seconds=_eta_seconds(info.get("eta")),
            detail=detail,
        )

    def _probe_torrent(self, torrent_hash: str) -> dict[str, Any] | None:
        if self._torrent_probe is not None:
            return self._torrent_probe(torrent_hash)
        pipeline = self._pipeline_cache
        client = getattr(pipeline, "client", None) if pipeline is not None else None
        if client is None:
            return None
        return client.torrent(torrent_hash)

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
                logger.exception("Remote bridge worker failed")

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            snapshot = dict(self._jobs.get(job_id) or {})
        if not snapshot:
            return
        with self._run_lock:
            self._active_job = job_id
            try:
                self._update(
                    job_id,
                    state="running",
                    stage="starting",
                    progress=_PROGRESS_STARTED,
                )
                pipeline = self._pipeline()
                if pipeline is None:
                    message = "qBittorrent is not configured (set MINUS_QBITTORRENT_URL)"
                    self._update(
                        job_id,
                        state="error",
                        stage="unavailable",
                        detail=message,
                        error=message,
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
                status = str(result.get("status") or "")
                torrent_hash = str(result.get("torrent_hash") or "")
                if status == "failed":
                    # The pipeline returns failures instead of raising; the
                    # bridge must not flatten that into "done". Its stage and
                    # error are the only explanation the operator will see.
                    error_text = str(result.get("error") or "download failed")
                    failed_stage = str(result.get("stage") or "failed")
                    self._update(
                        job_id,
                        state="error",
                        stage=failed_stage,
                        detail=error_text,
                        error=error_text,
                        eta_seconds=None,
                        result={
                            "status": status,
                            "torrent_hash": torrent_hash,
                            "stage": failed_stage,
                            "error": error_text,
                        },
                    )
                    return
                outcome = {"status": status, "torrent_hash": torrent_hash}
                auto_promote = (
                    bool(getattr(self.settings, "discovery_auto_promote", False))
                    and not snapshot["low_confidence"]
                )
                if outcome["status"] == "clean" and auto_promote:
                    self._update(
                        job_id,
                        stage="promote",
                        progress=_PROGRESS_PROMOTE,
                        eta_seconds=None,
                    )
                    try:
                        promoted = pipeline.promote(
                            result["torrent_hash"],
                            name=snapshot["title"],
                        )
                    except Exception as exc:
                        outcome["promote_error"] = str(exc)
                    else:
                        outcome["moved_to"] = str(promoted.get("moved_to") or "")
                name = str(result.get("name") or snapshot["title"])
                finished: dict[str, Any] = {
                    "state": "done",
                    "stage": "done",
                    "detail": outcome["status"] or "done",
                    "progress": _PROGRESS_DONE,
                    "eta_seconds": None,
                    "error": "",
                    "name": name,
                    "result": outcome,
                }
                if torrent_hash:
                    finished["torrent_hash"] = torrent_hash
                self._update(job_id, **finished)
            except Exception as exc:
                logger.exception("Remote bridge grab failed")
                self._update(
                    job_id,
                    state="error",
                    stage="failed",
                    detail=str(exc),
                    error=str(exc),
                    eta_seconds=None,
                )
            finally:
                self._active_job = None
