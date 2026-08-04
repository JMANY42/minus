"""MemoryService: the facade the rest of MINUS talks to about memory.

Holds three separable concerns together so callers do not have to:
  * the running transcript, written to disk after every turn (transcript.py)
  * condensing a finished conversation down to its user/assistant turns
  * extracting durable facts from that condensation and storing them

Each is injectable. The fact store is a FactStore, the model is a ChatModel,
and both may be absent -- a conversation can be recorded with neither, in which
case extraction is skipped rather than failing at the end of a session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from minus.config import Settings
from minus.core.prompts import SYSTEM_PROMPT
from minus.memory.condense import condense_conversation
from minus.memory.extraction import extract_facts_from_conversation
from minus.memory.facts.store import SqliteFactStore
from minus.memory.transcript import TranscriptFile
from minus.paths import condensed_conversations_dir, conversations_dir, semantic_memory_db
from minus.services.json import serialize_json

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = conversations_dir()
DEFAULT_CONDENSED_MEMORY_DIR = condensed_conversations_dir()
DEFAULT_SEMANTIC_MEMORY_DB = semantic_memory_db()

# Relevance cutoff for fact retrieval, derived by `minus calibrate` as the
# midpoint between the direct-match and related-topic similarity distributions.
# Settings.relevance_threshold is what actually applies at runtime.
DEFAULT_RELEVANCE_THRESHOLD = Settings.model_fields["relevance_threshold"].default
DEFAULT_FACT_SEARCH_TOP_K = Settings.model_fields["fact_search_top_k"].default


def new_conversation_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]


@dataclass
class MemoryService:
    """Conversation persistence plus the semantic fact store."""

    base_dir: Path = DEFAULT_MEMORY_DIR
    condensed_base_dir: Path = DEFAULT_CONDENSED_MEMORY_DIR
    conversation_id: str = field(default_factory=new_conversation_id)
    model: Any | None = None
    extraction_model_name: str | None = None
    store: Any | None = None
    system_prompt: str = SYSTEM_PROMPT
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD
    fact_search_top_k: int = DEFAULT_FACT_SEARCH_TOP_K

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.condensed_base_dir = Path(self.condensed_base_dir)
        self.started_at = datetime.now(UTC)
        self._transcript = TranscriptFile(
            directory=self.base_dir,
            conversation_id=self.conversation_id,
            started_at=self.started_at,
            system_prompt=self.system_prompt,
        )
        self._store = (
            self.store if self.store is not None else SqliteFactStore(DEFAULT_SEMANTIC_MEMORY_DB)
        )

    @property
    def file_path(self) -> Path:
        return self._transcript.path

    # ---- Transcript ----

    def save(self, messages: list[dict]) -> None:
        self._transcript.save(messages)

    def condense_conversation(self, messages: list[dict]) -> dict | None:
        return condense_conversation(
            messages,
            conversation_id=self.conversation_id,
            source_conversation_file=self.file_path,
            condensed_base_dir=self.condensed_base_dir,
            system_prompt=self.system_prompt,
        )

    # ---- Facts ----

    def extract_facts(self, condensed_conversation: dict | None) -> list[dict]:
        if condensed_conversation is None:
            logger.info("Skipping fact extraction: no condensed conversation.")
            return []

        condensed_messages = condensed_conversation.get("condensed_conversation", [])
        if not condensed_messages:
            logger.info("Skipping fact extraction: condensed conversation is empty.")
            return []

        if self.model is None:
            logger.info("Skipping fact extraction: no model available.")
            return []

        known_attributes = self._store.get_known_attributes()
        logger.debug("Known attributes: %s", known_attributes)
        return extract_facts_from_conversation(
            condensed_messages,
            known_attributes=serialize_json(known_attributes),
            model=self.model,
            model_name=self.extraction_model_name,
        )

    def store_facts(self, facts: list[dict]) -> None:
        for fact in facts:
            logger.info("Adding fact to semantic memory: %s", fact)
            self._store.add_fact(
                attribute=fact["attribute"],
                value=fact["value"],
                multi_valued=fact["multi_valued"],
                raw_text=fact["raw_text"],
                confidence=1.0,
                source_session_id=self.conversation_id,
            )

    def extract_and_store_semantic_memory(self, condensed_conversation: dict | None) -> list[dict]:
        facts = self.extract_facts(condensed_conversation)
        self.store_facts(facts)
        return facts

    def search_facts(
        self,
        query: str,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> list:
        """Stored facts close enough to `query` to be worth injecting into context."""
        top_k = self.fact_search_top_k if top_k is None else top_k
        threshold = self.relevance_threshold if threshold is None else threshold

        return [
            fact
            for fact in self._store.search_facts(query, top_k=top_k)
            if fact.similarity is not None and (threshold is None or fact.similarity >= threshold)
        ]

    def all_facts(self, only_active: bool = True) -> list:
        """Every stored fact.

        Exists so callers stop reaching into the private store; the CLI used to
        touch `_memory_store` directly to log the session's facts.
        """
        return self._store.get_all_facts(only_active=only_active)

    def close(self) -> None:
        self._store.close()


# The service was named MemoryManager before it was split; keep the old name
# working for callers and tests that still use it.
MemoryManager = MemoryService
