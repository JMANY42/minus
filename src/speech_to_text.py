import logging

from text_to_speech import request_interrupt


logger = logging.getLogger(__name__)
logger.disabled = True

EXIT_PHRASES = {"exit", "quit", "end conversation"}


def is_exit_phrase(text):
    return text.strip().lower().rstrip(".!?") in EXIT_PHRASES


def update(text):
    if text and text.strip():
        request_interrupt()


def stop_speaking(*args, **kwargs):
    request_interrupt()

def create_recorder():
    from RealtimeSTT import AudioToTextRecorder

    return AudioToTextRecorder(
        no_log_file=True,
        enable_realtime_transcription=True,
        on_realtime_transcription_update=update,
        on_vad_start=stop_speaking,
        realtime_model_type="tiny.en",
        realtime_processing_pause=1,
        model="small.en",
        device="cuda",
        post_speech_silence_duration=0.2,
        silero_sensitivity=0.4,
        # RealtimeSTT keeps the recorder armed for voice activity while we're
        # speaking (for barge-in), and its internal worker loop disarms
        # start_recording_on_voice_activity unconditionally after any voice
        # detection - even when the resulting self.start() silently no-ops
        # because it landed within min_gap_between_recordings of the previous
        # recording's stop. That permanently stops voice detection until the
        # next wait_audio() call, hanging the recorder in "listening" forever.
        # Zeroing the gap removes that no-op window.
        min_gap_between_recordings=0.0,
    )


def iter_transcripts(recorder):
    # RealtimeSTT's background reader/transcription workers are non-daemon
    # threads on Linux (a `deamon` typo in its own _start_thread() leaves the
    # real `daemon` flag False), so without an explicit shutdown() call they
    # keep running after this generator ends and the interpreter hangs at
    # exit waiting for them to finish.
    try:
        while True:
            text = recorder.text()
            if text:
                text = text.strip()
                if is_exit_phrase(text):
                    return
                yield text
    finally:
        recorder.shutdown()


def iter_cli_transcripts(prompt="You: "):
    while True:
        try:
            text = input(prompt)
        except EOFError:
            return

        if text is None:
            return

        text = text.strip()
        if not text:
            continue

        if is_exit_phrase(text):
            return

        yield text


# for testing
if __name__ == "__main__":
    recorder = create_recorder()

    for transcript in iter_transcripts(recorder):
        logger.info("Final transcription: %s", transcript)