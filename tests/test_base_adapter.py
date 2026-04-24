"""Tests for BaseSourceAdapter (Slice 1).

These exercise the framework class with a minimal concrete adapter and
an in-memory fake for the Redis client, avoiding any network I/O.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)


# ── Fake redis ─────────────────────────────────────────────────


class _FakeRedis:
    """In-memory stand-in for RedisClient used by BaseSourceAdapter."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def ensure_stream_and_group(self, stream: str, group: str) -> None:
        self.groups.append((stream, group))

    async def publish(self, stream: str, payload: dict) -> None:
        self.published.append((stream, payload))

    async def close(self) -> None:
        self.closed = True


# ── Minimal concrete adapter ───────────────────────────────────


class _OneShotAdapter(BaseSourceAdapter):
    plugin_id = "oneshot"

    def __init__(self, events_to_emit: list[ChangeEvent], **kwargs):
        super().__init__(**kwargs)
        self._events = events_to_emit
        self.configured = False

    async def configure(self, config: dict) -> None:
        self.config = config
        self.configured = True

    async def discover(self) -> AsyncIterator[Source]:
        yield Source(source_id="root", type="folder", name="fake")

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        for e in self._events:
            yield e
        # Then signal shutdown so start() returns
        self._shutdown.set()

    async def read(self, source: Source, path: str) -> bytes:
        return b"not used in this test"


# ── Tests ──────────────────────────────────────────────────────


class TestBaseSourceAdapter:
    def _make_event(self, path: str) -> ChangeEvent:
        return ChangeEvent(
            source_id="will-be-overwritten",
            source_type="",  # also overwritten by base
            operation=Operation.ADD,
            path=path,
            modality=Modality.TEXT,
            content_hash=f"hash-{path}",
        )

    def test_plugin_id_required(self):
        class NoId(BaseSourceAdapter):
            async def configure(self, config): pass
            async def discover(self): yield
            async def watch(self, source): yield
            async def read(self, source, path): return b""

        with pytest.raises(TypeError):
            NoId(instance_id="x")

    @pytest.mark.asyncio
    async def test_start_configures_discovers_publishes(self):
        events = [self._make_event(f"file-{i}.md") for i in range(3)]
        redis = _FakeRedis()
        adapter = _OneShotAdapter(events, instance_id="demo", redis_client=redis)

        await adapter.start({"foo": "bar"})

        # configure() ran
        assert adapter.configured
        assert adapter.config == {"foo": "bar"}
        # ensure_stream_and_group was called for change-events
        assert any(stream == "change-events" for stream, _ in redis.groups)
        # All events published to change-events
        change_events_published = [
            p for stream, p in redis.published if stream == "change-events"
        ]
        assert len(change_events_published) == 3

        # Each published event had source_id stamped by the base (not by plugin)
        for published in change_events_published:
            assert published["source_id"] == "demo"
            assert published["source_type"] == "oneshot"
            # idempotency_key filled automatically
            assert published["idempotency_key"] != ""

        # Metrics reflect publishing
        assert adapter.metrics["events_published"] == 3
        assert adapter.metrics["discover_calls"] == 1

    @pytest.mark.asyncio
    async def test_health_reflects_shutdown(self):
        events: list[ChangeEvent] = []
        redis = _FakeRedis()
        adapter = _OneShotAdapter(events, instance_id="demo", redis_client=redis)

        # Not shut down
        report = await adapter.health()
        assert report.status.value == "healthy"

        # Simulate shutdown
        adapter._shutdown.set()
        report = await adapter.health()
        assert report.status.value == "unhealthy"

    @pytest.mark.asyncio
    async def test_make_change_event_helper(self):
        adapter = _OneShotAdapter([], instance_id="demo")
        event = adapter._make_change_event(
            path="docs/x.md",
            operation=Operation.ADD,
            modality=Modality.TEXT,
            content_hash="abc123",
            size_bytes=42,
            content_ref=ContentRef(kind="inline_text", text="# hi"),
        )
        assert event.source_id == "demo"
        assert event.source_type == "oneshot"
        assert event.idempotency_key != ""
        assert event.content_ref.kind == "inline_text"
