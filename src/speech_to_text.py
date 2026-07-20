import logging

from text_to_speech import request_interrupt


logger = logging.getLogger(__name__)
logger.disabled = True

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
    )


def iter_transcripts(recorder):
    while True:
        text = recorder.text()
        if text:
            yield text.strip()


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

        if text.lower() in {"exit", "quit"}:
            return

        yield text


# for testing
if __name__ == "__main__":
    recorder = create_recorder()

    for transcript in iter_transcripts(recorder):
        logger.info("Final transcription: %s", transcript)