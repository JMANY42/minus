"""The conversation transcript, as types rather than untyped dicts.

Messages were previously bare dicts assembled by hand at each call site and
funnelled through `_make_json_safe`, a recursive converter that inspected
dataclasses, `__dict__`, tuples and lists to coerce whatever it was given into
something JSON could hold. That worked, but it meant the transcript's shape was
implicit -- nothing declared what keys a tool message needs, and a typo in a
role string would surface as a confusing API error rather than a local one.

These dataclasses declare the shape once. `to_wire()` produces exactly what the
chat API expects, and the reflective fallback is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: str
    type: str = "function"

    @classmethod
    def from_sdk(cls, tool_call: Any) -> ToolCall:
        return cls(
            id=tool_call.id,
            name=tool_call.function.name,
            arguments=tool_call.function.arguments,
            type=getattr(tool_call, "type", "function") or "function",
        )

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Message:
    """One turn in the transcript."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    # ---- Constructors ----

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        return cls(role="tool", content=content, tool_call_id=tool_call_id)

    @classmethod
    def from_completion(cls, message: Any, content: str | None = None) -> Message:
        """Build an assistant turn from an SDK completion message."""
        raw_calls = getattr(message, "tool_calls", None) or []
        return cls(
            role="assistant",
            content=message.content if content is None else content,
            tool_calls=[ToolCall.from_sdk(call) for call in raw_calls],
        )

    # ---- Serialization ----

    def to_wire(self) -> dict:
        """The dict shape the chat API expects."""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_wire() for call in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload

    @classmethod
    def from_wire(cls, payload: dict) -> Message:
        """Rebuild a message from its wire form (for reading saved transcripts)."""
        return cls(
            role=payload["role"],
            content=payload.get("content"),
            tool_calls=[
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"],
                    type=call.get("type", "function"),
                )
                for call in payload.get("tool_calls") or []
            ],
            tool_call_id=payload.get("tool_call_id"),
        )


class Transcript:
    """An ordered list of messages, with optional write-through persistence."""

    def __init__(self, memory: Any | None = None) -> None:
        self._messages: list[Message] = []
        self._memory = memory

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    def __getitem__(self, index: int) -> Message:
        return self._messages[index]

    def append(self, message: Message) -> Message:
        self._messages.append(message)
        if self._memory is not None:
            self._memory.save(self.to_wire())
        return message

    def to_wire(self) -> list[dict]:
        return [message.to_wire() for message in self._messages]

    def replace(self, messages: list[Message]) -> None:
        self._messages = list(messages)
