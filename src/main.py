import argparse
import logging

from speech_to_text import create_recorder, iter_cli_transcripts, iter_transcripts
from conversation import Conversation
from logging_utils import pretty_json, setup_logging
from text_to_speech import speak


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

    conversation = Conversation()

    transcripts = iter_cli_transcripts() if args.no_mic else iter_transcripts(create_recorder())

    for transcript in transcripts:
        logger.info("Transcript received:\n%s", pretty_json(transcript))

        response = conversation.reply(transcript)
        logger.info("Assistant response:\n%s", pretty_json(response))
        speak(response)


if __name__ == "__main__":
    main()