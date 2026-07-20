import os
import logging

from dotenv import load_dotenv
from groq import BadRequestError, Groq


load_dotenv()

client = Groq(api_key=os.getenv("GROQ_KEY"))
logger = logging.getLogger(__name__)
SYSTEM_PROMPT = (
    "You are Minus, a concise helpful voice assistant. Keep responses short and natural for speech. "
    "You operate inside a workspace rooted at /home/jmany42/Projects/minus. "
    "When using file tools, always provide paths relative to this workspace root. "
    "Do not invent absolute paths or paths outside the workspace. "
    "Always make sure the path exists before trying to read the content of a file."
    "IMPORTANT: If a file path is not known, ask the user or use the workspace listing tool first. "
    "Do not repeat the same tool call with identical arguments after you already have the result for it."
)
MAX_RETRIES = 3


def _build_payload(messages, tools=None, model="llama-3.1-8b-instant"):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    }

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    return payload


def _retry_messages(messages, retry_note):
    return [
        {"role": "system", "content": retry_note},
        *messages,
    ]


def _is_failed_generation_error(exc):
    error_text = str(exc)
    error_body = getattr(exc, "body", None)

    if isinstance(error_body, dict):
        error_text = f"{error_text} {error_body}"

    return "failed_generation" in error_text or "tool_use_failed" in error_text


def _validate_completion(completion):
    choices = getattr(completion, "choices", None)
    if not choices:
        raise RuntimeError("Groq returned an empty response.")

    message = getattr(choices[0], "message", None)
    if message is None:
        raise RuntimeError("Groq returned a response without a message.")

    content = getattr(message, "content", None)
    tool_calls = getattr(message, "tool_calls", None) or []
    if content is None and not tool_calls:
        raise RuntimeError("Groq returned an invalid response with no content or tool calls.")

    return completion


def generate_response(messages, tools=None, model="llama-3.1-8b-instant", max_retries=MAX_RETRIES):
    payload = _build_payload(messages, tools=tools, model=model)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(**payload)
            return _validate_completion(completion)
        except BadRequestError as exc:
            last_error = exc
            if not _is_failed_generation_error(exc) or attempt == max_retries:
                break

            logger.warning(
                "Groq failed_generation/tool_use_failed on attempt %s/%s; retrying.",
                attempt,
                max_retries,
            )
            retry_note = (
                "The previous attempt failed to generate a valid tool call. "
                "Return a valid response that matches the tool schema exactly. "
                "Do not repeat malformed arguments or duplicate keys."
            )
            payload = _build_payload(_retry_messages(messages, retry_note), tools=tools, model=model)

    raise RuntimeError(
        "Groq failed to produce a valid response after retries."
    ) from last_error