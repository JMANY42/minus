"""The deep tier: a stronger model, reached by tool call, answered off-thread.

The conversational model is chosen for latency. It has to answer "what time is
it?" fast enough to feel spoken, which rules out the models that are actually
good at thinking. Rather than compromise one for the other, there are two: the
fast model carries the conversation, and when it meets something beyond it, it
calls `escalate` and a slower model answers.

Three things make that work without the assistant going dead for a minute:

  * `escalate` returns immediately. It hands the work to a background thread
    and tells the fast model to say one short line. The conversation stays live
    the whole time the deep tier is running.
  * The answer arrives on a queue rather than as a return value, and is
    delivered by whoever owns the speaker (see cli.py). A tool result cannot
    carry it, because by then the fast model has long since moved on.
  * The answer comes back in two channels. The short one is spoken; the long
    one goes to a DetailSink and is never read aloud.

WHY THE TOOL LOOP IS DUPLICATED HERE
------------------------------------
`Conversation` has one too, and this is deliberately not shared with it. That
loop carries semantics this tier does not want: a failed tool round does not
consume the round budget, and a generation failure is recorded into the
transcript as a turn rather than raised. Both are right for a live conversation
being persisted to disk and wrong for a detached background job whose only
output is a queue message. Unifying them would mean parameterising away most of
what each one does.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Queue
from typing import Any

from minus.core.messages import Message
from minus.core.prompts import DEEP_SYSTEM_PROMPT
from minus.errors import ToolError
from minus.services.json import extract_json_object

logger = logging.getLogger(__name__)

# Spoken when the tier produced something but not in the shape agreed on. The
# work is not lost -- whatever came back is still published as detail.
GARBLED_SPOKEN = (
    "I finished thinking that one through, but my summary came out garbled. "
    "The write-up should still make sense."
)

# Spoken when the tier failed outright. Saying nothing would be worse: the user
# asked a question and is entitled to know it went nowhere.
FAILED_SPOKEN = "I tried to think that one through and it fell over. Sorry about that."

# The two channels arrive as marked sections rather than JSON fields.
#
# JSON was the obvious first choice and it does not survive contact with this
# tier. The detail channel is a long markdown document that is *supposed* to
# contain quotes, apostrophes, newlines, backticks and fenced code -- exactly
# the characters a JSON string has to escape -- and models emit it raw. A real
# response failed on both counts at once: a literal newline after the first
# heading (which strict json.loads rejects as a control character) and an
# unescaped pair of quotes around a phrase further down (which no amount of
# lenient parsing can recover, because the string simply ends there).
#
# Marked sections have no escaping rules to get wrong. Anchored to whole lines
# so that prose mentioning a marker cannot be mistaken for one, and matched
# first-occurrence-wins so a detail section discussing this format -- which the
# tier does, when asked to summarise this very file -- still splits correctly.
_SPOKEN_MARKER = re.compile(r"^[ \t]*<{2,3}\s*SPOKEN\s*>{2,3}[ \t]*$\n?", re.IGNORECASE | re.M)
_DETAIL_MARKER = re.compile(r"^[ \t]*<{2,3}\s*DETAIL\s*>{2,3}[ \t]*$\n?", re.IGNORECASE | re.M)


def split_channels(raw: str) -> tuple[str, str] | None:
    """Split a marked deep response into (spoken, detail).

    Returns None if the detail marker is absent, which is the caller's signal to
    try the JSON fallback before giving up.
    """
    match = _DETAIL_MARKER.search(raw)
    if match is None:
        return None

    spoken = _SPOKEN_MARKER.sub("", raw[: match.start()], count=1)
    return spoken.strip(), raw[match.end() :].strip()


@dataclass(frozen=True)
class DeepResult:
    """One escalated answer, split into its two channels."""

    question: str
    spoken: str
    detail: str


def conversation_context(messages: Iterable[dict]) -> list[dict]:
    """The plain user/assistant turns from a live transcript snapshot.

    Tool traffic is stripped for a correctness reason, not a tidiness one. The
    agent appends the assistant's tool-call message *before* dispatching the
    call, so a snapshot taken from inside `escalate` always ends with an
    assistant turn requesting a tool whose result does not exist yet. Replaying
    that verbatim is a malformed request that providers reject.

    Filtering to spoken turns also matches what condense.py keeps for the same
    underlying reason: the conversation is the part worth carrying forward.
    """
    kept = []
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        content = (message.get("content") or "").strip()
        if not content:
            continue
        kept.append({"role": message["role"], "content": content})
    return kept


class DeepThinker:
    """Runs the escalation tier off the conversation thread.

    One job at a time. A second `escalate` while one is in flight is refused
    rather than queued -- the fast model would otherwise cheerfully stack up
    several minutes of work from a user who was only rephrasing themselves.
    """

    def __init__(
        self,
        model: Any,
        *,
        deep_model: str,
        results: Queue,
        tools: Any | None = None,
        system_prompt: str = DEEP_SYSTEM_PROMPT,
        reasoning_effort: str | None = None,
        max_tool_rounds: int = 4,
        timeout_seconds: float = 120.0,
        executor: Any | None = None,
        recent_limit: int = 2,
    ) -> None:
        self.model = model
        self.deep_model = deep_model
        self.results = results
        self.tools = tools
        self.system_prompt = system_prompt
        self.reasoning_effort = reasoning_effort
        self.max_tool_rounds = max_tool_rounds
        self.timeout_seconds = timeout_seconds
        self.recent_limit = recent_limit

        # Not a `with` block, for the same reason as the TTS chunk executor:
        # __exit__ always waits, and shutdown must be able to abandon a deep
        # call that is blocked on a slow provider rather than hang the exit.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="deep-thinker"
        )

        self._lock = threading.Lock()
        # A plain flag rather than an inspected Future: "is a job in flight?"
        # is the only question asked, and a flag can be cleared by the worker
        # itself without the caller having to hold a reference to it.
        self._busy = False
        self._question: str | None = None
        self._started_at = 0.0
        self._recent: list[DeepResult] = []
        self._snapshot: Callable[[], list[dict]] = list

    # ---- Introspection ----

    def status(self) -> dict:
        """What the deep tier is doing, for anything that needs to wait on it.

        Reports `elapsed_seconds` rather than the start time on purpose:
        `_started_at` is a `time.monotonic()` reading, which is meaningful only
        inside this process and would be nonsense to a dashboard reading it
        over a socket.
        """
        with self._lock:
            return {
                "in_flight": self._busy,
                "question": self._question,
                "elapsed_seconds": (time.monotonic() - self._started_at if self._busy else None),
            }

    # ---- Wiring ----

    def bind_snapshot(self, provider: Callable[[], list[dict]]) -> None:
        """Supply the conversation this tier should read for context.

        Late-bound because the conversation needs the tool registry that holds
        `escalate`, and `escalate` needs the conversation -- a cycle the
        composition root breaks by constructing both and then connecting them.
        """
        self._snapshot = provider

    # ---- The tool ----

    def escalate(self, question: str) -> dict:
        """Hand a question to a slower, more capable model that can think properly.

        Use this for anything needing real analysis rather than a quick answer:
        designing or restructuring something, planning work in several steps,
        weighing tradeoffs, diagnosing a cause you cannot see, or reading
        several files and holding them together. Also use it whenever the user
        asks you to think hard or be thorough.

        Returns immediately, and ends your turn. The answer is delivered to the
        user on its own a little later, so all you do after calling this is say
        one short line letting them know you are on it, and stop. Do not attempt
        the analysis yourself, and do not call any tool after this one -- no
        listing, no reading, no second escalate. The deep model has its own
        tools and can see this conversation.

        Args:
            question: The full question to think about, written to stand on its
                own without the surrounding conversation.
        """
        # Taken here, on the conversation thread, so the worker never reads a
        # transcript that another thread is appending to.
        snapshot = conversation_context(self._snapshot())

        with self._lock:
            waited = time.monotonic() - self._started_at
            if self._busy and waited < self.timeout_seconds:
                logger.info("Escalation refused, already thinking: %s", self._question)
                return {
                    "status": "already_thinking",
                    "current_question": self._question,
                    "note": (
                        "You are already thinking about something else. Tell the user "
                        "that in one plain-text line and stop -- do not call escalate "
                        "again until it has landed, and do not reach for other tools "
                        "to work on it in the meantime."
                    ),
                }

            if self._busy:
                # Cannot be cancelled -- a thread blocked in native code stays
                # blocked. The new job queues behind it rather than replacing it.
                logger.warning(
                    "Previous escalation still running after %.0fs; queueing behind it.", waited
                )

            self._busy = True
            self._question = question
            self._started_at = time.monotonic()

        # Submitted outside the lock. The worker takes the same lock to record
        # its result, so holding it across submit() deadlocks the moment the
        # executor runs work on the calling thread instead of a pool thread.
        try:
            self._executor.submit(self._think, question, snapshot)
        except Exception:
            with self._lock:
                self._busy = False
            raise

        logger.info("Escalated to %s: %s", self.deep_model, question)
        return {
            "status": "thinking",
            "note": (
                "Started. Your turn is over except for one short, natural line telling "
                "the user you are on it. Reply with that line as plain text now -- no "
                "further tool calls, no listing, no reading, no second escalate -- and "
                "do not answer the question yourself. The answer arrives on its own."
            ),
        }

    # ---- Worker ----

    def _think(self, question: str, snapshot: list[dict]) -> None:
        """Entry point on the worker thread. Must never raise."""
        started = time.monotonic()
        try:
            result = self._deliverable(question, snapshot)
        finally:
            with self._lock:
                self._busy = False

        logger.info("Deep tier finished in %.2fs", time.monotonic() - started)
        self._remember(result)
        self.results.put(result)

    def _deliverable(self, question: str, snapshot: list[dict]) -> DeepResult:
        """Always produce something to say, however badly the call went."""
        try:
            return self._answer(question, snapshot)
        except Exception as exc:
            logger.exception("Deep tier failed for question: %s", question)
            return DeepResult(
                question=question,
                spoken=FAILED_SPOKEN,
                detail=f"The deep tier raised {type(exc).__name__}: {exc}",
            )

    def _answer(self, question: str, snapshot: list[dict]) -> DeepResult:
        completion = self._run_tool_loop(self._build_messages(question, snapshot))
        raw = (completion.choices[0].message.content or "").strip()
        return self._parse(question, raw)

    def _build_messages(self, question: str, snapshot: list[dict]) -> list[dict]:
        messages: list[dict] = []

        recent = self._recent_snapshot()
        if recent:
            prior = "\n\n".join(
                f"Earlier question: {result.question}\nWhat you concluded:\n{result.detail}"
                for result in recent
            )
            # Full detail never enters the conversation transcript, so without
            # this the tier would forget its own last answer while the fast
            # model still remembers the summary of it.
            messages.append(
                Message.system(
                    f"Your own earlier conclusions in this session:\n\n{prior}"
                ).to_wire()
            )

        messages.extend(snapshot)
        messages.append(Message.user(question).to_wire())
        return messages

    def _run_tool_loop(self, messages: list[dict]) -> Any:
        schemas = self.tools.schemas() if self.tools else None

        for _ in range(max(self.max_tool_rounds, 1)):
            completion = self._complete(messages, tools=schemas)
            message = Message.from_completion(completion.choices[0].message)

            if not message.tool_calls:
                return completion

            messages.append(message.to_wire())
            for call in message.tool_calls:
                messages.append(Message.tool_result(call.id, self._dispatch(call)).to_wire())

        # Out of rounds and still reaching for tools. Ask once more with none
        # offered so the tier answers from what it has, rather than the caller
        # getting nothing at all after paying for several rounds.
        logger.warning(
            "Deep tier hit its %s-round tool budget; forcing an answer.", self.max_tool_rounds
        )
        return self._complete(messages, tools=None)

    def _complete(self, messages: list[dict], *, tools: list[dict] | None) -> Any:
        return self.model.complete(
            messages,
            model=self.deep_model,
            system_prompt=self.system_prompt,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
        )

    def _dispatch(self, call: Any) -> str:
        if self.tools is None:
            # Unreachable in practice -- no schemas are offered when there is no
            # registry -- but a model that invents a call still gets an answer
            # it can act on rather than an AttributeError killing the job.
            return f"Tool {call.name} is not available."

        try:
            return self.tools.dispatch(call.name, call.arguments)
        except (ToolError, OSError) as exc:
            # Same containment as the conversation loop: a bad tool call is
            # information for the model, not a reason to abandon the answer.
            logger.warning("Deep tool %s failed: %s", call.name, exc)
            return f"Tool {call.name} failed: {exc}"

    # ---- Result handling ----

    @staticmethod
    def _parse_json(raw: str) -> tuple[str, str] | None:
        """Read the superseded JSON shape, for a tier that emits it anyway.

        Kept because a model ignoring the marker instructions and reaching for
        JSON is the single most likely way this contract gets missed, and a
        response that is otherwise perfect should not be thrown away over its
        envelope.
        """
        try:
            payload = extract_json_object(raw)
            return str(payload["spoken"]).strip(), str(payload.get("detail") or "").strip()
        except (ValueError, KeyError, TypeError):
            return None

    def _parse(self, question: str, raw: str) -> DeepResult:
        """Split a raw response into its spoken and written channels.

        Degrades rather than raises, matching how fact extraction treats a
        malformed response: an answer that arrived in the wrong shape is still
        worth showing, and the user is owed something spoken either way.
        """
        channels = split_channels(raw) or self._parse_json(raw)
        if channels is None:
            logger.warning("Deep tier response was not the agreed shape; publishing it raw.")
            return DeepResult(question=question, spoken=GARBLED_SPOKEN, detail=raw)

        spoken, detail = channels
        if not spoken:
            return DeepResult(question=question, spoken=GARBLED_SPOKEN, detail=detail or raw)

        return DeepResult(question=question, spoken=spoken, detail=detail or spoken)

    def _remember(self, result: DeepResult) -> None:
        with self._lock:
            self._recent.append(result)
            # Spelled with max() rather than a negative slice so that a
            # recent_limit of 0 drops everything instead of keeping everything.
            del self._recent[: max(len(self._recent) - self.recent_limit, 0)]

    def _recent_snapshot(self) -> list[DeepResult]:
        with self._lock:
            return list(self._recent)

    # ---- Teardown ----

    def shutdown(self) -> None:
        """Stop accepting work and abandon anything still running."""
        self._executor.shutdown(wait=False, cancel_futures=True)
