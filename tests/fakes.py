"""Test doubles.

These exist because the code under test now receives its collaborators. The
previous suite had to install fake `openai` and `dotenv` modules into
sys.modules before its first import, which meant every test file began with
twenty lines of module-system surgery and was partly testing its own stubs.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any


class FakeToolCall:
    def __init__(self, name: str, arguments: str, tool_id: str = "call-1") -> None:
        self.id = tool_id
        self.type = "function"
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeCompletion:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [types.SimpleNamespace(message=message)]


class FakeChatModel:
    """A ChatModel that replays scripted completions and records its calls.

    An entry that is an Exception is raised instead of returned, which is how
    retry behaviour gets exercised without a network.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        max_retries: int | None = None,
        retry_note: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "system_prompt": system_prompt,
                "tools": tools,
                "retry_note": retry_note,
            }
        )
        if not self.responses:
            raise AssertionError("FakeChatModel ran out of scripted responses")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeFact:
    id: str = "fact-1"
    attribute: str = "timezone"
    value: str = "PST"
    multi_valued: bool = False
    raw_text: str = "The user's timezone is PST."
    confidence: float = 1.0
    source_session_id: str | None = None
    created_at: float = 0.0
    active: bool = True
    superseded_by: str | None = None
    similarity: float | None = None


@dataclass
class FakeMemory:
    """A MemoryManager stand-in that records rather than persists."""

    facts: list[FakeFact] = field(default_factory=list)
    saved_messages: list[list] = field(default_factory=list)
    condense_calls: list[list] = field(default_factory=list)
    search_calls: list[tuple] = field(default_factory=list)

    def save(self, messages: list) -> None:
        self.saved_messages.append(list(messages))

    def condense_conversation(self, messages: list) -> dict:
        self.condense_calls.append(list(messages))
        return {"condensed_conversation": list(messages)}

    def search_facts(self, query: str, top_k: int = 5, **kwargs: Any) -> list[FakeFact]:
        self.search_calls.append((query, top_k))
        return list(self.facts)

    def extract_and_store_semantic_memory(self, condensed_conversation: Any) -> list:
        return []

    def all_facts(self, only_active: bool = True) -> list[FakeFact]:
        return list(self.facts)


class FakeEmbedder:
    """Deterministic embeddings, so the fact store can be tested without torch."""

    dimensions = 8

    def embed(self, text: str) -> bytes:
        import struct

        vector = [0.0] * self.dimensions
        for index, char in enumerate(text.lower()):
            vector[index % self.dimensions] += (ord(char) % 13) / 13.0
        magnitude = sum(v * v for v in vector) ** 0.5 or 1.0
        return struct.pack(f"{self.dimensions}f", *[v / magnitude for v in vector])
