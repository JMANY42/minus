import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.json import parse_json, serialize_json

import memory.conversation_memory as memory_module
import memory.condense_conversation as condense_module


class ConversationMemoryTests(unittest.TestCase):
    def test_creates_and_updates_conversation_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            conversation_memory = memory_module.ConversationMemory(base_dir=base_dir)

            self.assertTrue(conversation_memory.file_path.exists())
            self.assertEqual(conversation_memory.file_path.name, f"{conversation_memory.conversation_id}.json")

            first_messages = [{"role": "user", "content": "hello"}]
            conversation_memory.save(first_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], conversation_memory.conversation_id)
            self.assertEqual(payload["messages"], first_messages)
            self.assertIn("updated_at", payload)

            second_messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            conversation_memory.save(second_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["messages"], second_messages)
            self.assertEqual(payload["started_at"], conversation_memory.started_at.isoformat(timespec="seconds"))

    def test_condenses_and_saves_structured_conversation_json(self):
        completion = types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content=serialize_json(
                            {
                                "conversation": [
                                    {"role": "user", "content": "Please update memory naming."},
                                    {"role": "assistant", "content": "Updated to conversation_id.json."},
                                    {"role": "assistant", "content": "Updated to conversation_id.json."},
                                    {"role": "user", "content": "Thanks."},
                                ],
                            }
                        )
                    )
                )
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "conversations"
            condensed_dir = Path(temp_dir) / "condensed_conversations"
            conversation_memory = memory_module.ConversationMemory(
                base_dir=base_dir,
                condensed_base_dir=condensed_dir,
                conversation_id="conv-1234",
            )

            messages = [
                {"role": "user", "content": "Please update memory naming."},
                {"role": "assistant", "content": "Updated to conversation_id.json."},
            ]

            with patch.object(condense_module, "_groq_call", return_value=completion) as mock_call:
                saved_path = condense_module.condense_conversation(
                    messages,
                    conversation_id=conversation_memory.conversation_id,
                    source_conversation_file=conversation_memory.file_path,
                    condensed_base_dir=condensed_dir,
                )

            self.assertEqual(mock_call.call_count, 1)
            self.assertEqual(saved_path, condensed_dir / "conv-1234.json")
            self.assertTrue(saved_path.exists())

            payload = parse_json(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], "conv-1234")
            self.assertEqual(payload["source_conversation_file"], str(base_dir / "conv-1234.json"))
            self.assertEqual(
                payload["condensed_conversation"],
                {
                    "conversation": [
                        {"role": "user", "content": "Please update memory naming."},
                        {"role": "assistant", "content": "Updated to conversation_id.json."},
                        {"role": "user", "content": "Thanks."},
                    ],
                },
            )


if __name__ == "__main__":
    unittest.main()
