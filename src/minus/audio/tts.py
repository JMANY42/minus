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

    Safe to call from a worker thread. Playback prefers to install a SIGINT
    handler so that Ctrl-C during speech is heard as barge-in rather than
    killing the process, but CPython only permits signal.signal() on the main
    thread, and escalated answers are spoken from a delivery thread. Off the
    main thread the handler is simply skipped: the signal is delivered to the
    main thread regardless, where the conversation loop already handles it.
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

        # See the class docstring: signal.signal() is a main-thread privilege,
        # and deep-tier answers are spoken from a delivery thread.
        on_main_thread = threading.current_thread() is threading.main_thread()
        previous_handler = signal.getsignal(signal.SIGINT) if on_main_thread else None
        if on_main_thread:
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
            if on_main_thread:
                signal.signal(signal.SIGINT, previous_handler)
            executor.shutdown(wait=False, cancel_futures=True)
            if stream is not None and not stream_wedged:
                self._close_stream(stream)


if __name__ == "__main__":
    kokoro = KokoroSpeaker(InterruptBus())
    kokoro.speak(
        """Once upon a time, in a valley that lay between two great mountains, there was a village that everyone called Willowbrook. The village was small, with only a handful of houses made of stone and timber, and a single winding road that led to the market town in the next valley. The people of Willowbrook were simple folk, mostly farmers and woodcutters, who lived in harmony with the land. They had a tradition of telling stories by the fire each night, and the elders would weave tales of heroes, monsters, and the mysteries of the world.\n\nOne crisp autumn evening, as the sun dipped behind the mountains and painted the sky in hues of amber and violet, a young boy named Milo sat by the hearth with his grandmother. Milo was a curious child, always asking questions about the world beyond the valley. His grandmother, a woman with silver hair and eyes that seemed to hold the secrets of the ages, smiled and began to tell him a story that would stay with him for the rest of his life.\n\n"Long ago," she began, "there was a kingdom that stretched from the sea to the mountains, ruled by a wise king named Alaric. King Alaric was known for his fairness and his love for the arts. He had a daughter, Princess Elara, who was as brave as she was beautiful. She had a heart that beat for adventure, and she dreamed of seeing the world beyond the kingdom\'s borders."\n\nMilo listened intently, his eyes wide with wonder. "Did she ever go on an adventure?" he asked.\n\n"Yes," his grandmother replied. "One day, a mysterious traveler arrived at the palace gates. He was a wanderer, with a cloak of midnight blue and a staff that glowed with a faint, otherworldly light. He told the king of a hidden valley, a place where the stars fell to the earth and turned into silver rivers. He said that the valley was guarded by a dragon, but that the dragon was not a beast of fire and terror, but a guardian of knowledge."\n\nMilo\'s imagination ran wild. He pictured a dragon with scales that shimmered like the night sky, breathing not fire but stardust. "Did the princess go to the valley?" he asked.\n\n"She did," his grandmother said. "Princess Elara, with her heart full of courage, set out with a small band of loyal companions: a blacksmith named Roderick, a healer named Liora, and a young scribe named Finn. They journeyed through forests, over rivers, and across mountains. They faced many challenges: a band of thieves, a storm that threatened to drown them, and a riddle that only the wise could solve. But Elara\'s spirit never wavered."\n\nMilo could almost hear the crackle of the fire and feel the wind on his face. "What happened when they reached the valley?" he asked.\n\n"At the edge of the valley," his grandmother said, "they found a gate made of stone, etched with symbols that glowed faintly. The dragon, a magnificent creature with wings that spanned the horizon, emerged from the shadows. Its eyes were like polished amber, and its voice was deep and resonant. \'Who seeks the silver rivers?\' it asked."\n\nElara stepped forward, her voice steady. "I seek the knowledge of the stars, great dragon. I wish to bring it back to my people."\n\nThe dragon considered her words. "Many have come before you, seeking power. But true knowledge is not a treasure to be hoarded. It is a gift to be shared. If you wish to take it, you must prove your worth."\n\nThe dragon presented them with a series of trials. The first was a test of courage: they had to cross a chasm that seemed to stretch into infinity. The second was a test of wisdom: they had to solve a riddle that had baffled scholars for centuries. The third was a test of compassion: they had to heal a wounded creature that had been injured by a hunter.\n\nMilo could almost see the chasm, the riddle, and the wounded creature. He could feel the weight of the trials. "Did they succeed?" he asked.\n\n"Yes," his grandmother said. "Elara and her companions faced each challenge with bravery, intellect, and kindness. They crossed the chasm on a rope made of vines, solved the riddle by listening to the wind, and healed the creature with herbs from the forest. The dragon, impressed by their deeds, allowed them to take a single silver river."\n\nMilo\'s eyes widened. "What did they do with the silver river?" he asked.\n\n"Elara returned to her kingdom with the silver river," his grandmother said. "She used it to irrigate the fields, to heal the sick, and to illuminate the night. The people of the kingdom prospered, and the knowledge of the stars was shared with all. The dragon, seeing the good that came from the silver river, vowed to guard the valley and keep its secrets safe."\n\nMilo smiled. "That\'s a wonderful story," he said. "I want to go on an adventure too."\n\nHis grandmother chuckled. "You can, my dear. The world is full of wonders. All you need is a heart that beats with curiosity and a mind that seeks knowledge."\n\nMilo nodded, his mind buzzing with possibilities. He imagined himself as a brave adventurer, traveling to hidden valleys, meeting dragons, and discovering the secrets of the stars. He dreamed of the day when he would stand at the edge of a valley, looking at a silver river that glowed like the night sky, and feel the thrill of adventure in his chest.\n\nAnd so, as the fire crackled and the night grew deeper, Milo fell asleep with a heart full of wonder, dreaming of the day he would set out on his own grand adventure, just like Princess Elara. The story, like the stars, shone bright in his mind, guiding him toward a future full of possibilities."""
    )
