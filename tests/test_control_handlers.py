"""The commands the control socket exposes, over a real socket.

`run_assistant` itself cannot be exercised without the audio extra installed,
so this covers the half that does not need it: the handler table the
composition root builds, wired to a real MergedTranscriptSource and a real
server, which is where the interesting behaviour is anyway.
"""

from __future__ import annotations

import time
from queue import Queue
from types import SimpleNamespace

import pytest

from minus.assembly import build_config_controller, build_control_handlers, build_tool_policy
from minus.cli import _describe, build_parser
from minus.config import Settings
from minus.control.client import ControlClient, ControlError
from minus.control.server import ControlServer
from minus.control.state import THINKING, RuntimeState
from minus.core.messages import Message, Transcript
from minus.core.sources import MergedTranscriptSource
from minus.runtime import Assistant
from minus.tools.registry import ToolRegistry


class FakeInterrupts:
    def __init__(self) -> None:
        self.generation = 0

    def request(self) -> int:
        self.generation += 1
        return self.generation


def fake_registry(*names: str) -> ToolRegistry:
    """A real registry holding stand-ins for the tools a tier would have.

    Real rather than faked: the handlers ask a registry which of its tools are
    switched on and switch them, and a double that answered those from a dict
    would be pinning the double's behaviour rather than the registry's.
    """
    registry = ToolRegistry()

    def get_current_time() -> str:
        """What time it is."""
        return "now"

    def escalate(question: str) -> str:
        """Think harder."""
        return "on it"

    def read_workspace_file(path: str) -> str:
        """Read a file."""
        return "contents"

    available = {tool.__name__: tool for tool in (get_current_time, escalate, read_workspace_file)}
    for name in names:
        registry.tool(available[name])
    return registry


class FakeFact:
    def __init__(self, attribute: str, value: str) -> None:
        self.id = f"fact-{attribute}"
        self.attribute = attribute
        self.value = value
        self.active = True
        self.created_at = 1786400000.0


class FakeMemory:
    conversation_id = "20260810T205424Z-2cd0f926"
    file_path = "/tmp/minus-test/conversations/x.json"

    def __init__(self) -> None:
        # Held rather than rebuilt per call, so that forgetting one is visible
        # to the next `list_facts`.
        self.facts = [FakeFact("favorite_band", "queen"), FakeFact("chronotype", "night owl")]

    def all_facts(self) -> list[FakeFact]:
        return list(self.facts)

    def delete_fact(self, fact_id: str) -> None:
        self.facts = [fact for fact in self.facts if fact.id != fact_id]


class FakeConversation:
    def __init__(self) -> None:
        self.tools = fake_registry("get_current_time", "escalate")
        self.transcript = Transcript()
        self.transcript.append(Message.user("hello"))
        self.condensed = 0

    def post_conversation(self) -> list[dict]:
        self.condensed += 1
        return [{"attribute": "favorite_band", "value": "queen"}]

    def start_new_conversation(self) -> str:
        self.transcript = Transcript()
        return "20260811T090000Z-fresh"


class FakeThinker:
    def __init__(self) -> None:
        # A narrower set than the conversation's, as the real deep tier's is.
        self.tools = fake_registry("read_workspace_file")

    def status(self) -> dict:
        return {"in_flight": False, "question": None, "elapsed_seconds": None, "started_at": None}


@pytest.fixture
def assistant():
    return Assistant(
        conversation=FakeConversation(),
        memory=FakeMemory(),
        thinker=FakeThinker(),
        results=Queue(),
        details=None,
    )


@pytest.fixture
def wired(short_socket_path, assistant):
    interrupts = FakeInterrupts()
    state = RuntimeState(pid=4242)
    source = MergedTranscriptSource(None, idle_timeout=0)
    state.provide("deep", assistant.thinker.status)

    server = ControlServer(
        short_socket_path,
        build_control_handlers(assistant, source, interrupts, state),
        state=state,
    )
    server.start()
    with ControlClient(short_socket_path) as client:
        yield client, source, interrupts, state
    server.close()


