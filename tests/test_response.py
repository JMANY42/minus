import sys
import types
import unittest

fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *args, **kwargs: None
# pydantic-settings reaches for dotenv_values too. Stubbing a third-party
# module means tracking every symbol its consumers use -- these stubs go
# away entirely once the LLM client is dependency-injected.
fake_dotenv.dotenv_values = lambda *args, **kwargs: {}


class FakeBadRequestError(Exception):
    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


fake_openai = types.ModuleType("openai")


class _FakeOpenAIClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


fake_openai.OpenAI = _FakeOpenAIClient
fake_openai.BadRequestError = FakeBadRequestError

sys.modules["dotenv"] = fake_dotenv
sys.modules["openai"] = fake_openai
sys.modules.pop("minus.core.prompts", None)
sys.modules.pop("minus.llm.client", None)
sys.modules.pop("minus.llm", None)

import minus.llm.client as llm_module

# generate_response now lives with the LLM client; prompts.py is pure text.
response_module = llm_module


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeCompletion:
    def __init__(self, message):
        self.choices = [types.SimpleNamespace(message=message)]


class FakeCompletions:
    def __init__(self, side_effects):
        self.side_effects = list(side_effects)
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)
        result = self.side_effects.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ResponseRetryTests(unittest.TestCase):
    def test_retries_failed_generation_then_returns_valid_completion(self):
        first_error = FakeBadRequestError(
            "Error code: 400 - tool_use_failed",
            body={
                "error": {
                    "code": "tool_use_failed",
                    "failed_generation": "<function=list_workspace_files>{\"path\":\"src/main.py\",\"path\":\"data.json\"}",
                }
            },
        )
        final_completion = FakeCompletion(FakeMessage(content="retry worked", tool_calls=None))
        completions = FakeCompletions([first_error, final_completion])
        llm_module.llm_call = completions.create

        result = response_module.generate_response(
            messages=[{"role": "user", "content": "list files"}],
            tools=[{"type": "function", "function": {"name": "list_workspace_files"}}],
            max_retries=2,
        )

        self.assertIs(result, final_completion)
        self.assertEqual(len(completions.calls), 2)
        self.assertIn("tools", completions.calls[0])
        self.assertEqual(
            completions.calls[0]["messages"][0]["role"],
            "system",
        )
        self.assertEqual(
            completions.calls[1]["messages"][1]["content"],
            "The previous attempt failed to generate a valid tool call. Return a valid response that matches the tool schema exactly. Do not repeat malformed arguments or duplicate keys.",
        )


if __name__ == "__main__":
    unittest.main()
