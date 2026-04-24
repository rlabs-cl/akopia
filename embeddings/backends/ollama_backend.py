"""Ollama backend — HTTP client to an external Ollama server.

Ollama is NOT part of the akopia docker-compose. The user runs it
wherever they want (host, dedicated GPU box, remote service) and points
the embeddings service at it via ``OLLAMA_URL`` / akopia.yaml.

Prefers the newer ``/api/embed`` endpoint (batch, plural ``input``) and
falls back to legacy per-text ``/api/embeddings`` (singular ``prompt``)
for older Ollama versions.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("embeddings.ollama")

_DEFAULT_MODEL = "nomic-embed-text"
_DEFAULT_URL = "http://localhost:11434"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class OllamaBackend:
    """Talk to a running Ollama instance via HTTP.

    URL resolution order: constructor arg → ``OLLAMA_URL`` env → default.
    """

    def __init__(self, model: Optional[str] = None, url: Optional[str] = None) -> None:
        self._model_name = model or _DEFAULT_MODEL
        self._url = (url or os.getenv("OLLAMA_URL") or _DEFAULT_URL).rstrip("/")
        self._dim: Optional[int] = None
        self._batch_supported: Optional[bool] = None
        logger.info("OllamaBackend configured: url=%s model=%s", self._url, self._model_name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Try batch endpoint first; fall back once if 404.
            if self._batch_supported is not False:
                try:
                    vectors = await self._embed_batch(client, texts)
                    self._batch_supported = True
                    self._record_dim(vectors)
                    return vectors
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.info("Ollama %s lacks /api/embed; using legacy /api/embeddings", self._url)
                        self._batch_supported = False
                    else:
                        raise
            vectors = [await self._embed_legacy(client, t) for t in texts]
            self._record_dim(vectors)
            return vectors

    async def _embed_batch(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        resp = await client.post(
            f"{self._url}/api/embed",
            json={"model": self._model_name, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or not vectors:
            raise RuntimeError(f"Ollama /api/embed returned unexpected shape: {data!r}")
        return [list(map(float, v)) for v in vectors]

    async def _embed_legacy(self, client: httpx.AsyncClient, text: str) -> list[float]:
        resp = await client.post(
            f"{self._url}/api/embeddings",
            json={"model": self._model_name, "prompt": text},
        )
        resp.raise_for_status()
        data = resp.json()
        vector = data.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise RuntimeError(f"Ollama /api/embeddings returned unexpected shape: {data!r}")
        return list(map(float, vector))

    def _record_dim(self, vectors: list[list[float]]) -> None:
        if vectors and self._dim is None:
            self._dim = len(vectors[0])

    def dim(self) -> int:
        # Ollama models vary. nomic-embed-text is 768. We lazy-fill on first
        # call; before that, return the common default so callers that need
        # it at wire-up time still get a sane value.
        return self._dim or 768

    def model_id(self) -> str:
        return self._model_name
