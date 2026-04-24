"""Tests for common.models extensions (Slice 1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.models import (
    ChangeEvent,
    ContentRef,
    ExtractedContent,
    HealthReport,
    HealthStatus,
    Modality,
    Operation,
    Page,
    Priority,
)


# ── ContentRef ──────────────────────────────────────────────────


class TestContentRef:
    def test_path_kind(self):
        ref = ContentRef(kind="path", path="/tmp/x.txt")
        assert ref.path == "/tmp/x.txt"

    def test_inline_text_kind(self):
        ref = ContentRef(kind="inline_text", text="hello")
        assert ref.text == "hello"

    def test_url_kind(self):
        ref = ContentRef(kind="url", url="https://example.com/a.pdf")
        assert ref.url.endswith(".pdf")

    def test_inline_bytes_kind(self):
        ref = ContentRef(kind="inline_bytes", bytes_b64="aGVsbG8=")
        assert ref.bytes_b64 == "aGVsbG8="

    def test_object_storage_kind(self):
        ref = ContentRef(kind="object_storage", bucket="kb", key="docs/x.pdf")
        assert (ref.bucket, ref.key) == ("kb", "docs/x.pdf")

    def test_path_kind_without_path_rejected(self):
        with pytest.raises(ValidationError):
            ContentRef(kind="path")

    def test_url_kind_without_url_rejected(self):
        with pytest.raises(ValidationError):
            ContentRef(kind="url")

    def test_object_storage_kind_without_both_rejected(self):
        with pytest.raises(ValidationError):
            ContentRef(kind="object_storage", bucket="kb")  # missing key


# ── Page / ExtractedContent ─────────────────────────────────────


class TestExtractedContent:
    def test_minimal(self):
        ec = ExtractedContent(source_event_id="abc", text="hello")
        assert ec.text == "hello"
        assert ec.pages is None
        assert ec.attached == []
        assert ec.processing_time_ms == 0

    def test_with_pages(self):
        ec = ExtractedContent(
            source_event_id="abc",
            text="full",
            pages=[
                Page(index=0, text="page 1"),
                Page(index=1, text="page 2", title="Chapter 1"),
            ],
        )
        assert len(ec.pages) == 2
        assert ec.pages[1].title == "Chapter 1"

    def test_with_attached_change_events(self):
        """Extractor can emit follow-up events (e.g. PDF with embedded images)."""
        attached = ChangeEvent(
            source_id="s1",
            source_type="pdf-text",
            operation=Operation.ADD,
            path="parent.pdf#img-0",
            modality=Modality.IMAGE,
            content_ref=ContentRef(kind="inline_bytes", bytes_b64="aGVsbG8="),
            depth=1,
        )
        ec = ExtractedContent(
            source_event_id="abc",
            text="doc with image",
            attached=[attached],
        )
        assert len(ec.attached) == 1
        assert ec.attached[0].depth == 1


# ── ChangeEvent extensions ──────────────────────────────────────


class TestChangeEvent:
    def _minimal_kwargs(self):
        return dict(
            source_id="s1",
            source_type="git",
            operation=Operation.ADD,
            path="src/a.py",
            modality=Modality.TEXT,
        )

    def test_legacy_fields_unchanged(self):
        e = ChangeEvent(**self._minimal_kwargs())
        assert e.depth == 0
        assert e.content_ref is None
        assert e.content_mime is None

    def test_idempotency_key_deterministic(self):
        a = ChangeEvent(**self._minimal_kwargs(), content_hash="abc")
        b = ChangeEvent(**self._minimal_kwargs(), content_hash="abc")
        assert a.compute_idempotency_key() == b.compute_idempotency_key()

    def test_idempotency_key_changes_with_content_hash(self):
        a = ChangeEvent(**self._minimal_kwargs(), content_hash="abc")
        b = ChangeEvent(**self._minimal_kwargs(), content_hash="xyz")
        assert a.compute_idempotency_key() != b.compute_idempotency_key()

    def test_depth_exceeds_max(self):
        e = ChangeEvent(**self._minimal_kwargs(), depth=3)
        assert e.exceeds_max_depth()

    def test_depth_under_max(self):
        e = ChangeEvent(**self._minimal_kwargs(), depth=2)
        assert not e.exceeds_max_depth()

    def test_with_content_ref(self):
        e = ChangeEvent(
            **self._minimal_kwargs(),
            content_ref=ContentRef(kind="inline_text", text="# Hello"),
            content_mime="text/markdown",
        )
        assert e.content_ref.kind == "inline_text"
        assert e.content_mime == "text/markdown"

    def test_source_type_accepts_free_string(self):
        """source_type must accept any plugin_id, not just the legacy enum."""
        e = ChangeEvent(
            **{**self._minimal_kwargs(), "source_type": "web-deep"},
        )
        assert e.source_type == "web-deep"


class TestHealthReport:
    def test_defaults(self):
        h = HealthReport(status=HealthStatus.HEALTHY)
        assert h.detail == ""
        assert h.checks == {}

    def test_with_checks(self):
        h = HealthReport(
            status=HealthStatus.DEGRADED,
            detail="upstream slow",
            checks={"redis": "ok", "upstream": "slow"},
        )
        assert h.checks["upstream"] == "slow"
