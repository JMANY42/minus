"""Splitting reply text into synthesis-sized chunks.

Pure text processing, deliberately kept in its own module: it has no
relationship to PortAudio or ONNX, and living in tts.py meant it could not be
imported -- or tested -- without the whole native audio stack present.

WHERE A CHUNK MAY END
---------------------
A chunk boundary is audible whether or not there is silence at it: each chunk
is a separate `create()` call, so Kokoro renders it as a complete utterance,
with the falling pitch and final lengthening a speaker gives the end of a
phrase. Put that boundary after "in a valley that lay" and the listener hears
a phrase ending where the sentence has not ended.

So boundaries are placed where a speaker would already pause -- sentence end
first, then clause punctuation -- and only word-wrapped when a single clause
is longer than the budget. Chunk size no longer bounds barge-in latency (the
speaker checks for an interrupt every audio block, not every chunk), so the
budget is set by how much dead air the seams cost and how long the first chunk
makes the listener wait, not by how fast an interrupt must be honoured.

HOW BIG A CHUNK MAY BE
----------------------
The first chunk has its own, much smaller budget: nothing is heard until it has
been synthesized, so it trades a seam for a faster start. The budget then grows
until it reaches the full size.

It grows rather than jumping there because synthesis of the next chunk runs
while the current one plays, and it is not free: measured at ~0.4x realtime, a
chunk takes 40% of its own duration to produce. A short chunk followed by a
full-size one therefore leaves the stream with nothing to play -- with an 80
character opener and a 300 character follow-up, 3.9s of audio had to cover 6.1s
of synthesis, and the listener heard a 2.3s hole at the first seam. So each
chunk may be as long as everything already queued ahead of it: the budget
doubles at every seam until it reaches the full size, and the margin over what
synthesis costs stays the same the whole way up.
"""

from __future__ import annotations

import re
from collections import deque

# Both alternatives are fixed-width, which a lookbehind requires: a sentence
# ends at its punctuation, or at a closing quote or bracket after it.
_SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"')\]]))\s+")

# Clause punctuation, plus a spaced dash. All require the whitespace that
# follows, so "1,000" and "e.g." are left alone.
_CLAUSE_BREAK = re.compile(r"(?:(?<=[,;:])|(?<=--)|(?<=—))\s+")

# When a cut has to happen with no punctuation to hang it on, it goes in front
# of a word that opens a phrase -- "...that lay | between two great mountains"
# rather than "...there was a | village". Both are seams; only the second
# leaves a dangling determiner and a fragment that no speaker would end on.
_CONJUNCTIONS = frozenset(
    {"and", "but", "or", "nor", "so", "yet", "because", "although", "though", "while", "when",
     "whenever", "where", "wherever", "if", "unless", "until", "since", "as", "that", "which",
     "who", "whom", "whose", "whether"}
)  # fmt: skip
_PREPOSITIONS = frozenset(
    {"after", "before", "during", "between", "among", "through", "into", "onto", "upon", "with",
     "without", "within", "against", "about", "above", "below", "beneath", "beyond", "across",
     "around", "behind", "from", "in", "on", "at", "to", "by", "for", "of", "over", "under",
     "near", "despite"}
)  # fmt: skip
_DETERMINERS = frozenset(
    {"a", "an", "the", "this", "these", "those", "my", "your", "his", "her", "its", "our", "their"}
)

# A determiner opens a phrase too, but only just: cutting before "the village"
# is tolerable where cutting before "between two great mountains" is barely
# noticeable, so determiners are the fallback tier rather than the first choice.
_PHRASE_STARTERS = _CONJUNCTIONS | _PREPOSITIONS
_WEAK_STARTERS = _DETERMINERS


