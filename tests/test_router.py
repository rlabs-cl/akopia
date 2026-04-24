"""Tests for concentrador.router (Slice 5).

Exercise the routing logic in isolation: pick_extractor dispatch,
build_extract_job guards (delete / depth / image / no extractor),
build_embedding_jobs chunking (per-page vs single), and
repoint_attached lineage stamping.

Uses fake extractor classes registered in a local PluginRegistry to
avoid importing the real (dependency-heavy) extractors.
"""
from __future__ import annotations

import os

import pytest

from common.models import (
    ChangeEvent,
    ContentRef,
    ExtractedContent,
    Modality,
    Operation,
    Page,
    Priority,
)
from common.registry import PluginRegistry
from concentrador.router import Router, extractor_routing_enabled


# ── Fake extractors for routing tests ────────────────────────


class _PlainExtractor:
    plugin_id = "plain"
    supported_mime_types = ["text/plain", "text/markdown"]
    supported_extensions = [".txt", ".md"]
    priority = 0


class _OfficeExtractor:
    plugin_id = "office"
    supported_mime_types = []
    supported_extensions = [".docx", ".xlsx"]
    priority = 10


class _HTMLExtractor:
    plugin_id = "html"
    supported_mime_types = ["text/html"]
    supported_extensions = [".html"]
    priority = 20


def _router_with_extractors(*classes) -> Router:
    reg = PluginRegistry()
    for c in classes:
        reg.register_extractor(c.plugin_id, c)
    return Router(registry=reg)


def _mk_event(path: str, *, modality=Modality.TEXT, mime=None, depth=0,
              operation=Operation.ADD, content_ref=None) -> ChangeEvent:
    ev = ChangeEvent(
        source_id="inst-1",
        source_type="folder",
        operation=operation,
        path=path,
        modality=modality,
        content_mime=mime,
        depth=depth,
        content_ref=content_ref or ContentRef(kind="path", path=path),
    )
    ev.compute_idempotency_key()
    return ev


# ── pick_extractor ───────────────────────────────────────────


