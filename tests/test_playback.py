"""Tests for the playback loop, with PortAudio and Kokoro stubbed out.

Needs the `audio` extra installed to import the module at all (sounddevice and
kokoro_onnx are imported at module scope), but touches neither: the stream is a
fake that records what was written to it, and synthesis is replaced by a
constant tone. What is being checked is the loop's arithmetic -- that a reply
is written in full and drained, and that a barge-in stops it within a block
rather than at the end of a chunk.
"""

from __future__ import annotations

import signal
import threading

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


class TestCtrlCIsHeardWhoeverIsSpeaking:
    """Playback's half of the Ctrl-C fix.

    The speaker no longer installs a SIGINT handler -- it could not, on the
    courier thread that speaks escalated answers, which is how Ctrl-C during a
    deep answer came to quit MINUS. All it does now is mark playback
    interruptible, which is what the main thread's handler reads.
    """

    def test_the_bus_is_active_while_audio_is_being_written(self, speaker_with_stream):
        active_during_write: list[bool] = []
        holder: list[InterruptBus] = []

        speaker, bus, _ = speaker_with_stream(
            on_write=lambda stream: active_during_write.append(holder[0].is_active())
        )
        holder.append(bus)

        assert not bus.is_active()
        speaker.speak(TEXT)

        assert active_during_write and all(active_during_write)
        assert not bus.is_active()

    def test_it_is_cleared_after_a_barge_in_cuts_playback_short(self, speaker_with_stream):
        holder: list[InterruptBus] = []

        speaker, bus, _ = speaker_with_stream(
            on_write=lambda stream: holder[0].request() if stream.blocks == 2 else None
        )
        holder.append(bus)

        speaker.speak(TEXT)

        # Otherwise the next Ctrl-C would be swallowed as a second barge-in
        # instead of quitting.
        assert not bus.is_active()

    def test_speaking_from_a_worker_thread_installs_no_handler(self, speaker_with_stream):
        """The courier's thread: signal.signal() there would raise ValueError."""
        speaker, _, streams = speaker_with_stream()
        before = signal.getsignal(signal.SIGINT)
        failures: list[BaseException] = []

        def courier():
            try:
                speaker.speak(TEXT)
            except BaseException as exc:  # pragma: no cover - the failure we guard
                failures.append(exc)

        thread = threading.Thread(target=courier)
        thread.start()
        thread.join(timeout=10.0)

        assert not failures
        assert streams and streams[0].frames_written > 0
        assert signal.getsignal(signal.SIGINT) is before
