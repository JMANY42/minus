"""MergedTranscriptSource: the idle timeout and the injection queue.

The timings here are deliberately tiny (a 50ms timeout polled every 10ms) and
the assertions are about counts and ordering rather than about durations, so
that a slow machine makes the test slower rather than wrong.
"""

from __future__ import annotations

import threading
import time

from minus.core.sources import MergedTranscriptSource

# Everything here is scaled from one number, so the whole file can be slowed
# down at once if a CI box ever needs it.
TICK = 0.05


class FiniteSource:
    """A primary source that yields a few utterances and then ends."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts

    def __iter__(self):
        yield from self.texts


class BlockingSource:
    """A primary source that yields, then blocks the way a microphone does."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts
        self.released = threading.Event()
        self.stopped = False

    def __iter__(self):
        yield from self.texts
        self.released.wait(5)

    def stop(self) -> None:
        self.stopped = True
        self.released.set()


def drain(source) -> tuple[list[str], threading.Thread]:
    """Iterate `source` on its own thread, collecting what it yields."""
    received: list[str] = []
    thread = threading.Thread(target=lambda: received.extend(source), daemon=True)
    thread.start()
    return received, thread


def build(primary=None, on_idle=None, idle_timeout=TICK):
    return MergedTranscriptSource(
        primary,
        idle_timeout=idle_timeout,
        on_idle=on_idle,
        poll=TICK / 5,
    )


# ---- Passing utterances through ----


def test_yields_the_primary_sources_utterances_in_order():
    source = build(FiniteSource("first", "second", "third"))

    assert list(source) == ["first", "second", "third"]


def test_iteration_ends_when_the_primary_source_ends():
    received, thread = drain(build(FiniteSource("only")))
    thread.join(2)

    assert not thread.is_alive()
    assert received == ["only"]


def test_submitted_text_is_yielded():
    source = build(BlockingSource())
    received, thread = drain(source)

    source.submit("typed")
    time.sleep(TICK)
    source.close()
    thread.join(2)

    assert received == ["typed"]


def test_works_with_no_primary_source_at_all():
    source = build(primary=None)
    received, thread = drain(source)

    source.submit("socket only")
    time.sleep(TICK)
    source.close()
    thread.join(2)

    assert received == ["socket only"]


# ---- Closing ----


def test_close_from_another_thread_ends_iteration():
    source = build(BlockingSource())
    _, thread = drain(source)

    time.sleep(TICK)
    source.close()
    thread.join(2)

    assert not thread.is_alive()


def test_close_stops_the_primary_source():
    primary = BlockingSource()
    source = build(primary)
    _, thread = drain(source)

    time.sleep(TICK)
    source.close()
    thread.join(2)

    assert primary.stopped


def test_close_is_idempotent():
    source = build(BlockingSource())
    _, thread = drain(source)

    source.close()
    source.close()
    thread.join(2)

    assert not thread.is_alive()


# ---- The idle timeout ----


def test_idle_handler_fires_after_the_timeout():
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1))
    _, thread = drain(source)

    time.sleep(TICK * 4)
    source.close()
    thread.join(2)

    assert fired


def test_idle_handler_does_not_fire_before_the_timeout():
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1), idle_timeout=10.0)
    _, thread = drain(source)

    time.sleep(TICK * 3)
    source.close()
    thread.join(2)

    assert fired == []


def test_idle_handler_fires_once_per_silence():
    """A long silence is one conversation ending, not one every timeout."""
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1))
    _, thread = drain(source)

    time.sleep(TICK * 8)
    source.close()
    thread.join(2)

    assert len(fired) == 1


def test_an_utterance_rearms_the_idle_handler():
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1))
    received, thread = drain(source)

    time.sleep(TICK * 4)
    source.submit("still here")
    time.sleep(TICK * 4)
    source.close()
    thread.join(2)

    assert len(fired) == 2
    assert received == ["still here"]


def test_declining_leaves_the_timer_armed():
    """Returning False means "not now" -- the handler gets asked again."""
    fired: list[int] = []

    def decline():
        fired.append(1)
        return False

    source = build(BlockingSource(), on_idle=decline)
    _, thread = drain(source)

    time.sleep(TICK * 8)
    source.close()
    thread.join(2)

    assert len(fired) > 1


def test_a_raising_handler_neither_repeats_nor_stops_the_stream():
    fired: list[int] = []

    def on_idle():
        fired.append(1)
        raise RuntimeError("extraction blew up")

    source = build(BlockingSource(), on_idle=on_idle)
    received, thread = drain(source)

    time.sleep(TICK * 6)
    source.submit("after the failure")
    time.sleep(TICK)
    source.close()
    thread.join(2)

    assert len(fired) == 1
    assert received == ["after the failure"]


def test_marking_activity_restarts_the_clock():
    """A deep answer is spoken from another thread and must count as a turn."""
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1))
    _, thread = drain(source)

    # Keep the clock alive from outside, the way the courier does.
    for _ in range(6):
        time.sleep(TICK / 2)
        source.mark_activity()

    assert fired == []

    time.sleep(TICK * 3)
    source.close()
    thread.join(2)

    assert len(fired) == 1  # and it fires normally once the marking stops


def test_marking_activity_rearms_a_handler_that_already_fired():
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1))
    _, thread = drain(source)

    time.sleep(TICK * 4)
    source.mark_activity()
    time.sleep(TICK * 4)
    source.close()
    thread.join(2)

    assert len(fired) == 2


def test_seconds_since_activity_tracks_the_clock():
    source = build(BlockingSource(), idle_timeout=10.0)

    time.sleep(TICK)
    assert source.seconds_since_activity() >= TICK

    source.mark_activity()
    assert source.seconds_since_activity() < TICK


def test_a_zero_timeout_disables_the_rollover():
    fired: list[int] = []
    source = build(BlockingSource(), on_idle=lambda: fired.append(1), idle_timeout=0)
    _, thread = drain(source)

    time.sleep(TICK * 4)
    source.close()
    thread.join(2)

    assert fired == []


def test_no_handler_means_no_timeout_behaviour():
    source = build(BlockingSource(), on_idle=None)
    _, thread = drain(source)

    time.sleep(TICK * 4)
    assert thread.is_alive()

    source.close()
    thread.join(2)
    assert not thread.is_alive()
