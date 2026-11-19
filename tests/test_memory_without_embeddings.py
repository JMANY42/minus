"""Core install: the assistant must run with semantic memory switched off.

Runs only when the embeddings extra is absent, which is the configuration this
project promises to support (the suite passes on a bare core install).
"""

from __future__ import annotations

import importlib.util

import pytest

from minus.memory.service import MemoryService

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sentence_transformers") is not None,
    reason="embeddings extra is installed; the degraded path cannot be observed",
)


def test_service_reports_why_memory_is_off_and_does_not_raise(tmp_path, caplog):
    service = MemoryService(base_dir=tmp_path / "conv", condensed_base_dir=tmp_path / "cond")
    try:
        assert service._embeddings is False
        assert service.search_facts("anything") == []
        assert service.extract_facts(None) == []
        assert "embeddings extra is not installed" in caplog.text
    finally:
        service.close()
