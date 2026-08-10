"""A transcript source with an idle timeout and an injection queue.

Two things the conversation loop needs that a bare source cannot give it:

  * **A timeout.** The loop is `for transcript in transcripts:`, which blocks
    forever. Nothing can happen *because* nothing was said -- and ending a
    conversation after a silence is exactly that. The microphone blocks inside
    RealtimeSTT's `recorder.text()`, which is native code with no timeout and
    nothing to select() on, so the only way to bound the wait is to move the
    blocking read onto its own thread and consume through a queue.
  * **A second producer.** Once the wait goes through a queue, injecting an
    utterance from somewhere else -- a control socket, a test -- is a `put`.

Both fall out of the same structure: one queue, a pump thread draining the
primary source into it, and the consumer polling with a timeout.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from queue import Empty, Queue
from typing import Any

logger = logging.getLogger(__name__)

# Ends iteration. A sentinel rather than a flag because the consumer is parked
# in Queue.get() and needs something to arrive to wake it.
_END = object()

DEFAULT_IDLE_TIMEOUT = 30.0

# How often the consumer wakes to check the clock. Also the upper bound on how
# long close() takes to be noticed, which is why this is a poll rather than a
# bare blocking get(): a signal handler setting a flag is useless if the thread
# it needs to reach is parked in Condition.wait().
DEFAULT_POLL_SECONDS = 0.5


class MergedTranscriptSource:
    """A TranscriptSource that can time out and can be written to.

    Yields whatever arrives first: an utterance from `primary`, or one handed
    to `submit()`. When neither has arrived for `idle_timeout` seconds,
    `on_idle` is called **on the consumer's own thread, between yields** -- so
    a handler that touches the conversation cannot race the turn it is reading.

    `on_idle` returns False to decline, which leaves the timer armed for
    another interval. Anything else (including None) latches: the handler is
    not called again until the next utterance re-arms it. Without that latch a
    silence would fire the handler every `idle_timeout` seconds for as long as
    it lasted.

    `primary=None` is legal and means "only what is submitted", which is what a
    headless service without a microphone wants.
    """

    def __init__(
        self,
        primary: Any | None = None,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        on_idle: Callable[[], Any] | None = None,
        poll: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.primary = primary
        self.idle_timeout = idle_timeout
        self.on_idle = on_idle
        self.poll = poll

        self._queue: Queue[Any] = Queue()
        self._closed = threading.Event()

    # ---- Input ----

    def submit(self, text: str) -> None:
        """Inject an utterance as though it had been spoken."""
        if not self._closed.is_set():
            self._queue.put(text)

    def close(self) -> None:
        """End iteration. Idempotent, and safe to call from any thread."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(_END)

        # The pump is parked inside the primary source's own blocking read --
        # native code, for the microphone -- so it cannot notice the flag by
        # itself. Sources that can be woken say so by exposing stop().
        stop = getattr(self.primary, "stop", None)
        if stop is not None:
            try:
                stop()
            except Exception:
                logger.exception("Failed to stop the primary transcript source")

    # ---- Plumbing ----

    def _pump(self, primary: Any) -> None:
        """Drain the primary source into the queue, on its own thread."""
        try:
            for text in primary:
                if self._closed.is_set():
                    return
                self._queue.put(text)
        except Exception:
            # This thread is the only thing feeding the conversation; dying
            # quietly would leave a live-looking assistant that never hears
            # anything again.
            logger.exception("The transcript source failed; input has stopped")
        finally:
            self._queue.put(_END)

    def _fire_idle(self) -> bool:
        """Call the idle handler. Returns True if it should now be latched."""
        assert self.on_idle is not None
        try:
            return self.on_idle() is not False
        except Exception:
            # Latch on failure. A handler that raises every time would
            # otherwise do it again on every poll for the rest of the silence.
            logger.exception("The idle handler failed; waiting for the next utterance")
            return True

    def _idle_enabled(self) -> bool:
        return self.on_idle is not None and self.idle_timeout > 0

    def __iter__(self) -> Iterator[str]:
        if self.primary is not None:
            threading.Thread(
                target=self._pump,
                args=(self.primary,),
                name="transcript-pump",
                daemon=True,
            ).start()

        quiet_since = time.monotonic()
        fired = False

        while True:
            try:
                item = self._queue.get(timeout=self.poll)
            except Empty:
                if self._closed.is_set():
                    return
                if fired or not self._idle_enabled():
                    continue
                if time.monotonic() - quiet_since < self.idle_timeout:
                    continue

                if self._fire_idle():
                    fired = True
                else:
                    quiet_since = time.monotonic()
                continue

            if item is _END:
                return

            yield item

            # Timed from the end of the turn rather than its start: a reply
            # that takes twenty seconds to speak is not twenty seconds of
            # silence, and counting it as such would end conversations while
            # the user was still listening to one.
            quiet_since = time.monotonic()
            fired = False
