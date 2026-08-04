import argparse
import faulthandler
import logging
import signal

from minus.audio.stt import create_recorder, iter_cli_transcripts, iter_transcripts
from minus.audio.tts import speak
from minus.core.agent import Conversation
from minus.logging_config import setup_logging
from minus.memory.service import MemoryManager
from minus.services.json import pretty_json

logger = logging.getLogger(__name__)


def main():
    log_file = setup_logging()
    logger.info("Logging to %s", log_file)

    # When the recorder/TTS pipeline wedges, Ctrl-C isn't always reliable
    # (whatever's stuck may not be checking for interrupts). Send SIGUSR1
    # (kill -USR1 <pid>) to dump every thread's live Python stack to stderr
    # without killing the process - that pinpoints exactly what's blocked.
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1)

    parser = argparse.ArgumentParser(description="Run the MINUS assistant")
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Use terminal input instead of microphone speech recognition",
    )
    args = parser.parse_args()

    transcripts = iter_cli_transcripts() if args.no_mic else iter_transcripts(create_recorder())
    memory = MemoryManager()
    conversation = Conversation(max_tool_rounds=7, memory=memory)

    conversation_loop(transcripts, conversation)


def conversation_loop(transcripts, conversation):
    for transcript in transcripts:
        logger.info("Transcript received:\n%s", pretty_json(transcript))

        response = conversation.reply(transcript)
        logger.info("Assistant response:\n%s", pretty_json(response))
        speak(response)
    conversation.post_conversation()

    if conversation.memory._memory_store:
        logger.info("Semantic memory facts:\n%s", pretty_json(conversation.memory._memory_store.get_all_facts()))
    else:
        logger.info("No semantic memory extracted.")


if __name__ == "__main__":
    main()
