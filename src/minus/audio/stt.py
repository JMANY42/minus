"""Transcript sources: the microphone, and the terminal.

Both implement TranscriptSource, so the conversation loop iterates one without
knowing which. Neither imports the TTS module any more -- they receive an
InterruptBus and publish to it, which is what removes the STT -> TTS dependency
edge that `from text_to_speech import request_interrupt` created.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from minus.audio.interrupt import InterruptBus

logger = logging.getLogger(__name__)

EXIT_PHRASES = frozenset({"exit", "quit", "end conversation"})


def is_exit_phrase(text: str) -> bool:
    return text.strip().lower().rstrip(".!?") in EXIT_PHRASES


class CliTranscriptSource:
    """Typed input, for running without a microphone."""

    def __init__(self, interrupts: InterruptBus, prompt: str = "You: ") -> None:
        self.interrupts = interrupts
        self.prompt = prompt

    def __iter__(self) -> Iterator[str]:
        while True:
            try:
                text = input(self.prompt)
            except (EOFError, KeyboardInterrupt):
                return

            if text is None:
                return

            text = text.strip()
            if not text:
                continue
            if is_exit_phrase(text):
                return

            # Submitting a line counts as barging in: it stops any reply still
            # being spoken, matching what voice input does on speech onset.
            self.interrupts.request()
            yield text


class MicrophoneTranscriptSource:
    """RealtimeSTT-backed microphone input with barge-in."""

    def __init__(self, interrupts: InterruptBus, settings: Any | None = None) -> None:
        self.interrupts = interrupts
        self.settings = settings

    # These fire on RealtimeSTT's own threads.
    def _on_voice_activity(self, *args: Any, **kwargs: Any) -> None:
        self.interrupts.request()

    def _on_realtime_update(self, text: str) -> None:
        if text and text.strip():
            self.interrupts.request()

    def create_recorder(self) -> Any:
        from RealtimeSTT import AudioToTextRecorder

        settings = self.settings
        return AudioToTextRecorder(
            no_log_file=True,
            enable_realtime_transcription=True,
            on_realtime_transcription_update=self._on_realtime_update,
            on_vad_start=self._on_voice_activity,
            realtime_model_type=getattr(settings, "stt_realtime_model", "tiny.en"),
            realtime_processing_pause=1,
            model=getattr(settings, "stt_model", "small.en"),
            device=getattr(settings, "stt_device", "cuda"),
            post_speech_silence_duration=0.2,
            silero_sensitivity=0.4,
            # RealtimeSTT keeps the recorder armed for voice activity while
            # we're speaking (for barge-in), and its internal worker loop
            # disarms start_recording_on_voice_activity unconditionally after
            # any voice detection - even when the resulting self.start()
            # silently no-ops because it landed within
            # min_gap_between_recordings of the previous recording's stop. That
            # permanently stops voice detection until the next wait_audio()
            # call, hanging the recorder in "listening" forever. Zeroing the gap
            # removes that no-op window.
            min_gap_between_recordings=0.0,
        )

    def __iter__(self) -> Iterator[str]:
        # RealtimeSTT's background reader/transcription workers are non-daemon
        # threads on Linux (a `deamon` typo in its own _start_thread() leaves
        # the real `daemon` flag False), so without an explicit shutdown() call
        # they keep running after this generator ends and the interpreter hangs
        # at exit waiting for them to finish.
        recorder = self.create_recorder()
        try:
            while True:
                text = recorder.text()
                if not text:
                    continue

                text = text.strip()
                if is_exit_phrase(text):
                    return
                yield text
        finally:
            recorder.shutdown()
