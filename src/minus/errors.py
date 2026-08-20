"""Exception hierarchy for MINUS.

The code previously signalled every failure with bare `RuntimeError`,
`ValueError` and `TypeError`. That forced callers into either catching
`Exception` (swallowing genuine bugs) or enumerating unrelated builtin types
that happened to be raised nearby -- `except (ValueError, TypeError, OSError)`
in the tool loop is the clearest example, since it cannot distinguish "the
model sent bad arguments" from "the disk is full".

Every error raised deliberately by MINUS derives from `MinusError`, so callers
can catch our failures without catching the interpreter's.
"""

from __future__ import annotations


class MinusError(Exception):
    """Base class for every error MINUS raises deliberately."""


# ---- LLM ----


class LLMError(MinusError):
    """A chat completion could not be obtained."""


class MalformedToolCallError(LLMError):
    """The model answered, but the answer was not a usable tool call.

    Distinct from a transport failure: this is retryable by re-prompting with
    a correction, whereas a network error is retryable by simply trying again.
    """


class GenerationFailedError(LLMError):
    """The model failed to produce a valid response within the retry budget."""


# ---- Tools ----


class ToolError(MinusError):
    """Base class for tool registration and execution failures."""


class UnknownToolError(ToolError):
    """The model asked for a tool that is not registered."""


class ToolArgumentError(ToolError):
    """The model's arguments did not satisfy the tool's schema."""


class ToolExecutionError(ToolError):
    """A registered tool raised while running."""


class ToolDisabledError(ToolError):
    """A registered tool was called while switched off for that tier.

    Distinct from `UnknownToolError`: the tool exists and the name is right, so
    the answer is "not now" rather than "no such thing" -- and a model told the
    difference is less likely to spend a round guessing at spellings.
    """


class WorkspacePathError(ToolArgumentError):
    """A tool was given a path outside the workspace, or an unusable one."""


class ClarificationNeeded(ToolArgumentError):
    """A tool was called without a fact it is not allowed to invent.

    The distinction from `ToolArgumentError` is who can fix it. Bad arguments
    are the model's mistake and the model can correct them by trying again; a
    missing due date or an unnamed calendar is something only the user knows,
    and retrying can only produce a guess. So this one is turned into an
    instruction to *ask* rather than an invitation to retry -- see
    `core/loop.py::tool_failure_message`.

    The message is written as the question to put to the user, because that is
    what the model is about to say out loud.
    """


# ---- External services ----


class ExternalServiceError(MinusError):
    """A service outside this process refused, or could not be reached."""


class GoogleError(ExternalServiceError):
    """A Google API refused a request or was unreachable.

    One class for Tasks and Calendar rather than one each: they are one grant,
    one token and one failure mode from a caller's side, and nothing has ever
    wanted to catch a calendar outage without catching a tasks outage.
    """


class GoogleAuthError(GoogleError):
    """The stored Google credentials could not be turned into an access token.

    Separate from the API error above because the remedy is different and the
    user has to perform it: a revoked or expired refresh token is fixed by
    running `minus google-auth` again, not by retrying the call. The same is
    true of a grant that predates a scope: a token issued when MINUS only
    touched tasks cannot read a calendar, and no retry will change that.
    """


# ---- Memory ----


class MemoryError_(MinusError):
    """Base class for memory subsystem failures.

    Trailing underscore avoids shadowing the builtin `MemoryError`, which
    means something entirely different and must stay reachable.
    """


class FactStoreError(MemoryError_):
    """The semantic fact store could not complete an operation."""


class FactExtractionError(MemoryError_):
    """Durable facts could not be extracted from a transcript."""
