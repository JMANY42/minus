"""Tests for malformed-tool-call detection and corrective retry.

Replaces test_response.py, which could only run after installing fake `openai`
and `dotenv` modules into sys.modules -- a consequence of the client being
constructed at import time. The client now takes its transport as an argument,
so these tests drive the real retry logic against a stub.
"""

from __future__ import annotations

import pytest

from minus.config import Settings
from minus.core.prompts import RETRY_NOTE
from minus.errors import GenerationFailedError, MalformedToolCallError
from minus.llm.client import OpenRouterClient
from minus.llm.retry import is_retryable_tool_call_error, validate_completion

from .fakes import FakeCompletion, FakeMessage, FakeToolCall


class FakeBadRequestError(Exception):
    """Mirrors the shape of the SDK error: a message plus a `body` dict."""

    def __init__(self, message: str, body: dict | None = None) -> None:
        super().__init__(message)
        self.body = body


class FakeTransport:
    """Stands in for the OpenAI SDK client object."""

    def __init__(self, side_effects: list) -> None:
        self.side_effects = list(side_effects)
        self.calls: list[dict] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **payload):
        self.calls.append(payload)
        result = self.side_effects.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def settings() -> Settings:
    return Settings(openrouter_api_key="test-key")


def make_client(settings: Settings, side_effects: list) -> tuple[OpenRouterClient, FakeTransport]:
    transport = FakeTransport(side_effects)
    return OpenRouterClient(settings, client=transport), transport


TOOLS = [{"type": "function", "function": {"name": "list_workspace_files"}}]


class TestRetry:
    def test_malformed_tool_call_is_retried_with_a_correction(self, settings):
        failure = FakeBadRequestError(
            "Error code: 400 - tool_use_failed",
            body={
                "error": {
                    "code": "tool_use_failed",
                    "failed_generation": '<function=list_workspace_files>{"path":"a","path":"b"}',
                }
            },
        )
        success = FakeCompletion(FakeMessage(content="retry worked"))
        client, transport = make_client(settings, [failure, success])

        result = client.complete(
            [{"role": "user", "content": "list files"}],
            system_prompt="SYS",
            tools=TOOLS,
            retry_note=RETRY_NOTE,
            max_retries=2,
        )

        assert result is success
        assert len(transport.calls) == 2

        # The correction is appended at the END of the conversation. Prepending
        # it would bury it above the generation point where it changes nothing.
        correction = transport.calls[1]["messages"][-1]
        assert correction["role"] == "system"
        assert RETRY_NOTE in correction["content"]
        # It names the real error and the tools that actually exist.
        assert "tool_use_failed" in correction["content"]
        assert "list_workspace_files" in correction["content"]

    def test_second_failure_escalates_to_an_escape_hatch(self, settings):
        failure = FakeBadRequestError("tool_use_failed")
        success = FakeCompletion(FakeMessage(content="plain text answer"))
        client, transport = make_client(settings, [failure, failure, success])

        client.complete(
            [{"role": "user", "content": "hi"}],
            tools=TOOLS,
            retry_note=RETRY_NOTE,
            max_retries=3,
        )

        third_correction = transport.calls[2]["messages"][-1]["content"]
        assert "answer the user in plain text" in third_correction

    def test_exhausting_retries_raises_generation_failed(self, settings):
        failure = FakeBadRequestError("tool_use_failed")
        client, transport = make_client(settings, [failure, failure])

        with pytest.raises(GenerationFailedError, match="after retries"):
            client.complete(
                [{"role": "user", "content": "hi"}],
                tools=TOOLS,
                retry_note=RETRY_NOTE,
                max_retries=2,
            )
        assert len(transport.calls) == 2

    def test_non_retryable_errors_are_not_retried(self, settings):
        client, transport = make_client(settings, [FakeBadRequestError("insufficient credits")])

        with pytest.raises(GenerationFailedError):
            client.complete(
                [{"role": "user", "content": "hi"}],
                tools=TOOLS,
                retry_note=RETRY_NOTE,
                max_retries=3,
            )
        assert len(transport.calls) == 1

    def test_system_prompt_is_prepended_and_tools_forwarded(self, settings):
        client, transport = make_client(settings, [FakeCompletion(FakeMessage(content="hello"))])

        client.complete([{"role": "user", "content": "hi"}], system_prompt="SYS", tools=TOOLS)

        payload = transport.calls[0]
        assert payload["messages"][0] == {"role": "system", "content": "SYS"}
        assert payload["tools"] == TOOLS
        assert payload["tool_choice"] == "auto"


class TestValidation:
    def test_in_band_error_choice_is_rejected(self):
        """OpenRouter returns HTTP 200 with an error on the choice; the SDK does not raise."""
        completion = FakeCompletion(FakeMessage(content="ignored"))
        completion.choices[0].error = {"message": "upstream exploded"}

        with pytest.raises(MalformedToolCallError, match="error choice"):
            validate_completion(completion)

    def test_reasoning_only_response_is_rejected(self):
        # A turn that stops after reasoning has neither content nor tool calls;
        # that is a failed generation, not an answer.
        with pytest.raises(MalformedToolCallError, match="no content or tool calls"):
            validate_completion(FakeCompletion(FakeMessage(content="   ", tool_calls=[])))

    def test_tool_call_without_content_is_accepted(self):
        completion = FakeCompletion(
            FakeMessage(content=None, tool_calls=[FakeToolCall("get_current_time", "{}")])
        )
        assert validate_completion(completion) is completion

    def test_empty_choices_is_rejected(self):
        empty = FakeCompletion(FakeMessage(content="x"))
        empty.choices = []
        with pytest.raises(MalformedToolCallError, match="empty response"):
            validate_completion(empty)


class TestErrorClassification:
    @pytest.mark.parametrize(
        "text",
        [
            "failed_generation",
            "tool_use_failed",
            "tool call arguments do not satisfy the declared schema",
        ],
    )
    def test_provider_wordings_are_all_recognised(self, text):
        assert is_retryable_tool_call_error(Exception(f"Error code: 400 - {text}"))

    def test_marker_inside_the_body_dict_is_found(self):
        exc = FakeBadRequestError("Error code: 400", body={"error": {"code": "tool_use_failed"}})
        assert is_retryable_tool_call_error(exc)

    def test_unrelated_errors_are_not_retryable(self):
        assert not is_retryable_tool_call_error(Exception("connection reset"))
