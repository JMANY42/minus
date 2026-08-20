"""Composition root: the only place that decides which implementations are used.

Everything below this module receives its collaborators as arguments, so
swapping the model provider, the fact store or the audio backend is a change
here rather than one scattered through the modules that use them. The seams
themselves are declared in `core/protocols.py`.

Two kinds of wiring live here, and both are deliberately explicit tables rather
than anything clever:

  * `build_config_controller` names every setting that can be changed on a
    running assistant, and what each one writes. A field absent from it is not
    live, which is better than claiming a change took effect when the object
    holding that value read it once at construction.
  * `build_control_handlers` names what the control socket may ask for.
    `control/server.py` is handed the result and has no idea what any of it
    means, which is what keeps it testable with a handful of lambdas.

Audio imports are deferred into `run_assistant` for the same reason `cli.py`
defers its own: `minus memory` and `minus calibrate` must work on a machine
with no working PortAudio, and importing kokoro-onnx costs seconds even when it
succeeds.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from queue import Queue
from typing import Any

from minus import __version__
from minus.config import Settings
from minus.control.config_control import ConfigController, LiveField
from minus.control.instrument import ObservedSpeaker
from minus.control.protocol import BAD_PARAMS, PROTOCOL_VERSION, ProtocolError
from minus.control.server import ControlServer
from minus.control.state import HEARING, RuntimeState
from minus.core.agent import Conversation
from minus.core.escalation import DeepThinker
from minus.core.sources import MergedTranscriptSource
from minus.errors import ToolError
from minus.llm.client import OpenRouterClient
from minus.memory.service import MemoryService
from minus.paths import control_socket, conversations_dir, deep_notes_dir, project_root
from minus.prompts import build_system_prompt
from minus.runtime import (
    Assistant,
    conversation_loop,
    end_conversation_now,
    end_conversation_on_sigterm,
    end_conversation_when_idle,
)
from minus.services.detail import FileDetailSink
from minus.services.json import read_json
from minus.tools.policy import ToolPolicy, UnknownAgentError, build_policy

# What the deep tier is allowed to touch from its background thread. An
# explicit allowlist rather than "everything the fast model has": the fast
# tier's tools are chosen for a user who is present and listening, and a
# background job should not inherit that by default.
DEEP_TOOL_NAMES = ("list_workspace_files", "read_workspace_file")

logger = logging.getLogger(__name__)


def build_deep_tools():
    """The deliberate allowlist the deep tier may call from its thread."""
    from minus.tools import registry

    return registry.subset(DEEP_TOOL_NAMES)


def build_tool_policy(assistant: Assistant, settings: Settings | None = None) -> ToolPolicy:
    """The tiers' registries, gathered under the names a dashboard shows them by.

    Read off the assembled graph rather than built alongside it, so there is
    still exactly one place each registry is created and this cannot drift into
    describing a toolset nobody is actually using. The coding agent has no
    registry because there is no coding tier; it is listed anyway, with the
    reason, so the panel has the shape it will keep.

    `settings` is applied on the way out, which is what makes a tool switched
    off from the dashboard yesterday still switched off today.
    """
    policy = build_policy(
        conversational=assistant.conversation.tools,
        deep=getattr(assistant.thinker, "tools", None),
    )
    if settings is not None:
        policy.apply_spec(settings.disabled_tools)
    return policy


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
        # The id travels with it because a dashboard that can list facts is
        # expected to be able to forget one, and it cannot delete what it has
        # no name for.
        "id": fact.id,
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
    assistant: Assistant, speaker, source, settings: Settings, policy: ToolPolicy | None = None
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

    def set_disabled_tools(value: Any) -> None:
        """Apply a whole disabled-tools spec to the registries.

        Live because the registries are read per turn: `ToolLoop` asks for
        schemas at the start of each one, so a tool switched off here is gone
        from the next thing the model is offered. Idempotent, which matters
        because the tools panel has usually applied its own change to the
        registry already and is only passing through here to have it written.
        """
        if policy is not None:
            policy.apply_spec(value)
        settings.disabled_tools = value

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
        "disabled_tools": set_disabled_tools,
    }

    return ConfigController(
        settings,
        {name: LiveField(name, apply) for name, apply in table.items()},
        env_path=project_root() / ".env",
    )


def build_control_handlers(
    assistant: Assistant, source, interrupts, state, config=None, floor=None, policy=None
) -> dict:
    """What the control socket is allowed to ask of a running assistant.

    Built here because this is the module that knows the object graph. The
    server itself takes a table of callables and has no idea what any of them
    mean, which is what keeps it testable with a handful of lambdas.

    `floor` must be the lock the conversation loop holds, or a command that
    touches the transcript can run while a turn is being spoken into it. A
    fresh one is made when it is absent so this stays callable with the four
    collaborators a test cares about. `policy` is derived from the assistant on
    the same terms, and for the same reason.
    """
    floor = floor if floor is not None else threading.Lock()
    policy = policy if policy is not None else build_tool_policy(assistant)

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

    def roll_over() -> None:
        try:
            end_conversation_now(assistant, floor, source)
        except Exception:
            logger.exception("Ending the conversation on request failed")
        # Either way: the conversation section of the snapshot is read fresh,
        # so this is what tells a dashboard which conversation it is now
        # looking at -- and, when it failed, that it is still the old one.
        state.touch()

    def end_conversation(params: dict) -> dict:
        """End the current conversation now, and open a fresh one.

        Answered before the work is done, and the work is done on a thread of
        its own. Condensing is two model calls, which is many times the
        client's request timeout -- so doing it inline would report a failure
        for something that had in fact worked, and would block this connection
        throughout. What actually happened arrives as a status event instead.
        """
        # Barge in first, for the same reason `say` does: "now" has to mean
        # during a reply too, and the floor the rollover takes is held for as
        # long as one is still being spoken.
        interrupts.request()
        threading.Thread(target=roll_over, name="end-conversation", daemon=True).start()
        return {"accepted": True}

    def limit_of(params: dict, default: int = 20) -> int:
        value = params.get("limit", default)
        return max(1, min(int(value), 500))

    def delete_facts(params: dict) -> dict:
        """Forget the named facts. Hard deletes, so the caller confirms first."""
        ids = params.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ProtocolError("ids must be a non-empty list", BAD_PARAMS)
        for fact_id in ids:
            assistant.memory.delete_fact(str(fact_id))
        return {"deleted": len(ids)}

    def set_tool_enabled(params: dict) -> dict:
        """Switch one tool on or off for one agent.

        Persisted through the configuration controller rather than written
        here, so a toggle made from the dashboard survives a restart on exactly
        the terms every other change from the dashboard does -- and so there is
        still only one thing that writes .env.
        """
        agent = params.get("agent")
        tool = params.get("tool")
        enabled = params.get("enabled")
        if not isinstance(agent, str) or not isinstance(tool, str):
            raise ProtocolError("agent and tool must be strings", BAD_PARAMS)
        if not isinstance(enabled, bool):
            raise ProtocolError("enabled must be true or false", BAD_PARAMS)

        try:
            result = policy.set_enabled(agent, tool, enabled)
        except (UnknownAgentError, ToolError) as exc:
            raise ProtocolError(str(exc), BAD_PARAMS) from exc

        if config is not None:
            outcome = config.apply({"disabled_tools": result.pop("spec")}, persist=True)
            result["persisted"] = bool(outcome.get("persisted"))
        else:
            result.pop("spec")
            result["persisted"] = False
        return result

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
        "end_conversation": end_conversation,
        # Grouped by the agent that may call them, and every agent is listed --
        # including the coding tier, which has no tools because it does not
        # exist yet. A flat list could not say which tier a tool belonged to,
        # and the two tiers do not hold the same set.
        "list_tools": lambda params: policy.describe(),
        "set_tool_enabled": set_tool_enabled,
        "list_facts": lambda params: [
            _fact_summary(fact) for fact in assistant.memory.all_facts()[: limit_of(params)]
        ],
        "delete_facts": delete_facts,
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

    primary: Any = None
    if use_mic:
        # Wired to the microphone's own voice detection rather than to the
        # interrupt bus. Subscribing to the bus was free and wrong: every
        # interrupt reported speech, so pressing `s` in the dashboard -- or
        # Ctrl-C, or injecting a line -- left the assistant claiming to be
        # hearing someone with nothing to hear and nothing to move it back.
        primary = MicrophoneTranscriptSource(
            interrupts, settings, on_speech=lambda: state.set_phase(HEARING)
        )
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

    # Built here rather than inside the handlers so that the stored spec is
    # applied to the registries before the first turn, and so the config
    # controller and the control socket are talking about the same object.
    policy = build_tool_policy(assistant, settings)

    server = None
    if control:
        server = ControlServer(
            control_socket(),
            build_control_handlers(
                assistant,
                source,
                interrupts,
                state,
                build_config_controller(assistant, voice, source, settings, policy),
                floor=floor,
                policy=policy,
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
        with end_conversation_on_sigterm(source), barge_in_on_sigint(interrupts):
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
