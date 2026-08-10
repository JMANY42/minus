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
counter; the speaker checks that counter before writing each ~50ms block of
audio and stops feeding the stream. Aborting the PortAudio stream was tried and
abandoned -- ALSA leaves the PCM device in a bad XRUN state afterwards, after
which a later write() can block for 10+ seconds in native code with no
Python-level exception. Declining to write the rest bounds barge-in latency to
one block plus whatever PortAudio has already buffered.

CTRL-C
------
Ctrl-C is barge-in from the keyboard, and routing it lives here rather than in
the speaker for a reason worth recording. The speaker used to install its own
SIGINT handler for the duration of playback, which CPython only permits on the
main thread -- so an escalated answer, spoken from the courier thread, ran with
no handler at all. The signal went to the main thread sitting in the transcript
source, raised KeyboardInterrupt there, and the conversation loop dutifully
wrapped up the session: Ctrl-C during a deep answer quit MINUS instead of
shutting it up.

The handler is therefore installed once, on the main thread, for the whole
conversation, and decides what Ctrl-C means from `is_active()` -- barge-in
while anything is being spoken, whoever is speaking it, and the usual
KeyboardInterrupt when nothing is.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class InterruptBus:
    """A monotonic interrupt counter with subscribers.

    A "token" is the counter's value at some moment. Holding a token and
    comparing it later answers "has the user interrupted since I took this?",
    which is the question both playback and (eventually) background tasks need
    to ask.
    """

    def __init__(self) -> None:
        # Reentrant because the SIGINT handler runs on whatever thread the
        # signal interrupts, and calls straight back in here. A plain Lock
        # deadlocks outright if the interrupted thread was itself mid-token().
        self._lock = threading.RLock()
        self._generation = 0
        self._active = 0
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

    # ---- Is there anything worth interrupting? ----

    def is_active(self) -> bool:
        """True while some interruptible stretch of work is running."""
        with self._lock:
            return self._active > 0

    @contextmanager
    def interruptible(self) -> Iterator[None]:
        """Mark work that a barge-in can usefully cut short.

        Counted rather than a flag: the conversation loop and the deep-answer
        courier both speak, and while the floor lock in cli.py keeps them from
        overlapping today, a nested or concurrent speaker must not clear the
        marker out from under the one still going.
        """
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


@contextmanager
def barge_in_on_sigint(bus: InterruptBus) -> Iterator[None]:
    """Route Ctrl-C to barge-in while speech is in flight, quit otherwise.

    Must be entered on the main thread -- signal.signal() is a main-thread
    privilege -- and is a no-op anywhere else, so a test or an embedding
    caller running this off-thread degrades to the default handler rather
    than raising.

    A second Ctrl-C quits, and needs no special case to do so: the first one
    stops playback within a block or two, `is_active()` goes false as speak()
    returns, and the next signal raises KeyboardInterrupt normally.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.debug("Not on the main thread; leaving SIGINT handling alone.")
        yield
        return

    def handler(signum, frame) -> None:
        if bus.is_active():
            logger.info("Ctrl-C during playback; treating it as barge-in.")
            bus.request()
            return
        raise KeyboardInterrupt

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
