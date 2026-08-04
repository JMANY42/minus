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
from unittest.mock import patch

import pytest

from minus.audio.chunking import split_text_into_chunks
from minus.audio.interrupt import InterruptBus
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
