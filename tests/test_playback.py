"""Tests for the playback loop, with PortAudio and Kokoro stubbed out.

Needs the `audio` extra installed to import the module at all (sounddevice and
kokoro_onnx are imported at module scope), but touches neither: the stream is a
fake that records what was written to it, and synthesis is replaced by a
constant tone. What is being checked is the loop's arithmetic -- that a reply
is written in full and drained, and that a barge-in stops it within a block
rather than at the end of a chunk.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sounddevice")
pytest.importorskip("kokoro_onnx")

from minus.audio.interrupt import InterruptBus  # noqa: E402
from minus.audio.tts import PLAYBACK_BLOCK_SECONDS, KokoroSpeaker  # noqa: E402

RATE = 24000
SECONDS_PER_CHUNK = 2.0


class FakeStream:
    """Enough of sd.OutputStream for the playback loop, with a write hook."""

    def __init__(self, samplerate, channels, dtype, on_write=None):
        self.samplerate = samplerate
        self.closed = False
        self.started = False
        self.stopped = False
        self.frames_written = 0
        self.blocks = 0
        self._on_write = on_write

    def start(self):
        self.started = True

    def write(self, data):
        self.frames_written += len(data)
        self.blocks += 1
        if self._on_write:
            self._on_write(self)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def speaker_with_stream(monkeypatch):
    """A speaker whose synthesis and audio device are both fakes."""
    streams: list[FakeStream] = []

    def build(on_write=None, chunk_max_chars=40):
        bus = InterruptBus()

        def factory(samplerate, channels, dtype):
            stream = FakeStream(samplerate, channels, dtype, on_write)
            streams.append(stream)
            return stream

        monkeypatch.setattr("minus.audio.tts.sd.OutputStream", factory)
        # A steady tone: loud everywhere, so seam trimming keeps all of it and
        # frame counts stay predictable.
        tone = np.full(int(SECONDS_PER_CHUNK * RATE), 0.5, dtype=np.float32)
        monkeypatch.setattr(KokoroSpeaker, "_synthesize", lambda self, text: (tone, RATE))

        speaker = KokoroSpeaker(bus)
        speaker.chunk_max_chars = chunk_max_chars
        speaker.first_chunk_max_chars = chunk_max_chars
        return speaker, bus, streams

    return build


TEXT = " ".join(f"word{i}" for i in range(60))


class TestPlayback:
    def test_a_whole_reply_is_written_and_drained(self, speaker_with_stream):
        speaker, _, streams = speaker_with_stream()

        speaker.speak(TEXT)

        stream = streams[0]
        assert stream.frames_written > 0
        # write() only hands samples to PortAudio; without the drain the tail
        # of the reply would be discarded by close().
        assert stream.stopped
        assert stream.closed

    def test_it_is_written_in_blocks_not_one_call_per_chunk(self, speaker_with_stream):
        speaker, _, streams = speaker_with_stream()

        speaker.speak(TEXT)

        stream = streams[0]
        blocks_per_chunk = SECONDS_PER_CHUNK / PLAYBACK_BLOCK_SECONDS
        assert stream.blocks >= blocks_per_chunk

    def test_a_barge_in_stops_within_a_block(self, speaker_with_stream):
        """The bug this guards: an interrupt used to wait out the whole chunk."""
        barge_in: list[InterruptBus] = []

        def interrupt_after_two_blocks(stream):
            if stream.blocks == 2:
                barge_in[0].request()

        speaker, bus, streams = speaker_with_stream(on_write=interrupt_after_two_blocks)
        barge_in.append(bus)

        speaker.speak(TEXT)

        stream = streams[0]
        assert stream.blocks == 2
        assert stream.frames_written <= 2 * int(PLAYBACK_BLOCK_SECONDS * RATE)
        # Interrupted playback is torn down, not drained: what PortAudio still
        # holds is exactly what we no longer want played.
        assert not stream.stopped
        assert stream.closed

    def test_an_interrupt_before_speaking_plays_nothing(self, speaker_with_stream):
        speaker, bus, streams = speaker_with_stream()
        token = speaker.token()
        bus.request()

        speaker.speak(TEXT, token=token)

        assert streams == []
