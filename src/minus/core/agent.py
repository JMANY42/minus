"""The conversation agent: one user turn in, one spoken reply out.

What is left here is everything the conversation has that the escalation tier
does not: the transcript that persists as it grows, the facts recalled from
memory and folded into the user's turn, and the condense-and-extract work that
closes a session. The tool loop itself lives in `loop.py`, which the deep tier
runs too -- see that module for why a failed tool round is free and why running
out of rounds produces an answer rather than an exception.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.core.loop import ToolLoop
from minus.core.messages import Message, Transcript
from minus.memory.service import MemoryManager
from minus.prompts import FACTS_MARKER, SYSTEM_PROMPT
from minus.services.json import pretty_json
from minus.tools import registry as default_registry

logger = logging.getLogger(__name__)


class Conversation:
    """One session: transcript, tools, memory and the model that drives them."""

    def __init__(
        self,
        model: Any,
        tools: Any | None = None,
        max_tool_rounds: int = 5,
        memory: Any | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        fact_top_k: int = 5,
    ) -> None:
        """
        Args:
            model: A ChatModel. Injected rather than imported so that swapping
                providers, or stubbing the model in tests, is a caller's choice.
            tools: A ToolRegistry. Defaults to the shared registry of built-in
                tools; pass your own to scope what the model can call.
        """
        self.model = model
        self.memory = memory or MemoryManager()
        self.tools = tools if tools is not None else default_registry
        self.max_tool_rounds = max_tool_rounds
        self.system_prompt = system_prompt
        self.fact_top_k = fact_top_k
        self.transcript = Transcript(memory=self.memory)

    # Kept so callers (and tests) can read the transcript as wire dicts.
    @property
    def messages(self) -> list[dict]:
        return self.transcript.to_wire()

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        self.transcript.replace([Message.from_wire(m) for m in value])

    # ---- Fact recall ----

    def _build_user_message(self, transcript: str) -> Message:
        """The user's turn, with any relevant stored facts appended.

        Facts are appended to the user message rather than sent as a separate
        turn because the model attends to them far more reliably there.
        """
        facts = [
            fact.raw_text.strip() for fact in self.memory.search_facts(transcript, self.fact_top_k)
        ]
        logger.debug("Relevant facts: %s", facts)

        if not facts:
            return Message.user(transcript)

        return Message.user(f"{transcript}\n\n{FACTS_MARKER}\n{pretty_json(facts)}")

    # ---- Public API ----

    def reply(self, transcript: str) -> str:
        """Produce the assistant's spoken reply to one user utterance."""
        self.transcript.append(self._build_user_message(transcript))

        # Built per call rather than held as a field: `start_new_conversation`
        # and the `messages` setter both replace `self.transcript`, and a loop
        # constructed once would go on appending to the old one.
        message = ToolLoop(
            model=self.model,
            transcript=self.transcript,
            tools=self.tools,
            system_prompt=self.system_prompt,
            max_tool_rounds=self.max_tool_rounds,
        ).run()
        return message.content or ""

    def post_conversation(self) -> list[dict]:
        """Condense the finished conversation and extract durable facts."""
        condensed = self.memory.condense_conversation(self.messages)
        return self.memory.extract_and_store_semantic_memory(condensed)

    def start_new_conversation(self) -> str:
        """Begin a fresh conversation, leaving the finished one on disk.

        Call after post_conversation(): this drops the transcript, so anything
        not condensed and extracted by then is gone from memory's point of
        view. The fact store is untouched -- what the last conversation taught
        is exactly what survives into this one.
        """
        conversation_id = self.memory.start_new_conversation()
        self.transcript = Transcript(memory=self.memory)
        return conversation_id
