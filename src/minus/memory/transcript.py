"""On-disk persistence for a running conversation.

Split out of MemoryService so that "where the transcript lives and how it is
written" is separable from "what we remember about the user". Writing happens
after every appended message, so an interrupted session still leaves a complete
record; `write_json` in services/json.py makes that atomic via a temp file plus
os.replace, so a crash mid-write cannot truncate the previous good copy.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from minus.services.json import write_json

logger = logging.getLogger(__name__)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TranscriptFile:
    """One conversation's JSON file on disk."""

    def __init__(
        self,
        directory: Path,
        conversation_id: str,
        started_at: datetime,
        system_prompt: str,
    ) -> None:
        self.directory = Path(directory)
        self.conversation_id = conversation_id
        self.system_prompt = system_prompt
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{conversation_id}.json"

        started = started_at.isoformat(timespec="seconds")
        self._state: dict = {
            "conversation_id": conversation_id,
            "started_at": started,
            "updated_at": started,
            "messages": [],
        }
        self._persist()

    def save(self, messages: list[dict]) -> None:
        self._state["updated_at"] = _utc_timestamp()
        self._state["messages"] = self._with_system_message(messages)
        self._persist()

    def _with_system_message(self, messages: list[dict]) -> list[dict]:
        """Record the system prompt alongside the turns it governed.

        Without it a saved transcript cannot be replayed or audited -- the
        assistant's behaviour depended on instructions that were not stored.
        """
        if messages and messages[0].get("role") == "system":
            return list(messages)
        return [{"role": "system", "content": self.system_prompt}, *messages]

    def _persist(self) -> None:
        try:
            write_json(self.path, self._state)
        except OSError:
            # Losing the transcript must not take the conversation down with it.
            logger.exception("Failed to persist conversation transcript to %s", self.path)
