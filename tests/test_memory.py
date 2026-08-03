import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.json import parse_json

import memory.memory_manager as memory_module
import memory.condense_conversation as condense_module
from response import SYSTEM_PROMPT


class ConversationMemoryTests(unittest.TestCase):
    def test_creates_and_updates_conversation_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            conversation_memory = memory_module.MemoryManager(base_dir=base_dir)

            self.assertTrue(conversation_memory.file_path.exists())
            self.assertEqual(conversation_memory.file_path.name, f"{conversation_memory.conversation_id}.json")

            first_messages = [{"role": "user", "content": "hello"}]
            conversation_memory.save(first_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], conversation_memory.conversation_id)
            self.assertEqual(payload["messages"], [{"role": "system", "content": SYSTEM_PROMPT}, *first_messages])
            self.assertIn("updated_at", payload)

            second_messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            conversation_memory.save(second_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["messages"], [{"role": "system", "content": SYSTEM_PROMPT}, *second_messages])
            self.assertEqual(payload["started_at"], conversation_memory.started_at.isoformat(timespec="seconds"))

    def test_condenses_conversation_by_filtering_out_tool_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "conversations"
            condensed_dir = Path(temp_dir) / "condensed_conversations"
            conversation_memory = memory_module.MemoryManager(
                base_dir=base_dir,
                condensed_base_dir=condensed_dir,
                conversation_id="conv-1234",
            )

            messages = [
                {"role": "user", "content": "What time is it?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_current_time", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "2026-08-03T17:00:00"},
                {"role": "assistant", "content": "It is 5pm."},
                {"role": "user", "content": "Thanks!"},
            ]

            result = condense_module.condense_conversation(
                messages,
                conversation_id=conversation_memory.conversation_id,
                source_conversation_file=conversation_memory.file_path,
                condensed_base_dir=condensed_dir,
            )

            expected_condensed_conversation = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "What time is it?"},
                {"role": "assistant", "content": "It is 5pm."},
                {"role": "user", "content": "Thanks!"},
            ]

            self.assertEqual(result["conversation_id"], "conv-1234")
            self.assertEqual(result["source_conversation_file"], str(base_dir / "conv-1234.json"))
            self.assertEqual(result["condensed_conversation"], expected_condensed_conversation)

            saved_path = condensed_dir / "conv-1234.json"
            self.assertTrue(saved_path.exists())
            payload = parse_json(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["condensed_conversation"], expected_condensed_conversation)

    def test_condense_conversation_skips_when_no_user_or_assistant_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            condensed_dir = Path(temp_dir) / "condensed_conversations"
            messages = [
                {"role": "tool", "tool_call_id": "call-1", "content": "some tool result"},
            ]

            result = condense_module.condense_conversation(
                messages,
                conversation_id="conv-empty",
                source_conversation_file=Path(temp_dir) / "conv-empty.json",
                condensed_base_dir=condensed_dir,
            )

            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
