import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


logger = logging.getLogger(__name__)
DEFAULT_MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory" / "conversations"


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conversation_filename(created_at):
    return f"{created_at.strftime('%m-%d-%Y_%H:%M:%S')}.json"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")

    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    os.replace(temp_path, path)


@dataclass
class ConversationMemory:
    base_dir: Path = DEFAULT_MEMORY_DIR
    conversation_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])

    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(timezone.utc)
        self.file_path = self.base_dir / _conversation_filename(self.started_at)
        self._state = {
            "conversation_id": self.conversation_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": self.started_at.isoformat(timespec="seconds"),
            "messages": [],
        }
        self._persist()

    def _persist(self):
        try:
            _write_json(self.file_path, self._state)
        except OSError:
            logger.exception("Failed to persist conversation memory to %s", self.file_path)

    def save(self, messages):
        self._state["updated_at"] = _utc_timestamp()
        self._state["messages"] = list(messages)
        self._persist()

