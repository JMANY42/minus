"""What MINUS is doing right now, for anything that wants to watch.

The assistant had no way to say what it was up to. The conversation loop knew
it was waiting, the deep tier knew it was thinking, the speaker knew it was
talking, and none of that left the process. This is the one place that
answers "what is it doing", so a dashboard does not have to infer it by
tailing a log.

Shaped like `InterruptBus` on purpose -- `subscribe()`, subscribers copied
under the lock and called outside it, exceptions contained. Two mechanisms in
one codebase for "somebody wants to know" should not look different.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

IDLE = "idle"
LISTENING = "listening"
HEARING = "hearing"
THINKING = "thinking"
SPEAKING = "speaking"

PHASES = (IDLE, LISTENING, HEARING, THINKING, SPEAKING)


class RuntimeState:
    """The assistant's current phase, plus whatever else it can report."""

    def __init__(self, pid: int | None = None) -> None:
        self._lock = threading.Lock()
        self._phase = IDLE
        self._subscribers: list[Callable[[], None]] = []
        # Sections of the snapshot that are owned by somebody else and read on
        # demand: the deep tier's status, the live conversation's id. Held as
        # callables so this class never has to hold a reference to the object
        # graph, and never goes stale.
        self._sections: dict[str, Callable[[], Any]] = {}
        self._seq = 0

        self.pid = pid if pid is not None else os.getpid()
        self.started_at = time.time()

    # ---- Wiring ----

    def provide(self, key: str, source: Callable[[], Any]) -> None:
        """Register a snapshot section, read fresh each time it is asked for."""
        with self._lock:
            self._sections[key] = source

    def subscribe(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    # ---- Phase ----

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    def set_phase(self, phase: str) -> None:
        """Record what MINUS is doing, notifying watchers only on a change.

        The no-op on an unchanged phase matters: `listening` is set at the top
        of every poll of the transcript queue, which is twice a second.
        """
        with self._lock:
            if phase == self._phase:
                return
            self._phase = phase
            self._seq += 1
            subscribers = list(self._subscribers)

        self._notify(subscribers)

    def touch(self) -> None:
        """Announce that something changed which is not the phase.

        The deep tier is the case this exists for: it starts and finishes on
        its own thread while the phase stays exactly where it was, and
        `set_phase` deliberately says nothing when the phase has not moved. A
        watcher still needs to hear about it, because the snapshot it would
        now read is different.
        """
        with self._lock:
            self._seq += 1
            subscribers = list(self._subscribers)

        self._notify(subscribers)

    def _notify(self, subscribers: list[Callable[[], None]]) -> None:
        for subscriber in subscribers:
            try:
                subscriber()
            except Exception:
                # A dashboard that has crashed, or a socket that has gone
                # away, must not stop the assistant from working.
                logger.exception("Runtime state subscriber raised")

    # ---- Reporting ----

    def snapshot(self) -> dict:
        """Everything known about the current state, as JSON-ready data.

        Sections are read outside the lock. They call into the object graph --
        `DeepThinker.status()` takes a lock of its own -- and holding ours
        across that is how two locks become a deadlock.
        """
        with self._lock:
            snapshot = {
                "phase": self._phase,
                "seq": self._seq,
                "pid": self.pid,
                "started_at": self.started_at,
                "uptime_seconds": time.time() - self.started_at,
            }
            sections = dict(self._sections)

        for key, source in sections.items():
            try:
                snapshot[key] = source()
            except Exception:
                logger.exception("Runtime state section %r failed", key)
                snapshot[key] = None

        return snapshot
