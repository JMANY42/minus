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
import contextlib
import faulthandler
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any

from minus import __version__
from minus.config import Settings, load_settings
from minus.control.config_control import ConfigController, LiveField
from minus.control.instrument import ObservedSpeaker
from minus.control.protocol import BAD_PARAMS, PROTOCOL_VERSION, ProtocolError
from minus.control.server import ControlServer
from minus.control.state import HEARING, LISTENING, THINKING, RuntimeState
from minus.core.agent import Conversation
from minus.core.escalation import DeepThinker
from minus.core.messages import Message
from minus.core.prompts import build_system_prompt
from minus.core.protocols import DetailSink
from minus.core.sources import MergedTranscriptSource
from minus.llm.client import OpenRouterClient
from minus.logging_config import setup_logging
from minus.memory.service import MemoryService
from minus.paths import (
    control_socket,
    conversations_dir,
    deep_notes_dir,
    project_root,
    semantic_memory_db,
)
from minus.services.detail import FileDetailSink
from minus.services.json import pretty_json, read_json

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
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="Do not listen on the control socket (lets a second MINUS run alongside one)",
    )

    subcommands = parser.add_subparsers(dest="command")

    say = subcommands.add_parser("say", help="Send a line to the running assistant")
    say.add_argument("text", nargs="+", help="What to say")

    status = subcommands.add_parser("status", help="Print what the running assistant is doing")
    status.add_argument("--watch", action="store_true", help="Keep printing as the state changes")

    serve = subcommands.add_parser("serve", help="Run headless, for a service manager")
    # SUPPRESS so that omitting it here leaves the root-level flag alone rather
    # than overwriting it with this parser's default.
    serve.add_argument(
        "--no-mic",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Take input only from the control socket",
    )

    subcommands.add_parser("systemd-unit", help="Print a systemd --user unit for this checkout")

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


