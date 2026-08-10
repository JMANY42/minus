"""Tests for the silence at chunk seams.

Separate from test_audio.py because these need numpy, which arrives with the
`audio` extra rather than the core dependencies. The module under test needs
nothing else -- no PortAudio, no ONNX runtime -- so the arithmetic that decides
what a seam sounds like is checkable without a sound card.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from minus.audio.seams import (  # noqa: E402
    CLAUSE_PAUSE_SECONDS,
    GUARD_SECONDS,
    SENTENCE_PAUSE_SECONDS,
    join_ready,
    pause_after,
)

RATE = 24000


def padded_clip(lead=0.030, speech=0.200, trail=0.050, amplitude=0.5):
    """A clip shaped like Kokoro's: speech with silence padded on either side."""
    return np.concatenate(
        [
            np.zeros(int(lead * RATE), dtype=np.float32),
            np.full(int(speech * RATE), amplitude, dtype=np.float32),
            np.zeros(int(trail * RATE), dtype=np.float32),
        ]
    )


class TestPauseAfter:
    @pytest.mark.parametrize("chunk", ["That was all.", "Really?", "Stop!", 'She said "no."'])
    def test_a_sentence_end_gets_the_longer_pause(self, chunk):
        assert pause_after(chunk) == SENTENCE_PAUSE_SECONDS

    @pytest.mark.parametrize("chunk", ["in a valley that lay,", "one thing;", "as follows:"])
    def test_a_clause_end_gets_the_shorter_pause(self, chunk):
        assert pause_after(chunk) == CLAUSE_PAUSE_SECONDS

    def test_a_seam_forced_mid_phrase_gets_none(self):
        """Nothing ended here - the clause was just too long to synthesize."""
        assert pause_after("in a valley that lay") == 0.0


class TestJoinReady:
    def test_padding_is_trimmed_to_the_guard(self):
        clip = join_ready(padded_clip(), RATE)

        expected = int(0.200 * RATE) + 2 * int(GUARD_SECONDS * RATE)
        assert clip.size == pytest.approx(expected, abs=2)

    def test_two_clips_now_join_with_no_dead_air_between_them(self):
        """The bug: ~80ms of stacked padding at every seam."""
        joined = np.concatenate([join_ready(padded_clip(), RATE), join_ready(padded_clip(), RATE)])

        quiet = np.abs(joined) <= 1e-3
        longest_gap = max(
            (len(run) for run in "".join("q" if q else "." for q in quiet).split(".")), default=0
        )
        assert longest_gap / RATE < 2 * GUARD_SECONDS + 0.001

    def test_a_requested_pause_is_appended_exactly(self):
        without = join_ready(padded_clip(), RATE)
        with_pause = join_ready(padded_clip(), RATE, tail_pause=SENTENCE_PAUSE_SECONDS)

        assert with_pause.size - without.size == int(SENTENCE_PAUSE_SECONDS * RATE)
        assert not with_pause[without.size :].any()

    def test_a_clip_with_no_speech_contributes_nothing(self):
        assert join_ready(np.zeros(RATE, dtype=np.float32), RATE).size == 0

    def test_cut_edges_are_ramped_so_the_join_cannot_click(self):
        """A clip Kokoro did not pad gets cut flush against the waveform."""
        clip = join_ready(np.full(RATE, 0.5, dtype=np.float32), RATE)

        assert clip[0] == pytest.approx(0.0, abs=1e-6)
        assert clip[-1] == pytest.approx(0.0, abs=1e-6)
        assert clip[clip.size // 2] == pytest.approx(0.5)

    def test_the_source_clip_is_left_alone(self):
        original = padded_clip()
        before = original.copy()

        join_ready(original, RATE, tail_pause=CLAUSE_PAUSE_SECONDS)

        assert np.array_equal(original, before)
