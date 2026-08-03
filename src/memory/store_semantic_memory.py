"""
memory_store.py

Semantic memory store for a local, single-user AI agent: SQLite + sqlite-vec
for fuzzy retrieval, with facts stored as structured (attribute, value) slots
rather than free-form sentences.

WHY STRUCTURED FACTS INSTEAD OF FREE SENTENCES
-----------------------------------------------
An earlier version of this store compared whole sentences via cosine
similarity to detect duplicates/conflicts ("The timezone is PST" vs "Timezone
is Pacific Standard Time"). That asks embeddings to solve two different
problems at once:
  1. "Is this the same slot with a different value?" (timezone: PST -> EST)
  2. "Is this semantically related content?" (fuzzy retrieval)
Problem 1 doesn't need embeddings at all if facts are structured -- it's an
exact match on a normalized attribute name. Problem 2 is what embeddings are
actually good at, and they do it better once they're not also being asked to
resolve exact-duplicate/conflict questions with a similarity threshold.

So: every fact is (attribute, value, multi_valued), plus a canonical raw_text
used only for embedding/fuzzy search and display -- not for dedupe logic.
  - attribute: normalized snake_case category, e.g. "timezone", "diet",
    "preferred_editor". Same category -> same slot, every time.
  - value: the short canonical answer, not a sentence.
  - multi_valued: False if only one value can be true at a time (timezone,
    job, home_city -- a new value replaces the old one). True if several
    values can coexist (allergies, interests, hobbies -- a new value is
    appended alongside existing ones).
  - raw_text: a consistently-templated sentence ("{attribute}: {value}" by
    default) used purely for the embedding, so surface-form variance (tense,
    phrasing, filler words) doesn't fragment cosine similarity the way full
    free-form sentences did.

DEDUPE / SUPERSEDE LOGIC (now exact-match, not threshold-based)
-----------------------------------------------------------------
- multi_valued=False, same attribute already active:
    - same value (case-insensitive)  -> duplicate, just refresh confidence/timestamp
    - different value                -> automatically supersede: old fact marked
                                         inactive, new fact inserted and linked
- multi_valued=True, same attribute already active:
    - same value (case-insensitive)  -> duplicate, just refresh confidence/timestamp
    - different value                -> insert as an additional active fact under
                                         that attribute (no supersede)
- No attribute match yet             -> plain insert

Embeddings (and search_facts) are still there, but now only serve fuzzy
retrieval -- "what do we know that's relevant to this new message?" -- not
duplicate/conflict detection. There is no dup_threshold/conflict_threshold
left to tune.
"""

import re
import sqlite3
import sqlite_vec
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from sentence_transformers import SentenceTransformer

EMBEDDING_DIM = 384  # matches all-MiniLM-L6-v2
_model = None


def get_model():
    """Lazy-load the embedding model (avoids slow import at module load time)."""
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed(text: str) -> bytes:
    """Return a fact's embedding as raw bytes for sqlite-vec storage."""
    vec = get_model().encode(text, normalize_embeddings=True)
    return sqlite_vec.serialize_float32(vec.tolist())


def normalize_attribute(attribute: str) -> str:
    """Force attribute names into a consistent snake_case slot name so
    'Preferred Editor', 'preferred-editor', and 'preferred_editor' all
    collide into the same slot instead of silently creating three."""
    s = attribute.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def default_raw_text(attribute: str, value: str) -> str:
    """Consistently-templated NATURAL SENTENCE used for embedding.
 
    This intentionally does NOT use a terse "attribute: value" format.
    Dedupe/supersede logic is exact-match on `attribute` (see module
    docstring), so raw_text's embedding has exactly one job: matching
    against natural-language queries in search_facts(). General-purpose
    sentence embedding models align best when both sides of a comparison
    look like natural language -- a query like "what editor do you use?"
    matches "The user's preferred editor is VS Code." much better than it
    matches the technical token "preferred_editor: VS Code".
 
    For fact styles where this generic template reads awkwardly (e.g.
    "The user's allergy is peanuts"), prefer having your extraction LLM
    generate a natural raw_text directly and pass it to add_fact(raw_text=...)
    instead of relying on this fallback.
    """
    readable = attribute.replace("_", " ")
    return f"The user's {readable} is {value}."



@dataclass
class Fact:
    id: str
    attribute: str
    value: str
    multi_valued: bool
    raw_text: str
    confidence: float
    source_session_id: Optional[str]
    created_at: float
    active: bool
    superseded_by: Optional[str]
    similarity: Optional[float] = None  # populated on search results only


