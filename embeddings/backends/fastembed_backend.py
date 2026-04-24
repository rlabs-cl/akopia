"""FastEmbed backend — in-process ONNX runtime, CPU-friendly.

Default choice for `docker compose up` quickstart. No external service
dependency. Model is downloaded on first use and cached under
``/root/.cache/fastembed`` inside the container.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("embeddings.fastembed")

_DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
_DIM = 768


class FastEmbedBackend:
    """Lazy-loaded fastembed `TextEmbedding`. Thread-safe via asyncio.to_thread."""

    def __init__(self, model: Optional[str] = None, quantized: Optional[bool] = None) -> None:
        self._model_name = model or _DEFAULT_MODEL
        # `quantized` is accepted for API compatibility; fastembed picks
        # the Q variant automatically for supported models.
        self._quantized = bool(quantized) if quantized is not None else True
        self._impl = None

    def _load(self):
        if self._impl is None:
            logger.info("Loading fastembed model %s (quantized=%s)", self._model_name, self._quantized)
            from fastembed import TextEmbedding
            self._impl = TextEmbedding(model_name=self._model_name)
        return self._impl

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        def _run() -> list[list[float]]:
            model = self._load()
            return [emb.tolist() for emb in model.embed(texts)]

        return await asyncio.to_thread(_run)

    def dim(self) -> int:
        return _DIM

    def model_id(self) -> str:
        return self._model_name
