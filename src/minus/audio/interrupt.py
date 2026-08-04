"""Barge-in coordination.

Interrupts were previously a module-level counter inside the TTS module,
bumped through a `request_interrupt()` function that the STT module imported.
That single import was the whole reason speech *recognition* depended on
speech *synthesis*, and it meant nothing else could ever learn that the user
had started talking -- there was one publisher and one hard-coded subscriber.

An InterruptBus is passed to both instead. Neither module knows the other
exists, and a dashboard or an idle-task supervisor can subscribe later without
touching either.

WHAT THIS DOES NOT DO
---------------------
It does not stop playback directly. `request()` only bumps a generation
counter; the speaker checks that counter between audio chunks. Aborting the
PortAudio stream mid-chunk was tried and abandoned -- ALSA leaves the PCM
device in a bad XRUN state afterwards, after which a later write() can block
for 10+ seconds in native code with no Python-level exception. Short chunks
plus a between-chunk check bounds barge-in latency to one chunk instead.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class InterruptBus:
    """A monotonic interrupt counter with subscribers.

    A "token" is the counter's value at some moment. Holding a token and
    comparing it later answers "has the user interrupted since I took this?",
    which is the question both playback and (eventually) background tasks need
    to ask.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._subscribers: list[Callable[[], None]] = []

    def token(self) -> int:
        """Capture the current generation."""
        with self._lock:
            return self._generation

    def request(self) -> int:
        """Signal that the user has interrupted. Returns the new generation."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber()
            except Exception:
                # A misbehaving subscriber must not break barge-in itself.
                logger.exception("Interrupt subscriber raised")

        return generation

    def is_stale(self, token: int | None) -> bool:
        """True if an interrupt has landed since `token` was taken.

        A None token means "no opinion" and is never stale, so callers that do
        not track interrupts keep working unchanged.
        """
        if token is None:
            return False
        return self.token() != token

    def subscribe(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)
