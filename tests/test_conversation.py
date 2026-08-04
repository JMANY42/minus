import json
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *args, **kwargs: None
# pydantic-settings reaches for dotenv_values too. Stubbing a third-party
# module means tracking every symbol its consumers use -- these stubs go
# away entirely once the LLM client is dependency-injected.
fake_dotenv.dotenv_values = lambda *args, **kwargs: {}

fake_openai = types.ModuleType("openai")


class FakeBadRequestError(Exception):
    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


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

import minus.core.agent as conversation_module
from minus.tools.registry import ToolRegistry


@dataclass
class FakeFact:
    id: str
    attribute: str
    value: str
    multi_valued: bool = False
    raw_text: str = ""
    confidence: float = 1.0
    source_session_id: str | None = None
    created_at: float = 0.0
    active: bool = True
    superseded_by: str | None = None
    similarity: float | None = None


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


class FakeMemory:
    def __init__(self, *args, **kwargs):
        self.saved_messages = []
        self.condense_calls = []
        self.search_calls = []

    def save(self, messages):
        self.saved_messages.append(list(messages))

    def condense_conversation(self, messages):
        self.condense_calls.append(list(messages))
        return Path("memory/condensed_conversations/fake.json")

    def search_facts(self, query, top_k=5):
        self.search_calls.append((query, top_k))
        return []

    def extract_and_store_semantic_memory(self, condensed_conversation):
        return []


class ConversationToolLoopTests(unittest.TestCase):
    def test_repeated_identical_tool_calls_are_not_reexecuted(self):
        first_response = FakeCompletion(FakeMessage(content=None, tool_calls=[FakeToolCall("list_workspace_files", '{"path":"."}')]))
        second_response = FakeCompletion(FakeMessage(content="I have the workspace listing.", tool_calls=[]))

        calls = []
        tools = ToolRegistry()

        @tools.tool
        def list_workspace_files(path: str = ".") -> str:
            """List workspace files.

            Args:
                path: Workspace-relative directory.
            """
            calls.append(path)
            return "workspace listing"

        with patch.object(conversation_module, "MemoryManager", FakeMemory):
            conversation = conversation_module.Conversation(tools=tools)

        with patch.object(conversation_module, "generate_response", side_effect=[first_response, second_response]) as mock_generate:
            result = conversation.reply("list the workspace")

        self.assertEqual(result, "I have the workspace listing.")
        self.assertEqual(calls, ["."])
        self.assertEqual(mock_generate.call_count, 2)

    def test_post_conversation_delegates_to_memory(self):
        with patch.object(conversation_module, "MemoryManager", FakeMemory):
            conversation = conversation_module.Conversation(tools=ToolRegistry())

        conversation.messages = [
            {"role": "user", "content": "Please update memory naming."},
            {"role": "assistant", "content": "Updated to conversation_id.json."},
        ]

        saved_path = conversation.post_conversation()

        self.assertEqual(saved_path, [])
        self.assertEqual(conversation.memory.condense_calls, [conversation.messages])


if __name__ == "__main__":
    unittest.main()