@contextlib.contextmanager
def _end_conversation_on_sigterm(source):
    """Make `systemctl stop` end the conversation rather than discard it.

    systemd sends SIGTERM, which Python's default handler turns into an
    immediate exit -- so the work in `conversation_loop`'s `finally`, the
    condensation and fact extraction that the whole session's learning depends
    on, never ran. Closing the source instead ends the loop the same way an
    exit phrase does, and the polling get() in MergedTranscriptSource is what
    guarantees the flag is noticed promptly.

    Only on the main thread, because CPython permits signal handlers nowhere
    else, and restored afterwards so this composes with barge_in_on_sigint.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.signal(signal.SIGTERM, lambda *_: source.close())
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


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


def conversation_loop(
    transcripts, assistant, speaker, floor, mark_activity=None, state=None
) -> None:
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
    state = state if state is not None else RuntimeState()
    courier = threading.Thread(
        target=deep_result_courier,
        args=(assistant, speaker, floor, mark_activity),
        name="deep-courier",
        daemon=True,
    )
    courier.start()

    try:
        state.set_phase(LISTENING)
        for transcript in transcripts:
            logger.info("Transcript received:\n%s", pretty_json(transcript))
            state.set_phase(THINKING)

            with floor:
                # Captured before generation starts: if the user begins talking
                # while the model is still thinking, this token goes stale and
                # the reply is dropped rather than spoken over them.
                token = speaker.token()

                response = conversation.reply(transcript)
                logger.info("Assistant response:\n%s", pretty_json(response))
                # Speaking and idle are reported by ObservedSpeaker, which
                # wraps this one -- the courier speaks too, and instrumenting
                # the speaker covers both without a second copy here.
                speaker.speak(response, token=token)

            state.set_phase(LISTENING)
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


def _conversation_section(assistant: Assistant) -> dict:
    """The live conversation, for the status snapshot."""
    return {
        "id": assistant.memory.conversation_id,
        "path": str(assistant.memory.file_path),
        "message_count": len(assistant.conversation.transcript),
    }


def build_conversation(settings: Settings, state=None) -> Assistant:
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
        # Pushes a status event on both edges, so a watcher learns that the
        # deep tier started without polling for it.
        on_change=state.touch if state is not None else None,
    )
    if state is not None:
        state.provide("deep", thinker.status)

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


def _fact_summary(fact) -> dict:
    return {
        "attribute": fact.attribute,
        "value": fact.value,
        "active": fact.active,
        "created_at": fact.created_at,
    }


def _note_summary(path: Path) -> dict:
    payload = read_json(path) or {}
    return {
        "path": str(path),
        "title": payload.get("title", path.stem),
        "created_at": payload.get("created_at"),
    }


def _newest(directory: Path, limit: int) -> list[Path]:
    """The most recent files, by name.

    Both directories are named with a UTC timestamp prefix, so lexicographic
    order is chronological order and this needs no stat() per file.
    """
    return sorted(directory.glob("*.json"), reverse=True)[:limit]


def build_config_controller(
    assistant: Assistant, speaker, source, settings: Settings
) -> ConfigController:
    """Which settings apply live, and what each one actually writes.

    An explicit table rather than anything clever. A field is live only if it
    is here, so this never claims a change took effect when the object holding
    that value read it once at construction and will not look again.

    `speaker` must be the real KokoroSpeaker, not the ObservedSpeaker wrapping
    it -- the wrapper forwards attribute *reads* and would otherwise quietly
    absorb the writes.
    """
    conversation = assistant.conversation
    memory = assistant.memory
    thinker = assistant.thinker

    def both(*applies: Callable[[Any], None]) -> Callable[[Any], None]:
        def apply(value: Any) -> None:
            for one in applies:
                one(value)

        return apply

    def on_settings(name: str) -> Callable[[Any], None]:
        return lambda value: setattr(settings, name, value)

    def on(target: Any, attribute: str) -> Callable[[Any], None]:
        return lambda value: setattr(target, attribute, value)

    def set_log_level(value: Any) -> None:
        logging.getLogger().setLevel(value)
        settings.log_level = value

    table: dict[str, Callable[[Any], None]] = {
        # Read per call from the Settings object the client holds.
        "chat_model": on_settings("chat_model"),
        "max_retries": on_settings("max_retries"),
        # Held as attributes on collaborators, and re-read on each use.
        "deep_model": both(on(thinker, "deep_model"), on_settings("deep_model")),
        "deep_reasoning_effort": both(
            on(thinker, "reasoning_effort"), on_settings("deep_reasoning_effort")
        ),
        "deep_max_tool_rounds": both(
            on(thinker, "max_tool_rounds"), on_settings("deep_max_tool_rounds")
        ),
        "deep_timeout_seconds": both(
            on(thinker, "timeout_seconds"), on_settings("deep_timeout_seconds")
        ),
        "max_tool_rounds": both(
            on(conversation, "max_tool_rounds"), on_settings("max_tool_rounds")
        ),
        "relevance_threshold": both(
            on(memory, "relevance_threshold"), on_settings("relevance_threshold")
        ),
        "fact_extraction_model": both(
            on(memory, "extraction_model_name"), on_settings("fact_extraction_model")
        ),
        # Two copies of one value: MemoryService takes it, and Conversation
        # keeps its own from the same field. Both, or a change is half-applied.
        "fact_search_top_k": both(
            on(memory, "fact_search_top_k"),
            on(conversation, "fact_top_k"),
            on_settings("fact_search_top_k"),
        ),
        # Read per chunk, so these land on the next thing spoken.
        "tts_voice": both(on(speaker, "voice"), on_settings("tts_voice")),
        "tts_speed": both(on(speaker, "speed"), on_settings("tts_speed")),
        "tts_lang": both(on(speaker, "lang"), on_settings("tts_lang")),
        "tts_chunk_max_chars": both(
            on(speaker, "chunk_max_chars"), on_settings("tts_chunk_max_chars")
        ),
        "tts_first_chunk_max_chars": both(
            on(speaker, "first_chunk_max_chars"), on_settings("tts_first_chunk_max_chars")
        ),
        "idle_conversation_seconds": both(
            on(source, "idle_timeout"), on_settings("idle_conversation_seconds")
        ),
        "log_level": set_log_level,
    }

    return ConfigController(
        settings,
        {name: LiveField(name, apply) for name, apply in table.items()},
        env_path=project_root() / ".env",
    )


def build_control_handlers(assistant: Assistant, source, interrupts, state, config=None) -> dict:
    """What the control socket is allowed to ask of a running assistant.

    Built here because this is the module that knows the object graph. The
    server itself takes a table of callables and has no idea what any of them
    mean, which is what keeps it testable with a handful of lambdas.
    """

    def say(params: dict) -> dict:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("text must be a non-empty string", BAD_PARAMS)

        # Barge in first, exactly as typing at the CLI prompt does
        # (audio/stt.py:51). Injected speech should be indistinguishable from
        # the real thing, including cutting off a reply already in progress.
        interrupts.request()
        source.submit(text.strip())
        return {"accepted": True}

    def limit_of(params: dict, default: int = 20) -> int:
        value = params.get("limit", default)
        return max(1, min(int(value), 500))

    def get_config(params: dict) -> dict:
        if config is None:
            raise ProtocolError("Configuration is not available", BAD_PARAMS)
        return config.describe()

    def set_config(params: dict) -> dict:
        if config is None:
            raise ProtocolError("Configuration is not available", BAD_PARAMS)
        values = params.get("values")
        if not isinstance(values, dict) or not values:
            raise ProtocolError("values must be a non-empty object", BAD_PARAMS)
        return config.apply(values, persist=bool(params.get("persist", True)))

    return {
        "get_config": get_config,
        "set_config": set_config,
        "hello": lambda params: {
            "server_version": __version__,
            "protocol": PROTOCOL_VERSION,
            "pid": state.pid,
            "started_at": state.started_at,
        },
        "ping": lambda params: {"pong": True},
        "get_status": lambda params: state.snapshot(),
        "say": say,
        "interrupt": lambda params: {"generation": interrupts.request()},
        "list_tools": lambda params: [
            {
                "name": schema["function"]["name"],
                "description": schema["function"]["description"],
            }
            for schema in assistant.conversation.tools.schemas()
        ],
        "list_facts": lambda params: [
            _fact_summary(fact) for fact in assistant.memory.all_facts()[: limit_of(params)]
        ],
        "list_deep_notes": lambda params: [
            _note_summary(path) for path in _newest(deep_notes_dir(), limit_of(params))
        ],
        "list_conversations": lambda params: [
            {"id": path.stem, "path": str(path)}
            for path in _newest(conversations_dir(), limit_of(params))
        ],
        # Frozen now, empty until there is something behind them. Settling the
        # shape early means the dashboard panel does not change when the
        # feature arrives -- only where the data comes from.
        "list_agents": lambda params: [],
        "list_programs": lambda params: [],
        "shutdown": lambda params: _shutdown(source),
    }


def _shutdown(source) -> dict:
    """End the conversation loop cleanly, as Ctrl-C would."""
    source.close()
    return {"stopping": True}


def run_assistant(
    settings: Settings,
    use_mic: bool,
    *,
    control: bool = True,
    interactive: bool = True,
) -> None:
    from minus.audio.interrupt import InterruptBus, barge_in_on_sigint
    from minus.audio.stt import CliTranscriptSource, MicrophoneTranscriptSource
    from minus.audio.tts import KokoroSpeaker

    # One bus shared by input and output: the recognizer publishes barge-in,
    # the speaker consumes it. Neither knows the other exists.
    interrupts = InterruptBus()
    state = RuntimeState()

    # The inner one is kept: live TTS config writes to it directly, because
    # ObservedSpeaker forwards attribute reads and would swallow the writes.
    voice = KokoroSpeaker(interrupts, settings)
    # Wrapped once rather than at each `speak()` call site: the courier speaks
    # too, and one decorator covers both callers and any future third.
    speaker = ObservedSpeaker(voice, state)

    # Free, and the best available use of the existing bus: the microphone
    # already calls interrupts.request() from on_vad_start, so this is a real
    # "the user is talking right now" indicator with no change to stt.py.
    interrupts.subscribe(lambda: state.set_phase(HEARING))

    primary: Any = None
    if use_mic:
        primary = MicrophoneTranscriptSource(interrupts, settings)
    elif interactive:
        primary = CliTranscriptSource(interrupts)
    # Otherwise the socket is the only way in. A CLI source on a dead stdin
    # would EOF immediately and end the session before it began, which is not
    # what "headless" should mean.

    assistant = build_conversation(settings, state=state)

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
    state.provide("conversation", lambda: _conversation_section(assistant))

    server = None
    if control:
        server = ControlServer(
            control_socket(),
            build_control_handlers(
                assistant,
                source,
                interrupts,
                state,
                build_config_controller(assistant, voice, source, settings),
            ),
            state=state,
        )
        server.start()

    try:
        # Installed here, on the main thread, rather than inside playback: a
        # deep answer is spoken from the courier thread, where signal handlers
        # cannot be installed at all. See interrupt.py. It stays installed
        # through the loop's own teardown, which changes nothing there --
        # nothing is being spoken by then, so Ctrl-C during condensing and
        # fact extraction still raises and still quits.
        with _end_conversation_on_sigterm(source), barge_in_on_sigint(interrupts):
            conversation_loop(source, assistant, speaker, floor, source.mark_activity, state)
    finally:
        # First: the pump thread is parked in the recorder's blocking read and
        # will not end on its own, and RealtimeSTT's workers are non-daemon --
        # leaving them running hangs the interpreter at exit.
        source.close()
        if server is not None:
            server.close()
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


def _describe(snapshot: dict) -> str:
    """One line of status, for a terminal rather than a dashboard."""
    deep = snapshot.get("deep") or {}
    conversation = snapshot.get("conversation") or {}

    line = f"{snapshot.get('phase', '?'):<10}"
    if conversation.get("id"):
        line += f"  conversation {conversation['id']} ({conversation.get('message_count', 0)} msgs)"
    if deep.get("in_flight"):
        line += f"  [deep: {deep.get('elapsed_seconds', 0):.0f}s -- {deep.get('question')}]"
    return line


def run_say(args) -> int:
    """Hand a line to the running assistant, as though it had been spoken."""
    from minus.control.client import ControlClient, NotRunning

    try:
        with ControlClient(control_socket()) as client:
            client.request("say", text=" ".join(args.text))
    except NotRunning as exc:
        print(exc)
        return 1
    return 0


def run_status(args) -> int:
    """Print what the assistant is doing, once or until interrupted."""
    from minus.control.client import ControlClient, NotRunning

    try:
        if not args.watch:
            with ControlClient(control_socket()) as client:
                print(_describe(client.request("get_status")))
            return 0

        with ControlClient(
            control_socket(),
            on_event=lambda frame: print(_describe(frame.get("data") or {})),
        ) as client:
            client.subscribe()
            # Nothing to do but wait: the event handler does the printing, and
            # the reader thread does the reading.
            while True:
                time.sleep(0.5)
    except NotRunning as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        return 0


def run_serve(settings: Settings, use_mic: bool) -> None:
    """Run with no terminal, for a service manager to supervise.

    Differs from the interactive path only in what it does *not* do: no stderr
    handler, because journald already receives the file log's contents once and
    does not need them twice, and no CLI transcript source, because there is no
    stdin worth reading.
    """
    log_file = setup_logging(
        level=settings.log_level,
        retention=settings.log_retention,
        console=False,
    )
    logger.info("Serving; logging to %s", log_file)

    _install_stack_dumper()
    run_assistant(settings, use_mic=use_mic, control=True, interactive=False)


def run_systemd_unit() -> None:
    import sys

    from minus.control.systemd import render_unit

    # sys.executable is the interpreter; the console script beside it is what
    # has the entry point.
    console_script = Path(sys.executable).with_name("minus")
    print(render_unit(console_script, project_root()), end="")


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    if args.command == "systemd-unit":
        run_systemd_unit()
        return

    if args.command == "say":
        raise SystemExit(run_say(args))

    if args.command == "status":
        raise SystemExit(run_status(args))

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

    if args.command == "serve":
        run_serve(settings, use_mic=not args.no_mic)
        return

    log_file = setup_logging(
        level=settings.log_level,
        console_level=settings.console_log_level,
        retention=settings.log_retention,
    )
    logger.info("Logging to %s", log_file)

    _install_stack_dumper()
    run_assistant(settings, use_mic=not args.no_mic, control=not args.no_control)


if __name__ == "__main__":
    main()
