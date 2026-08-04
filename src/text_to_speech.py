from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import logging
import os
import re
import signal
import threading

from kokoro_onnx import Kokoro
import sounddevice as sd


logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models"))
MODEL_PATH = os.path.join(BASE_DIR, "kokoro-v1.0.onnx")
VOICE_PATH = os.path.join(BASE_DIR, "voices-v1.0.bin")


_interrupt_lock = threading.Lock()
_interrupt_generation = 0

# Chunks are kept short so that when an interrupt lands mid-chunk, letting
# that one chunk finish playing naturally is barely noticeable - a couple of
# seconds at most, bounded by this constant's worth of speech. This is what
# makes it safe to NOT call stream.abort() on interrupt (see
# request_interrupt() below): PortAudio's ALSA backend has been observed to
# leave the PCM device in a bad XRUN state after an abort() call
# (`alsa_snd_pcm_mmap_begin` failing internally in pa_linux_alsa.c), after
# which a later write() can block for 10+ seconds with no Python-level
# exception and no way to interrupt it - worse than just waiting out a short
# chunk. There's no host API other than ALSA available on this system to
# route around the bug, so avoiding abort() entirely sidesteps it instead.
CHUNK_MAX_CHARS = 40


@lru_cache(maxsize=1)
def get_kokoro():
    return Kokoro(MODEL_PATH, VOICE_PATH)


def _call_with_timeout(func, timeout, description):
    """Run a blocking PortAudio call with a hard time limit.

    write()/close() are supposed to return promptly, but PortAudio calls can
    still wedge in native code for reasons outside our control (a stuck
    audio device, driver flakiness). Run the call on a throwaway thread and
    give up on it after `timeout` rather than blocking forever - a leaked
    stream object is a vastly better outcome than the whole process (and,
    transitively, the recorder thread that joins on shutdown) wedging.
    """
    result = {}

    def _run():
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


def request_interrupt():
    global _interrupt_generation

    with _interrupt_lock:
        _interrupt_generation += 1

    # Deliberately doesn't touch the playback stream (no abort()) - see the
    # CHUNK_MAX_CHARS comment above. speak()'s generation check between
    # chunks is what actually stops playback, within one short chunk's delay.


def _current_interrupt_generation():
    with _interrupt_lock:
        return _interrupt_generation


def _close_stream(stream):
    if not stream.closed:
        try:
            _call_with_timeout(stream.close, 2.0, "stream.close()")
        except Exception:
            logger.exception("stream.close() failed while tearing down playback stream")


def _split_text_into_chunks(text, max_chars=200):
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []

    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks = []
    # Prefer sentence boundaries first, then words, while keeping a hard limit.
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)

    for sentence in sentences:
        if not sentence:
            continue

        if len(sentence) <= max_chars:
            if chunks and len(chunks[-1]) + 1 + len(sentence) <= max_chars:
                chunks[-1] = f"{chunks[-1]} {sentence}"
            else:
                chunks.append(sentence)
            continue

        words = sentence.split()
        current = ""
        for word in words:
            if len(word) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                start = 0
                while start < len(word):
                    chunks.append(word[start : start + max_chars])
                    start += max_chars
                continue

            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_chars:
                current = candidate
            else:
                chunks.append(current)
                current = word

        if current:
            if chunks and len(chunks[-1]) + 1 + len(current) <= max_chars:
                chunks[-1] = f"{chunks[-1]} {current}"
            else:
                chunks.append(current)

    return chunks


def _synthesize_chunk(text, voice, speed, lang):
    return get_kokoro().create(text, voice=voice, speed=speed, lang=lang)


def speak(text, voice="am_puck", speed=1.0, lang="en-us"):
    start_generation = _current_interrupt_generation()
    chunks = _split_text_into_chunks(text, max_chars=CHUNK_MAX_CHARS)
    if not chunks:
        return

    def _handle_sigint(signum, frame):
        request_interrupt()

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)

    stream = None
    stream_wedged = False
    # Not a `with` block: ThreadPoolExecutor.__exit__ always shuts down with
    # wait=True, which would block an interrupted speak() on whatever chunk
    # happens to be synthesizing in the background. shutdown(wait=False) in
    # the finally block below lets us return immediately and abandon it.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        next_chunk_future = executor.submit(_synthesize_chunk, chunks[0], voice, speed, lang)

        for index in range(len(chunks)):
            if _current_interrupt_generation() != start_generation:
                return

            samples, sample_rate = next_chunk_future.result()

            # Re-check before speculatively synthesizing the next chunk -
            # an interrupt may have landed while we were blocked above.
            if _current_interrupt_generation() != start_generation:
                return

            if index + 1 < len(chunks):
                next_chunk_future = executor.submit(
                    _synthesize_chunk,
                    chunks[index + 1],
                    voice,
                    speed,
                    lang,
                )

            if stream is None or stream.samplerate != sample_rate:
                if stream is not None:
                    _close_stream(stream)
                stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
                stream.start()

            data = samples.reshape(-1, 1)
            # Generous margin over the chunk's natural playback duration -
            # long enough that a real (non-wedged) write never trips it.
            # Chunks are short (CHUNK_MAX_CHARS), so this ceiling stays low.
            write_timeout = len(samples) / float(sample_rate) + 2.0

            try:
                completed = _call_with_timeout(lambda: stream.write(data), write_timeout, "stream.write()")
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
        request_interrupt()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        executor.shutdown(wait=False, cancel_futures=True)
        if stream is not None and not stream_wedged:
            _close_stream(stream)

# for testing
if __name__ == "__main__":
    speak("hello")
#     speak("In a tiny village by an old river, there lived a young girl named Luna. She had a special gift - she could talk to the stars at night. One evening, she asked a shooting star, 'What's the most magical thing you've seen on your journey?' The star whispered, 'A hidden rainbow bridge over a shimmering lake.' Intrigued, Luna set out to find this bridge. She followed a winding path, and the stars guided her through the darkness. After hours of walking, she heard the gentle sound of lapping water. As she turned a corner, a beautiful rainbow bridge materialized before her eyes, spanning the shimmering lake. Luna took a deep breath and stepped onto the bridge, feeling the magic of the unknown beneath her feet.")