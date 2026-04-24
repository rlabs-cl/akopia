"""Typed Pydantic models for akopia.yaml (version 1).

See docs/plugin-contracts.md §Configuration for the RFC that drove these
shapes. JSON Schema in `common/akopia_schema.json` is the authoritative
wire contract; these models are the in-process representation used by
the core and plugins.

`config` dicts on sources/extractors are intentionally untyped here:
each plugin validates its own config in `configure()`.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base class: forbid unknown fields at the top level (matches JSON
    Schema's `additionalProperties: false`).
    """

    model_config = ConfigDict(extra="forbid")


class VectorStorageConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    url: str
    collection_prefix: Optional[str] = None


class LexicalStorageConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    url: str
    master_key: Optional[str] = None


class QueueStorageConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    url: str


class StorageConfig(_StrictModel):
    vector: VectorStorageConfig
    lexical: LexicalStorageConfig
    queue: QueueStorageConfig


class TextEmbeddingConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    provider: Optional[str] = None
    model: Optional[str] = None
    quantized: Optional[bool] = None


class ImageEmbeddingConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None


class EmbeddingsConfig(_StrictModel):
    text: TextEmbeddingConfig
    image: ImageEmbeddingConfig


class RouterConfig(_StrictModel):
    max_event_depth: int = 3
    idempotency_ttl: str = "7d"


class ChunkerConfig(_StrictModel):
    """Chunker settings threaded through every ``chunk_text()`` call.

    Defaults match the historical hard-coded values (recursive, 512 max,
    50 overlap). Surfaced here so akopia.yaml can actually tune them
    without patching the router.
    """

    strategy: str = "recursive"
    chunk_size_tokens: int = 512
    overlap_tokens: int = 50


class AuthConfig(_StrictModel):
    model_config = ConfigDict(extra="allow")

    mode: str
    token: Optional[str] = None


class CoreConfig(_StrictModel):
    storage: StorageConfig
    embeddings: EmbeddingsConfig
    router: RouterConfig
    auth: AuthConfig
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)


class SourceInstance(_StrictModel):
    """One configured source adapter instance (e.g. one git repo, one folder).

    `config` is plugin-specific and validated by the plugin's `configure()`.
    """

    id: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class ExtractorInstance(_StrictModel):
    """One configured extractor instance.

    Extractors typically have one instance per type (unlike sources which
    are often multi-instance). Therefore no `id` field.
    """

    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class AkopiaConfig(_StrictModel):
    """Root of akopia.yaml. Version 1."""

    version: int
    core: CoreConfig
    sources: list[SourceInstance]
    extractors: list[ExtractorInstance]
