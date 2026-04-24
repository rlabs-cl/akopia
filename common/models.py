"""Shared data models for Akopia messaging.

See docs/plugin-contracts.md for the RFC that drove the ContentRef +
ExtractedContent additions. These types are the wire format between the
plugin layers (sources → router → extractors → embedders).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Operation(str, Enum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class Modality(str, Enum):
    """Modalities fully wired end-to-end.

    Intentionally narrow: only ``text`` and ``image`` flow through the
    whole pipeline today. PDF/audio/video used to live here but were
    cargo-cult — PDFs extract to TEXT, audio/video never had a working
    embedder branch. Extending this enum is the first step of the
    checklist in ``docs/adding-a-modality.md``.
    """

    TEXT = "text"
    IMAGE = "image"


class EmbeddingModality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO_TRANSCRIPT = "audio_transcript"
    VIDEO_TRANSCRIPT = "video_transcript"


class Priority(str, Enum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    CRON = "cron"


class SourceStatus(str, Enum):
    IDLE = "idle"
    DETECTING = "detecting"
    PREPROCESSING = "preprocessing"
    INDEXING = "indexing"
    ERROR = "error"


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class DerivedContent(BaseModel):
    """Legacy field retained for backward compat during the migration.

    New code should use ExtractedContent emitted by a ContentExtractor
    plugin. This class lingers because the current concentrador still
    reads ocr_text / transcript_segments inline. Deleted in the final
    refactor slice once all modalities flow through extractors.
    """
    ocr_text: Optional[str] = None
    transcript_segments: Optional[list[TranscriptSegment]] = None
    keyframe_paths: Optional[list[str]] = None


# ── Content retrieval strategy (RFC plugin-contracts §Data model) ──

class ContentRef(BaseModel):
    """How to retrieve the bytes backing a ChangeEvent.

    Sources emit one of five retrieval strategies so downstream
    extractors know where to go for the payload. Inline modes are for
    small bodies (<1 MiB); path/url/object_storage are for anything
    larger.
    """

    kind: Literal["path", "inline_bytes", "inline_text", "url", "object_storage"]
    path: Optional[str] = None            # kind=path     → local filesystem
    bytes_b64: Optional[str] = None       # kind=inline_bytes (<1 MiB)
    text: Optional[str] = None            # kind=inline_text (already-extracted)
    url: Optional[str] = None             # kind=url      → HTTP fetch
    bucket: Optional[str] = None          # kind=object_storage → S3/MinIO
    key: Optional[str] = None

    @model_validator(mode="after")
    def _one_payload_field(self) -> "ContentRef":
        required = {
            "path": self.path,
            "inline_bytes": self.bytes_b64,
            "inline_text": self.text,
            "url": self.url,
            "object_storage": self.bucket and self.key,
        }
        if not required[self.kind]:
            raise ValueError(
                f"ContentRef(kind={self.kind!r}) requires the matching field "
                "to be set (path, bytes_b64, text, url, or bucket+key)."
            )
        return self


class Page(BaseModel):
    """One logical unit inside a multi-page extractor output.

    PDFs use pages, PPTX uses slides, XLSX uses sheets. All map onto
    this model with a monotonic 0-based index.
    """

    index: int
    text: str
    title: Optional[str] = None


class ExtractedContent(BaseModel):
    """Result of a ContentExtractor run.

    Consumed by the router to produce chunks and trigger embedding.
    The ``attached`` field lets extractors emit follow-up ChangeEvents
    (e.g. images found inside a PDF); the router republishes them to
    ``change-events`` with depth incremented to prevent infinite cycles.
    """

    source_event_id: str
    text: str
    pages: Optional[list[Page]] = None
    structure: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    attached: list["ChangeEvent"] = Field(default_factory=list)
    extractor_id: str = ""            # plugin_id of the extractor that produced this
    extractor_version: str = ""       # semantic version; used for re-index invalidation
    processing_time_ms: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Health & lifecycle ─────────────────────────────────────────────

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # working but impaired (e.g. slow upstream)
    UNHEALTHY = "unhealthy"
    STARTING = "starting"


class HealthReport(BaseModel):
    status: HealthStatus
    detail: str = ""
    checks: dict[str, str] = Field(default_factory=dict)  # sub-check name → status


class ChangeEvent(BaseModel):
    """Emitted by a SourceAdapter. Describes "something exists/changed".

    Flows through the ``change-events`` Redis stream, consumed by the
    router which decides which extractor (if any) to dispatch to, then
    produces EmbeddingJob(s). See docs/plugin-contracts.md §Data model.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = ""
    source_id: str
    source_type: str                                # NB: was SourceType enum; now free string (plugin_id)
    operation: Operation
    path: str                                        # logical path (URL for web, fs path for folder)
    old_path: Optional[str] = None
    modality: Modality
    content_hash: Optional[str] = None
    size_bytes: int = 0
    priority: Priority = Priority.CRON
    commit_sha: Optional[str] = None                # git-specific; optional for other sources
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    retry_count: int = 0

    # ── Freshness metadata (2026-04-22) ────────────────────────────────
    # When the upstream content itself was last modified. Distinct from
    # ``timestamp`` (which is when kb observed the change). Populated by
    # adapters per source type:
    #   - folder: p.stat().st_mtime
    #   - git:    last commit time touching this specific file
    #   - web:    HTTP Last-Modified header
    # Used by the search layer for ``max_age_days`` hard filter and
    # ``freshness_boost`` soft re-ranking.
    content_modified_at: Optional[datetime] = None

    # ── Plugin-contracts additions (RFC 2026-04-22) ───────────────────
    content_ref: Optional[ContentRef] = None        # how to retrieve bytes
    content_mime: Optional[str] = None              # explicit MIME override
    depth: int = 0                                   # cycle-protection counter
                                                     # incremented by extractors on
                                                     # attached content; router
                                                     # rejects events at depth >= MAX
    MAX_DEPTH: int = Field(default=3, exclude=True)

    # Legacy — kept for backward compat during refactor
    derived_content: Optional[DerivedContent] = None

    def compute_idempotency_key(self) -> str:
        """Deterministic hash over identity fields.

        Stable across retries and across extractor versions (extractor
        output is cached separately by content_hash, not by this key).
        """
        raw = f"{self.source_id}:{self.path}:{self.content_hash or ''}:{self.modality.value}"
        self.idempotency_key = hashlib.sha256(raw.encode()).hexdigest()
        return self.idempotency_key

    def exceeds_max_depth(self) -> bool:
        return self.depth >= self.MAX_DEPTH


