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
from minus.core.sources import MergedTranscriptSource
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


def deliver_deep_result(assistant, speaker, floor, result, mark_activity=None) -> None:
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

        # After speaking, and still holding the floor. A deep answer arrives
        # long after the question, so by now the idle clock has been running
        # through the whole wait -- and the user has just been handed something
        # to respond to. Restarting it here gives them the full silence to
        # answer in, and an idle rollover already queued behind this lock sees
        # the fresh clock rather than condensing on top of the answer.
        if mark_activity is not None:
            mark_activity()


def deep_result_courier(assistant, speaker, floor, mark_activity=None) -> None:
    """Deliver escalated answers as they land, until told to stop."""
    while True:
        result = assistant.results.get()
        if result is _STOP:
            return
        try:
            deliver_deep_result(assistant, speaker, floor, result, mark_activity)
        except Exception:
            # This thread is the only thing delivering deep answers; letting it
            # die over one bad result would silently disable escalation for the
            # rest of the session.
            logger.exception("Failed to deliver a deep answer")


def end_conversation_when_idle(assistant: Assistant, floor: threading.Lock, source=None) -> bool:
    """End the current conversation after a silence and open a fresh one.

    Returns False to decline, which leaves the idle timer armed for another
    interval; True means "done, do not ask again until the user says
    something".
    """
    conversation = assistant.conversation

    with floor:
        # Re-checked under the lock, because acquiring it may have meant
        # waiting for the courier to finish speaking a deep answer. The
        # decision to roll over was made before that answer existed.
        if source is not None and source.seconds_since_activity() < source.idle_timeout:
            return False

        if not conversation.transcript:
            # Nothing was said. Condensing would write an empty file, and would
            # do it again on every timeout for as long as the silence lasted.
            return True

        if assistant.thinker.status()["in_flight"]:
            # An escalation outlives a half-minute silence easily --
            # deep_timeout_seconds defaults to 120. Rolling over now would
            # condense a conversation that is missing its own answer, and then
            # deliver that answer into a fresh, unrelated one.
            logger.debug("Idle, but a deep answer is still coming; leaving the conversation open.")
            return False

        facts = conversation.post_conversation()
        conversation_id = conversation.start_new_conversation()

    logger.info("Idle; conversation ended. Now recording to %s", conversation_id)
    if facts:
        logger.info("Facts extracted from the finished conversation:\n%s", pretty_json(facts))
    return True


def conversation_loop(transcripts, assistant, speaker, floor, mark_activity=None) -> None:
    """Drive one conversation to completion.

    The post-conversation work runs in a `finally` so that quitting with Ctrl-C
    still condenses the transcript and extracts durable facts. It previously sat
    after the loop, so an interrupt discarded everything the session had learned.

    Three producers share one speaker and one transcript: this loop, the courier
    thread carrying escalated answers, and the idle rollover. `floor` is what
    stops a deep answer from being spoken over a live reply, and stops any two
    of them from touching the transcript at once. It is built by the caller,
    since all three need the same one.
    """
    conversation = assistant.conversation
    courier = threading.Thread(
        target=deep_result_courier,
        args=(assistant, speaker, floor, mark_activity),
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
    from minus.audio.interrupt import InterruptBus, barge_in_on_sigint
    from minus.audio.stt import CliTranscriptSource, MicrophoneTranscriptSource
    from minus.audio.tts import KokoroSpeaker

    # One bus shared by input and output: the recognizer publishes barge-in,
    # the speaker consumes it. Neither knows the other exists.
    interrupts = InterruptBus()
    speaker = KokoroSpeaker(interrupts, settings)
    primary = (
        MicrophoneTranscriptSource(interrupts, settings)
        if use_mic
        else CliTranscriptSource(interrupts)
    )

    assistant = build_conversation(settings)

    # One lock for everything that may touch the transcript or the speaker:
    # the loop, the deep courier, and the idle rollover. Built here rather than
    # inside the loop now that a third collaborator needs the same one.
    floor = threading.Lock()
    # `source` is referenced by the handler it is being constructed with. That
    # resolves at call time, not now, which is what lets the rollover re-check
    # the idle clock it was triggered by.
    source = MergedTranscriptSource(
        primary,
        idle_timeout=settings.idle_conversation_seconds,
        on_idle=lambda: end_conversation_when_idle(assistant, floor, source),
    )

    try:
        # Installed here, on the main thread, rather than inside playback: a
        # deep answer is spoken from the courier thread, where signal handlers
        # cannot be installed at all. See interrupt.py. It stays installed
        # through the loop's own teardown, which changes nothing there --
        # nothing is being spoken by then, so Ctrl-C during condensing and
        # fact extraction still raises and still quits.
        with barge_in_on_sigint(interrupts):
            conversation_loop(source, assistant, speaker, floor, source.mark_activity)
    finally:
        # First: the pump thread is parked in the recorder's blocking read and
        # will not end on its own, and RealtimeSTT's workers are non-daemon --
        # leaving them running hangs the interpreter at exit.
        source.close()
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
