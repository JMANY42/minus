import json
import sys
import types
import unittest
from dataclasses import dataclass
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

sys.modules["dotenv"] = fake_dotenv
sys.modules["groq"] = fake_groq
sys.modules.pop("response", None)
sys.modules.pop("services.groq", None)
sys.modules.pop("services", None)

import conversation as conversation_module


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
    def test_fact_messages_are_json_serializable(self):
        fact = FakeFact(id="fact-1", attribute="timezone", value="PST", raw_text="The user's timezone is PST.")

        message = conversation_module.create_fact_message([fact])

        serialized = json.dumps(message)

        self.assertIn("timezone", serialized)
        self.assertIn("PST", serialized)

    def test_repeated_identical_tool_calls_are_not_reexecuted(self):
        first_response = FakeCompletion(FakeMessage(content=None, tool_calls=[FakeToolCall("list_workspace_files", '{"path":"."}')]))
        second_response = FakeCompletion(FakeMessage(content="I have the workspace listing.", tool_calls=[]))

        with patch.object(conversation_module, "ConversationMemory", FakeMemory):
            conversation = conversation_module.Conversation(tools_path="/tmp/unused-tools.json")

        with patch.object(conversation_module, "generate_response", side_effect=[first_response, second_response]) as mock_generate:
            with patch.object(conversation.tool_handler, "execute", return_value="workspace listing") as mock_execute:
                result = conversation.reply("list the workspace")

        self.assertEqual(result, "I have the workspace listing.")
        self.assertEqual(mock_execute.call_count, 1)
        self.assertEqual(mock_generate.call_count, 2)

    def test_post_conversation_delegates_to_memory(self):
        with patch.object(conversation_module, "ConversationMemory", FakeMemory):
            conversation = conversation_module.Conversation(tools_path="/tmp/unused-tools.json")

        conversation.messages = [
            {"role": "user", "content": "Please update memory naming."},
            {"role": "assistant", "content": "Updated to conversation_id.json."},
        ]

        saved_path = conversation.post_conversation()

        self.assertEqual(saved_path, [])
        self.assertEqual(conversation.memory.condense_calls, [conversation.messages])


if __name__ == "__main__":
    unittest.main()
