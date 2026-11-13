"""Embedding model wrapper using sentence-transformers.

Loads all-MiniLM-L6-v2 at startup and exposes a simple embed() interface.
The sentence-transformers import is deferred to __init__ so tests can
import this module without the heavy ML dependency installed.
"""

from __future__ import annotations

import hashlib

import numpy as np
import structlog

from app.tokens import content_to_text

logger = structlog.get_logger()


class Embedder:
    """Wrapper around sentence-transformers for prompt embedding."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        logger.info("embedder.loading", model=model_name)
        self._model = SentenceTransformer(model_name)
        self._dimension = self._model.get_sentence_embedding_dimension()
        logger.info("embedder.loaded", dimension=self._dimension)

    @property
    def dimension(self) -> int:
        """Return the embedding vector dimension."""
        return self._dimension

    def embed(self, text: str) -> np.ndarray:
        """Embed a single text string into a dense vector.

        Returns a 1-D float32 numpy array of shape (dimension,).
        """
        vector = self._model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return vector.astype(np.float32)

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed multiple texts into a matrix.

        Returns a float32 numpy array of shape (len(texts), dimension).
        """
        vectors = self._model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return vectors.astype(np.float32)

    @staticmethod
    def content_hash(model: str, messages: list[dict]) -> str:
        """Create a deterministic hash key from model + messages.

        Used for exact-match cache lookups in Redis.
        """
        raw = f"{model}::{messages}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def messages_to_text(messages: list[dict]) -> str:
        """Flatten a messages list into a single text string for embedding.

        Concatenates role + content pairs, focusing on the last user message
        for semantic similarity since that drives the response.
        """
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = content_to_text(msg.get("content"))
            if content:
                parts.append(f"{role}: {content}")
        return "\n".join(parts)
