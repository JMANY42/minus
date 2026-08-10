"""Tests for barge-in coordination, chunking and transcript sources.

Replaces test_speech_to_text.py and test_text_to_speech.py, both of which had
gone stale against the code they covered: one asserted an interrupt call that
the CLI path no longer made, the other mocked sd.play/wait/stop after playback
had moved to sd.OutputStream. Neither failure was noticed because the suite was
already red.

These import minus.audio.{interrupt,stt,chunking}, none of which pull in
sounddevice or kokoro-onnx -- so the whole file runs without the `audio` extra.
"""

from __future__ import annotations

import builtins
import signal
import threading
from unittest.mock import patch

import pytest

from minus.audio.chunking import split_text_into_chunks
from minus.audio.interrupt import InterruptBus, barge_in_on_sigint
from minus.audio.stt import CliTranscriptSource, is_exit_phrase


class TestInterruptBus:
    def test_token_is_stable_until_an_interrupt(self):
        bus = InterruptBus()
        token = bus.token()

        assert not bus.is_stale(token)
        bus.request()
        assert bus.is_stale(token)

    def test_a_fresh_token_is_not_stale_after_the_interrupt(self):
        bus = InterruptBus()
        bus.request()
        assert not bus.is_stale(bus.token())

    def test_none_token_is_never_stale(self):
        """Callers that do not track interrupts keep working unchanged."""
        bus = InterruptBus()
        bus.request()
        assert not bus.is_stale(None)

    def test_subscribers_are_notified(self):
        bus = InterruptBus()
        seen = []
        bus.subscribe(lambda: seen.append("interrupted"))

        bus.request()
        bus.request()

        assert seen == ["interrupted", "interrupted"]

    def test_a_raising_subscriber_does_not_break_barge_in(self):
        bus = InterruptBus()
        good = []
        bus.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("bad subscriber")))
        bus.subscribe(lambda: good.append(1))

        assert bus.request() == 1
        assert good == [1]

    def test_generation_increments_monotonically(self):
        bus = InterruptBus()
        assert [bus.request() for _ in range(3)] == [1, 2, 3]


class TestSwallowedInterruptRegression:
    """The bug: a barge-in during generation used to be discarded.

    speak() captured the interrupt generation at entry. If the user started
    talking while the model was still generating, request() had already bumped
    the counter with nothing playing -- so speak()'s fresh capture matched, no
    interrupt was detected, and the assistant talked over them. Because
    on_vad_start fires on speech *onset*, a user who kept talking produced no
    second onset, so the entire reply played over them.
    """

    def test_interrupt_during_generation_marks_the_turn_stale(self):
        bus = InterruptBus()

        # The loop captures a token when the transcript arrives...
        token = bus.token()

        # ...the user starts talking while the model is still generating...
        bus.request()

        # ...so by the time there is a reply to speak, the turn is stale.
        assert bus.is_stale(token)

    def test_capturing_after_generation_would_have_missed_it(self):
        """Demonstrates the old behaviour, to keep the regression legible."""
        bus = InterruptBus()
        bus.request()  # user barges in during generation

        stale_by_old_rule = bus.is_stale(bus.token())  # captured too late
        assert stale_by_old_rule is False


class TestInterruptibleWork:
    def test_a_fresh_bus_has_nothing_to_interrupt(self):
        assert not InterruptBus().is_active()

    def test_it_is_active_only_inside_the_block(self):
        bus = InterruptBus()

        with bus.interruptible():
            assert bus.is_active()

        assert not bus.is_active()

    def test_it_is_cleared_even_when_the_work_raises(self):
        bus = InterruptBus()

        with pytest.raises(RuntimeError), bus.interruptible():
            raise RuntimeError("playback fell over")

        assert not bus.is_active()

    def test_nesting_stays_active_until_the_outermost_exits(self):
        """Counted, not a flag: two speakers must not clear each other."""
        bus = InterruptBus()

        with bus.interruptible():
            with bus.interruptible():
                assert bus.is_active()
            assert bus.is_active()

        assert not bus.is_active()


