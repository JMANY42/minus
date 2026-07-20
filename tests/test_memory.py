import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import memory as memory_module


class ConversationMemoryTests(unittest.TestCase):
    def test_creates_and_updates_conversation_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            conversation_memory = memory_module.ConversationMemory(base_dir=base_dir)

            self.assertTrue(conversation_memory.file_path.exists())
            self.assertRegex(conversation_memory.file_path.name, r"^\d{2}-\d{2}-\d{4}_\d{2}:\d{2}:\d{2}\.json$")

            first_messages = [{"role": "user", "content": "hello"}]
            conversation_memory.save(first_messages)

            payload = json.loads(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["conversation_id"], conversation_memory.conversation_id)
            self.assertEqual(payload["messages"], first_messages)
            self.assertIn("updated_at", payload)

            second_messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
            conversation_memory.save(second_messages)

            payload = json.loads(conversation_memory.file_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["messages"], second_messages)
            self.assertEqual(payload["started_at"], conversation_memory.started_at.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()
