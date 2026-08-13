import tempfile
import unittest
from pathlib import Path

import minus.memory.condense as condense_module
import minus.memory.service as memory_module
from minus.prompts import SYSTEM_PROMPT
from minus.services.json import parse_json


class ConversationMemoryTests(unittest.TestCase):
    def test_creates_and_updates_conversation_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            conversation_memory = memory_module.MemoryManager(base_dir=base_dir)

            self.assertTrue(conversation_memory.file_path.exists())
            self.assertEqual(
                conversation_memory.file_path.name, f"{conversation_memory.conversation_id}.json"
            )

            first_messages = [{"role": "user", "content": "hello"}]
            conversation_memory.save(first_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], conversation_memory.conversation_id)
            self.assertEqual(
                payload["messages"], [{"role": "system", "content": SYSTEM_PROMPT}, *first_messages]
            )
            self.assertIn("updated_at", payload)

            second_messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            conversation_memory.save(second_messages)

            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["messages"],
                [{"role": "system", "content": SYSTEM_PROMPT}, *second_messages],
            )
            self.assertEqual(
                payload["started_at"], conversation_memory.started_at.isoformat(timespec="seconds")
            )

    def test_start_new_conversation_leaves_the_finished_one_on_disk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            conversation_memory = memory_module.MemoryManager(base_dir=base_dir)

            first_id = conversation_memory.conversation_id
            first_path = conversation_memory.file_path
            conversation_memory.save([{"role": "user", "content": "before the silence"}])

            second_id = conversation_memory.start_new_conversation()

            self.assertNotEqual(second_id, first_id)
            self.assertEqual(conversation_memory.conversation_id, second_id)
            self.assertNotEqual(conversation_memory.file_path, first_path)

            # The finished conversation is still there, still complete.
            payload = parse_json(first_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["messages"],
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "before the silence"},
                ],
            )

            # And writes now land in the new one without touching the old.
            conversation_memory.save([{"role": "user", "content": "after the silence"}])
            payload = parse_json(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], second_id)
            self.assertEqual(
                payload["messages"],
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "after the silence"},
                ],
            )

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


class RecordingStore:
    """The fact-store half of a MemoryService, minus the sqlite."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_fact(self, fact_id: str) -> None:
        self.deleted.append(fact_id)


class SemanticMemoryTests(unittest.TestCase):
    def test_forgetting_a_fact_reaches_the_store(self):
        """Callers ask the memory to forget; only it touches the store."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RecordingStore()
            memory = memory_module.MemoryManager(base_dir=Path(temp_dir), store=store)

            memory.delete_fact("fact-1")

            self.assertEqual(store.deleted, ["fact-1"])


if __name__ == "__main__":
    unittest.main()
