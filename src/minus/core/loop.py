"""The tool loop both model tiers run on.

Call the model; if it answered, stop; if it asked for tools, run them, append
what they returned, and go round again. That is the whole shape, and it was
written twice -- once in `Conversation.reply` for the live conversation, once in
`DeepThinker._run_tool_loop` for the escalation tier. The second copy carried a
comment explaining that the two could not be shared because their semantics
differed. Most of what it listed turned out to be incidental: one kept its
messages in a `Transcript` and the other in a list of dicts, one returned text
and the other a completion object, one passed `retry_note` and the other
`model` and `reasoning_effort`. None of that is policy; it is plumbing, and it
is settled here by taking a `Transcript` (which persists only if it was built
with a memory), returning a `Message`, and forwarding every completion argument
either tier needs.

Two differences were real, and both are resolved toward one behaviour rather
than a flag:

  * A failed tool round does not spend the round budget. The model sent bad
    arguments; making that cost it a turn punishes the wrong thing. What stops
    a tool that fails forever from looping forever is `max_attempts`, an
    absolute ceiling on iterations that the productive-round budget sits
    inside. The conversation loop had the first half of this rule and not the
    second, so a permanently broken tool could spin without bound.

  * Running out of rounds is not an error. Asking once more with no tools
    offered gets an answer out of whatever the model has already gathered,
    which is better than raising in both tiers -- the deep tier would turn the
    exception into an apology, and the conversation loop would die of it,
    since `conversation_loop` guards `reply()` against nothing but
    `KeyboardInterrupt`.

A generation failure is recorded into the transcript as a system turn rather
than raised, so the next attempt sees the dead end instead of walking back into
it. Retries inside the client are invisible from here, so a round that burned
all of them would otherwise leave no trace at all.

The loop returns the final `Message` rather than a completion or a bare string.
`Conversation` wants its text, the deep tier wants its text to split into two
channels, and both want it already appended to the transcript -- a `Message` is
the smallest thing that serves all three without a caller reaching into the
SDK's response shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from minus.core.messages import Message, Transcript
from minus.errors import GenerationFailedError, LLMError, ToolError
from minus.prompts import RETRY_NOTE
from minus.services.json import pretty_json

logger = logging.getLogger(__name__)


def generation_failure_message(exc: BaseException) -> Message:
    """Record a failed generation in the transcript."""
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


@dataclass
class ToolLoop:
    """Drive a model to a final answer, running whatever tools it asks for.

    Owns the loop and nothing else. Memory, persistence, threading and however
    the answer is eventually delivered belong to the caller; this appends turns
    to the transcript it was handed and returns the one the model finished on.
    """

    model: Any
    transcript: Transcript
    tools: Any | None = None
    system_prompt: str | None = None
    max_tool_rounds: int = 5
    # Passed straight through to `ChatModel.complete`. `model_name` is spelled
    # apart from `model` because that name is taken here by the client itself.
    model_name: str | None = None
    reasoning_effort: str | None = None
    retry_note: str | None = RETRY_NOTE
    # The ceiling that makes free failed rounds safe. Derived from the budget
    # rather than configured, since the two only ever move together.
    max_attempts: int = field(default=0)

    def __post_init__(self) -> None:
        if not self.max_attempts:
            self.max_attempts = max(self.max_tool_rounds, 1) * 2

    # ---- The model ----

    def _complete(self, *, tools: list[dict] | None) -> Any:
        return self.model.complete(
            self.transcript.to_wire(),
            model=self.model_name,
            system_prompt=self.system_prompt,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
            retry_note=self.retry_note,
        )

    def _schemas(self) -> list[dict] | None:
        return self.tools.schemas() if self.tools is not None else None

    # ---- Tools ----

    def _run_tool_call(self, tool_call) -> bool:
        """Execute one tool call. Returns True if it failed."""
        if self.tools is None:
            # Unreachable while no schemas are offered, but a model that
            # invents a call still gets an answer it can act on rather than an
            # AttributeError taking down the whole turn.
            self.transcript.append(
                Message.tool_result(tool_call.id, f"Tool {tool_call.name} is not available.")
            )
            return True

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
        """Execute every tool call in a round. Returns True if any failed.

        Every call runs, including the ones after a failure. This was an `any`
        over a generator, which short-circuits -- so a round whose first call
        failed left the rest unanswered, and a provider rejects an assistant
        `tool_calls` message whose calls are not all responded to. Stopping
        early does not save a bad round; it breaks the next request.
        """
        failed = False
        for call in tool_calls:
            failed |= self._run_tool_call(call)
        return failed

    # ---- The loop ----

    def _final(self, completion) -> Message:
        """Append the answer the model finished on, and hand it back."""
        raw_message = completion.choices[0].message
        # validate_completion guarantees non-blank content when there are no
        # tool calls, so .strip() is safe here.
        text = (raw_message.content or "").strip()
        return self.transcript.append(Message.from_completion(raw_message, content=text))

    def _forced_answer(self) -> Message:
        """Out of rounds and still reaching for tools; ask with none offered.

        Whatever the model gathered on the way is already in the transcript, so
        this usually produces a real answer rather than a stub. A failure here
        is a genuine dead end and raises.
        """
        logger.warning("Round budget of %s spent; forcing an answer.", self.max_tool_rounds)
        try:
            message = self._final(self._complete(tools=None))
        except LLMError as exc:
            raise GenerationFailedError(
                f"Tool call limit reached and the final answer could not be generated: {exc}"
            ) from exc

        if not message.content:
            raise GenerationFailedError(
                "Tool call limit reached and the model produced no final response."
            )
        return message

    def run(self) -> Message:
        """Run to a final assistant turn, appending everything on the way."""
        schemas = self._schemas()
        completed_tool_rounds = 0

        for _ in range(self.max_attempts):
            if completed_tool_rounds >= self.max_tool_rounds:
                break

            try:
                completion = self._complete(tools=schemas)
            except LLMError as exc:
                # Spend an attempt but not a round: the dead end is now in the
                # transcript, and the next try can see it and go elsewhere.
                logger.warning("Generation failed this attempt; recording it. Error: %s", exc)
                self.transcript.append(generation_failure_message(exc))
                continue

            message = Message.from_completion(completion.choices[0].message)
            if not message.tool_calls:
                return self._final(completion)

            self.transcript.append(message)
            # A failed tool round is not charged against the budget: the model
            # deserves a chance to correct its arguments. `max_attempts` is
            # what keeps that from running forever.
            if not self._run_tool_calls(message.tool_calls):
                completed_tool_rounds += 1

        return self._forced_answer()
