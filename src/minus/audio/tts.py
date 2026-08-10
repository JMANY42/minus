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

The other change worth knowing about is that chunking and barge-in latency are
no longer the same knob. Chunks used to be tiny because an interrupt was only
noticed between them, and that made replies sound chopped up: a boundary every
couple of seconds, each one rendered by Kokoro as a phrase ending and separated
from the next by both clips' silence padding. Clips are now written to the
stream a block at a time with the interrupt checked before each one, which
leaves chunking free to cut where a speaker would pause (chunking.py) and the
seams free to be tightened to the pause that belongs there (seams.py).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from functools import lru_cache
from typing import Any

import sounddevice as sd
from kokoro_onnx import Kokoro

from minus.audio.chunking import split_text_into_chunks
from minus.audio.interrupt import InterruptBus
from minus.audio.seams import join_ready, pause_after
from minus.paths import models_dir

logger = logging.getLogger(__name__)

MODEL_PATH = models_dir() / "kokoro-v1.0.onnx"
VOICE_PATH = models_dir() / "voices-v1.0.bin"

# Chunk size is a speech-quality knob, not a latency one. Every chunk is a
# separate synthesis call that Kokoro renders as a complete utterance, so a
# seam is audible as a phrase ending wherever it falls; chunks are therefore
# big enough that seams are rare and land where a speaker would pause anyway
# (chunking.py places them). The first chunk gets a smaller budget because
# nothing is heard until it is synthesized.
CHUNK_MAX_CHARS = 300
FIRST_CHUNK_MAX_CHARS = 60

# What actually bounds barge-in latency: a clip is written to the stream this
# much at a time, and the interrupt is checked before every block. That is what
# makes it safe to NOT call stream.abort() on interrupt: PortAudio's ALSA
# backend has been observed to leave the PCM device in a bad XRUN state after
# an abort() call (`alsa_snd_pcm_mmap_begin` failing internally in
# pa_linux_alsa.c), after which a later write() can block for 10+ seconds with
# no Python-level exception and no way to interrupt it. There's no host API
# other than ALSA available on this system to route around the bug, so we stop
# feeding the stream instead and let close() take it down.
PLAYBACK_BLOCK_SECONDS = 0.05