class TestCtrlCDuringSpeech:
    """The bug: Ctrl-C while a deep answer was being spoken quit MINUS.

    The speaker installed its own SIGINT handler for the duration of playback,
    and signal.signal() is a main-thread privilege. Escalated answers are
    spoken from the courier thread, so no handler was installed at all -- the
    signal went to the main thread waiting in the transcript source, raised
    KeyboardInterrupt there, and the conversation loop ended the session.

    These raise a real SIGINT rather than calling the handler directly, so
    what is being checked is what the process actually does with Ctrl-C.
    """

    def _speak_on_another_thread(self, bus):
        """Hold the bus active from a worker, as the courier's speak() does."""
        speaking = threading.Event()
        release = threading.Event()

        def courier():
            with bus.interruptible():
                speaking.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(target=courier, daemon=True)
        thread.start()
        speaking.wait(timeout=5.0)
        return thread, release

    def test_ctrl_c_while_a_worker_thread_speaks_does_not_kill_the_program(self):
        bus = InterruptBus()
        thread, release = self._speak_on_another_thread(bus)
        token = bus.token()

        try:
            with barge_in_on_sigint(bus):
                # No pytest.raises: the whole point is that this does not
                # raise on the main thread the way it used to.
                signal.raise_signal(signal.SIGINT)
        finally:
            release.set()
            thread.join(timeout=5.0)

        assert bus.is_stale(token), "Ctrl-C should have registered as a barge-in"

    def test_ctrl_c_with_nothing_being_spoken_still_quits(self):
        bus = InterruptBus()

        with pytest.raises(KeyboardInterrupt), barge_in_on_sigint(bus):
            signal.raise_signal(signal.SIGINT)

    def test_a_second_ctrl_c_quits_once_playback_has_stopped(self):
        bus = InterruptBus()
        thread, release = self._speak_on_another_thread(bus)

        with barge_in_on_sigint(bus):
            signal.raise_signal(signal.SIGINT)  # barge-in
            release.set()
            thread.join(timeout=5.0)

            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)

    def test_the_previous_handler_is_restored(self):
        bus = InterruptBus()
        before = signal.getsignal(signal.SIGINT)

        with barge_in_on_sigint(bus):
            assert signal.getsignal(signal.SIGINT) is not before

        assert signal.getsignal(signal.SIGINT) is before

    def test_off_the_main_thread_it_leaves_signals_alone(self):
        """The courier must be able to enter it without CPython refusing."""
        bus = InterruptBus()
        outcome: list[str] = []

        def worker():
            try:
                with barge_in_on_sigint(bus):
                    outcome.append("entered")
            except Exception as exc:  # pragma: no cover - the failure we guard
                outcome.append(f"raised {type(exc).__name__}")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5.0)

        assert outcome == ["entered"]


class TestExitPhrases:
    @pytest.mark.parametrize(
        "text", ["exit", "quit", "end conversation", "Exit.", "  QUIT!  ", "end conversation?"]
    )
    def test_recognised(self, text):
        assert is_exit_phrase(text)

    @pytest.mark.parametrize("text", ["exit the building", "quitting time", "hello"])
    def test_not_recognised(self, text):
        assert not is_exit_phrase(text)


class TestCliTranscriptSource:
    def test_yields_input_and_signals_an_interrupt(self):
        bus = InterruptBus()
        source = CliTranscriptSource(bus)

        with patch.object(builtins, "input", side_effect=["hello there", EOFError()]):
            transcripts = iter(source)
            assert next(transcripts) == "hello there"
            # Submitting a line stops anything still being spoken.
            assert bus.token() == 1

            with pytest.raises(StopIteration):
                next(transcripts)

    def test_blank_lines_are_skipped(self):
        source = CliTranscriptSource(InterruptBus())

        with patch.object(builtins, "input", side_effect=["", "   ", "real input", EOFError()]):
            assert list(source) == ["real input"]

    def test_exit_phrase_ends_the_conversation(self):
        source = CliTranscriptSource(InterruptBus())

        with patch.object(builtins, "input", side_effect=["first", "quit", "never reached"]):
            assert list(source) == ["first"]

    def test_ctrl_c_ends_the_conversation_cleanly(self):
        source = CliTranscriptSource(InterruptBus())

        with patch.object(builtins, "input", side_effect=["first", KeyboardInterrupt()]):
            assert list(source) == ["first"]


