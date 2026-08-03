import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .condense_conversation import DEFAULT_CONDENSED_MEMORY_DIR, condense_conversation
from .store_semantic_memory import StoreSemanticMemory
from services.json import write_json


logger = logging.getLogger(__name__)
DEFAULT_MEMORY_DIR = Path(__file__).resolve().parents[2] / "memory" / "conversations"
DEFAULT_SEMANTIC_MEMORY_DB = Path(__file__).resolve().parents[2] / "memory" / "semantic_memory.db"

RELEVENCE_THRESHOLD = 0.356 # magic number from calibrate_threshold.py

def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ConversationMemory:
    base_dir: Path = DEFAULT_MEMORY_DIR
    condensed_base_dir: Path = DEFAULT_CONDENSED_MEMORY_DIR
    conversation_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])

    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.condensed_base_dir = Path(self.condensed_base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(timezone.utc)
        self.file_path = self.base_dir / f"{self.conversation_id}.json"
        self._state = {
            "conversation_id": self.conversation_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": self.started_at.isoformat(timespec="seconds"),
            "messages": [],
        }
        self._semantic_memory = None
        self._persist()

    def _persist(self):
        try:
            write_json(self.file_path, self._state)
        except OSError:
            logger.exception("Failed to persist conversation memory to %s", self.file_path)

    def save(self, messages):
        self._state["updated_at"] = _utc_timestamp()
        self._state["messages"] = list(messages)
        self._persist()

    def condense_conversation(self, messages):
        return condense_conversation(
            messages,
            conversation_id=self.conversation_id,
            source_conversation_file=self.file_path,
            condensed_base_dir=self.condensed_base_dir,
        )

    def extract_and_store_semantic_memory(self, condensed_conversation):
        from .extract_semantic_memory import extract_facts_from_conversation

        if condensed_conversation is None:
            logger.info("Skipping semantic memory extraction because condensed conversation is None.")
            return []

        print(f"Condensed conversation: {condensed_conversation}")
        condensed_messages = condensed_conversation.get("condensed_conversation", [])
        if not condensed_messages:
            logger.info("Skipping semantic memory extraction because condensed conversation is empty.")
            return []

        facts = extract_facts_from_conversation(condensed_messages)
        if self._semantic_memory is None:
            self._semantic_memory = StoreSemanticMemory(DEFAULT_SEMANTIC_MEMORY_DB)

        for fact in facts:
            print(f"Adding fact to semantic memory: {fact})")
            self._semantic_memory.add_fact(
                attribute=fact["attribute"],
                value=fact["value"],
                multi_valued=fact["multi_valued"],
                raw_text=fact["raw_text"],
                confidence=1.0,
                source_session_id=self.conversation_id,
            )
        return facts

    def search_facts(self, query, top_k=5):
        if self._semantic_memory is None:
            self._semantic_memory = StoreSemanticMemory(DEFAULT_SEMANTIC_MEMORY_DB)
        top_k_facts = self._semantic_memory.search_facts(query, top_k=top_k)
        relevent_facts = []
        for fact in top_k_facts:
            if fact.similarity is not None and fact.similarity >= RELEVENCE_THRESHOLD:
                relevent_facts.append(fact)
        return relevent_facts