class Chunk(BaseModel):
    chunk_id: str
    content: str
    path: str
    repo: Optional[str] = None
    chunk_index: int = 0
    total_chunks: int = 1
    # Carried from the originating ChangeEvent so freshness metadata
    # survives the chunker boundary.
    content_modified_at: Optional[datetime] = None


class EmbeddingJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    batch_id: str = ""
    idempotency_key: str = ""
    source_id: str
    modality: EmbeddingModality
    derived_from: Optional[str] = None
    chunks: list[Chunk]
    priority: Priority = Priority.CRON
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    retry_count: int = 0


class EmbeddingEntry(BaseModel):
    chunk_id: str
    vector: list[float]
    model: str
    modality: str
    path: str
    repo: Optional[str] = None
    source_id: str
    derived_from: Optional[str] = None
    snippet: str = ""
    # Propagated through the pipeline for index-time freshness storage.
    content_modified_at: Optional[datetime] = None


class EmbeddingResult(BaseModel):
    job_id: str
    batch_id: str
    idempotency_key: str
    status: str  # success, partial_failure, failure
    embeddings: list[EmbeddingEntry] = []
    error: Optional[str] = None
    processing_time_ms: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Source(BaseModel):
    source_id: str
    # Free-form string (plugin_id of the adapter, e.g. "git", "folder",
    # "web-deep"). Was a closed enum (SourceType) but BaseSourceAdapter
    # already overwrote event.source_type with self.plugin_id, so the
    # enum carried no behaviour — just a trap for new source types.
    type: str
    name: str
    url: Optional[str] = None
    branch: str = "main"
    folder_path: Optional[str] = None
    status: SourceStatus = SourceStatus.IDLE
    last_sync: Optional[str] = None
    indexed_files: int = 0
    pending_jobs: int = 0
    sync_cron: str = "*/5 * * * *"
    file_patterns: list[str] = Field(default_factory=lambda: ["**/*"])


# Resolve forward references so ExtractedContent.attached: list[ChangeEvent]
# typed correctly after the whole module is loaded.
ExtractedContent.model_rebuild()


__all__ = [
    # Legacy enums + models
    "Operation", "Modality", "EmbeddingModality", "Priority",
    "SourceStatus",
    "TranscriptSegment", "DerivedContent",
    "ChangeEvent", "Chunk",
    "EmbeddingJob", "EmbeddingEntry", "EmbeddingResult",
    "Source",
    # Plugin-contracts additions
    "ContentRef", "Page", "ExtractedContent",
    "HealthStatus", "HealthReport",
]