class TestSay:
    def test_reaches_the_transcript_source(self, wired):
        client, source, _, _ = wired

        assert client.request("say", text="what time is it") == {"accepted": True}
        assert next(iter(source)) == "what time is it"

    def test_barges_in_like_real_speech(self, wired):
        """Typing at the CLI prompt interrupts a reply; injection must too."""
        client, _, interrupts, _ = wired

        client.request("say", text="stop talking")

        assert interrupts.generation == 1

    def test_refuses_an_empty_line(self, wired):
        client, _, interrupts, _ = wired

        with pytest.raises(ControlError):
            client.request("say", text="   ")

        assert interrupts.generation == 0

    def test_refuses_a_non_string(self, wired):
        client, _, _, _ = wired

        with pytest.raises(ControlError):
            client.request("say", text=42)


class TestIntrospection:
    def test_hello_reports_the_protocol_version(self, wired):
        client, _, _, _ = wired

        assert client.request("hello")["protocol"] == 1

    def test_status_carries_the_phase_and_conversation(self, wired):
        client, _, _, state = wired
        state.set_phase(THINKING)

        snapshot = client.request("get_status")

        assert snapshot["phase"] == THINKING
        assert snapshot["deep"]["in_flight"] is False

    def test_lists_tools_by_agent(self, wired):
        """The composed registries are authoritative -- escalate is only in one."""
        client, _, _, _ = wired

        agents = {agent["key"]: agent for agent in client.request("list_tools")}

        conversational = [tool["name"] for tool in agents["conversational"]["tools"]]
        assert "escalate" in conversational
        assert "get_current_time" in conversational
        assert [tool["name"] for tool in agents["deep"]["tools"]] == ["read_workspace_file"]

    def test_lists_the_coding_agent_with_the_reason_it_is_empty(self, wired):
        """The seat kept warm: the panel's shape does not change when it arrives."""
        client, _, _, _ = wired

        coding = next(a for a in client.request("list_tools") if a["key"] == "coding")

        assert coding["tools"] == []
        assert coding["note"]

    def test_a_listed_tool_says_whether_it_is_switched_on(self, wired):
        client, _, _, _ = wired

        agents = client.request("list_tools")

        assert all(tool["enabled"] for agent in agents for tool in agent["tools"])

    def test_lists_facts(self, wired):
        client, _, _, _ = wired

        facts = client.request("list_facts")

        assert facts[0]["attribute"] == "favorite_band"
        assert facts[0]["value"] == "queen"

    def test_a_listed_fact_carries_the_id_needed_to_forget_it(self, wired):
        """The dashboard deletes by id; a summary without one is read-only."""
        client, _, _, _ = wired

        facts = client.request("list_facts")

        assert facts[0]["id"] == "fact-favorite_band"


class TestForgettingFacts:
    """The dashboard's `d` key, from the other end of the socket."""

    def test_it_forgets_the_named_facts(self, wired):
        client, _, _, _ = wired

        assert client.request("delete_facts", ids=["fact-chronotype"]) == {"deleted": 1}

        assert [fact["attribute"] for fact in client.request("list_facts")] == ["favorite_band"]

    def test_it_forgets_several_at_once(self, wired):
        client, _, _, _ = wired

        client.request("delete_facts", ids=["fact-chronotype", "fact-favorite_band"])

        assert client.request("list_facts") == []

    @pytest.mark.parametrize("params", [{}, {"ids": []}, {"ids": "fact-chronotype"}])
    def test_it_refuses_anything_that_is_not_a_list_of_ids(self, wired, params):
        """A hard delete is the wrong place to guess at what was meant."""
        client, _, _, _ = wired

        with pytest.raises(ControlError):
            client.request("delete_facts", **params)

        assert len(client.request("list_facts")) == 2

    def test_the_unbuilt_panels_return_an_empty_list(self, wired):
        """Shape frozen now; only the data source changes when they exist."""
        client, _, _, _ = wired

        assert client.request("list_agents") == []
        assert client.request("list_programs") == []

    def test_interrupt_bumps_the_generation(self, wired):
        client, _, interrupts, _ = wired

        assert client.request("interrupt") == {"generation": 1}
        assert interrupts.generation == 1


