"""Tests for the deep tier.

Escalation is interesting precisely because it is asynchronous, which makes it
awkward to assert on. Rather than sleep and hope, these swap the executor: an
InlineExecutor runs the job on the calling thread so `escalate()` has finished
by the time it returns, and a StalledExecutor never runs it at all, which is
what "already thinking" needs in order to be observable.
"""

from __future__ import annotations

import types
from queue import Queue

import pytest

from minus.core.escalation import (
    FAILED_SPOKEN,
    GARBLED_SPOKEN,
    DeepResult,
    DeepThinker,
    conversation_context,
)
from minus.core.messages import Transcript
from minus.tools.registry import ToolRegistry

from .fakes import (
    FakeChatModel,
    FakeCompletion,
    FakeDetailSink,
    FakeMemory,
    FakeMessage,
    FakeSpeaker,
    FakeToolCall,
    InlineExecutor,
)

DEEP_MODEL = "deepseek/deepseek-v4-flash-0731"


class StalledExecutor:
    """Accepts work and never runs it."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        return types.SimpleNamespace(done=lambda: False)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None


def answer(
    spoken: str = "Short spoken line.", detail: str = "The long write-up."
) -> FakeCompletion:
    return FakeCompletion(FakeMessage(content=f'{{"spoken": "{spoken}", "detail": "{detail}"}}'))


def build_thinker(responses, *, tools=None, executor=None, **kwargs):
    model = FakeChatModel(responses)
    results: Queue = Queue()
    thinker = DeepThinker(
        model=model,
        deep_model=DEEP_MODEL,
        results=results,
        tools=tools,
        executor=executor or InlineExecutor(),
        reasoning_effort="high",
        **kwargs,
    )
    return thinker, model, results


class TestEscalateTool:
    def test_returns_immediately_and_delivers_on_the_queue(self):
        thinker, _, results = build_thinker([answer()])

        response = thinker.escalate("How should the memory layer be restructured?")

        assert response["status"] == "thinking"
        result = results.get_nowait()
        assert result.spoken == "Short spoken line."
        assert result.detail == "The long write-up."

    def test_uses_the_deep_model_and_asks_for_reasoning_effort(self):
        thinker, model, _ = build_thinker([answer()])

        thinker.escalate("Why is playback stuttering?")

        assert model.calls[0]["model"] == DEEP_MODEL
        # Without this the deep tier is just the fast model with extra latency.
        assert model.calls[0]["reasoning_effort"] == "high"

    def test_refuses_a_second_question_while_one_is_in_flight(self):
        thinker, _, results = build_thinker([answer()], executor=StalledExecutor())

        thinker.escalate("First question")
        response = thinker.escalate("Second question")

        assert response["status"] == "already_thinking"
        assert response["current_question"] == "First question"
        assert results.empty()

    def test_carries_the_conversation_as_context(self):
        thinker, model, _ = build_thinker([answer()])
        thinker.bind_snapshot(
            lambda: [
                {"role": "user", "content": "I am refactoring the audio package."},
                {"role": "assistant", "content": "Sounds fun."},
            ]
        )

        thinker.escalate("Where should chunking live?")

        contents = [message["content"] for message in model.calls[0]["messages"]]
        assert "I am refactoring the audio package." in contents
        assert "Where should chunking live?" in contents


class TestConversationContext:
    def test_drops_the_dangling_tool_call_that_escalate_itself_creates(self):
        # The agent appends the assistant's tool-call turn before dispatching,
        # so a snapshot taken inside escalate() always ends with a tool call
        # whose result does not exist. Replaying it is a malformed request.
        snapshot = [
            {"role": "user", "content": "Think hard about this."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "escalate", "arguments": "{}"},
                    }
                ],
            },
        ]

        kept = conversation_context(snapshot)

        assert kept == [{"role": "user", "content": "Think hard about this."}]

    def test_drops_tool_results_and_blank_turns(self):
        snapshot = [
            {"role": "tool", "content": "a listing", "tool_call_id": "call-1"},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": "still here"},
        ]

        assert conversation_context(snapshot) == [{"role": "user", "content": "still here"}]


class TestDeepToolLoop:
    def test_reads_a_file_before_answering(self):
        tools = ToolRegistry()
        seen = []

        @tools.tool
        def read_workspace_file(path: str) -> str:
            """Read a workspace file.

            Args:
                path: Workspace-relative path.
            """
            seen.append(path)
            return "file contents"

        thinker, _, results = build_thinker(
            [
                FakeCompletion(
                    FakeMessage(
                        tool_calls=[FakeToolCall("read_workspace_file", '{"path":"src/a.py"}')]
                    )
                ),
                answer(spoken="I read it.", detail="Grounded in src/a.py:1."),
            ],
            tools=tools,
        )

        thinker.escalate("What does a.py do?")

        assert seen == ["src/a.py"]
        assert results.get_nowait().detail == "Grounded in src/a.py:1."

    def test_a_failing_tool_is_reported_back_rather_than_killing_the_job(self):
        tools = ToolRegistry()

        @tools.tool
        def read_workspace_file(path: str) -> str:
            """Read a workspace file.

            Args:
                path: Workspace-relative path.
            """
            raise OSError("disk is on fire")

        thinker, model, results = build_thinker(
            [
                FakeCompletion(
                    FakeMessage(tool_calls=[FakeToolCall("read_workspace_file", '{"path":"x"}')])
                ),
                answer(spoken="Recovered.", detail="Could not read it."),
            ],
            tools=tools,
        )

        thinker.escalate("Read x")

        assert results.get_nowait().spoken == "Recovered."
        tool_messages = [m for m in model.calls[1]["messages"] if m.get("role") == "tool"]
        assert "disk is on fire" in tool_messages[0]["content"]

    def test_forces_an_answer_once_the_round_budget_is_spent(self):
        tools = ToolRegistry()

        @tools.tool
        def list_workspace_files(path: str = ".") -> str:
            """List workspace files.

            Args:
                path: Workspace-relative directory.
            """
            return "a listing"

        wants_tool = FakeCompletion(
            FakeMessage(tool_calls=[FakeToolCall("list_workspace_files", "{}")])
        )
        thinker, model, results = build_thinker(
            [wants_tool, wants_tool, answer(spoken="Fine, here it is.")],
            tools=tools,
            max_tool_rounds=2,
        )

        thinker.escalate("Keep listing forever")

        assert results.get_nowait().spoken == "Fine, here it is."
        # The final call is made with no tools on offer, which is what stops the
        # loop from returning nothing after paying for every round.
        assert model.calls[-1]["tools"] is None


class TestResponseParsing:
    def test_malformed_output_is_still_published_as_detail(self):
        thinker, _, results = build_thinker(
            [FakeCompletion(FakeMessage(content="I have thoughts but no JSON."))]
        )

        thinker.escalate("anything")

        result = results.get_nowait()
        assert result.spoken == GARBLED_SPOKEN
        assert result.detail == "I have thoughts but no JSON."

    def test_json_wrapped_in_prose_and_fences_is_recovered(self):
        thinker, _, results = build_thinker(
            [
                FakeCompletion(
                    FakeMessage(
                        content='Sure!\n```json\n{"spoken": "Recovered.", "detail": "Body."}\n```'
                    )
                )
            ]
        )

        thinker.escalate("anything")

        assert results.get_nowait().spoken == "Recovered."

    def test_a_blank_spoken_channel_falls_back_rather_than_speaking_nothing(self):
        thinker, _, results = build_thinker(
            [FakeCompletion(FakeMessage(content='{"spoken": "  ", "detail": "Body."}'))]
        )

        thinker.escalate("anything")

        result = results.get_nowait()
        assert result.spoken == GARBLED_SPOKEN
        assert result.detail == "Body."

    def test_a_crash_still_tells_the_user_something(self):
        thinker, _, results = build_thinker([RuntimeError("provider exploded")])

        thinker.escalate("anything")

        result = results.get_nowait()
        assert result.spoken == FAILED_SPOKEN
        assert "provider exploded" in result.detail


class TestRecentResults:
    def test_a_later_escalation_sees_the_earlier_conclusion(self):
        thinker, model, results = build_thinker(
            [answer(spoken="First.", detail="First conclusion."), answer(spoken="Second.")]
        )

        thinker.escalate("first question")
        results.get_nowait()
        thinker.escalate("second question")

        system_turns = [m for m in model.calls[1]["messages"] if m.get("role") == "system"]
        assert "First conclusion." in system_turns[0]["content"]


class TestDelivery:
    """The courier half, which lives in cli.py."""

    def _assistant(self, sink):
        transcript = Transcript(memory=FakeMemory())
        return types.SimpleNamespace(
            conversation=types.SimpleNamespace(transcript=transcript),
            details=sink,
            results=Queue(),
        )

    def test_publishes_detail_speaks_the_summary_and_records_the_turn(self):
        import threading

        from minus.cli import deliver_deep_result

        sink = FakeDetailSink()
        assistant = self._assistant(sink)
        speaker = FakeSpeaker(token_value=7)
        result = DeepResult(question="Why?", spoken="Because X.", detail="Long form.")

        deliver_deep_result(assistant, speaker, threading.Lock(), result)

        assert sink.published == [("Why?", "Long form.")]
        # Spoken with a token taken at delivery time. Reusing the token from
        # when the escalation started would be stale -- the user has spoken
        # since -- and every deep answer would be silently dropped.
        assert speaker.spoken == [("Because X.", 7)]
        assert assistant.conversation.transcript.to_wire() == [
            {"role": "assistant", "content": "Because X."}
        ]

    def test_only_the_summary_reaches_the_transcript(self):
        import threading

        from minus.cli import deliver_deep_result

        assistant = self._assistant(FakeDetailSink())
        result = DeepResult(question="Why?", spoken="Short.", detail="A" * 5000)

        deliver_deep_result(assistant, FakeSpeaker(), threading.Lock(), result)

        recorded = assistant.conversation.transcript.to_wire()[0]["content"]
        assert recorded == "Short."


class TestFullLoop:
    """The whole path, with only the model and the speaker faked.

    Everything else is real: the agent's tool loop, the registry, the thinker,
    the courier thread and the lock they share. This is what catches the
    threading mistakes that the unit tests above cannot -- the deadlock between
    escalate() and the worker was invisible until the two ran together.
    """

    def test_a_hard_question_is_acked_fast_then_answered_deeply(self):
        from minus.cli import Assistant, conversation_loop
        from minus.core.agent import Conversation

        model = FakeChatModel(
            [
                # The fast model decides this is beyond it.
                FakeCompletion(
                    FakeMessage(
                        tool_calls=[
                            FakeToolCall("escalate", '{"question":"How should memory be split?"}')
                        ]
                    )
                ),
                # The deep tier's two-channel answer, produced during dispatch.
                answer(spoken="Split it by lifetime.", detail="Long form, cites store.py:65."),
                # The fast model's one-line acknowledgement.
                FakeCompletion(FakeMessage(content="On it.")),
            ]
        )

        results: Queue = Queue()
        sink = FakeDetailSink()
        thinker = DeepThinker(
            model=model,
            deep_model=DEEP_MODEL,
            results=results,
            executor=InlineExecutor(),
            reasoning_effort="high",
        )

        tools = ToolRegistry()
        tools.tool(thinker.escalate)

        conversation = Conversation(model=model, tools=tools, memory=FakeMemory())
        thinker.bind_snapshot(lambda: conversation.messages)

        assistant = Assistant(
            conversation=conversation,
            memory=conversation.memory,
            thinker=thinker,
            results=results,
            details=sink,
        )
        speaker = FakeSpeaker(token_value=3)

        conversation_loop(["think hard about how memory should be split"], assistant, speaker)

        spoken = [text for text, _ in speaker.spoken]
        # The quick acknowledgement is spoken first, the deep answer afterwards.
        assert spoken == ["On it.", "Split it by lifetime."]
        assert sink.published == [("How should memory be split?", "Long form, cites store.py:65.")]
        # The deep tier was asked for effort; the conversational turns were not.
        assert model.calls[1]["reasoning_effort"] == "high"
        assert model.calls[0]["reasoning_effort"] is None
        assert model.calls[2]["reasoning_effort"] is None

    def test_an_ordinary_turn_never_touches_the_deep_tier(self):
        from minus.cli import Assistant, conversation_loop
        from minus.core.agent import Conversation

        model = FakeChatModel([FakeCompletion(FakeMessage(content="Just after four."))])
        results: Queue = Queue()
        thinker = DeepThinker(
            model=model, deep_model=DEEP_MODEL, results=results, executor=InlineExecutor()
        )
        tools = ToolRegistry()
        tools.tool(thinker.escalate)

        conversation = Conversation(model=model, tools=tools, memory=FakeMemory())
        assistant = Assistant(
            conversation=conversation,
            memory=conversation.memory,
            thinker=thinker,
            results=results,
            details=FakeDetailSink(),
        )
        speaker = FakeSpeaker()

        conversation_loop(["what time is it"], assistant, speaker)

        assert [text for text, _ in speaker.spoken] == ["Just after four."]
        # One call, on the fast tier. This is the property the whole design
        # exists to protect: cheap turns stay cheap.
        assert len(model.calls) == 1
        assert model.calls[0]["model"] is None


class TestSystemPromptWiring:
    def test_escalation_guidance_is_opt_in(self):
        from pathlib import Path

        from minus.core.prompts import build_system_prompt

        plain = build_system_prompt(Path("/tmp/ws"))
        with_escalation = build_system_prompt(Path("/tmp/ws"), can_escalate=True)

        assert "escalate" not in plain
        assert "escalate" in with_escalation


@pytest.mark.parametrize("effort", [None, "high"])
def test_reasoning_effort_is_only_sent_when_asked_for(effort):
    from minus.config import load_settings
    from minus.llm.client import OpenRouterClient

    sent = {}

    class Transport:
        class chat:
            class completions:
                @staticmethod
                def create(**payload):
                    sent.update(payload)
                    return FakeCompletion(FakeMessage(content="ok"))

    client = OpenRouterClient(load_settings(openrouter_api_key="x"), client=Transport())
    client.complete([{"role": "user", "content": "hi"}], reasoning_effort=effort)

    assert sent.get("reasoning_effort") == effort
