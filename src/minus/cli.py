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
import threading
from dataclasses import dataclass
from queue import Queue

from minus.config import Settings, load_settings
from minus.core.agent import Conversation
from minus.core.escalation import DeepThinker
from minus.core.messages import Message
from minus.core.prompts import build_system_prompt
from minus.core.protocols import DetailSink
from minus.llm.client import OpenRouterClient
from minus.logging_config import setup_logging
from minus.memory.service import MemoryService
from minus.paths import semantic_memory_db
from minus.services.detail import FileDetailSink
from minus.services.json import pretty_json

# What the deep tier is allowed to touch from its background thread. An
# explicit allowlist rather than "everything the fast model has": the fast
# tier's tools are chosen for a user who is present and listening, and a
# background job should not inherit that by default.
DEEP_TOOL_NAMES = ("list_workspace_files", "read_workspace_file")

# Ends the courier thread. A sentinel rather than a flag because the courier
# is blocked in Queue.get() and needs something to arrive to wake it.
_STOP = object()

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


def deliver_deep_result(assistant, speaker, floor, result) -> None:
    """Publish one escalated answer's two channels."""
    # Detail first, and outside the lock: it is file I/O with nothing to
    # serialize against, and the spoken line may refer to the write-up.
    assistant.details.publish(result.question, result.detail)

    with floor:
        # Captured here rather than when the escalation started. The user has
        # almost certainly spoken during the seconds the deep tier was running,
        # which would make an escalation-time token stale and silently drop
        # every single deep answer.
        token = speaker.token()
        assistant.conversation.transcript.append(Message(role="assistant", content=result.spoken))
        logger.info("Deep answer:\n%s", pretty_json(result.spoken))
        speaker.speak(result.spoken, token=token)


def deep_result_courier(assistant, speaker, floor) -> None:
    """Deliver escalated answers as they land, until told to stop."""
    while True:
        result = assistant.results.get()
        if result is _STOP:
            return
        try:
            deliver_deep_result(assistant, speaker, floor, result)
        except Exception:
            # This thread is the only thing delivering deep answers; letting it
            # die over one bad result would silently disable escalation for the
            # rest of the session.
            logger.exception("Failed to deliver a deep answer")


def conversation_loop(transcripts, assistant, speaker) -> None:
    """Drive one conversation to completion.

    The post-conversation work runs in a `finally` so that quitting with Ctrl-C
    still condenses the transcript and extracts durable facts. It previously sat
    after the loop, so an interrupt discarded everything the session had learned.

    Two producers share one speaker: this loop, and the courier thread carrying
    escalated answers. `floor` is what stops a deep answer from being spoken
    over a live reply, and stops both from appending to the transcript at once.
    """
    conversation = assistant.conversation
    floor = threading.Lock()
    courier = threading.Thread(
        target=deep_result_courier,
        args=(assistant, speaker, floor),
        name="deep-courier",
        daemon=True,
    )
    courier.start()

    try:
        for transcript in transcripts:
            logger.info("Transcript received:\n%s", pretty_json(transcript))

            with floor:
                # Captured before generation starts: if the user begins talking
                # while the model is still thinking, this token goes stale and
                # the reply is dropped rather than spoken over them.
                token = speaker.token()

                response = conversation.reply(transcript)
                logger.info("Assistant response:\n%s", pretty_json(response))
                speaker.speak(response, token=token)
    except KeyboardInterrupt:
        logger.info("Interrupted; wrapping up the conversation.")
    finally:
        assistant.results.put(_STOP)
        courier.join(timeout=2.0)

        conversation.post_conversation()
        facts = conversation.memory.all_facts()
        if facts:
            logger.info("Semantic memory facts:\n%s", pretty_json(facts))
        else:
            logger.info("No semantic memory stored.")


@dataclass
class Assistant:
    """The wired object graph one conversation runs on."""

    conversation: Conversation
    memory: MemoryService
    thinker: DeepThinker
    results: Queue
    details: DetailSink


def build_deep_tools():
    """The deliberate allowlist the deep tier may call from its thread."""
    from minus.tools import registry

    return registry.subset(DEEP_TOOL_NAMES)


def build_fast_tools(thinker: DeepThinker):
    """Everything registered, plus `escalate`.

    Deriving the fast tier from the whole registry rather than an allowlist
    means a newly added built-in reaches the conversational model without a
    second edit here -- which is the property that made the registry worth
    having in the first place.
    """
    from minus.tools import registry

    fast = registry.subset(registry.names())
    fast.tool(thinker.escalate)
    return fast


def build_conversation(settings: Settings) -> Assistant:
    """Construct the model tiers, memory and agent graph."""
    model = OpenRouterClient(settings)
    system_prompt = build_system_prompt(settings.project_root, can_escalate=True)

    memory = MemoryService(
        model=model,
        extraction_model_name=settings.fact_extraction_model,
        system_prompt=system_prompt,
        relevance_threshold=settings.relevance_threshold,
        fact_search_top_k=settings.fact_search_top_k,
    )

    results: Queue = Queue()
    thinker = DeepThinker(
        model=model,
        deep_model=settings.deep_model,
        results=results,
        tools=build_deep_tools(),
        reasoning_effort=settings.deep_reasoning_effort,
        max_tool_rounds=settings.deep_max_tool_rounds,
        timeout_seconds=settings.deep_timeout_seconds,
    )

    conversation = Conversation(
        model=model,
        tools=build_fast_tools(thinker),
        max_tool_rounds=settings.max_tool_rounds,
        memory=memory,
        system_prompt=system_prompt,
        fact_top_k=settings.fact_search_top_k,
    )
    # Late-bound: the conversation needs the registry holding `escalate`, and
    # `escalate` needs the conversation to read for context.
    thinker.bind_snapshot(lambda: conversation.messages)

    return Assistant(
        conversation=conversation,
        memory=memory,
        thinker=thinker,
        results=results,
        details=FileDetailSink(),
    )


def run_assistant(settings: Settings, use_mic: bool) -> None:
    from minus.audio.interrupt import InterruptBus
    from minus.audio.stt import CliTranscriptSource, MicrophoneTranscriptSource
    from minus.audio.tts import KokoroSpeaker

    # One bus shared by input and output: the recognizer publishes barge-in,
    # the speaker consumes it. Neither knows the other exists.
    interrupts = InterruptBus()
    speaker = KokoroSpeaker(interrupts, settings)
    source = (
        MicrophoneTranscriptSource(interrupts, settings)
        if use_mic
        else CliTranscriptSource(interrupts)
    )

    assistant = build_conversation(settings)
    try:
        conversation_loop(source, assistant, speaker)
    finally:
        # Before memory.close(): a deep call still in flight holds no fact-store
        # handle, but stopping new work first keeps teardown ordered.
        assistant.thinker.shutdown()
        assistant.memory.close()


def run_memory_tui(args) -> None:
    from minus.scripts.memory_tui import run_memory_tui as tui

    tui(args.db or str(semantic_memory_db()), include_inactive=args.include_inactive)


def run_tools() -> None:
    # Built through the same tiering the assistant uses, so `escalate` is
    # listed rather than being invisible until it is called.
    thinker = DeepThinker(model=None, deep_model="", results=Queue())
    fast_tools = build_fast_tools(thinker)
    thinker.shutdown()

    for schema in fast_tools.schemas():
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
