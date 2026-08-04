"""The Fact record.

Facts are stored as structured (attribute, value) slots rather than free-form
sentences. The reasoning, preserved from the original store because it still
governs the design:

An earlier version compared whole sentences by cosine similarity to detect
duplicates and conflicts ("The timezone is PST" vs "Timezone is Pacific
Standard Time"). That asked embeddings to solve two different problems at once:

  1. "Is this the same slot with a different value?"  (timezone: PST -> EST)
  2. "Is this semantically related content?"          (fuzzy retrieval)

Problem 1 does not need embeddings at all once facts are structured -- it is an
exact match on a normalized attribute name. Problem 2 is what embeddings are
actually good at, and they do it better when they are not also being asked to
resolve exact-duplicate questions against a similarity threshold.

So every fact carries:
  - attribute:    normalized snake_case slot, e.g. "timezone", "preferred_editor".
                  Same category -> same slot, every time.
  - value:        the short canonical answer, not a sentence.
  - multi_valued: False if only one value can hold at a time (timezone, job --
                  a new value supersedes the old). True if several can coexist
                  (allergies, interests -- a new value is appended).
  - raw_text:     a natural sentence used purely for the embedding, so that
                  surface-form variance does not fragment cosine similarity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Fact:
    id: str
    attribute: str
    value: str
    multi_valued: bool
    raw_text: str
    confidence: float
    source_session_id: str | None
    created_at: float
    active: bool
    superseded_by: str | None
    similarity: float | None = None  # populated on search results only


def normalize_attribute(attribute: str) -> str:
    """Force attribute names into one consistent slot name.

    'Preferred Editor', 'preferred-editor' and 'preferred_editor' must all
    collide into the same slot instead of silently creating three.
    """
    text = attribute.strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    return re.sub(r"_+", "_", text).strip("_")


def default_raw_text(attribute: str, value: str) -> str:
    """A templated natural sentence used for embedding.

    Deliberately not a terse "attribute: value". Dedupe is exact-match on the
    attribute, so raw_text's embedding has exactly one job: matching
    natural-language queries in search_facts(). General-purpose sentence
    embedding models align best when both sides look like natural language -- a
    query like "what editor do you use?" matches "The user's preferred editor
    is VS Code." far better than it matches "preferred_editor: VS Code".

    For fact styles where this template reads awkwardly ("The user's allergy is
    peanuts"), prefer having the extraction model produce raw_text directly.
    """
    readable = attribute.replace("_", " ")
    return f"The user's {readable} is {value}."
