"""Tests for the typed transcript.

These pin the wire format, because it is what the chat API sees and what gets
written to the saved conversation JSON. The previous code built these dicts by
hand at four call sites and ran everything through a reflective
`_make_json_safe`; nothing declared the shape, so nothing could check it.
"""

from __future__ import annotations

import json

from minus.core.messages import Message, ToolCall, Transcript

from .fakes import FakeCompletion, FakeMessage, FakeToolCall


class TestWireFormat:
    def test_user_message(self):
        assert Message.user("hello").to_wire() == {"role": "user", "content": "hello"}

    def test_tool_result_carries_its_call_id(self):
        wire = Message.tool_result("call-7", "the result").to_wire()
        assert wire == {"role": "tool", "content": "the result", "tool_call_id": "call-7"}

    def test_assistant_with_tool_calls(self):
        message = Message.from_completion(
            FakeMessage(content=None, tool_calls=[FakeToolCall("get_time", '{"tz":"UTC"}', "c1")])
        )
        wire = message.to_wire()

        assert wire["role"] == "assistant"
        assert wire["content"] is None
        assert wire["tool_calls"] == [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "get_time", "arguments": '{"tz":"UTC"}'},
            }
        ]

    def test_assistant_without_tool_calls_omits_the_key(self):
        wire = Message.from_completion(FakeMessage(content="plain answer")).to_wire()
        assert "tool_calls" not in wire

    def test_every_message_is_json_serializable(self):
        """The transcript is persisted as JSON, so nothing may leak an SDK object."""
        transcript = Transcript()
        transcript.append(Message.user("hi"))
        transcript.append(
            Message.from_completion(
                FakeMessage(tool_calls=[FakeToolCall("get_time", "{}", "c1")])
            )
        )
        transcript.append(Message.tool_result("c1", "12:00"))

        # Would raise TypeError on any non-serializable value.
        assert len(json.loads(json.dumps(transcript.to_wire()))) == 3


class TestRoundTrip:
    def test_wire_round_trip_preserves_tool_calls(self):
        original = Message.from_completion(
            FakeMessage(content="x", tool_calls=[FakeToolCall("t", '{"a":1}', "c9")])
        )
        restored = Message.from_wire(original.to_wire())

        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.tool_calls == [ToolCall(id="c9", name="t", arguments='{"a":1}')]

    def test_content_override_wins(self):
        completion = FakeCompletion(FakeMessage(content="  padded  "))
        message = Message.from_completion(completion.choices[0].message, content="trimmed")
        assert message.content == "trimmed"


class TestTranscriptPersistence:
    def test_append_writes_through_to_memory(self):
        saved = []

        class RecordingMemory:
            def save(self, messages):
                saved.append(messages)

        transcript = Transcript(memory=RecordingMemory())
        transcript.append(Message.user("one"))
        transcript.append(Message.user("two"))

        # Every append persists the whole transcript, so a crash mid-session
        # still leaves a complete record on disk.
        assert len(saved) == 2
        assert len(saved[-1]) == 2

    def test_replace_swaps_the_whole_transcript(self):
        transcript = Transcript()
        transcript.append(Message.user("old"))
        transcript.replace([Message.user("new")])

        assert transcript.to_wire() == [{"role": "user", "content": "new"}]
