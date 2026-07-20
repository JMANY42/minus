import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *args, **kwargs: None

fake_groq = types.ModuleType("groq")


class FakeBadRequestError(Exception):
    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


class _FakeGroqClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

fake_groq.Groq = _FakeGroqClient
fake_groq.BadRequestError = FakeBadRequestError

sys.modules.setdefault("dotenv", fake_dotenv)
sys.modules.setdefault("groq", fake_groq)

import conversation as conversation_module


class FakeToolCallFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name, arguments, tool_id="call-1"):
        self.id = tool_id
        self.type = "function"
        self.function = FakeToolCallFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeCompletion:
    def __init__(self, message):
        self.choices = [types.SimpleNamespace(message=message)]


class ConversationToolLoopTests(unittest.TestCase):
    def test_repeated_identical_tool_calls_are_not_reexecuted(self):
        conversation = conversation_module.Conversation(tools_path="/tmp/unused-tools.json")

        first_response = FakeCompletion(FakeMessage(content=None, tool_calls=[FakeToolCall("list_workspace_files", '{"path":"."}')]))
        second_response = FakeCompletion(FakeMessage(content="I have the workspace listing.", tool_calls=[]))

        with patch.object(conversation_module, "generate_response", side_effect=[first_response, second_response]) as mock_generate:
            with patch.object(conversation.tool_handler, "execute", return_value="workspace listing") as mock_execute:
                result = conversation.reply("list the workspace")

        self.assertEqual(result, "I have the workspace listing.")
        self.assertEqual(mock_execute.call_count, 1)
        self.assertEqual(mock_generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
