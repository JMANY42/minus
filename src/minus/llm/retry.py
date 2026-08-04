"""Detecting and correcting malformed tool-call generations.

This logic is preserved essentially as it was written -- it encodes real,
hard-won observations about how OpenRouter and its upstreams fail, and none of
it is incidental:

  * Providers report a bad tool-call generation with differently-worded
    errors, hence matching on substrings rather than an error code.
  * OpenRouter reports some upstream failures *in band*: HTTP 200, with an
    `error` on the choice and finish_reason "error". The SDK does not raise
    for that, so a response that looks successful has to be inspected.
  * A response that stops after reasoning, with neither content nor tool
    calls, is a failed generation rather than an answer.

The only change is where the correction is appended and what it says, both of
which the original already got right and which are documented below.
"""

from __future__ import annotations

from typing import Any

from minus.errors import MalformedToolCallError

# Substrings marking an upstream failure caused by bad tool-call generation
# rather than by our request.
RETRYABLE_ERROR_MARKERS = (
    "failed_generation",
    "tool_use_failed",
    "tool call arguments do not satisfy the declared schema",
)


def is_retryable_tool_call_error(exc: BaseException) -> bool:
    """True if `exc` indicates the model failed to produce a valid tool call."""
    if isinstance(exc, MalformedToolCallError):
        return True

    error_text = str(exc)
    error_body = getattr(exc, "body", None)
    if isinstance(error_body, dict):
        error_text = f"{error_text} {error_body}"

    return any(marker in error_text for marker in RETRYABLE_ERROR_MARKERS)


def validate_completion(completion: Any) -> Any:
    """Raise unless `completion` carries a usable assistant turn."""
    choices = getattr(completion, "choices", None)
    if not choices:
        raise MalformedToolCallError("LLM returned an empty response.")

    choice = choices[0]

    # OpenRouter reports upstream failures in-band: HTTP 200 with an `error`
    # on the choice and finish_reason "error". The SDK does not raise for it.
    choice_error = getattr(choice, "error", None)
    if choice_error:
        raise MalformedToolCallError(f"LLM returned an error choice: {choice_error}")

    message = getattr(choice, "message", None)
    if message is None:
        raise MalformedToolCallError("LLM returned a response without a message.")

    # Reasoning is deliberately not accepted as a substitute for content: a
    # response that stops after reasoning is a failed generation, not an answer.
    content = getattr(message, "content", None) or ""
    tool_calls = getattr(message, "tool_calls", None) or []
    if not content.strip() and not tool_calls:
        raise MalformedToolCallError(
            "LLM returned an invalid response with no content or tool calls."
        )

    return completion


def tool_names(tools: list[dict] | None) -> list[str]:
    return [
        tool["function"]["name"]
        for tool in tools or []
        if isinstance(tool, dict) and tool.get("function", {}).get("name")
    ]


def build_retry_message(
    retry_note: str,
    exc: BaseException,
    tools: list[dict] | None,
    attempt: int,
) -> dict:
    """Build the corrective message appended to the END of the conversation.

    Position matters: prepending the note buries it thousands of tokens above
    the generation point, where it does not change the output. Naming the real
    error and the real tool list gives the model something to act on, and the
    escalation gives it a way out when it keeps reaching for a tool that does
    not exist.
    """
    parts = [retry_note, f"The previous attempt failed with: {exc}"]

    names = tool_names(tools)
    if names:
        parts.append(
            "The only tools that exist are: " + ", ".join(names) + ". No other tool is available."
        )

    if attempt >= 2:
        parts.append(
            "You have now failed more than once. If you cannot produce a valid tool call, "
            "answer the user in plain text instead of calling a tool."
        )

    return {"role": "system", "content": "\n\n".join(parts)}
