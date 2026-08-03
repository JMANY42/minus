import argparse
import logging

from memory.memory_manager import MemoryManager
from speech_to_text import create_recorder, iter_cli_transcripts, iter_transcripts
from conversation import Conversation
from logging_utils import setup_logging
from text_to_speech import speak

from services.json import pretty_json


logger = logging.getLogger(__name__)


def main():
    log_file = setup_logging()
    logger.info("Logging to %s", log_file)

    parser = argparse.ArgumentParser(description="Run the MINUS assistant")
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Use terminal input instead of microphone speech recognition",
    )
    args = parser.parse_args()

    transcripts = iter_cli_transcripts() if args.no_mic else iter_transcripts(create_recorder())
    memory = MemoryManager()
    conversation = Conversation(memory=memory)

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