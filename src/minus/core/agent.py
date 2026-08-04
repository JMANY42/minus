"""The conversation agent: one user turn in, one spoken reply out.

The loop was previously a set of module-level functions that each took the
conversation object as their first argument -- `build_reply(conversation, ...)`,
`process_tool_calls(conversation, ...)` -- which is a class written inside out.
Folding them into methods removes the parameter and makes the state each step
touches explicit.

The loop itself is unchanged in shape, including the two decisions that are
easy to mistake for bugs and are not:

  * A generation failure is recorded in the transcript rather than raised, so
    the next round sees the dead end instead of walking back into it.
  * A failed tool round does not consume the round budget, so an argument
    mistake leaves room to recover rather than costing the model its turn.
"""

from __future__ import annotations

import logging
from typing import Any

from minus.core.messages import Message, Transcript
from minus.core.prompts import FACTS_MARKER, RETRY_NOTE, SYSTEM_PROMPT
from minus.errors import GenerationFailedError, LLMError, ToolError
from minus.memory.service import MemoryManager
from minus.services.json import pretty_json
from minus.tools import registry as default_registry

logger = logging.getLogger(__name__)


def generation_failure_message(exc: BaseException) -> Message:
    """Record a failed generation in the transcript.

    Retries inside the client are invisible to later rounds, so a round that
    burned all its retries would otherwise leave no trace and the next round
    would regenerate the same dead end.
    """
    return Message.system(
        f"The previous assistant turn could not be generated: {exc} "
        "Do not repeat that attempt. Either call one of the available tools "
        "with valid arguments, or answer the user in plain text."
    )


def tool_failure_message(tool_name: str, exc: BaseException) -> str:
    return (
        f"Tool execution failed for {tool_name!r}: {exc}. "
        "Please retry with valid arguments, or answer without the tool."
    )


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
            fact.raw_text.strip()
            for fact in self.memory.search_facts(transcript, self.fact_top_k)
        ]
        logger.debug("Relevant facts: %s", facts)

        if not facts:
            return Message.user(transcript)

        return Message.user(f"{transcript}\n\n{FACTS_MARKER}\n{pretty_json(facts)}")

    # ---- Tool execution ----

    def _run_tool_call(self, tool_call) -> bool:
        """Execute one tool call. Returns True if it failed."""
        try:
            result = self.tools.dispatch(tool_call.name, tool_call.arguments)
        # ToolError covers unknown tools, bad arguments and failures inside a
        # tool body. OSError is kept because a tool may touch the filesystem in
        # ways the registry cannot wrap. Anything else is a genuine bug.
        except (ToolError, OSError) as exc:
            logger.warning("Tool %s failed: %s", tool_call.name, exc)
            self.transcript.append(
                Message.tool_result(tool_call.id, tool_failure_message(tool_call.name, exc))
            )
            return True

        logger.debug("Tool result for %s:\n%s", tool_call.name, pretty_json(result))
        self.transcript.append(Message.tool_result(tool_call.id, result))
        return False

    def _run_tool_calls(self, tool_calls) -> bool:
        """Execute tool calls in order, stopping at the first failure."""
        return any(self._run_tool_call(call) for call in tool_calls)

    # ---- One round ----

    def _handle_completion(self, completion) -> tuple[str | None, bool]:
        """Record the assistant turn. Returns (final_text, tool_round_failed)."""
        raw_message = completion.choices[0].message
        message = Message.from_completion(raw_message)

        if not message.tool_calls:
            # validate_completion guarantees non-blank content when there are
            # no tool calls, so .strip() is safe here.
            text = (message.content or "").strip()
            self.transcript.append(Message.from_completion(raw_message, content=text))
            return text, False

        self.transcript.append(message)
        return None, self._run_tool_calls(message.tool_calls)

    # ---- Public API ----

    def reply(self, transcript: str) -> str:
        """Produce the assistant's spoken reply to one user utterance."""
        self.transcript.append(self._build_user_message(transcript))

        completed_tool_rounds = 0
        while completed_tool_rounds < self.max_tool_rounds:
            try:
                completion = self.model.complete(
                    self.messages,
                    system_prompt=self.system_prompt,
                    tools=self.tools.schemas(),
                    retry_note=RETRY_NOTE,
                )
            except LLMError as exc:
                # Keep the failure in the transcript and spend a round on it so
                # the next attempt sees the dead end instead of repeating it.
                logger.warning("Generation failed this round; recording it. Error: %s", exc)
                self.transcript.append(generation_failure_message(exc))
                completed_tool_rounds += 1
                continue

            response_text, tool_round_failed = self._handle_completion(completion)

            if response_text is not None:
                return response_text

            # A failed tool round is not charged against the budget: the model
            # deserves a chance to correct its arguments.
            if not tool_round_failed:
                completed_tool_rounds += 1

        raise GenerationFailedError(
            "Tool call limit reached before the model produced a final response."
        )

    def post_conversation(self) -> list[dict]:
        """Condense the finished conversation and extract durable facts."""
        condensed = self.memory.condense_conversation(self.messages)
        return self.memory.extract_and_store_semantic_memory(condensed)
