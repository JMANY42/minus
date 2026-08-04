import logging
import os
import time

from dotenv import load_dotenv
from openai import BadRequestError, OpenAI


load_dotenv()

# gpt-oss-120b:nitro is very expensive but its super fast and could chain together multiple tool calls without failing.
# Next change it to switch to a multi model architecture
DEFAULT_MODEL = "openai/gpt-oss-120b:nitro"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    default_headers={"X-Title": "Minus"},
)
logger = logging.getLogger(__name__)


def llm_call(**payload):
    model = payload.get("model", DEFAULT_MODEL)
    logger.debug("Creating LLM completion with payload keys: %s", sorted(payload.keys()))

    start = time.monotonic()
    try:
        completion = client.chat.completions.create(**payload)
    except Exception:
        logger.warning("LLM call to %s failed after %.2fs", model, time.monotonic() - start)
        raise

    logger.info("LLM call to %s completed in %.2fs\nResponse: %s", model, time.monotonic() - start, completion)
    return completion


class MalformedToolCallError(RuntimeError):
    """The model answered, but the answer was not a usable tool call."""


# Substrings that mark an upstream failure caused by bad tool-call generation
# rather than by our request. Providers word these differently.
_RETRYABLE_ERROR_MARKERS = (
    "failed_generation",
    "tool_use_failed",
    "tool call arguments do not satisfy the declared schema",
)


def is_retryable_tool_call_error(exc):
    """True if exc indicates the model failed to produce a valid tool call."""
    if isinstance(exc, MalformedToolCallError):
        return True

    error_text = str(exc)
    error_body = getattr(exc, "body", None)

    if isinstance(error_body, dict):
        error_text = f"{error_text} {error_body}"

    return any(marker in error_text for marker in _RETRYABLE_ERROR_MARKERS)


def validate_completion(completion):
    choices = getattr(completion, "choices", None)
    if not choices:
        raise RuntimeError("LLM returned an empty response.")

    choice = choices[0]

    # OpenRouter reports upstream failures in-band: HTTP 200 with an `error`
    # on the choice and finish_reason "error". The SDK does not raise for it.
    choice_error = getattr(choice, "error", None)
    if choice_error:
        raise MalformedToolCallError(f"LLM returned an error choice: {choice_error}")

    message = getattr(choice, "message", None)
    if message is None:
        raise RuntimeError("LLM returned a response without a message.")

    # Reasoning is deliberately not accepted as a substitute for content: a
    # response that stops after reasoning is a failed generation, not an answer.
    content = getattr(message, "content", None) or ""
    tool_calls = getattr(message, "tool_calls", None) or []
    if not content.strip() and not tool_calls:
        raise MalformedToolCallError("LLM returned an invalid response with no content or tool calls.")

    return completion


def _tool_names(tools):
    return [
        tool["function"]["name"]
        for tool in tools or []
        if isinstance(tool, dict) and tool.get("function", {}).get("name")
    ]


def _build_retry_message(retry_note, exc, tools, attempt):
    """Build the corrective message appended to the END of the conversation.

    Position matters: prepending the note buries it thousands of tokens above
    the generation point, where it does not change the output. Naming the real
    error and the real tool list gives the model something to act on, and the
    escalation gives it a way out when it keeps reaching for a tool that does
    not exist.
    """
    parts = [retry_note, f"The previous attempt failed with: {exc}"]

    names = _tool_names(tools)
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


def generate_completion(
    messages,
    *,
    model=DEFAULT_MODEL,
    system_prompt=None,
    tools=None,
    tool_choice="auto",
    max_retries=1,
    retry_note=None,
):
    """Call the LLM and return a validated completion.

    Shared by every call site so retry/error handling for malformed tool
    calls lives in one place. If `retry_note` is given, a BadRequestError
    diagnosed as a malformed tool call is retried up to `max_retries` times
    with a corrective system message injected.
    """

    def build_payload(msgs):
        full_messages = [{"role": "system", "content": system_prompt}, *msgs] if system_prompt else msgs
        payload = {"model": model, "messages": full_messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        return payload

    payload = build_payload(messages)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            completion = llm_call(**payload)
            return validate_completion(completion)
        except Exception as exc:
            last_error = exc
            if not retry_note or not is_retryable_tool_call_error(exc) or attempt == max_retries:
                break

            logger.warning(
                "Retryable tool-call error on attempt %s/%s; retrying. Error: %s",
                attempt,
                max_retries,
                last_error,
            )
            payload = build_payload([*messages, _build_retry_message(retry_note, exc, tools, attempt)])

    raise RuntimeError(
        f"LLM failed to produce a valid response after retries. Last error: {last_error}"
    ) from last_error
