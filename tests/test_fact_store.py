"""Tests for the semantic fact store's dedupe and supersede rules.

These run without sentence-transformers (and therefore without torch) because
the store takes an injected Embedder. That separation is the only reason CI can
exercise this logic at all.
"""

from __future__ import annotations

import pytest

from minus.errors import FactStoreError
from minus.memory.facts.models import default_raw_text, normalize_attribute
from minus.memory.facts.store import SqliteFactStore

from .fakes import FakeEmbedder


@pytest.fixture
def store(tmp_path):
    store = SqliteFactStore(tmp_path / "facts.db", embedder=FakeEmbedder())
    yield store
    store.close()


class TestAttributeNormalization:
    @pytest.mark.parametrize(
        "raw",
        ["preferred_editor", "Preferred Editor", "preferred-editor", "  Preferred__Editor  "],
    )
    def test_spelling_variants_collapse_to_one_slot(self, raw):
        assert normalize_attribute(raw) == "preferred_editor"

    def test_default_raw_text_reads_as_a_sentence(self):
        # Natural language, not "attribute: value" -- the embedding is compared
        # against natural-language queries.
        assert default_raw_text("preferred_editor", "VS Code") == (
            "The user's preferred editor is VS Code."
        )

    def test_attribute_that_normalizes_to_nothing_is_rejected(self, store):
        with pytest.raises(FactStoreError, match="empty"):
            store.add_fact("!!!", "value")


class TestSingleValued:
    def test_new_attribute_is_inserted(self, store):
        assert store.add_fact("timezone", "PST")["action"] == "inserted"

    def test_identical_value_is_a_duplicate(self, store):
        store.add_fact("timezone", "PST")
        result = store.add_fact("timezone", "PST")

        assert result["action"] == "duplicate_skipped"
        assert len(store.get_all_facts()) == 1

    def test_duplicate_detection_ignores_case_and_padding(self, store):
        store.add_fact("timezone", "PST")
        assert store.add_fact("timezone", "  pst ")["action"] == "duplicate_skipped"

    def test_different_value_supersedes_the_old_one(self, store):
        store.add_fact("timezone", "PST")
        result = store.add_fact("timezone", "EST")

        assert result["action"] == "superseded"

        active = store.get_all_facts()
        assert [(f.attribute, f.value) for f in active] == [("timezone", "EST")]

        # The old fact survives as history, linked forward.
        everything = store.get_all_facts(only_active=False)
        old = next(f for f in everything if f.value == "PST")
        assert old.active is False
        assert old.superseded_by == result["new_fact_id"]

    def test_attribute_variants_supersede_rather_than_fork(self, store):
        store.add_fact("preferred_editor", "VS Code")
        result = store.add_fact("Preferred Editor", "Neovim")

        assert result["action"] == "superseded"
        assert len(store.get_all_facts()) == 1


class TestMultiValued:
    def test_different_values_coexist(self, store):
        store.add_fact("allergy", "peanuts", multi_valued=True)
        result = store.add_fact("allergy", "shellfish", multi_valued=True)

        assert result["action"] == "inserted"
        assert {f.value for f in store.get_facts_by_attribute("allergy")} == {
            "peanuts",
            "shellfish",
        }

    def test_identical_value_is_still_a_duplicate(self, store):
        store.add_fact("allergy", "peanuts", multi_valued=True)
        result = store.add_fact("allergy", "peanuts", multi_valued=True)

        assert result["action"] == "duplicate_skipped"
        assert len(store.get_facts_by_attribute("allergy")) == 1


class TestReads:
    def test_known_attributes_gives_one_example_per_attribute(self, store):
        store.add_fact("timezone", "PST")
        store.add_fact("timezone", "EST")  # supersedes
        store.add_fact("allergy", "peanuts", multi_valued=True)

        known = store.get_known_attributes()

        assert {entry["attribute"] for entry in known} == {"timezone", "allergy"}
        # The example is the current value, not the superseded one.
        timezone = next(e for e in known if e["attribute"] == "timezone")
        assert timezone["example_value"] == "EST"

    def test_known_attributes_can_include_inactive(self, store):
        store.add_fact("timezone", "PST")
        store.add_fact("timezone", "EST")

        assert len(store.get_known_attributes(only_active=False)) == 1

    def test_search_returns_similarity_scores(self, store):
        store.add_fact("favorite_band", "Queen")
        results = store.search_facts("what band do I like?", top_k=5)

        assert len(results) == 1
        assert results[0].similarity is not None

    def test_search_excludes_superseded_facts_by_default(self, store):
        store.add_fact("timezone", "PST")
        store.add_fact("timezone", "EST")

        values = {f.value for f in store.search_facts("timezone", top_k=10)}
        assert values == {"EST"}


class TestMutation:
    def test_supersede_fact_forces_a_replacement(self, store):
        fact_id = store.add_fact("allergy", "peanuts", multi_valued=True)["fact_id"]
        store.supersede_fact(fact_id, "tree nuts")

        assert [f.value for f in store.get_facts_by_attribute("allergy")] == ["tree nuts"]

    def test_delete_removes_the_fact_and_its_embedding(self, store):
        fact_id = store.add_fact("timezone", "PST")["fact_id"]
        store.delete_fact(fact_id)

        assert store.get_all_facts(only_active=False) == []
        assert store.search_facts("timezone", top_k=5) == []

    def test_merge_attributes_moves_facts_onto_the_canonical_name(self, store):
        store.add_fact("programming_language", "Python")
        store.merge_attributes(["programming_language"], "preferred_language")

        assert store.get_facts_by_attribute("programming_language") == []
        assert [f.value for f in store.get_facts_by_attribute("preferred_language")] == ["Python"]


class TestPersistence:
    def test_facts_survive_reopening_the_database(self, tmp_path):
        path = tmp_path / "facts.db"

        first = SqliteFactStore(path, embedder=FakeEmbedder())
        first.add_fact("timezone", "PST")
        first.close()

        second = SqliteFactStore(path, embedder=FakeEmbedder())
        assert [f.value for f in second.get_all_facts()] == ["PST"]
        second.close()
