from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import os
import re
import signal
import threading

from kokoro_onnx import Kokoro
import sounddevice as sd


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models"))
MODEL_PATH = os.path.join(BASE_DIR, "kokoro-v1.0.onnx")
VOICE_PATH = os.path.join(BASE_DIR, "voices-v1.0.bin")


_interrupt_lock = threading.Lock()
_interrupt_generation = 0


@lru_cache(maxsize=1)
def get_kokoro():
    return Kokoro(MODEL_PATH, VOICE_PATH)


def request_interrupt():
    global _interrupt_generation

    with _interrupt_lock:
        _interrupt_generation += 1

    sd.stop()


def _current_interrupt_generation():
    with _interrupt_lock:
        return _interrupt_generation


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
    chunks = _split_text_into_chunks(text, max_chars=200)
    if not chunks:
        return

    def _handle_sigint(signum, frame):
        request_interrupt()

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            next_chunk_future = executor.submit(_synthesize_chunk, chunks[0], voice, speed, lang)

            for index in range(len(chunks)):
                if _current_interrupt_generation() != start_generation:
                    return

                samples, sample_rate = next_chunk_future.result()

                if index + 1 < len(chunks):
                    next_chunk_future = executor.submit(
                        _synthesize_chunk,
                        chunks[index + 1],
                        voice,
                        speed,
                        lang,
                    )

                if _current_interrupt_generation() != start_generation:
                    return

                sd.play(samples, sample_rate)
                sd.wait()
    except KeyboardInterrupt:
        request_interrupt()
    finally:
        signal.signal(signal.SIGINT, previous_handler)

# for testing
if __name__ == "__main__":
    speak("hello")
#     speak("In a tiny village by an old river, there lived a young girl named Luna. She had a special gift - she could talk to the stars at night. One evening, she asked a shooting star, 'What's the most magical thing you've seen on your journey?' The star whispered, 'A hidden rainbow bridge over a shimmering lake.' Intrigued, Luna set out to find this bridge. She followed a winding path, and the stars guided her through the darkness. After hours of walking, she heard the gentle sound of lapping water. As she turned a corner, a beautiful rainbow bridge materialized before her eyes, spanning the shimmering lake. Luna took a deep breath and stepped onto the bridge, feeling the magic of the unknown beneath her feet.")