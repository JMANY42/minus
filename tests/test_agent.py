"""Tests for the conversation tool loop.

Formerly test_conversation.py, which needed fake `openai` and `dotenv` modules
installed into sys.modules before importing anything. The agent now takes its
model and tool registry as arguments, so these drive the real loop directly.
"""

from __future__ import annotations

import pytest

import minus.core.agent as agent_module
from minus.core.prompts import FACTS_MARKER
from minus.errors import GenerationFailedError
from minus.tools.registry import ToolRegistry

from .fakes import FakeChatModel, FakeCompletion, FakeFact, FakeMemory, FakeMessage, FakeToolCall


@pytest.fixture
def memory() -> FakeMemory:
    return FakeMemory()


def build_conversation(responses, tools=None, memory=None, **kwargs):
    model = FakeChatModel(responses)
    conversation = agent_module.Conversation(
        model=model,
        tools=tools if tools is not None else ToolRegistry(),
        memory=memory or FakeMemory(),
        **kwargs,
    )
    return conversation, model


class TestToolLoop:
    def test_tool_result_feeds_back_into_a_second_turn(self, memory):
        calls = []
        tools = ToolRegistry()

        @tools.tool
        def list_workspace_files(path: str = ".") -> str:
            """List workspace files.

            Args:
                path: Workspace-relative directory.
            """
            calls.append(path)
            return "workspace listing"

        conversation, model = build_conversation(
            [
                FakeCompletion(
                    FakeMessage(tool_calls=[FakeToolCall("list_workspace_files", '{"path":"."}')])
                ),
                FakeCompletion(FakeMessage(content="I have the workspace listing.")),
            ],
            tools=tools,
            memory=memory,
        )

        result = conversation.reply("list the workspace")

        assert result == "I have the workspace listing."
        assert calls == ["."]
        assert len(model.calls) == 2
        # The tool result is in the transcript the second call sees.
        roles = [m["role"] for m in model.calls[1]["messages"]]
        assert "tool" in roles

    def test_unknown_tool_is_reported_back_instead_of_crashing(self, memory):
        conversation, model = build_conversation(
            [
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("set_light", "{}")])),
                FakeCompletion(FakeMessage(content="Sorry, I cannot do that.")),
            ],
            memory=memory,
        )

        assert conversation.reply("turn on the lights") == "Sorry, I cannot do that."

        tool_messages = [m for m in conversation.messages if m["role"] == "tool"]
        assert len(tool_messages) == 1
        assert "set_light" in tool_messages[0]["content"]

    def test_a_failed_tool_round_does_not_consume_the_round_budget(self, memory):
        """A tool error should leave room to recover rather than burning a round."""
        tools = ToolRegistry()

        @tools.tool
        def broken() -> str:
            """Always fails."""
            raise RuntimeError("nope")

        conversation, _ = build_conversation(
            [
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("broken", "{}")])),
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("broken", "{}")])),
                FakeCompletion(FakeMessage(content="Giving up gracefully.")),
            ],
            tools=tools,
            memory=memory,
            max_tool_rounds=1,
        )

        assert conversation.reply("do the thing") == "Giving up gracefully."

    def test_exceeding_the_tool_round_budget_raises(self, memory):
        tools = ToolRegistry()

        @tools.tool
        def noop() -> str:
            """Does nothing."""
            return "ok"

        conversation, _ = build_conversation(
            [FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("noop", "{}")]))] * 4,
            tools=tools,
            memory=memory,
            max_tool_rounds=2,
        )

        with pytest.raises(GenerationFailedError, match="Tool call limit"):
            conversation.reply("loop forever")


class TestFactInjection:
    def test_relevant_facts_are_appended_under_the_shared_marker(self):
        memory = FakeMemory(facts=[FakeFact(raw_text="The user's favorite band is Queen.")])
        conversation, model = build_conversation(
            [FakeCompletion(FakeMessage(content="Queen, of course."))], memory=memory
        )

        conversation.reply("who is my favorite band?")

        user_message = model.calls[0]["messages"][0]["content"]
        # The marker must be the one the system prompt tells the model to find.
        assert FACTS_MARKER in user_message
        assert "Queen" in user_message

    def test_no_facts_means_an_unmodified_message(self):
        memory = FakeMemory(facts=[])
        conversation, model = build_conversation(
            [FakeCompletion(FakeMessage(content="hi"))], memory=memory
        )

        conversation.reply("hello there")

        assert model.calls[0]["messages"][0]["content"] == "hello there"


class TestPostConversation:
    def test_post_conversation_condenses_and_extracts(self, memory):
        conversation, _ = build_conversation([], memory=memory)
        conversation.messages = [
            {"role": "user", "content": "Please update memory naming."},
            {"role": "assistant", "content": "Updated to conversation_id.json."},
        ]

        assert conversation.post_conversation() == []
        assert memory.condense_calls == [conversation.messages]
