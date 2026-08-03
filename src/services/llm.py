import logging
import os
import time

from dotenv import load_dotenv
from openai import BadRequestError, OpenAI


load_dotenv()

DEFAULT_MODEL = "openai/gpt-oss-20b:free"

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


def is_retryable_tool_call_error(exc):
    """True if exc indicates the model failed to produce a valid tool call."""
    error_text = str(exc)
    error_body = getattr(exc, "body", None)

    if isinstance(error_body, dict):
        error_text = f"{error_text} {error_body}"

    return "failed_generation" in error_text or "tool_use_failed" in error_text


def validate_completion(completion):
    choices = getattr(completion, "choices", None)
    if not choices:
        raise RuntimeError("LLM returned an empty response.")

    message = getattr(choices[0], "message", None)
    if message is None:
        raise RuntimeError("LLM returned a response without a message.")

    content = getattr(message, "content", None)
    reasoning = getattr(message, "reasoning", None)
    tool_calls = getattr(message, "tool_calls", None) or []
    if content is None and reasoning is None and not tool_calls:
        raise RuntimeError("LLM returned an invalid response with no content or tool calls.")

    return completion


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
        except BadRequestError as exc:
            last_error = exc
            if not retry_note or not is_retryable_tool_call_error(exc) or attempt == max_retries:
                break

            logger.warning(
                "Retryable tool-call error on attempt %s/%s; retrying.",
                attempt,
                max_retries,
            )
            retry_messages = [{"role": "system", "content": retry_note}, *messages]
            payload = build_payload(retry_messages)

    raise RuntimeError("LLM failed to produce a valid response after retries.") from last_error