class TestPickExtractor:
    def test_match_by_extension(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("notes/a.md")
        hit = r.pick_extractor(ev)
        assert hit is _PlainExtractor

    def test_match_by_mime(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("unknown.xyz", mime="text/markdown")
        hit = r.pick_extractor(ev)
        assert hit is _PlainExtractor

    def test_priority_tie_break(self):
        """Two extractors match same ext → higher priority wins."""
        r = _router_with_extractors(_PlainExtractor, _HTMLExtractor)
        # Register a plain that also handles .html (low priority);
        # html extractor wins on .html via priority=20.
        plain_with_html = type("PlainHTML", (), dict(
            plugin_id="plain-html", supported_extensions=[".html", ".md"],
            supported_mime_types=[], priority=0,
        ))
        r = _router_with_extractors(plain_with_html, _HTMLExtractor)
        ev = _mk_event("page.html")
        hit = r.pick_extractor(ev)
        assert hit is _HTMLExtractor  # priority 20 > 0

    def test_no_match_returns_none(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("binary.bin")
        assert r.pick_extractor(ev) is None

    def test_mime_case_insensitive(self):
        r = _router_with_extractors(_HTMLExtractor)
        ev = _mk_event("page.html", mime="TEXT/HTML")
        assert r.pick_extractor(ev) is _HTMLExtractor

    def test_extension_case_insensitive(self):
        r = _router_with_extractors(_OfficeExtractor)
        ev = _mk_event("Report.DOCX")
        assert r.pick_extractor(ev) is _OfficeExtractor


# ── build_extract_job ─────────────────────────────────────────


class TestBuildExtractJob:
    def test_happy_path(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("x.md")
        job = r.build_extract_job(ev)
        assert job is not None
        assert job["extractor_id"] == "plain"
        assert job["event"]["event_id"] == ev.event_id
        assert job["content_ref"]["kind"] == "path"

    def test_delete_returns_none(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("x.md", operation=Operation.DELETE)
        assert r.build_extract_job(ev) is None

    def test_image_returns_none(self):
        """Image events bypass extractors — CLIP takes pixels directly."""
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("cat.jpg", modality=Modality.IMAGE)
        assert r.build_extract_job(ev) is None

    def test_over_max_depth_returns_none(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("x.md", depth=3)  # MAX_DEPTH default
        assert r.build_extract_job(ev) is None

    def test_no_content_ref_returns_none(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = ChangeEvent(
            source_id="i", source_type="folder", operation=Operation.ADD,
            path="x.md", modality=Modality.TEXT,
        )  # no content_ref
        assert r.build_extract_job(ev) is None

    def test_no_extractor_match_returns_none(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("binary.bin")
        assert r.build_extract_job(ev) is None


# ── build_embedding_jobs ──────────────────────────────────────


class TestBuildEmbeddingJobs:
    def test_flat_text_produces_one_job(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("a.md")
        ec = ExtractedContent(
            source_event_id=ev.event_id,
            text="hello world " * 20,  # small enough for 1 chunk
            extractor_id="plain",
        )
        jobs = r.build_embedding_jobs(ev, ec)
        assert len(jobs) == 1
        assert jobs[0].source_id == "inst-1"
        assert jobs[0].chunks[0].path == "a.md"

    def test_paged_content_one_job_per_page(self):
        r = _router_with_extractors(_OfficeExtractor)
        ev = _mk_event("deck.pptx")
        ec = ExtractedContent(
            source_event_id=ev.event_id,
            text="",  # ignored when pages set
            pages=[
                Page(index=0, text="slide 1 content " * 10),
                Page(index=1, text="slide 2 content " * 10),
                Page(index=2, text="slide 3 content " * 10),
            ],
            extractor_id="office",
        )
        jobs = r.build_embedding_jobs(ev, ec)
        assert len(jobs) == 3
        # Path carries the page marker so citations stay accurate.
        assert all("#page=" in jobs[i].chunks[0].path for i in range(3))
        assert "#page=0" in jobs[0].chunks[0].path
        assert "#page=2" in jobs[2].chunks[0].path

    def test_empty_text_produces_no_jobs(self):
        r = _router_with_extractors(_PlainExtractor)
        ev = _mk_event("empty.md")
        ec = ExtractedContent(
            source_event_id=ev.event_id, text="", extractor_id="plain",
        )
        assert r.build_embedding_jobs(ev, ec) == []

    def test_empty_pages_skipped_but_others_included(self):
        r = _router_with_extractors(_OfficeExtractor)
        ev = _mk_event("deck.pptx")
        ec = ExtractedContent(
            source_event_id=ev.event_id, text="",
            pages=[
                Page(index=0, text=""),                    # empty — skipped
                Page(index=1, text="content " * 20),       # kept
            ],
            extractor_id="office",
        )
        jobs = r.build_embedding_jobs(ev, ec)
        assert len(jobs) == 1
        assert "#page=1" in jobs[0].chunks[0].path


# ── repoint_attached ──────────────────────────────────────────


class TestRepointAttached:
    def test_stamps_parent_identity_and_depth(self):
        r = _router_with_extractors(_PlainExtractor)
        parent = _mk_event("doc.pdf", depth=0)
        child = ChangeEvent(
            source_id="",
            source_type="",
            operation=Operation.ADD,
            path="doc.pdf#img-0",
            modality=Modality.IMAGE,
        )
        out = r.repoint_attached([child], parent)
        assert len(out) == 1
        assert out[0].source_id == "inst-1"
        assert out[0].source_type == "folder"
        assert out[0].depth == 1
        assert out[0].idempotency_key != ""

    def test_drops_attached_exceeding_max_depth(self):
        r = _router_with_extractors(_PlainExtractor)
        parent = _mk_event("doc.pdf", depth=2)  # child will be depth=3 == MAX
        child = ChangeEvent(
            source_id="",
            source_type="",
            operation=Operation.ADD,
            path="doc.pdf#deeply-nested",
            modality=Modality.IMAGE,
        )
        out = r.repoint_attached([child], parent)
        assert out == []

    def test_preserves_existing_source_id_if_set(self):
        r = _router_with_extractors(_PlainExtractor)
        parent = _mk_event("doc.pdf", depth=0)
        child = ChangeEvent(
            source_id="preexisting",
            source_type="preexisting-type",
            operation=Operation.ADD,
            path="x",
            modality=Modality.TEXT,
        )
        out = r.repoint_attached([child], parent)
        # Repoint only fills blanks — doesn't overwrite.
        assert out[0].source_id == "preexisting"
        assert out[0].source_type == "preexisting-type"
        assert out[0].depth == 1


# ── Feature flag ──────────────────────────────────────────────


class TestFeatureFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("AKOPIA_ROUTER_USE_EXTRACTORS", raising=False)
        assert extractor_routing_enabled() is False

    def test_on_with_1(self, monkeypatch):
        monkeypatch.setenv("AKOPIA_ROUTER_USE_EXTRACTORS", "1")
        assert extractor_routing_enabled() is True

    def test_on_with_true(self, monkeypatch):
        monkeypatch.setenv("AKOPIA_ROUTER_USE_EXTRACTORS", "true")
        assert extractor_routing_enabled() is True

    def test_off_with_0(self, monkeypatch):
        monkeypatch.setenv("AKOPIA_ROUTER_USE_EXTRACTORS", "0")
        assert extractor_routing_enabled() is False
