"""Text embeddings for semantic fact retrieval.

Split out of the fact store so the store can be exercised without installing
sentence-transformers, which pulls in torch and several hundred MB of CUDA
wheels. The store takes an Embedder; tests supply a deterministic fake and CI
never downloads a model.

The import of sentence_transformers stays inside the method for the same
reason it always has: it is slow enough to be worth deferring until something
actually needs a vector.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlite_vec

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "all-MiniLM-L6-v2"
DEFAULT_DIMENSIONS = 384  # matches all-MiniLM-L6-v2


class SentenceTransformerEmbedder:
    """Embeddings from a local sentence-transformers model."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
    ) -> None:
        self.model_name = model_name
        self._dimensions = dimensions
        self._model: Any | None = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _load(self) -> Any:
        if self._model is None:
            # Deferred: importing sentence_transformers costs seconds and pulls
            # torch into the process, which most commands never need.
            from sentence_transformers import SentenceTransformer

            logger.debug("Loading embedding model %s", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text: str) -> bytes:
        """Return the embedding as the raw float32 bytes sqlite-vec stores."""
        vector = self._load().encode(text, normalize_embeddings=True)
        return sqlite_vec.serialize_float32(vector.tolist())
