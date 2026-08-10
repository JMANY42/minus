"""The silence at chunk seams: what to cut, and what to put back.

Kokoro pads every clip it synthesizes with a little silence -- measured on this
model at ~30ms before the speech and ~50ms after it. Inside one utterance that
is inaudible. But a reply is not one utterance: it is split into chunks (see
chunking.py) so playback can start before the whole reply is synthesized and so
a barge-in does not have to wait out the rest of it, and each chunk is its own
`create()` call carrying its own padding. Every seam therefore stacks one
clip's trailing pad onto the next clip's leading pad -- ~80ms of dead air,
480ms across a ten-second reply.

The gap is *not* starvation, which is worth recording because it is the obvious
first guess: synthesis measures 0.3x realtime and is pipelined one chunk ahead,
so the next clip is ready well before the current one finishes. Feeding the
stream faster would fix nothing. The dead air is inside the samples.

Cutting the pads is only half of it. Chunks now end where a speaker pauses, so
at most seams the padding was standing in -- badly, and by accident -- for a
pause that belongs there. Cut every pad, then put back a pause sized by what
the chunk actually ends with: a sentence gets a longer one than a comma, and a
seam forced mid-phrase by a clause too long to synthesize whole gets none at
all, so the two halves join with no break in between.
"""

from __future__ import annotations

import numpy as np

# Kokoro's padding sits at digital silence; ordinary breath and room tone in
# the speech itself stays well above this.
SILENCE_THRESHOLD = 1e-3

# Left on each side of the speech after trimming. A soft consonant onset --
# an "f" or a "th" -- starts below the threshold, so cutting flush against the
# first loud sample would clip the front of the word.
GUARD_SECONDS = 0.010

# Ramped at each cut edge. The cut lands in near-silence, so this is cheap
# insurance against a click rather than a fix for one.
FADE_SECONDS = 0.005

# Sized against what Kokoro does at the same punctuation inside a single
# utterance: a comma there is 60-110ms of near-silence, a full stop rather
# more. A seam is given the short end of that range, and the guard above adds
# ~20ms on top of whichever is used. Two reasons to stay short. The silence
# inserted here is digital, where Kokoro's own pause keeps low-level room tone
# under it and reads as continuous; and the chunk before a seam has already
# been rendered as a complete utterance, so its falling pitch and final
# lengthening are supplying part of the break before any silence is added.
SENTENCE_PAUSE_SECONDS = 0.18
CLAUSE_PAUSE_SECONDS = 0.04


def pause_after(chunk: str) -> float:
    """How long a pause the end of `chunk` calls for, in seconds.

    Zero for a chunk that ends mid-phrase: it was cut there because the clause
    would not fit in one synthesis call, not because anything ended.
    """
    stripped = chunk.rstrip("\"')]}")
    if stripped.endswith((".", "!", "?", "…")):
        return SENTENCE_PAUSE_SECONDS
    if stripped.endswith((",", ";", ":", "--", "—")):
        return CLAUSE_PAUSE_SECONDS
    return 0.0


def join_ready(samples: np.ndarray, sample_rate: int, tail_pause: float = 0.0) -> np.ndarray:
    """Strip a clip's padding and give it exactly the tail it should have.

    The result is meant to be written straight after the previous clip: what
    separates the two is `tail_pause` and nothing else.
    """
    clip = _ramp_edges(_trim_padding(samples, sample_rate), sample_rate)
    if clip.size == 0:
        return clip

    pause_frames = int(tail_pause * sample_rate)
    if pause_frames <= 0:
        return clip
    return np.concatenate([clip, np.zeros(pause_frames, dtype=clip.dtype)])


def _trim_padding(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    loud = np.flatnonzero(np.abs(samples) > SILENCE_THRESHOLD)
    if loud.size == 0:
        # Nothing but padding: a chunk of punctuation, or a synthesis that
        # produced no speech. Contributing no samples at all is right.
        return samples[:0]

    guard = int(GUARD_SECONDS * sample_rate)
    start = max(0, int(loud[0]) - guard)
    end = min(samples.size, int(loud[-1]) + 1 + guard)
    return samples[start:end]


def _ramp_edges(clip: np.ndarray, sample_rate: int) -> np.ndarray:
    fade = min(int(FADE_SECONDS * sample_rate), clip.size // 2)
    if fade <= 0:
        return clip

    ramped = clip.copy()
    ramp = np.linspace(0.0, 1.0, fade, dtype=clip.dtype)
    ramped[:fade] *= ramp
    ramped[-fade:] *= ramp[::-1]
    return ramped