class StoreSemanticMemory:
    def __init__(self, db_path: str = "memory.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self):
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
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_facts_attribute
            ON facts (attribute, active)
        """)
        # sqlite-vec virtual table for embeddings, keyed by fact id.
        # distance_metric=cosine is required -- vec0 defaults to L2, which
        # produces meaningless "similarity" values on normalized vectors.
        self.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(
                fact_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine
            )
        """)
        self.conn.commit()

    # ---------- Writing ----------

    def add_fact(
        self,
        attribute: str,
        value: str,
        multi_valued: bool = False,
        raw_text: Optional[str] = None,
        confidence: float = 1.0,
        source_session_id: Optional[str] = None,
    ) -> dict:
        """
        Insert a fact using exact-match dedupe/supersede logic on
        (attribute, value) -- see module docstring for the full rules.

        Returns one of:
          {"action": "inserted", "fact_id": ...}
          {"action": "duplicate_skipped", "existing_fact_id": ...}
          {"action": "superseded", "old_fact_id": ..., "new_fact_id": ...}
        """
        attribute = normalize_attribute(attribute)
        if raw_text is None:
            raw_text = default_raw_text(attribute, value)

        existing_for_attribute = self.get_facts_by_attribute(attribute, only_active=True)

        # Look for an exact (case-insensitive) value match first, regardless
        # of multi_valued -- an identical fact is always a duplicate.
        for f in existing_for_attribute:
            if f.value.strip().lower() == value.strip().lower():
                self._touch_fact(f.id, confidence)
                return {"action": "duplicate_skipped", "existing_fact_id": f.id}

        if not multi_valued and existing_for_attribute:
            # Single-valued slot already has a different active value ->
            # this is an update, not a new independent fact. Auto-supersede.
            old = existing_for_attribute[0]
            new_id = self._insert(attribute, value, multi_valued, raw_text,
                                   confidence, source_session_id)
            self._mark_superseded(old.id, new_id)
            return {"action": "superseded", "old_fact_id": old.id, "new_fact_id": new_id}

        # Either multi-valued (append alongside existing values) or no
        # existing fact for this attribute at all.
        fact_id = self._insert(attribute, value, multi_valued, raw_text,
                                confidence, source_session_id)
        return {"action": "inserted", "fact_id": fact_id}

    def _insert(self, attribute, value, multi_valued, raw_text,
                confidence, source_session_id) -> str:
        fact_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO facts (id, attribute, value, multi_valued, raw_text, "
            "confidence, source_session_id, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (fact_id, attribute, value, int(multi_valued), raw_text,
             confidence, source_session_id, now),
        )
        self.conn.execute(
            "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?, ?)",
            (fact_id, embed(raw_text)),
        )
        self.conn.commit()
        return fact_id

    def _touch_fact(self, fact_id: str, confidence: float):
        self.conn.execute(
            "UPDATE facts SET confidence = MAX(confidence, ?), created_at = ? WHERE id = ?",
            (confidence, time.time(), fact_id),
        )
        self.conn.commit()

    def _mark_superseded(self, old_fact_id: str, new_fact_id: str):
        self.conn.execute(
            "UPDATE facts SET active = 0, superseded_by = ? WHERE id = ?",
            (new_fact_id, old_fact_id),
        )
        self.conn.commit()

    def supersede_fact(self, old_fact_id: str, new_value: str,
                        raw_text: Optional[str] = None, confidence: float = 1.0,
                        source_session_id: Optional[str] = None) -> str:
        """Manual override: force-replace a specific fact with a new value,
        regardless of multi_valued. Useful if the LLM extraction step (or you)
        decides an update should happen outside the automatic attribute-match
        flow above."""
        row = self.conn.execute(
            "SELECT attribute, multi_valued FROM facts WHERE id = ?", (old_fact_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No fact with id {old_fact_id}")
        attribute, multi_valued = row[0], bool(row[1])

        if raw_text is None:
            raw_text = default_raw_text(attribute, new_value)

        new_id = self._insert(attribute, new_value, multi_valued, raw_text,
                               confidence, source_session_id)
        self._mark_superseded(old_fact_id, new_id)
        return new_id

    # ---------- Reading ----------

    def get_facts_by_attribute(self, attribute: str, only_active: bool = True) -> list[Fact]:
        """Exact-match lookup by attribute -- no embeddings involved. This is
        the primary path for dedupe/supersede logic and for slot-style lookups
        like 'what's the current timezone?'."""
        attribute = normalize_attribute(attribute)
        active_clause = "AND active = 1" if only_active else ""
        rows = self.conn.execute(
            f"SELECT id, attribute, value, multi_valued, raw_text, confidence, "
            f"source_session_id, created_at, active, superseded_by "
            f"FROM facts WHERE attribute = ? {active_clause} ORDER BY created_at DESC",
            (attribute,),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def search_facts(self, query: str, top_k: int = 5,
                      only_active: bool = True) -> list[Fact]:
        """Fuzzy semantic search over raw_text -- for retrieving facts
        relevant to a new conversation, not for dedupe (that's exact-match,
        see get_facts_by_attribute / add_fact)."""
        query_embedding = embed(query)

        if only_active:
            sql = """
                SELECT f.id, f.attribute, f.value, f.multi_valued, f.raw_text,
                       f.confidence, f.source_session_id, f.created_at,
                       f.active, f.superseded_by, e.distance
                FROM fact_embeddings e
                JOIN facts f ON f.id = e.fact_id
                WHERE f.active = 1
                AND e.embedding MATCH ?
                AND k = ?
                ORDER BY e.distance
            """
        else:
            sql = """
                SELECT f.id, f.attribute, f.value, f.multi_valued, f.raw_text,
                       f.confidence, f.source_session_id, f.created_at,
                       f.active, f.superseded_by, e.distance
                FROM fact_embeddings e
                JOIN facts f ON f.id = e.fact_id
                WHERE e.embedding MATCH ?
                AND k = ?
                ORDER BY e.distance
            """

        rows = self.conn.execute(sql, (query_embedding, top_k)).fetchall()

        results = []
        for r in rows:
            fact = self._row_to_fact(r[:10])
            fact.similarity = 1 - r[10]
            results.append(fact)
        return results

    def get_all_facts(self, only_active: bool = True) -> list[Fact]:
        active_clause = "WHERE active = 1" if only_active else ""
        rows = self.conn.execute(
            f"SELECT id, attribute, value, multi_valued, raw_text, confidence, "
            f"source_session_id, created_at, active, superseded_by "
            f"FROM facts {active_clause} ORDER BY created_at DESC",
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    @staticmethod
    def _row_to_fact(r) -> Fact:
        return Fact(
            id=r[0], attribute=r[1], value=r[2], multi_valued=bool(r[3]),
            raw_text=r[4], confidence=r[5], source_session_id=r[6],
            created_at=r[7], active=bool(r[8]), superseded_by=r[9],
        )

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Demo / smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    store = StoreSemanticMemory(":memory:")

    print("--- Inserting single-valued facts ---")
    print(store.add_fact("timezone", "PST"))
    print(store.add_fact("preferred_editor", "VS Code"))
    print(store.add_fact("job_title", "Software Engineer"))

    print("\n--- Inserting an exact duplicate (should be skipped) ---")
    print(store.add_fact("timezone", "PST"))

    print("\n--- Inserting an update to a single-valued attribute (should auto-supersede) ---")
    print(store.add_fact("timezone", "EST"))

    print("\n--- Inserting multi-valued facts (allergies) ---")
    print(store.add_fact("allergy", "peanuts", multi_valued=True))
    print(store.add_fact("allergy", "shellfish", multi_valued=True))
    print("Duplicate allergy (should be skipped):", store.add_fact("allergy", "peanuts", multi_valued=True))

    print("\n--- Attribute-normalization collapsing variant spellings ---")
    print(store.add_fact("Preferred Editor", "Neovim"))  # should collide with preferred_editor and supersede

    print("\n--- Active facts ---")
    for f in store.get_all_facts():
        print(f" - [{f.attribute}={f.value}] multi_valued={f.multi_valued}")

    print("\n--- All facts including inactive (history) ---")
    for f in store.get_all_facts(only_active=False):
        print(f" - active={f.active} attr={f.attribute} value={f.value} superseded_by={f.superseded_by}")

    print("\n--- get_facts_by_attribute('allergy') ---")
    for f in store.get_facts_by_attribute("allergy"):
        print(f" - {f.value}")

    relevant = store.search_facts("what editor does the user like?", top_k=5)
    print("\n--- Relevant facts ---")
    for f in relevant:
        print(f" - {f.value} - {f.similarity:.3f}")

    store.close()