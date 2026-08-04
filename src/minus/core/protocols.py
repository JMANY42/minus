"""The interfaces MINUS is built against.

Each protocol marks a place where an implementation is expected to be swapped:
a different model provider, a cloud TTS voice, a non-SQLite fact store, a
transcript source that is a websocket rather than a microphone. Typing against
these rather than against concrete classes is what keeps those swaps local to
the composition root.

These are `typing.Protocol`, so implementations do not inherit from anything --
they simply have the right methods. Tests supply plain fakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatModel(Protocol):
    """Something that can turn a message list into an assistant completion."""

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
        """Return a validated completion, retrying malformed tool calls."""
        ...


@runtime_checkable
class TranscriptSource(Protocol):
    """A stream of finalized user utterances.

    Iteration ends when the conversation is over -- an exit phrase, EOF, or a
    closed audio device. Implementations own their own teardown.
    """

    def __iter__(self) -> Iterator[str]: ...


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Something that can speak text aloud.

    `token` carries the interrupt generation captured when the turn began, so
    a reply whose user has already started talking again is dropped instead of
    spoken over them. Implementations must treat an absent token as "speak".

    Note: the Kokoro implementation installs a SIGINT handler for the duration
    of playback, which CPython only permits on the main thread. A synthesizer
    called from a worker thread must not do that.
    """

    def speak(self, text: str, *, token: int | None = None) -> None: ...


@runtime_checkable
class FactStore(Protocol):
    """Persistent storage for durable facts about the user."""

    def add_fact(
        self,
        attribute: str,
        value: str,
        multi_valued: bool = False,
        raw_text: str | None = None,
        confidence: float = 1.0,
        source_session_id: str | None = None,
    ) -> dict: ...

    def search_facts(self, query: str, top_k: int = 5, only_active: bool = True) -> list: ...

    def get_all_facts(self, only_active: bool = True) -> list: ...

    def get_known_attributes(self, only_active: bool = True) -> list[dict]: ...

    def delete_fact(self, fact_id: str) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a vector for similarity search.

    Separated from the fact store so that the store can be exercised without
    installing sentence-transformers (and therefore torch); tests inject a
    deterministic fake.
    """

    def embed(self, text: str) -> bytes: ...

    @property
    def dimensions(self) -> int: ...