def split_text_into_chunks(
    text: str, max_chars: int = 200, first_max_chars: int | None = None
) -> list[str]:
    """Split text for synthesis at the points a speaker would pause.

    Every returned chunk is at most `max_chars`, and the first is at most
    `first_max_chars` (defaulting to `max_chars`); a single word longer than
    the limit is hard-split rather than allowed to overflow.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []

    first_limit = min(max_chars, first_max_chars or max_chars)

    units: deque[str] = deque()
    for sentence in _SENTENCE_BREAK.split(cleaned):
        if not sentence:
            continue
        for clause in _CLAUSE_BREAK.split(sentence):
            if clause:
                units.extend(_wrap_words(clause, max_chars))

    chunks: list[str] = []
    spoken = 0
    while units:
        limit = _chunk_limit(spoken, first_limit, max_chars)

        chunk = units.popleft()
        if len(chunk) > limit:
            # A clause longer than this chunk's budget. Cutting it is what
            # keeps the pipeline fed; the cut goes at the best boundary the
            # clause offers and the rest is packed into the chunks after it.
            head, rest = _split_head(chunk, limit)
            if rest:
                units.appendleft(rest)
            chunk = head

        while units and len(chunk) + 1 + len(units[0]) <= limit:
            chunk = f"{chunk} {units.popleft()}"

        chunks.append(chunk)
        spoken += len(chunk)

    return chunks


def _chunk_limit(spoken_so_far: int, first_limit: int, max_chars: int) -> int:
    """The budget for the next chunk, ramping up to the full size.

    A chunk may be as long as everything queued ahead of it, which is the
    audio available to cover its synthesis. At the measured ~0.4x realtime that
    leaves a wide margin, and it is the ratio -- not any particular number of
    characters -- that keeps the margin the same at every seam.

    Measured against what previous chunks actually came out as, not what they
    were allowed to be: chunks routinely land well under budget because a clause
    ended, and a chunk that is short for that reason buys only its own length in
    playback time. Ramping off the budget instead is what left a 2.3s hole at
    the first seam. Ramping off the previous chunk alone has the opposite
    failure -- a run of short clauses pins the budget at the floor and it never
    grows -- which is why this accumulates.
    """
    return max(first_limit, min(max_chars, spoken_so_far))


def _split_head(text: str, limit: int) -> tuple[str, str]:
    """Cut `text` at the best boundary that fits in `limit`.

    Returns the text unsplit if not even the first word fits: breaking inside a
    word would put a seam mid-syllable, which is worse than a chunk that runs
    long.
    """
    words = text.split()
    fits = _words_that_fit(words, limit)
    if fits == 0:
        # The opening word alone overruns the budget. Keeping it whole is the
        # lesser evil; the next word still starts a new chunk.
        return words[0], " ".join(words[1:])
    if fits == len(words):
        return text, ""

    cut = _phrase_boundary(words, fits) or fits
    return " ".join(words[:cut]), " ".join(words[cut:])


def _words_that_fit(words: list[str], limit: int) -> int:
    """How many of `words` fit in `limit`, joined by single spaces."""
    length = 0
    for index, word in enumerate(words):
        length += len(word) + (1 if index else 0)
        if length > limit:
            return index
    return len(words)


def _phrase_boundary(words: list[str], fits: int) -> int | None:
    """The latest cut point in `words[:fits]` that opens a phrase, if any.

    A boundary is only worth taking if it leaves a chunk of reasonable size:
    cutting after the second word to catch an "and" trades one bad seam for a
    scrap of a chunk and a seam anyway.
    """
    floor = max(1, fits // 2)
    for tier in (_PHRASE_STARTERS, _WEAK_STARTERS):
        for cut in range(fits, floor - 1, -1):
            if words[cut].strip("\"'([").lower() in tier:
                return cut
    return None


def _wrap_words(text: str, max_chars: int) -> list[str]:
    """Break a clause too long to synthesize in one piece.

    Every seam this produces is one the text gave no good place for, so each is
    put at the best phrase boundary available. A single word longer than the
    limit -- a URL, usually -- is cut mid-word as a last resort.
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    rest = text
    while rest:
        head, rest = _split_head(rest, max_chars)
        if len(head) > max_chars:
            pieces.extend(
                head[start : start + max_chars] for start in range(0, len(head), max_chars)
            )
        else:
            pieces.append(head)
    return pieces