class TestFactsOverTheSocket:
    """The whole path, with a real fact store rather than a fake one.

    The handler runs on the server's reader thread while the store was opened
    on this one -- which is the arrangement that made a live dashboard fail
    with "SQLite objects created in a thread can only be used in that same
    thread". A fake memory cannot express that.
    """

    def test_list_facts_answers_from_a_real_store(self, short_socket_path, tmp_path):
        from minus.memory.facts.store import SqliteFactStore

        from .fakes import FakeEmbedder

        store = SqliteFactStore(tmp_path / "facts.db", embedder=FakeEmbedder())
        store.add_fact("favorite_band", "queen")

        class RealBackedMemory:
            conversation_id = "c1"
            file_path = str(tmp_path / "c1.json")

            def all_facts(self):
                return store.get_all_facts()

        state = RuntimeState()
        assistant = Assistant(
            conversation=FakeConversation(),
            memory=RealBackedMemory(),
            thinker=FakeThinker(),
            results=Queue(),
            details=None,
        )
        server = ControlServer(
            short_socket_path,
            build_control_handlers(
                assistant, MergedTranscriptSource(None, idle_timeout=0), FakeInterrupts(), state
            ),
            state=state,
        )
        server.start()
        try:
            with ControlClient(short_socket_path) as client:
                facts = client.request("list_facts")
        finally:
            server.close()
            store.close()

        assert [fact["value"] for fact in facts] == ["queen"]


class TestSwitchingTools:
    """The tools panel's space bar, from the other end of the socket."""

    def test_it_switches_one_agents_tool_off(self, wired, assistant):
        client, _, _, _ = wired

        result = client.request(
            "set_tool_enabled", agent="conversational", tool="escalate", enabled=False
        )

        assert result["enabled"] is False
        assert assistant.conversation.tools.enabled("escalate") is False

    def test_a_tool_switched_off_is_no_longer_offered_to_the_model(self, wired, assistant):
        """The point of the switch: `schemas()` is what the model is told about."""
        client, _, _, _ = wired

        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=False)

        offered = [s["function"]["name"] for s in assistant.conversation.tools.schemas()]
        assert offered == ["get_current_time"]

    def test_it_is_still_listed_so_it_can_be_switched_back_on(self, wired):
        client, _, _, _ = wired
        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=False)

        agents = {agent["key"]: agent for agent in client.request("list_tools")}
        escalate = next(t for t in agents["conversational"]["tools"] if t["name"] == "escalate")

        assert escalate["enabled"] is False

    def test_switching_it_back_on_restores_it(self, wired, assistant):
        client, _, _, _ = wired
        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=False)

        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=True)

        assert assistant.conversation.tools.enabled("escalate") is True

    def test_the_tiers_are_switched_separately(self, wired, assistant):
        """One Tool object is shared by both registries; the switch is not."""
        client, _, _, _ = wired

        client.request("set_tool_enabled", agent="deep", tool="read_workspace_file", enabled=False)

        assert assistant.thinker.tools.enabled("read_workspace_file") is False
        assert "read_workspace_file" not in assistant.conversation.tools.disabled

    def test_an_unknown_agent_is_refused(self, wired):
        client, _, _, _ = wired

        with pytest.raises(ControlError):
            client.request("set_tool_enabled", agent="coding", tool="escalate", enabled=False)

    def test_an_unknown_tool_is_refused(self, wired):
        client, _, _, _ = wired

        with pytest.raises(ControlError):
            client.request(
                "set_tool_enabled", agent="conversational", tool="nonesuch", enabled=False
            )

    def test_a_missing_flag_is_refused(self, wired):
        """`enabled` has to be said outright: absent is not off."""
        client, _, _, _ = wired

        with pytest.raises(ControlError):
            client.request("set_tool_enabled", agent="conversational", tool="escalate")

    def test_without_configuration_it_applies_but_does_not_persist(self, wired):
        """The `wired` assistant has no config behind it, and says so."""
        client, _, _, _ = wired

        result = client.request(
            "set_tool_enabled", agent="conversational", tool="escalate", enabled=False
        )

        assert result["persisted"] is False


