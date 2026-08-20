"""Tests for the conversation: transcript, fact recall, and closing a session.

Formerly test_conversation.py, which needed fake `openai` and `dotenv` modules
installed into sys.modules before importing anything. The agent now takes its
model and tool registry as arguments, so these drive the real thing directly.

The tool loop moved to test_tool_loop.py along with the loop itself. What is
left here is what `reply` adds on top of it, plus the two ways a conversation
ends.
"""

from __future__ import annotations

import pytest

import minus.core.agent as agent_module
from minus.prompts import FACTS_MARKER
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


class TestReply:
    def test_the_tool_loop_runs_against_the_persisted_transcript(self, memory):
        """The loop appends through the conversation's own transcript, not a copy."""
        tools = ToolRegistry()

        @tools.tool
        def list_workspace_files(path: str = ".") -> str:
            """List workspace files.

            Args:
                path: Workspace-relative directory.
            """
            return "workspace listing"

        conversation, _ = build_conversation(
            [
                FakeCompletion(
                    FakeMessage(tool_calls=[FakeToolCall("list_workspace_files", '{"path":"."}')])
                ),
                FakeCompletion(FakeMessage(content="I have the workspace listing.")),
            ],
            tools=tools,
            memory=memory,
        )

        assert conversation.reply("list the workspace") == "I have the workspace listing."

        roles = [m["role"] for m in conversation.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        # Every turn was written through to memory as it landed.
        assert memory.saved_messages[-1] == conversation.messages

    def test_a_spent_round_budget_answers_rather_than_raising(self, memory):
        """Exhaustion used to raise, which killed the loop in conversation_loop."""
        tools = ToolRegistry()

        @tools.tool
        def noop() -> str:
            """Does nothing."""
            return "ok"

        conversation, _ = build_conversation(
            [
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("noop", "{}")])),
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("noop", "{}")])),
                FakeCompletion(FakeMessage(content="Here is what I have so far.")),
            ],
            tools=tools,
            memory=memory,
            max_tool_rounds=2,
        )

        assert conversation.reply("loop forever") == "Here is what I have so far."


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


class TestClarification:
    """A tool that will not guess turns into a question, not a retry.

    Driven through the real loop rather than by calling
    `tool_failure_message` directly: what matters is that the instruction
    reaches the model as the result of its own call, in the same turn, and that
    the loop does not treat it as a round worth spending.
    """

    def test_the_model_is_told_to_ask_rather_than_to_retry(self, memory):
        from minus.errors import ClarificationNeeded

        tools = ToolRegistry()

        @tools.tool
        def add_google_task(title: str, due: str) -> dict:
            """Add a task."""
            raise ClarificationNeeded("When is it due?")

        conversation, _ = build_conversation(
            [
                FakeCompletion(
                    FakeMessage(
                        tool_calls=[
                            FakeToolCall("add_google_task", '{"title": "Buy milk", "due": ""}')
                        ]
                    )
                ),
                FakeCompletion(FakeMessage(content="When would you like that done by?")),
            ],
            tools=tools,
            memory=memory,
        )

        assert conversation.reply("add buy milk to my tasks") == (
            "When would you like that done by?"
        )

        result = next(m for m in conversation.transcript if m.role == "tool").content
        assert "When is it due?" in result
        assert "Ask the user this now" in result
        # The distinguishing half: an ordinary bad-argument failure invites
        # another attempt, and this one must not.
        assert "retry" not in result.lower()
        assert "Do not call 'add_google_task' again" in result

    def test_an_ordinary_tool_failure_still_invites_a_retry(self, memory):
        tools = ToolRegistry()

        @tools.tool
        def flaky() -> dict:
            """Fail."""
            raise ValueError("disk on fire")

        conversation, _ = build_conversation(
            [
                FakeCompletion(FakeMessage(tool_calls=[FakeToolCall("flaky", "{}")])),
                FakeCompletion(FakeMessage(content="Sorry, that failed.")),
            ],
            tools=tools,
            memory=memory,
        )
        conversation.reply("go")

        result = next(m for m in conversation.transcript if m.role == "tool").content
        assert "Please retry with valid arguments" in result
