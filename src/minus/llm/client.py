"""The OpenRouter-backed chat model.

Previously this module built an `OpenAI` client at import time from an
environment variable. That single line shaped the whole test suite: importing
anything that transitively reached the LLM layer would construct a real client,
so every test had to install a fake `openai` module into `sys.modules` before
its first import, and then a fake `dotenv` alongside it. Tests were testing
their own stubs as much as the code.

The client is now constructed explicitly, in the composition root, from
Settings. Nothing happens at import time, and tests inject a fake model
instead of patching the module system.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openai import OpenAI

from minus.config import Settings
from minus.errors import GenerationFailedError
from minus.llm.retry import build_retry_message, is_retryable_tool_call_error, validate_completion

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """A ChatModel backed by OpenRouter's OpenAI-compatible API."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            default_headers={"X-Title": settings.app_title},
        )

    # ---- Transport ----

    def _call(self, **payload: Any) -> Any:
        model = payload.get("model", self._settings.chat_model)
        logger.debug("Creating LLM completion with payload keys: %s", sorted(payload))

        start = time.monotonic()
        try:
            completion = self._client.chat.completions.create(**payload)
        except Exception:
            logger.warning("LLM call to %s failed after %.2fs", model, time.monotonic() - start)
            raise

        logger.info(
            "LLM call to %s completed in %.2fs\nResponse: %s",
            model,
            time.monotonic() - start,
            completion,
        )
        return completion

    # ---- ChatModel ----

    def complete(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        max_retries: int | None = None,
        retry_note: str | None = None,
    ) -> Any:
        """Call the model and return a validated completion.

        Every call site shares this method so retry and error handling for
        malformed tool calls lives in one place. When `retry_note` is given, an
        error diagnosed as a bad tool-call generation is retried up to
        `max_retries` times with a corrective system message appended.
        """
        model = model or self._settings.chat_model
        retries = self._settings.max_retries if max_retries is None else max_retries

        def build_payload(msgs: list[dict]) -> dict:
            full = [{"role": "system", "content": system_prompt}, *msgs] if system_prompt else msgs
            payload: dict[str, Any] = {"model": model, "messages": full}
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = tool_choice
            return payload

        payload = build_payload(messages)
        last_error: BaseException | None = None

        for attempt in range(1, max(retries, 1) + 1):
            try:
                return validate_completion(self._call(**payload))
            except Exception as exc:
                last_error = exc
                if not retry_note or not is_retryable_tool_call_error(exc) or attempt == retries:
                    break

                logger.warning(
                    "Retryable tool-call error on attempt %s/%s; retrying. Error: %s",
                    attempt,
                    retries,
                    exc,
                )
                payload = build_payload(
                    [*messages, build_retry_message(retry_note, exc, tools, attempt)]
                )

        raise GenerationFailedError(
            f"LLM failed to produce a valid response after retries. Last error: {last_error}"
        ) from last_error
