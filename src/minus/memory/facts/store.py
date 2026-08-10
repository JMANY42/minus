"""SQLite + sqlite-vec storage for durable facts.

See models.py for why facts are structured slots rather than sentences.

Dedupe and supersede are exact-match on the normalized attribute, not
threshold-based -- there is no dup_threshold or conflict_threshold left to
tune:

  multi_valued=False, attribute already active
      same value (case-insensitive) -> duplicate; refresh confidence/timestamp
      different value               -> supersede; old marked inactive and linked
  multi_valued=True, attribute already active
      same value                    -> duplicate; refresh
      different value               -> insert alongside (no supersede)
  no active fact for the attribute  -> plain insert

Embeddings serve fuzzy retrieval only -- "what do we know that is relevant to
this message?" -- never duplicate detection.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import sqlite_vec

from minus.errors import FactStoreError
from minus.memory.facts.embeddings import DEFAULT_DIMENSIONS, SentenceTransformerEmbedder
from minus.memory.facts.models import Fact, default_raw_text, normalize_attribute

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id",
    "attribute",
    "value",
    "multi_valued",
    "raw_text",
    "confidence",
    "source_session_id",
    "created_at",
    "active",
    "superseded_by",
)
_SELECT_COLUMNS = ", ".join(_COLUMNS)
_SELECT_COLUMNS_F = ", ".join(f"f.{column}" for column in _COLUMNS)


class SqliteFactStore:
    """A FactStore backed by SQLite with a sqlite-vec embedding index."""

    def __init__(self, db_path: str | Path = "memory.db", embedder: Any | None = None) -> None:
        """
        Args:
            embedder: An Embedder. Defaults to the sentence-transformers one;
                injected so the store can be tested without torch installed.
        """
        self.db_path = str(db_path)
        self.embedder = embedder or SentenceTransformerEmbedder()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self) -> None:
        dimensions = getattr(self.embedder, "dimensions", DEFAULT_DIMENSIONS)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                attribute TEXT NOT NULL,
                value TEXT NOT NULL,
                multi_valued INTEGER NOT NULL DEFAULT 0,
                raw_text TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_session_id TEXT,
                created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                superseded_by TEXT
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_attribute ON facts (attribute, active)"
        )
        # distance_metric=cosine is required -- vec0 defaults to L2, which
        # produces meaningless "similarity" values on normalized vectors.
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(
                fact_id TEXT PRIMARY KEY,
                embedding FLOAT[{dimensions}] distance_metric=cosine
            )
        """)
        self.conn.commit()

    # ---- Writing ----

    def add_fact(
        self,
        attribute: str,
        value: str,
        multi_valued: bool = False,
        raw_text: str | None = None,
        confidence: float = 1.0,
        source_session_id: str | None = None,
    ) -> dict:
        """Insert a fact, applying the dedupe/supersede rules in the module docstring.

        Returns one of:
            {"action": "inserted", "fact_id": ...}
            {"action": "duplicate_skipped", "existing_fact_id": ...}
            {"action": "superseded", "old_fact_id": ..., "new_fact_id": ...}
        """
        attribute = normalize_attribute(attribute)
        if not attribute:
            raise FactStoreError("Fact attribute normalized to an empty string")
        if raw_text is None:
            raw_text = default_raw_text(attribute, value)

        existing = self.get_facts_by_attribute(attribute, only_active=True)

        # An identical value is always a duplicate, whatever multi_valued says.
        for fact in existing:
            if fact.value.strip().lower() == value.strip().lower():
                self._touch_fact(fact.id, confidence)
                return {"action": "duplicate_skipped", "existing_fact_id": fact.id}

        if not multi_valued and existing:
            # A single-valued slot holding a different value: this is an update,
            # not a new independent fact.
            old = existing[0]
            new_id = self._insert(
                attribute, value, multi_valued, raw_text, confidence, source_session_id
            )
            self._mark_superseded(old.id, new_id)
            return {"action": "superseded", "old_fact_id": old.id, "new_fact_id": new_id}

        fact_id = self._insert(
            attribute, value, multi_valued, raw_text, confidence, source_session_id
        )
        return {"action": "inserted", "fact_id": fact_id}

    def _insert(
        self,
        attribute: str,
        value: str,
        multi_valued: bool,
        raw_text: str,
        confidence: float,
        source_session_id: str | None,
    ) -> str:
        fact_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO facts (id, attribute, value, multi_valued, raw_text, "
            "confidence, source_session_id, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                fact_id,
                attribute,
                value,
                int(multi_valued),
                raw_text,
                confidence,
                source_session_id,
                time.time(),
            ),
        )
        self.conn.execute(
            "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?, ?)",
            (fact_id, self.embedder.embed(raw_text)),
        )
        self.conn.commit()
        return fact_id

    def _touch_fact(self, fact_id: str, confidence: float) -> None:
        self.conn.execute(
            "UPDATE facts SET confidence = MAX(confidence, ?), created_at = ? WHERE id = ?",
            (confidence, time.time(), fact_id),
        )
        self.conn.commit()

    def _mark_superseded(self, old_fact_id: str, new_fact_id: str) -> None:
        self.conn.execute(
            "UPDATE facts SET active = 0, superseded_by = ? WHERE id = ?",
            (new_fact_id, old_fact_id),
        )
        self.conn.commit()

    def supersede_fact(
        self,
        old_fact_id: str,
        new_value: str,
        raw_text: str | None = None,
        confidence: float = 1.0,
        source_session_id: str | None = None,
    ) -> str:
        """Force-replace a specific fact, regardless of multi_valued."""
        row = self.conn.execute(
            "SELECT attribute, multi_valued FROM facts WHERE id = ?", (old_fact_id,)
        ).fetchone()
        if row is None:
            raise FactStoreError(f"No fact with id {old_fact_id}")

        attribute, multi_valued = row[0], bool(row[1])
        if raw_text is None:
            raw_text = default_raw_text(attribute, new_value)

        new_id = self._insert(
            attribute, new_value, multi_valued, raw_text, confidence, source_session_id
        )
        self._mark_superseded(old_fact_id, new_id)
        return new_id

    def delete_fact(self, fact_id: str) -> None:
        """Hard-delete a fact and its embedding.

        Unlike supersede this leaves no history -- use it for pruning bad facts,
        not for recording that a value changed.
        """
        self.conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (fact_id,))
        self.conn.commit()

    # ---- Reading ----

    def get_facts_by_attribute(self, attribute: str, only_active: bool = True) -> list[Fact]:
        """Exact-match lookup. The primary path for dedupe and slot-style reads."""
        attribute = normalize_attribute(attribute)
        active = " AND active = 1" if only_active else ""
        rows = self.conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM facts WHERE attribute = ?{active} "
            "ORDER BY created_at DESC",
            (attribute,),
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def search_facts(self, query: str, top_k: int = 5, only_active: bool = True) -> list[Fact]:
        """Fuzzy semantic search over raw_text, for retrieval only (never dedupe)."""
        active = "f.active = 1 AND " if only_active else ""
        rows = self.conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS_F}, e.distance
            FROM fact_embeddings e
            JOIN facts f ON f.id = e.fact_id
            WHERE {active}e.embedding MATCH ? AND k = ?
            ORDER BY e.distance
            """,
            (self.embedder.embed(query), top_k),
        ).fetchall()

        results = []
        for row in rows:
            fact = self._row_to_fact(row[: len(_COLUMNS)])
            fact.similarity = 1 - row[len(_COLUMNS)]
            results.append(fact)
        return results

    def get_all_facts(self, only_active: bool = True) -> list[Fact]:
        active = " WHERE active = 1" if only_active else ""
        rows = self.conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM facts{active} ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def get_known_attributes(self, only_active: bool = True) -> list[dict]:
        """Every distinct attribute in use, with its most recent value as an example.

        Fed to the extraction prompt so the model reuses an existing attribute
        name instead of inventing a near-duplicate ('preferred_language' vs
        'programming_language'). That is the cheap, reliable fix; do not try to
        catch it with embedding similarity on the attribute names themselves,
        which is unreliable for short labels. See merge_attributes() for the
        backstop cleanup pass.

        The two branches are spelled out rather than assembled by substituting
        into a shared fragment. The previous version built the correlated
        subquery's filter with `active_filter.replace("active", "f2.active")`,
        which silently depended on the exact wording of the string it patched.
        """
        if only_active:
            sql = f"""
                SELECT {_SELECT_COLUMNS_F} FROM facts f
                WHERE f.active = 1
                  AND f.created_at = (
                      SELECT MAX(f2.created_at) FROM facts f2
                      WHERE f2.attribute = f.attribute AND f2.active = 1
                  )
                ORDER BY f.attribute
            """
        else:
            sql = f"""
                SELECT {_SELECT_COLUMNS_F} FROM facts f
                WHERE f.created_at = (
                    SELECT MAX(f2.created_at) FROM facts f2
                    WHERE f2.attribute = f.attribute
                )
                ORDER BY f.attribute
            """

        rows = self.conn.execute(sql).fetchall()
        return [
            {"attribute": fact.attribute, "example_value": fact.value}
            for fact in (self._row_to_fact(row) for row in rows)
        ]

    def merge_attributes(self, duplicate_attributes: list[str], canonical_attribute: str) -> dict:
        """Reassign facts from near-duplicate attribute names onto one canonical name.

        Backstop for names that slipped past the known-attributes prompt. Moved
        facts go back through add_fact(), so a merge that creates a duplicate or
        a single-valued conflict resolves the same way any other fact would.
        """
        canonical_attribute = normalize_attribute(canonical_attribute)
        moved = []

        for duplicate in duplicate_attributes:
            duplicate = normalize_attribute(duplicate)
            if duplicate == canonical_attribute:
                continue

            for fact in self.get_facts_by_attribute(duplicate, only_active=True):
                # Deactivate first so add_fact's lookup under the canonical name
                # does not see a stale duplicate.
                self.conn.execute("UPDATE facts SET active = 0 WHERE id = ?", (fact.id,))
                self.conn.commit()

                result = self.add_fact(
                    canonical_attribute,
                    fact.value,
                    fact.multi_valued,
                    confidence=fact.confidence,
                    source_session_id=fact.source_session_id,
                )
                new_id = (
                    result.get("fact_id")
                    or result.get("new_fact_id")
                    or result.get("existing_fact_id")
                )
                # Link the old fact forward, same as a normal supersede.
                self.conn.execute(
                    "UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, fact.id)
                )
                self.conn.commit()
                moved.append({"old_attribute": duplicate, "old_fact_id": fact.id, "result": result})

        return {"canonical_attribute": canonical_attribute, "moved": moved}

    @staticmethod
    def _row_to_fact(row) -> Fact:
        return Fact(
            id=row[0],
            attribute=row[1],
            value=row[2],
            multi_valued=bool(row[3]),
            raw_text=row[4],
            confidence=row[5],
            source_session_id=row[6],
            created_at=row[7],
            active=bool(row[8]),
            superseded_by=row[9],
        )

    def close(self) -> None:
        self.conn.close()


# Retained: the store was named MemoryStore when it was the only memory
# component, and scripts and tests still refer to it by that name.
MemoryStore = SqliteFactStore