class TestChunking:
    def test_short_text_is_one_chunk(self):
        assert split_text_into_chunks("hello there", max_chars=40) == ["hello there"]

    def test_empty_text_produces_no_chunks(self):
        assert split_text_into_chunks("", max_chars=40) == []
        assert split_text_into_chunks("   ", max_chars=40) == []

    def test_sentence_boundaries_are_preferred(self):
        chunks = split_text_into_chunks("One two three. Four five six. Seven.", max_chars=20)
        assert all(len(chunk) <= 20 for chunk in chunks)
        assert "".join(chunks).replace(" ", "") == "Onetwothree.Fourfivesix.Seven."

    def test_a_word_longer_than_the_limit_is_split(self):
        chunks = split_text_into_chunks("x" * 100, max_chars=40)
        assert all(len(chunk) <= 40 for chunk in chunks)
        assert "".join(chunks) == "x" * 100

    def test_every_chunk_respects_the_limit(self):
        text = " ".join(f"word{i}" for i in range(200))
        assert all(len(chunk) <= 40 for chunk in split_text_into_chunks(text, max_chars=40))


class TestChunkBoundariesAreGrammatical:
    """The chopped-speech bug: chunks are separate utterances, so a boundary
    mid-phrase is heard as a phrase ending even with no silence at it."""

    def test_a_long_sentence_breaks_at_its_clauses(self):
        text = (
            "Once upon a time in a valley that lay between two great mountains, "
            "there was a village that everyone called Willowbrook, and the people "
            "who lived there were farmers."
        )
        chunks = split_text_into_chunks(text, max_chars=80)

        assert len(chunks) > 1
        for chunk in chunks[:-1]:
            assert chunk.endswith((",", ";", ":", ".", "!", "?")), chunk

    def test_short_sentences_are_packed_back_together(self):
        """Otherwise every full stop would cost a seam of its own."""
        text = "One two three. Four five six. Seven eight nine."
        assert split_text_into_chunks(text, max_chars=200) == [text]

    def test_only_a_clause_too_long_to_fit_is_broken_mid_phrase(self):
        text = " ".join(f"word{i}" for i in range(40))
        chunks = split_text_into_chunks(text, max_chars=40)

        assert len(chunks) > 1
        assert " ".join(chunks) == text

    def test_the_first_chunk_is_held_short_for_a_fast_start(self):
        text = (
            "Once upon a time in a valley that lay between two great mountains, "
            "there was a village that everyone called Willowbrook, and the people "
            "who lived there were farmers."
        )
        chunks = split_text_into_chunks(text, max_chars=200, first_max_chars=40)

        assert len(chunks[0]) <= 40
        assert " ".join(chunks) == text

    def test_the_budget_ramps_up_to_the_full_size(self):
        """The bug: a short opener followed by a full-size chunk starved the
        stream, because synthesizing the second took longer than playing the
        first. Each chunk may only outgrow its predecessor by so much."""
        text = " ".join(f"word{i}" for i in range(200))
        chunks = split_text_into_chunks(text, max_chars=300, first_max_chars=40)

        spoken = 0
        for chunk in chunks:
            assert len(chunk) <= max(40, min(300, spoken)), chunk
            spoken += len(chunk)
        assert max(len(chunk) for chunk in chunks) > 250, "never reaches full size"

    def test_a_forced_cut_lands_in_front_of_a_phrase(self):
        """ "...that lay | between two great mountains" beats "...there was a |"."""
        text = (
            "Once upon a time in a valley that lay between two great mountains there was a village"
        )
        chunks = split_text_into_chunks(text, max_chars=200, first_max_chars=60)

        assert chunks[0] == "Once upon a time in a valley that lay"
        assert " ".join(chunks) == text

    def test_a_determiner_is_only_the_fallback_boundary(self):
        text = "The runners crossed the finish line together the crowd roared"
        chunks = split_text_into_chunks(text, max_chars=200, first_max_chars=45)

        assert chunks[0] == "The runners crossed the finish line together"
        assert " ".join(chunks) == text

    def test_a_word_longer_than_the_first_bound_is_not_cut_mid_syllable(self):
        chunks = split_text_into_chunks(
            "Supercalifragilistic and more", max_chars=200, first_max_chars=5
        )
        assert chunks[0] == "Supercalifragilistic"
