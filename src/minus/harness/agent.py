"""Thread-side helpers: turn tool traffic into one line, and collect deep answers.

The harness draws tool activity from `ToolLoop.observer`, which runs on the
worker thread that is executing the turn -- not on Textual's loop. Nothing here
touches a widget: it only formats the payload into the short form the screen
has room for, or waits on a queue so `app.py` can post the result with
`call_from_thread`.

The deep tier publishes on a `Queue`, exactly as it does for the voice runtime.
A daemon thread is the smallest thing that turns that queue into UI events
without making the UI poll it. The thread is a daemon on purpose: a harness that
closes mid-thought should not keep the process alive for an answer it can no
longer show.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any

logger = logging.getLogger(__name__)

# Arguments are shown beside the tool name, never in full. A tool call can carry
# a whole file; the rail is one line.
ARG_LIMIT = 60


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def format_value(value: Any, limit: int = 40) -> str:
    """A value as the few characters a one-line card can afford.

    Dicts are summarised by the field that usually carries the answer first
    (`label`, then `state`/`status`), because a verdict is a dict full of file
    names and the reader only needs its verdict. Lists report their length.
    """
    if isinstance(value, dict):
        for key in ("label", "verdict", "state", "status", "message", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return _clip(candidate, limit)
        if "torrent_hash" in value:
            return _clip(str(value["torrent_hash"]), limit)
        preview = ", ".join(
            f"{key}={format_value(item, 18)}" for key, item in list(value.items())[:2]
        )
        return _clip("{" + preview + "}", limit) if preview else "{}"
    if isinstance(value, (list, tuple, set)):
        return f"{len(value)} item(s)"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _clip(str(value), limit)


def summarize_arguments(arguments: Any, limit: int = ARG_LIMIT) -> str:
    """`path="notes.txt", mode="read"` for the first few arguments.

    JSON is the fallback rather than the normal shape: the common call has two
    or three scalar arguments, and `{"path": "notes.txt"}` spends punctuation
    the rail does not have.
    """
    if not arguments:
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return _clip(arguments, limit)
    if not isinstance(arguments, dict):
        return _clip(str(arguments), limit)

    parts: list[str] = []
    for key, value in list(arguments.items())[:3]:
        shown = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={_clip(shown, max(8, limit // 2))}")
    summary = ", ".join(parts)
    return _clip(summary, limit)


def tool_line(name: str, arguments: Any) -> str:
    """The center card's first line: `read_file path="notes.txt"`."""
    summary = summarize_arguments(arguments)
    return f"{name} {summary}".rstrip() if summary else name


def payload_summary(payload: dict[str, Any], limit: int = 72) -> str:
    """A notification payload without the alert text, which gets its own block."""
    parts = [
        f"{key}={format_value(value, 24)}"
        for key, value in payload.items()
        if key not in ("alert", "detail") and value not in (None, "", {}, [])
    ]
    return _clip(", ".join(parts) or "event", limit)


class DeepResultWatcher:
    """A daemon that drains the deep tier's queue into a callback.

    `publish` is deliberately allowed to marshal to another thread -- `app.py`
    hands it a function that does. Exceptions are swallowed for the same reason
    the runtime's courier swallows them: one unrenderable answer must not end
    the only watcher there is.
    """

    def __init__(
        self,
        results: Queue,
        publish: Callable[[Any], None],
        *,
        interval: float = 0.25,
        name: str = "minus-harness-deep",
    ) -> None:
        self._results = results
        self._publish = publish
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self._results.get(timeout=self._interval)
            except Empty:
                continue
            try:
                self._publish(result)
            except Exception:  # pragma: no cover - the callback is UI code
                logger.debug("Harness failed to publish a deep result", exc_info=True)
