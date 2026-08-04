"""Command line entry point and composition root for MINUS.

This is the only module allowed to decide *which* implementations are used.
Everything below it receives its collaborators as arguments, so swapping the
model, the fact store or the audio backend is a change here rather than one
scattered through the modules that use them.

Audio imports are deliberately deferred into the commands that need them:
`minus memory` and `minus calibrate` must work on a machine with no working
PortAudio, and importing kokoro-onnx costs seconds even when it succeeds.
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import signal

from minus.config import Settings, load_settings
from minus.core.agent import Conversation
from minus.core.prompts import build_system_prompt
from minus.llm.client import OpenRouterClient
from minus.logging_config import setup_logging
from minus.memory.service import MemoryService
from minus.paths import semantic_memory_db
from minus.services.json import pretty_json

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minus", description="The MINUS voice assistant")
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Use terminal input instead of microphone speech recognition",
    )

    subcommands = parser.add_subparsers(dest="command")

    memory = subcommands.add_parser("memory", help="Interactively prune stored facts")
    memory.add_argument("--db", default=None, help="Path to the semantic memory database")
    memory.add_argument(
        "--include-inactive", action="store_true", help="Also show superseded facts"
    )

    subcommands.add_parser(
        "calibrate", help="Print similarity distributions and a suggested relevance threshold"
    )
    subcommands.add_parser("tools", help="List the tools available to the assistant")

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


def conversation_loop(transcripts, conversation, speaker) -> None:
    """Drive one conversation to completion.

    The post-conversation work runs in a `finally` so that quitting with Ctrl-C
    still condenses the transcript and extracts durable facts. It previously sat
    after the loop, so an interrupt discarded everything the session had learned.
    """
    try:
        for transcript in transcripts:
            logger.info("Transcript received:\n%s", pretty_json(transcript))

            response = conversation.reply(transcript)
            logger.info("Assistant response:\n%s", pretty_json(response))
            speaker(response)
    except KeyboardInterrupt:
        logger.info("Interrupted; wrapping up the conversation.")
    finally:
        conversation.post_conversation()
        facts = conversation.memory.all_facts()
        if facts:
            logger.info("Semantic memory facts:\n%s", pretty_json(facts))
        else:
            logger.info("No semantic memory stored.")


def build_conversation(settings: Settings) -> tuple[Conversation, MemoryService]:
    """Construct the model, memory and agent graph."""
    model = OpenRouterClient(settings)
    memory = MemoryService(
        model=model,
        extraction_model_name=settings.fact_extraction_model,
        system_prompt=build_system_prompt(settings.project_root),
        relevance_threshold=settings.relevance_threshold,
        fact_search_top_k=settings.fact_search_top_k,
    )
    conversation = Conversation(
        model=model,
        max_tool_rounds=settings.max_tool_rounds,
        memory=memory,
        system_prompt=build_system_prompt(settings.project_root),
        fact_top_k=settings.fact_search_top_k,
    )
    return conversation, memory


def run_assistant(settings: Settings, use_mic: bool) -> None:
    from minus.audio.stt import create_recorder, iter_cli_transcripts, iter_transcripts
    from minus.audio.tts import speak

    source = iter_transcripts(create_recorder()) if use_mic else iter_cli_transcripts()
    conversation, memory = build_conversation(settings)
    try:
        conversation_loop(source, conversation, speak)
    finally:
        memory.close()


def run_memory_tui(args) -> None:
    from minus.scripts.memory_tui import run_memory_tui as tui

    tui(args.db or str(semantic_memory_db()), include_inactive=args.include_inactive)


def run_tools() -> None:
    from minus.tools import registry

    for schema in registry.schemas():
        function = schema["function"]
        required = set(function["parameters"].get("required", []))
        params = ", ".join(
            name if name in required else f"{name}?"
            for name in function["parameters"]["properties"]
        )
        print(f"{function['name']}({params})\n    {function['description']}")


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    if args.command == "memory":
        run_memory_tui(args)
        return

    if args.command == "calibrate":
        from minus.scripts.calibrate import run_calibration

        run_calibration()
        return

    if args.command == "tools":
        run_tools()
        return

    log_file = setup_logging(
        level=settings.log_level,
        console_level=settings.console_log_level,
        retention=settings.log_retention,
    )
    logger.info("Logging to %s", log_file)

    _install_stack_dumper()
    run_assistant(settings, use_mic=not args.no_mic)


if __name__ == "__main__":
    main()
