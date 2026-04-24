"""Router — dispatches ChangeEvents to the right ContentExtractor.

This is the module the RFC (docs/plugin-contracts.md) refers to as the
"Router" in the core pipeline. It replaces the modality-if-elif block
in ``concentrador.main.create_embedding_jobs`` by consulting the
plugin registry for an extractor that matches the incoming event.

Responsibilities:

- Decide which extractor handles a given ``ChangeEvent`` using MIME
  type (preferred) + file extension (fallback).
- Enforce cycle protection via ``event.exceeds_max_depth()`` before
  dispatching.
- Package the event + ``ContentRef`` into an ``extract-job`` message.
- Handle the complementary path: consume ``extract-results``, turn
  ``ExtractedContent`` into one or more ``EmbeddingJob``s, and
  republish any ``attached`` events with incremented depth.

Lives next to the legacy ``create_embedding_jobs`` during the cutover.
A feature flag (``router.enable_extractors`` in akopia.yaml, or the env
``AKOPIA_ROUTER_USE_EXTRACTORS=1``) controls which path runs at runtime.

Separated from ``concentrador.main`` so the routing logic is testable
without spinning up FastAPI / Redis.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from pathlib import PurePosixPath
from typing import Optional, Protocol

from common.chunker import chunk_text, get_chunker_config
from common.models import (
    ChangeEvent,
    Chunk,
    ContentRef,
    EmbeddingJob,
    EmbeddingModality,
    ExtractedContent,
    Modality,
)
from common.registry import PluginRegistry, default_registry

logger = logging.getLogger(__name__)


class ExtractorMatch(Protocol):
    """Minimal structural type we need from an extractor class.

    Lets the router work with any class that exposes the attributes
    documented in ``common.protocols.ContentExtractor`` without
    importing it (avoids circular imports and lets tests substitute
    simpler fakes).
    """
    plugin_id: str
    supported_mime_types: list[str]
    supported_extensions: list[str]
    priority: int


class Router:
    """Dispatch ChangeEvents → extract-jobs and extract-results → embedding-jobs.

    Holds no state beyond the plugin registry reference and a local
    MIME resolver. Safe to instantiate once at module import.
    """

    def __init__(self, registry: Optional[PluginRegistry] = None):
        self._registry = registry or default_registry
        # Ensure stdlib mimetypes knows common types we care about.
        # Idempotent; cheap.
        mimetypes.init()
        mimetypes.add_type("text/markdown", ".md")
        mimetypes.add_type("text/markdown", ".markdown")
        mimetypes.add_type("text/yaml", ".yaml")
        mimetypes.add_type("text/yaml", ".yml")
        mimetypes.add_type("text/x-python", ".py")

    # ── Routing ────────────────────────────────────────────────

    def pick_extractor(self, event: ChangeEvent) -> Optional[ExtractorMatch]:
        """Return the highest-priority extractor that handles this event.

        Matching rule:
          1. If ``event.content_mime`` is set, try exact MIME match first.
          2. Else/also: match by path extension (lowercased, with leading dot).
          3. Break ties by ``priority`` (higher wins).

        Returns ``None`` if no extractor is registered for this
        event — caller decides how to handle (legacy path / skip / error).
        """
        mime = (event.content_mime or "").lower()
        ext = ""
        if event.path:
            ext = PurePosixPath(event.path).suffix.lower()

        candidates: list[ExtractorMatch] = []
        for name in self._registry.list_extractors():
            cls = self._registry.get_extractor_class(name)
            handles = False
            if mime and mime in (m.lower() for m in getattr(cls, "supported_mime_types", [])):
                handles = True
            if not handles and ext and ext in (e.lower() for e in getattr(cls, "supported_extensions", [])):
                handles = True
            if handles:
                candidates.append(cls)
        if not candidates:
            return None
        candidates.sort(key=lambda c: getattr(c, "priority", 0), reverse=True)
        return candidates[0]

    def build_extract_job(self, event: ChangeEvent) -> Optional[dict]:
        """Produce the message payload for the ``extract-jobs`` stream.

        Returns ``None`` if the event shouldn't be extracted (deletes,
        over-depth, or modality the extractor layer can't handle like
        raw images going straight to CLIP).
        """
        if event.operation.value == "delete":
            return None
        if event.exceeds_max_depth():
            logger.warning(
                "dropping event %s at depth %d (max=%d)",
                event.event_id, event.depth, event.MAX_DEPTH,
            )
            return None
        # Image modality bypasses extractors entirely — CLIP takes pixels.
        if event.modality == Modality.IMAGE:
            return None
        if event.content_ref is None:
            logger.warning(
                "event %s has no content_ref; cannot dispatch to extractor",
                event.event_id,
            )
            return None
        extractor = self.pick_extractor(event)
        if extractor is None:
            logger.info(
                "no extractor matches event %s (mime=%s ext=%s) — skipping extract layer",
                event.event_id, event.content_mime, event.path,
            )
            return None
        return {
            "event": event.model_dump(mode="json"),
            "content_ref": event.content_ref.model_dump(mode="json"),
            "extractor_id": extractor.plugin_id,
        }

    # ── Extract result → EmbeddingJob ─────────────────────────

    def build_embedding_jobs(
        self,
        event: ChangeEvent,
        extracted: ExtractedContent,
        *,
        batch_id: Optional[str] = None,
    ) -> list[EmbeddingJob]:
        """Turn an ExtractedContent into chunk-sized EmbeddingJobs.

        Uses ``common.chunker.chunk_text`` to split on semantic boundaries.
        If the extractor produced ``pages``, chunks are scoped per-page
        so metadata stays accurate (citations can point at slide 3 of a
        pptx, sheet 2 of an xlsx).
        """
        batch_id = batch_id or str(uuid.uuid4())
        ccfg = get_chunker_config()
        if extracted.pages:
            jobs: list[EmbeddingJob] = []
            for page in extracted.pages:
                chunks = chunk_text(
                    page.text,
                    max_tokens=ccfg.chunk_size_tokens,
                    overlap_tokens=ccfg.overlap_tokens,
                    strategy=ccfg.strategy,
                    source_id=event.source_id,
                    path=f"{event.path}#page={page.index}",
                )
                if not chunks:
                    continue
                jobs.append(self._embedding_job(event, batch_id, chunks))
            return jobs
        chunks = chunk_text(
            extracted.text,
            max_tokens=ccfg.chunk_size_tokens,
            overlap_tokens=ccfg.overlap_tokens,
            strategy=ccfg.strategy,
            source_id=event.source_id,
            path=event.path,
        )
        if not chunks:
            return []
        return [self._embedding_job(event, batch_id, chunks)]

    def _embedding_job(
        self,
        event: ChangeEvent,
        batch_id: str,
        chunks: list[dict],
    ) -> EmbeddingJob:
        # Stamp content_modified_at onto every chunk so the embedder
        # carries it into EmbeddingEntry (and then into the indices).
        chunk_models: list[Chunk] = []
        for c in chunks:
            chunk = Chunk(**c)
            chunk.content_modified_at = event.content_modified_at
            chunk_models.append(chunk)
        return EmbeddingJob(
            batch_id=batch_id,
            idempotency_key=event.idempotency_key,
            source_id=event.source_id,
            modality=EmbeddingModality.TEXT,
            chunks=chunk_models,
            priority=event.priority,
        )

    # ── Attached content ──────────────────────────────────────

    def repoint_attached(
        self,
        attached: list[ChangeEvent],
        parent: ChangeEvent,
    ) -> list[ChangeEvent]:
        """Stamp attached events with parent identity + depth+1.

        Extractors may discover embedded assets (images inside a PDF,
        videos inside a pptx). They emit new ChangeEvents in
        ``ExtractedContent.attached``; the router is responsible for
        re-publishing them to ``change-events`` with correct lineage.
        """
        out: list[ChangeEvent] = []
        for child in attached:
            if not child.source_id:
                child.source_id = parent.source_id
            if not child.source_type:
                child.source_type = parent.source_type
            child.depth = parent.depth + 1
            child.compute_idempotency_key()
            if child.exceeds_max_depth():
                logger.warning(
                    "dropping attached %s from parent %s — depth %d exceeds max",
                    child.event_id, parent.event_id, child.depth,
                )
                continue
            out.append(child)
        return out


# ── Runtime switch helpers ─────────────────────────────────────


def extractor_routing_enabled() -> bool:
    """Feature flag.

    Slices 1-4 keep ``create_embedding_jobs`` (legacy) running for
    every event. When ``AKOPIA_ROUTER_USE_EXTRACTORS=1`` (or the akopia.yaml
    ``core.router.use_extractors: true`` once Slice 6 wires it), the
    change_event_consumer routes through the Router instead.

    Deliberately a runtime env check rather than a module-level constant
    so integration tests can flip it with ``monkeypatch.setenv``.
    """
    return os.environ.get("AKOPIA_ROUTER_USE_EXTRACTORS", "0") in ("1", "true", "yes")


__all__ = [
    "Router",
    "ExtractorMatch",
    "extractor_routing_enabled",
]
