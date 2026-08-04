"""Speech synthesis and playback via Kokoro + PortAudio.

Most of the care in this module is defensive, and all of it is load-bearing.
The comments explaining why are kept verbatim from the original because each
records a real failure that was diagnosed the hard way -- an ALSA XRUN state
that wedges later writes, a PortAudio call that blocks in native code with no
Python-level exception, an executor shutdown that would block an interrupted
reply on a chunk still being synthesized.

The behavioural change here is the interrupt token. `speak()` used to capture
the interrupt generation at entry, which meant an interrupt that landed while
the model was still generating had already bumped the counter -- so the freshly
captured value matched, and the assistant talked straight over a user who was
mid-sentence. The token is now captured by the caller before generation starts
and passed in.
"""

from __future__ import annotations

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any

import sounddevice as sd
from kokoro_onnx import Kokoro

from minus.audio.chunking import split_text_into_chunks
from minus.audio.interrupt import InterruptBus
from minus.paths import models_dir

logger = logging.getLogger(__name__)

MODEL_PATH = models_dir() / "kokoro-v1.0.onnx"
VOICE_PATH = models_dir() / "voices-v1.0.bin"

# Chunks are kept short so that when an interrupt lands mid-chunk, letting that
# one chunk finish playing naturally is barely noticeable - a couple of seconds
# at most, bounded by this constant's worth of speech. This is what makes it
# safe to NOT call stream.abort() on interrupt: PortAudio's ALSA backend has
# been observed to leave the PCM device in a bad XRUN state after an abort()
# call (`alsa_snd_pcm_mmap_begin` failing internally in pa_linux_alsa.c), after
# which a later write() can block for 10+ seconds with no Python-level
# exception and no way to interrupt it - worse than just waiting out a short
# chunk. There's no host API other than ALSA available on this system to route
# around the bug, so avoiding abort() entirely sidesteps it instead.
CHUNK_MAX_CHARS = 40


@lru_cache(maxsize=1)
def get_kokoro() -> Kokoro:
    return Kokoro(str(MODEL_PATH), str(VOICE_PATH))


def call_with_timeout(func, timeout: float, description: str) -> bool:
    """Run a blocking PortAudio call with a hard time limit.

    write()/close() are supposed to return promptly, but PortAudio calls can
    still wedge in native code for reasons outside our control (a stuck audio
    device, driver flakiness). Run the call on a throwaway thread and give up on
    it after `timeout` rather than blocking forever - a leaked stream object is
    a vastly better outcome than the whole process (and, transitively, the
    recorder thread that joins on shutdown) wedging.
    """
    result: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            func()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        logger.error("%s did not return within %.1fs; abandoning it", description, timeout)
        return False
    if "error" in result:
        raise result["error"]
    return True


class KokoroSpeaker:
    """A SpeechSynthesizer backed by Kokoro ONNX and PortAudio.

    Must be called from the main thread: playback installs a SIGINT handler for
    its duration, and CPython only permits signal.signal() on the main thread.
    """

    def __init__(self, interrupts: InterruptBus, settings: Any | None = None) -> None:
        self.interrupts = interrupts
        self.voice = getattr(settings, "tts_voice", "am_puck")
        self.speed = getattr(settings, "tts_speed", 1.0)
        self.lang = getattr(settings, "tts_lang", "en-us")
        self.chunk_max_chars = getattr(settings, "tts_chunk_max_chars", CHUNK_MAX_CHARS)

    def token(self) -> int:
        """Capture the interrupt generation, to be passed back to speak()."""
        return self.interrupts.token()

    def _synthesize(self, text: str):
        return get_kokoro().create(text, voice=self.voice, speed=self.speed, lang=self.lang)

    def _close_stream(self, stream) -> None:
        if not stream.closed:
            try:
                call_with_timeout(stream.close, 2.0, "stream.close()")
            except Exception:
                logger.exception("stream.close() failed while tearing down playback stream")

    def speak(self, text: str, *, token: int | None = None) -> None:
        """Speak `text`, stopping early if an interrupt lands.

        Args:
            token: The interrupt generation captured before this reply was
                generated. If an interrupt arrived since, nothing is spoken --
                the user is already talking.
        """
        if self.interrupts.is_stale(token):
            logger.info("Skipping playback: the user interrupted during generation.")
            return

        start_generation = self.interrupts.token() if token is None else token

        chunks = split_text_into_chunks(text, max_chars=self.chunk_max_chars)
        if not chunks:
            return

        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda signum, frame: self.interrupts.request())

        stream = None
        stream_wedged = False
        # Not a `with` block: ThreadPoolExecutor.__exit__ always shuts down with
        # wait=True, which would block an interrupted speak() on whatever chunk
        # happens to be synthesizing in the background. shutdown(wait=False) in
        # the finally block below lets us return immediately and abandon it.
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            next_chunk = executor.submit(self._synthesize, chunks[0])

            for index in range(len(chunks)):
                if self.interrupts.token() != start_generation:
                    return

                samples, sample_rate = next_chunk.result()

                # Re-check before speculatively synthesizing the next chunk -
                # an interrupt may have landed while we were blocked above.
                if self.interrupts.token() != start_generation:
                    return

                if index + 1 < len(chunks):
                    next_chunk = executor.submit(self._synthesize, chunks[index + 1])

                if stream is None or stream.samplerate != sample_rate:
                    if stream is not None:
                        self._close_stream(stream)
                    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
                    stream.start()

                data = samples.reshape(-1, 1)
                # Generous margin over the chunk's natural playback duration -
                # long enough that a real (non-wedged) write never trips it.
                # Chunks are short, so this ceiling stays low.
                write_timeout = len(samples) / float(sample_rate) + 2.0

                try:
                    completed = call_with_timeout(
                        lambda s=stream, d=data: s.write(d), write_timeout, "stream.write()"
                    )
                except sd.PortAudioError:
                    logger.exception("stream.write() raised a PortAudioError")
                    return

                if not completed:
                    # The write is stuck in native code on a leaked thread with
                    # no way to cancel it - never touch this stream again,
                    # including closing it (close() could hang the same way).
                    stream_wedged = True
                    return
        except KeyboardInterrupt:
            self.interrupts.request()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            executor.shutdown(wait=False, cancel_futures=True)
            if stream is not None and not stream_wedged:
                self._close_stream(stream)
