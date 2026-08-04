"""Splitting reply text into synthesis-sized chunks.

Pure text processing, deliberately kept in its own module: it has no
relationship to PortAudio or ONNX, and living in tts.py meant it could not be
imported -- or tested -- without the whole native audio stack present.

Chunks are small because an interrupt is only honoured between them; see the
CHUNK_MAX_CHARS comment in tts.py for why playback is never aborted mid-chunk.
"""

from __future__ import annotations

import re


def split_text_into_chunks(text: str, max_chars: int = 200) -> list[str]:
    """Split text for synthesis, preferring sentence then word boundaries.

    Every returned chunk is at most `max_chars`; a single word longer than the
    limit is hard-split rather than allowed to overflow.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks: list[str] = []
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

        current = ""
        for word in sentence.split():
            if len(word) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                for start in range(0, len(word), max_chars):
                    chunks.append(word[start : start + max_chars])
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
