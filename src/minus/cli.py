"""Command line entry point and composition root for MINUS.

This is the only module that is allowed to decide *which* implementations get
used. Everything below it receives its collaborators as arguments, so swapping
the model, the fact store or the audio backend is a change here rather than a
change scattered through the modules that use them.
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import signal

from minus.audio.stt import create_recorder, iter_cli_transcripts, iter_transcripts
from minus.audio.tts import speak
from minus.config import Settings, load_settings
from minus.core.agent import Conversation
from minus.core.prompts import build_system_prompt
from minus.llm.client import OpenRouterClient
from minus.logging_config import setup_logging
from minus.memory.service import MemoryManager
from minus.services.json import pretty_json

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minus", description="Run the MINUS assistant")
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Use terminal input instead of microphone speech recognition",
    )
    return parser


def _install_stack_dumper() -> None:
    """Allow a wedged process to be inspected without killing it.

    When the recorder/TTS pipeline wedges, Ctrl-C is not always reliable --
    whatever is stuck may be blocked in native code that never checks for
    interrupts. `kill -USR1 <pid>` dumps every thread's live Python stack to
    stderr, which pinpoints exactly what is blocked.
    """
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1)


def conversation_loop(transcripts, conversation) -> None:
    """Drive one conversation to completion.

    The post-conversation work runs in a `finally` so that quitting with
    Ctrl-C still condenses the transcript and extracts durable facts. It
    previously sat after the loop, so an interrupt discarded everything the
    session had learned.
    """
    try:
        for transcript in transcripts:
            logger.info("Transcript received:\n%s", pretty_json(transcript))

            response = conversation.reply(transcript)
            logger.info("Assistant response:\n%s", pretty_json(response))
            speak(response)
    except KeyboardInterrupt:
        logger.info("Interrupted; wrapping up the conversation.")
    finally:
        conversation.post_conversation()
        facts = conversation.memory.all_facts()
        if facts:
            logger.info("Semantic memory facts:\n%s", pretty_json(facts))
        else:
            logger.info("No semantic memory stored.")


def run(settings: Settings, use_mic: bool) -> None:
    """Build the object graph and drive one conversation.

    Every collaborator is constructed here and handed down, so switching model
    provider, tool set or audio backend is a change to this function alone.
    """
    model = OpenRouterClient(settings)
    memory = MemoryManager(
        model=model,
        extraction_model_name=settings.fact_extraction_model,
    )
    conversation = Conversation(
        model=model,
        max_tool_rounds=settings.max_tool_rounds,
        memory=memory,
        system_prompt=build_system_prompt(settings.project_root),
    )
    transcripts = iter_transcripts(create_recorder()) if use_mic else iter_cli_transcripts()
    conversation_loop(transcripts, conversation)


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    log_file = setup_logging(
        level=settings.log_level,
        console_level=settings.console_log_level,
        retention=settings.log_retention,
    )
    logger.info("Logging to %s", log_file)

    _install_stack_dumper()
    run(settings, use_mic=not args.no_mic)


if __name__ == "__main__":
    main()
