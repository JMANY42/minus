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


class WorkspacePathError(ToolArgumentError):
    """A tool was given a path outside the workspace, or an unusable one."""


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
