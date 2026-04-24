"""Pluggable embedder backends.

The embedding service picks a backend at startup based on
``core.embeddings.text.provider`` in akopia.yaml (or the ``EMBEDDER_PROVIDER``
env fallback). All backends implement the ``EmbedderBackend`` protocol so
downstream code doesn't care whether vectors come from a local fastembed
ONNX runtime, an external Ollama box, or a hosted TEI/OpenAI endpoint.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class EmbedderBackend(Protocol):
    """Minimum contract every embedder backend must satisfy."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text."""
        ...

    def dim(self) -> int:
        """Vector dimensionality. Stable for the lifetime of the instance."""
        ...

    def model_id(self) -> str:
        """Human-readable model identifier for provenance stamping."""
        ...


def build_backend(
    provider: str,
    *,
    model: Optional[str] = None,
    quantized: Optional[bool] = None,
    url: Optional[str] = None,
    **extra,
) -> EmbedderBackend:
    """Factory: resolve ``provider`` to a concrete backend instance.

    Unknown providers raise ``ValueError`` so misconfiguration fails fast.
    """
    provider = (provider or "fastembed").lower()
    if provider == "fastembed":
        from embeddings.backends.fastembed_backend import FastEmbedBackend
        return FastEmbedBackend(model=model, quantized=quantized)
    if provider == "ollama":
        from embeddings.backends.ollama_backend import OllamaBackend
        return OllamaBackend(model=model, url=url)
    raise ValueError(
        f"unsupported embedder provider: {provider!r} "
        "(supported: fastembed, ollama)"
    )


__all__ = ["EmbedderBackend", "build_backend"]