# How many chunks may be synthesized ahead of the one playing. Bounded
# rather than "all of them": synthesis competes for CPU with the recogniser
# listening for the barge-in this whole design exists to honour.
LOOKAHEAD_CHUNKS = 3


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

    Safe to call from a worker thread, and thread-agnostic about Ctrl-C:
    playback runs inside `InterruptBus.interruptible()`, and the handler that
    reads that lives on the main thread for the whole conversation
    (`barge_in_on_sigint`). Installing it per-playback here is what used to
    make Ctrl-C during an escalated answer -- spoken from the courier thread,
    where signal.signal() is not allowed -- quit the process instead of
    stopping the speech.
    """

    def __init__(self, interrupts: InterruptBus, settings: Any | None = None) -> None:
        self.interrupts = interrupts
        self.voice = getattr(settings, "tts_voice", "am_puck")
        self.speed = getattr(settings, "tts_speed", 1.0)
        self.lang = getattr(settings, "tts_lang", "en-us")
        self.chunk_max_chars = getattr(settings, "tts_chunk_max_chars", CHUNK_MAX_CHARS)
        self.first_chunk_max_chars = getattr(
            settings, "tts_first_chunk_max_chars", FIRST_CHUNK_MAX_CHARS
        )

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

    def _drain_stream(self, stream) -> bool:
        """Let the audio still buffered in PortAudio finish playing.

        write() returns once the samples are handed to PortAudio, not once they
        are audible, so a reply that has been fully written still has up to the
        stream's latency left to play. close() discards that, which used to be
        masked by the trailing silence Kokoro pads onto every clip - now that
        the pad is trimmed off, closing without draining would clip the last
        word. stop() plays it out first.
        """
        try:
            return call_with_timeout(stream.stop, 2.0, "stream.stop()")
        except Exception:
            logger.exception("stream.stop() failed while draining playback stream")
            return False

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

        chunks = split_text_into_chunks(
            text, max_chars=self.chunk_max_chars, first_max_chars=self.first_chunk_max_chars
        )
        if not chunks:
            return

        # Everything from here until the stream is torn down is something a
        # barge-in can usefully cut short, which is what tells the main
        # thread's SIGINT handler that Ctrl-C means "stop talking" rather than
        # "quit" -- whichever thread this playback is running on.
        with self.interrupts.interruptible():
            self._play(chunks, start_generation)

    def _play(self, chunks: list[str], start_generation: int) -> None:
        """Synthesize and write `chunks`, stopping at the first interrupt."""
        stream = None
        stream_wedged = False
        # Not a `with` block: ThreadPoolExecutor.__exit__ always shuts down with
        # wait=True, which would block an interrupted speak() on whatever chunk
        # happens to be synthesizing in the background. shutdown(wait=False) in
        # the finally block below lets us return immediately and abandon it.
        executor = ThreadPoolExecutor(max_workers=1)
        pending: deque[Future] = deque()
        submitted = 0
        try:
            for index in range(len(chunks)):
                # Keep the synthesizer working several chunks ahead rather than
                # exactly one. Synthesis is faster than playback, so with a
                # single chunk of lookahead the worker idles through most of
                # every chunk; letting it run ahead banks that time as lead,
                # which is what absorbs a chunk that turns out slower than the
                # one playing bought time for.
                while len(pending) < LOOKAHEAD_CHUNKS and submitted < len(chunks):
                    pending.append(executor.submit(self._synthesize, chunks[submitted]))
                    submitted += 1

                if self.interrupts.token() != start_generation:
                    return

                samples, sample_rate = pending.popleft().result()

                # Re-check after blocking above - an interrupt may have landed
                # while we waited for synthesis.
                if self.interrupts.token() != start_generation:
                    return

                if stream is None or stream.samplerate != sample_rate:
                    if stream is not None and self._drain_stream(stream):
                        # Drained first for the same reason as at the end of a
                        # reply: whatever is still buffered is speech.
                        self._close_stream(stream)
                    stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32")
                    stream.start()

                # Kokoro's padding is cut off and replaced with the pause this
                # particular seam calls for; the reply's own end needs neither.
                last_chunk = index + 1 == len(chunks)
                clip = join_ready(
                    samples,
                    sample_rate,
                    tail_pause=0.0 if last_chunk else pause_after(chunks[index]),
                )
                if clip.size == 0:
                    continue

                data = clip.reshape(-1, 1)
                block_frames = max(1, int(PLAYBACK_BLOCK_SECONDS * sample_rate))

                for start in range(0, len(data), block_frames):
                    # Checked per block rather than per chunk: chunks are now
                    # sized for natural-sounding speech and can run to several
                    # seconds, which is far too long to keep talking over
                    # someone who has started speaking.
                    if self.interrupts.token() != start_generation:
                        return

                    block = data[start : start + block_frames]
                    # Generous margin over the block's playback duration - long
                    # enough that a real (non-wedged) write never trips it.
                    write_timeout = len(block) / float(sample_rate) + 2.0

                    try:
                        completed = call_with_timeout(
                            lambda s=stream, d=block: s.write(d), write_timeout, "stream.write()"
                        )
                    except sd.PortAudioError:
                        logger.exception("stream.write() raised a PortAudioError")
                        return

                    if not completed:
                        # The write is stuck in native code on a leaked thread
                        # with no way to cancel it - never touch this stream
                        # again, including closing it (close() could hang the
                        # same way).
                        stream_wedged = True
                        return

            # Only reached when the whole reply was written without an
            # interrupt: there is buffered audio worth waiting for.
            if stream is not None and not self._drain_stream(stream):
                stream_wedged = True
        except KeyboardInterrupt:
            # Only reachable when no barge-in handler is installed -- the
            # module's own __main__ demo, or an embedding caller that never
            # entered barge_in_on_sigint. Still treated as barge-in rather
            # than propagating out of a reply that is half spoken.
            self.interrupts.request()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            if stream is not None and not stream_wedged:
                self._close_stream(stream)


if __name__ == "__main__":
    kokoro = KokoroSpeaker(InterruptBus())
    kokoro.speak(
        """Once upon a time, in a valley that lay between two great mountains, there was a village that everyone called Willowbrook.
        The village was small, with only a handful of houses made of stone and timber, and a single winding road that led to the market
        town in the next valley. The people of Willowbrook were simple folk, mostly farmers and woodcutters, who lived in harmony with the land. 
        They had a tradition of telling stories by the fire each night, and the elders would weave tales of heroes, monsters, and the mysteries 
        of the world.\n\nOne crisp autumn evening, as the sun dipped behind the mountains and painted the sky in hues of amber and violet, a young 
        boy named Milo sat by the hearth with his grandmother. Milo was a curious child, always asking questions about the world beyond the valley. 
        His grandmother, a woman with silver hair and eyes that seemed to hold the secrets of the ages, smiled and began to tell him a story that 
        would stay with him for the rest of his life.\n\n"Long ago," she began, "there was a kingdom that stretched from the sea to the mountains, 
        ruled by a wise king named Alaric. King Alaric was known for his fairness and his love for the arts. He had a daughter, Princess Elara, 
        who was as brave as she was beautiful. She had a heart that beat for adventure, and she dreamed of seeing the world beyond the kingdom\'s 
        borders."\n\nMilo listened intently, his eyes wide with wonder. "Did she ever go on an adventure?" he asked.\n\n"Yes," his grandmother replied. 
        "One day, a mysterious traveler arrived at the palace gates. He was a wanderer, with a cloak of midnight blue and a staff that glowed with a faint, 
        otherworldly light. He told the king of a hidden valley, a place where the stars fell to the earth and turned into silver rivers. 
        He said that the valley was guarded by a dragon, but that the dragon was not a beast of fire and terror, but a guardian of knowledge."
        \n\nMilo\'s imagination ran wild. He pictured a dragon with scales that shimmered like the night sky, breathing not fire but stardust. 
        "Did the princess go to the valley?" he asked.\n\n"She did," his grandmother said. "Princess Elara, with her heart full of courage, set out 
        with a small band of loyal companions: a blacksmith named Roderick, a healer named Liora, and a young scribe named Finn. They journeyed 
        through forests, over rivers, and across mountains. They faced many challenges: a band of thieves, a storm that threatened to drown them, 
        and a riddle that only the wise could solve. But Elara\'s spirit never wavered."\n\nMilo could almost hear the crackle of the fire and feel 
        the wind on his face. "What happened when they reached the valley?" he asked.\n\n"At the edge of the valley," his grandmother said, "they 
        found a gate made of stone, etched with symbols that glowed faintly. The dragon, a magnificent creature with wings that spanned the horizon, 
        emerged from the shadows. Its eyes were like polished amber, and its voice was deep and resonant. \'Who seeks the silver rivers?\' it asked."
        \n\nElara stepped forward, her voice steady. "I seek the knowledge of the stars, great dragon. I wish to bring it back to my people."\n\n
        The dragon considered her words. "Many have come before you, seeking power. But true knowledge is not a treasure to be hoarded. 
        It is a gift to be shared. If you wish to take it, you must prove your worth."\n\nThe dragon presented them with a series of trials. 
        The first was a test of courage: they had to cross a chasm that seemed to stretch into infinity. The second was a test of wisdom: they had 
        to solve a riddle that had baffled scholars for centuries. The third was a test of compassion: they h"""
    )