class TestSwitchingToolsPersists:
    """With configuration behind it, a switch outlives the process."""

    @pytest.fixture
    def wired_with_config(self, short_socket_path, assistant, tmp_path, monkeypatch):
        # The real controller, on the real table, writing to a .env under
        # tmp_path -- so this pins that `disabled_tools` is actually one of the
        # live fields the composition root declares, rather than a stand-in
        # that would go on passing after it was dropped from there.
        monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
        state = RuntimeState()
        settings = Settings(openrouter_api_key="x")
        policy = build_tool_policy(assistant, settings)
        config = build_config_controller(
            assistant, SimpleNamespace(), SimpleNamespace(), settings, policy
        )
        server = ControlServer(
            short_socket_path,
            build_control_handlers(
                assistant,
                MergedTranscriptSource(None, idle_timeout=0),
                FakeInterrupts(),
                state,
                config,
                policy=policy,
            ),
            state=state,
        )
        server.start()
        with ControlClient(short_socket_path) as client:
            yield client, settings, tmp_path / ".env"
        server.close()

    def test_it_writes_the_switch_to_the_env_file(self, wired_with_config):
        client, settings, env_path = wired_with_config

        result = client.request(
            "set_tool_enabled", agent="conversational", tool="escalate", enabled=False
        )

        assert result["persisted"] is True
        assert "MINUS_DISABLED_TOOLS=conversational:escalate" in env_path.read_text()
        assert settings.disabled_tools == "conversational:escalate"

    def test_switching_back_on_leaves_nothing_behind(self, wired_with_config):
        """Only what is off is written, so an emptied list is an empty string."""
        client, settings, _ = wired_with_config
        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=False)

        client.request("set_tool_enabled", agent="conversational", tool="escalate", enabled=True)

        assert settings.disabled_tools == ""

    def test_a_stored_switch_is_applied_at_startup(self, assistant):
        """What `build_tool_policy` is for: the spec reaches the registries."""
        settings = Settings(openrouter_api_key="x", disabled_tools="deep:read_workspace_file")

        build_tool_policy(assistant, settings)

        assert assistant.thinker.tools.enabled("read_workspace_file") is False
        assert assistant.conversation.tools.disabled == []


class TestEndConversation:
    """The `e` key's command. The rollover itself is test_idle_rollover.py's."""

    def test_it_answers_before_it_condenses(self, wired):
        """Condensing is two model calls -- many times the client's timeout."""
        client, _, _, _ = wired

        assert client.request("end_conversation") == {"accepted": True}

    def test_it_barges_in_first(self, wired):
        """`immediately` has to mean during a reply too, or the floor is held."""
        client, _, interrupts, _ = wired

        client.request("end_conversation")

        assert interrupts.generation == 1

    def test_the_rollover_really_runs_and_is_announced(self, wired, assistant):
        """A status event is the only way the caller learns it landed."""
        client, _, _, _ = wired
        events: list[dict] = []

        with ControlClient(client.path, on_event=events.append) as watcher:
            watcher.subscribe()
            client.request("end_conversation")

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(events) < 2:
                time.sleep(0.01)

        assert assistant.conversation.condensed == 1
        assert len(assistant.conversation.transcript) == 0
        # The subscription's own snapshot, then the one the rollover pushed.
        assert len(events) >= 2


class TestShutdown:
    def test_ends_the_transcript_source(self, wired):
        client, source, _, _ = wired

        assert client.request("shutdown") == {"stopping": True}
        assert list(source) == []


class TestStatusLine:
    def test_describes_an_idle_assistant(self):
        line = _describe({"phase": "listening", "conversation": {"id": "abc", "message_count": 4}})

        assert "listening" in line
        assert "abc" in line

    def test_mentions_a_deep_answer_in_flight(self):
        line = _describe(
            {
                "phase": "listening",
                "deep": {"in_flight": True, "elapsed_seconds": 12.4, "question": "why"},
            }
        )

        assert "deep" in line
        assert "12s" in line

    def test_survives_a_snapshot_with_nothing_in_it(self):
        assert _describe({})


class TestParser:
    def test_say_takes_an_unquoted_sentence(self):
        args = build_parser().parse_args(["say", "what", "time", "is", "it"])

        assert args.command == "say"
        assert " ".join(args.text) == "what time is it"

    def test_status_supports_watching(self):
        assert build_parser().parse_args(["status", "--watch"]).watch is True

    def test_control_can_be_turned_off(self):
        assert build_parser().parse_args(["--no-control"]).no_control is True

    def test_the_existing_surface_is_unchanged(self):
        assert build_parser().parse_args(["--no-mic"]).no_mic is True
        assert build_parser().parse_args(["tools"]).command == "tools"
