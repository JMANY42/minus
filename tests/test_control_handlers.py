"""The commands the control socket exposes, over a real socket.

`run_assistant` itself cannot be exercised without the audio extra installed,
so this covers the half that does not need it: the handler table the
composition root builds, wired to a real MergedTranscriptSource and a real
server, which is where the interesting behaviour is anyway.
"""

from __future__ import annotations

from queue import Queue

import pytest

from minus.cli import Assistant, _describe, build_control_handlers, build_parser
from minus.control.client import ControlClient, ControlError
from minus.control.server import ControlServer
from minus.control.state import THINKING, RuntimeState
from minus.core.messages import Message, Transcript
from minus.core.sources import MergedTranscriptSource


class FakeInterrupts:
    def __init__(self) -> None:
        self.generation = 0

    def request(self) -> int:
        self.generation += 1
        return self.generation


class FakeRegistry:
    def schemas(self) -> list[dict]:
        return [
            {"function": {"name": "get_current_time", "description": "What time it is."}},
            {"function": {"name": "escalate", "description": "Think harder."}},
        ]


class FakeFact:
    def __init__(self, attribute: str, value: str) -> None:
        self.attribute = attribute
        self.value = value
        self.active = True
        self.created_at = 1786400000.0


class FakeMemory:
    conversation_id = "20260810T205424Z-2cd0f926"
    file_path = "/tmp/minus-test/conversations/x.json"

    def all_facts(self) -> list[FakeFact]:
        return [FakeFact("favorite_band", "queen"), FakeFact("chronotype", "night owl")]


class FakeConversation:
    def __init__(self) -> None:
        self.tools = FakeRegistry()
        self.transcript = Transcript()
        self.transcript.append(Message.user("hello"))


class FakeThinker:
    def status(self) -> dict:
        return {"in_flight": False, "question": None, "elapsed_seconds": None}


@pytest.fixture
def wired(short_socket_path):
    interrupts = FakeInterrupts()
    state = RuntimeState(pid=4242)
    source = MergedTranscriptSource(None, idle_timeout=0)
    assistant = Assistant(
        conversation=FakeConversation(),
        memory=FakeMemory(),
        thinker=FakeThinker(),
        results=Queue(),
        details=None,
    )
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

    def test_lists_tools_including_escalate(self, wired):
        """The composed registry is authoritative -- escalate is only in that one."""
        client, _, _, _ = wired

        names = [tool["name"] for tool in client.request("list_tools")]

        assert "escalate" in names
        assert "get_current_time" in names

    def test_lists_facts(self, wired):
        client, _, _, _ = wired

        facts = client.request("list_facts")

        assert facts[0]["attribute"] == "favorite_band"
        assert facts[0]["value"] == "queen"

    def test_the_unbuilt_panels_return_an_empty_list(self, wired):
        """Shape frozen now; only the data source changes when they exist."""
        client, _, _, _ = wired

        assert client.request("list_agents") == []
        assert client.request("list_programs") == []

    def test_interrupt_bumps_the_generation(self, wired):
        client, _, interrupts, _ = wired

        assert client.request("interrupt") == {"generation": 1}
        assert interrupts.generation == 1


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
