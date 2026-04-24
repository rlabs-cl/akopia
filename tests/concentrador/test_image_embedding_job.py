"""create_embedding_jobs must emit IMAGE-modality jobs for image events.

Before this slice the image branch early-returned with a TODO and the
``akopia_image`` Qdrant collection therefore stayed empty end-to-end. These
tests pin the wired behaviour:

  1. ChangeEvent(modality=IMAGE) with a path-kind ContentRef → one
     EmbeddingJob with EmbeddingModality.IMAGE whose chunk.content is
     the absolute filesystem path the embedder will open.
  2. ChangeEvent(modality=IMAGE) without a usable ContentRef → skipped
     (empty job list, warning logged). Inline-bytes / url modes aren't
     wired yet; skipping here is intentional and loud.
  3. Text events still produce a TEXT-modality job (regression guard
     that the image branch didn't break the text branch).
"""
from __future__ import annotations

import logging

import pytest

from common.models import (
    ChangeEvent,
    ContentRef,
    EmbeddingModality,
    Modality,
    Operation,
)
from concentrador.main import create_embedding_jobs


def _image_event(
    *,
    source_id: str = "src-git-abc",
    rel_path: str = "docs/diagram.png",
    abs_path: str | None = "/tmp/akopia-repos/acme/docs/diagram.png",
) -> ChangeEvent:
    ref = ContentRef(kind="path", path=abs_path) if abs_path else None
    event = ChangeEvent(
        source_id=source_id,
        source_type="git",
        operation=Operation.ADD,
        path=rel_path,
        modality=Modality.IMAGE,
        content_hash="deadbeef",
        size_bytes=1024,
        content_ref=ref,
    )
    event.compute_idempotency_key()
    return event


def test_image_event_produces_image_job():
    event = _image_event()
    jobs = create_embedding_jobs(event, batch_id="b-1")
    assert len(jobs) == 1, "expected exactly one image embedding job"
    job = jobs[0]
    assert job.modality == EmbeddingModality.IMAGE
    assert job.source_id == event.source_id
    assert job.batch_id == "b-1"
    assert job.idempotency_key == event.idempotency_key
    assert len(job.chunks) == 1
    chunk = job.chunks[0]
    # chunk.content is what the embedder pods reads from disk.
    assert chunk.content == "/tmp/akopia-repos/acme/docs/diagram.png"
    # chunk.path stays as the logical source-relative path so search
    # results can cite the original location.
    assert chunk.path == "docs/diagram.png"
    assert chunk.chunk_index == 0
    assert chunk.total_chunks == 1


def test_image_event_without_content_ref_is_skipped(caplog):
    event = _image_event(abs_path=None)
    with caplog.at_level(logging.WARNING, logger="concentrador"):
        jobs = create_embedding_jobs(event, batch_id="b-2")
    assert jobs == []
    # Skip must be loud — silent drops are how the "akopia_image is empty"
    # bug stayed alive for months.
    assert any("no usable ContentRef" in rec.message for rec in caplog.records)


def test_image_event_with_nonpath_content_ref_is_skipped(caplog):
    """Inline bytes / url / object_storage aren't wired for images yet."""
    event = ChangeEvent(
        source_id="src-web-abc",
        source_type="web-single",
        operation=Operation.ADD,
        path="https://example.com/foo.png",
        modality=Modality.IMAGE,
        content_hash="cafebabe",
        size_bytes=2048,
        content_ref=ContentRef(kind="url", url="https://example.com/foo.png"),
    )
    event.compute_idempotency_key()
    with caplog.at_level(logging.WARNING, logger="concentrador"):
        jobs = create_embedding_jobs(event, batch_id="b-3")
    assert jobs == []
    assert any("no usable ContentRef" in rec.message for rec in caplog.records)


def test_text_event_still_produces_text_job_regression():
    """The image-branch fix must not regress the text branch."""
    event = ChangeEvent(
        source_id="src-git-xyz",
        source_type="git",
        operation=Operation.ADD,
        path="README.md",
        modality=Modality.TEXT,
        content_hash="feedface",
        size_bytes=256,
        content_ref=ContentRef(kind="path", path="/tmp/akopia-repos/x/README.md"),
    )
    event.derived_content = None
    event.compute_idempotency_key()
    jobs = create_embedding_jobs(event, batch_id="b-4")
    assert len(jobs) == 1
    assert jobs[0].modality == EmbeddingModality.TEXT


def test_image_job_carries_content_modified_at():
    """Freshness metadata must flow through the image branch too."""
    from datetime import datetime, timezone
    event = _image_event()
    event.content_modified_at = datetime(2026, 4, 23, tzinfo=timezone.utc)
    jobs = create_embedding_jobs(event, batch_id="b-5")
    assert jobs and jobs[0].chunks[0].content_modified_at == event.content_modified_at
