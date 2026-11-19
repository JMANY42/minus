"""One append-only record of what the triage pipeline did, and why.

A triage run crosses four processes (qBittorrent, the static analyser, the
model reviewer, the Windows guest) and a human ends up asking the same
questions afterwards: was the torrent already there, did the sandbox run, why
was promotion refused. Log lines answer some of that while the run is live and
then scroll away; `verdict.json` records the outcome but not the trip.

So every stage emits `TraceEvent`s into one JSONL file. It is append-only and
written by whoever is doing the work, which means a stage that dies still
leaves its events behind. CLI, MCP and the dashboard all read the same file
through `read_trace` rather than each inventing a progress feed.

Tracing is an observer, never a participant: a full disk, a bad permission or
an embedding callback that throws must not turn a scan into a failure, so
`JsonlTracer.emit` swallows and logs. The pipeline can also run with no tracer
at all (`NullTracer`), which is the default for library callers that only want
a verdict.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceEvent:
    """One thing that happened, at one stage, for one operation."""

    stage: str
    event: str
    op_id: str = ""
    torrent_hash: str = ""
    duration_ms: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. `ts` is filled here so callers need not care."""
        return {
            "ts": self.ts or datetime.now(UTC).isoformat(timespec="microseconds"),
            "op_id": self.op_id,
            "stage": self.stage,
            "event": self.event,
            "torrent_hash": self.torrent_hash,
            "duration_ms": self.duration_ms,
            "data": dict(self.data),
        }


@runtime_checkable
class Tracer(Protocol):
    """Somewhere trace events go. Implementations must not raise."""

    def emit(self, event: TraceEvent) -> None:
        """Record one event."""


class NullTracer:
    """A tracer that records nothing, for callers that asked for no trace."""

    def emit(self, event: TraceEvent) -> None:
        return None


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")


def new_op_id(stage: str, subject: str = "") -> str:
    """A readable grouping key for one operation.

    A URL or torrent name is useful in the id (it is what a human searches
    for), but it is not path-safe and may be long, so only alphanumerics from
    the first eight characters survive -- enough to recognise, short enough to
    keep every line scannable.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", subject or "")[:8]
    stamp = _now_stamp()
    return f"{stage}-{cleaned}-{stamp}" if cleaned else f"{stage}-{stamp}"


class JsonlTracer:
    """Append one JSON object per line to `path`.

    Thread-safe because the pipeline's courier, scan and MCP calls can emit
    concurrently. The parent is created `0700` and the file `0600` because
    trace data names the content a user downloaded, which is nobody else's
    business on a shared machine.
    """

    def __init__(self, path: Path, *, echo: Callable[[TraceEvent], None] | None = None) -> None:
        self.path = Path(path)
        self.echo = echo
        self._lock = threading.Lock()
        self._prepared = False

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def emit(self, event: TraceEvent) -> None:
        try:
            line = json.dumps(
                event.as_dict(), separators=(",", ":"), ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            logger.debug("triage trace event is not serializable: %r", event, exc_info=True)
            return
        try:
            with self._lock:
                if not self._prepared:
                    self._prepare_parent()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                if not self._prepared:
                    # The file exists now; mode only applies on creation, and
                    # an existing trace from an older build may be laxer.
                    with contextlib.suppress(OSError):
                        os.chmod(self.path, 0o600)
                    self._prepared = True
        except OSError:
            # Observability must never be the reason a scan fails.
            logger.debug("triage trace write failed for %s", self.path, exc_info=True)
            return
        if self.echo is not None:
            try:
                self.echo(event)
            except Exception:
                logger.debug("triage trace echo failed for %s", event.event, exc_info=True)


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


@contextlib.contextmanager
def trace_span(
    tracer: Tracer | None,
    *,
    op_id: str,
    stage: str,
    event: str,
    torrent_hash: str = "",
    **data: Any,
) -> Iterator[dict[str, Any]]:
    """Emit `.start`, then `.done` or `.failed`, around a piece of work.

    The yielded dict is the result the caller is building: whatever it puts
    there is merged into the `.done` event, so a stage's outcome and its trace
    line cannot disagree. Failures are re-raised unchanged -- this observes,
    it does not handle.
    """
    span: dict[str, Any] = dict(data)
    if tracer is None:
        yield span
        return

    tracer.emit(
        TraceEvent(
            stage=stage,
            event=f"{event}.start",
            op_id=op_id,
            torrent_hash=torrent_hash,
            data=dict(data),
        )
    )
    started = time.perf_counter()
    try:
        yield span
    except Exception as exc:
        tracer.emit(
            TraceEvent(
                stage=stage,
                event=f"{event}.failed",
                op_id=op_id,
                torrent_hash=torrent_hash,
                duration_ms=_duration_ms(started),
                data={
                    **data,
                    **span,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
        )
        raise
    else:
        tracer.emit(
            TraceEvent(
                stage=stage,
                event=f"{event}.done",
                op_id=op_id,
                torrent_hash=torrent_hash,
                duration_ms=_duration_ms(started),
                data={**data, **span},
            )
        )


def read_trace(path: Path, *, torrent_hash: str = "", limit: int = 200) -> list[dict]:
    """The most recent events, optionally for one torrent.

    A missing file is an empty history, not an error: asking for the trace of
    an operation that never started should render as "nothing happened". A
    malformed line is skipped for the same reason -- one crash mid-write must
    not make the rest unreadable.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []

    wanted = torrent_hash.lower()
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if wanted and str(event.get("torrent_hash") or "").lower() != wanted:
            continue
        events.append(event)

    if limit > 0:
        events = events[-limit:]
    return events
