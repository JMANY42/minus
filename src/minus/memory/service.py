import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from minus.memory.condense import condense_conversation
from minus.memory.facts.store import MemoryStore
from minus.paths import condensed_conversations_dir, conversations_dir, semantic_memory_db
from minus.services.json import serialize_json, write_json

logger = logging.getLogger(__name__)
DEFAULT_MEMORY_DIR = conversations_dir()
DEFAULT_CONDENSED_MEMORY_DIR = condensed_conversations_dir()
DEFAULT_SEMANTIC_MEMORY_DB = semantic_memory_db()

RELEVENCE_THRESHOLD = 0.356 # magic number from calibrate_threshold.py

def _utc_timestamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class MemoryManager:
    base_dir: Path = DEFAULT_MEMORY_DIR
    condensed_base_dir: Path = DEFAULT_CONDENSED_MEMORY_DIR
    conversation_id: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])

    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.condensed_base_dir = Path(self.condensed_base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(UTC)
        self.file_path = self.base_dir / f"{self.conversation_id}.json"
        self._state = {
            "conversation_id": self.conversation_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": self.started_at.isoformat(timespec="seconds"),
            "messages": [],
        }
        self._memory_store = MemoryStore(DEFAULT_SEMANTIC_MEMORY_DB)
        self._persist()

    def _persist(self):
        try:
            write_json(self.file_path, self._state)
        except OSError:
            logger.exception("Failed to persist conversation memory to %s", self.file_path)

    def save(self, messages):
        self._state["updated_at"] = _utc_timestamp()
        self._state["messages"] = self._with_system_message(messages)
        self._persist()

    def _with_system_message(self, messages):
        # Lazy import avoids pulling in the LLM client module just to read the system prompt.
        from minus.core.prompts import SYSTEM_PROMPT

        if messages and messages[0].get("role") == "system":
            return list(messages)

        return [{"role": "system", "content": SYSTEM_PROMPT}, *messages]

    def condense_conversation(self, messages):
        return condense_conversation(
            messages,
            conversation_id=self.conversation_id,
            source_conversation_file=self.file_path,
            condensed_base_dir=self.condensed_base_dir,
        )

    def extract_facts(self, condensed_conversation):
        from minus.memory.extraction import extract_facts_from_conversation

        if condensed_conversation is None:
            logger.info("Skipping semantic memory extraction because condensed conversation is None.")
            return []

        logger.debug("Condensed conversation: %s", condensed_conversation)
        condensed_messages = condensed_conversation.get("condensed_conversation", [])
        if not condensed_messages:
            logger.info("Skipping semantic memory extraction because condensed conversation is empty.")
            return []
        logger.debug("known attributes: %s", self._memory_store.get_known_attributes())
        return extract_facts_from_conversation(condensed_messages, known_attributes=serialize_json(self._memory_store.get_known_attributes()))

    def store_facts(self, facts):
        for fact in facts:
            logger.info("Adding fact to semantic memory: %s", fact)
            self._memory_store.add_fact(
                attribute=fact["attribute"],
                value=fact["value"],
                multi_valued=fact["multi_valued"],
                raw_text=fact["raw_text"],
                confidence=1.0,
                source_session_id=self.conversation_id,
            )


    def extract_and_store_semantic_memory(self, condensed_conversation):
        facts = self.extract_facts(condensed_conversation)
        self.store_facts(facts)
        return facts


    def search_facts(self, query, top_k=5, threshold=RELEVENCE_THRESHOLD):
        top_k_facts = self._memory_store.search_facts(query, top_k=top_k)

        relevent_facts = []
        for fact in top_k_facts:
            if fact.similarity is not None and (threshold is None or fact.similarity >= threshold):
                relevent_facts.append(fact)

        return relevent_facts
