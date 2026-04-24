"""ChangeEvent / Chunk / EmbeddingEntry serialize ``content_modified_at``.

Guards the freshness metadata wire contract: the field has to survive a
``model_dump(mode='json')`` → ``Model(**payload)`` round-trip because it
flows through Redis streams as JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.models import (
    ChangeEvent,
    Chunk,
    EmbeddingEntry,
    Modality,
    Operation,
)


def _base_event(**over) -> ChangeEvent:
    kwargs = dict(
        source_id="s",
        source_type="folder",
        operation=Operation.ADD,
        path="a.md",
        modality=Modality.TEXT,
    )
    kwargs.update(over)
    return ChangeEvent(**kwargs)


class TestChangeEventContentModified:
    def test_defaults_none(self):
        ev = _base_event()
        assert ev.content_modified_at is None

    def test_accepts_datetime(self):
        dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        ev = _base_event(content_modified_at=dt)
        assert ev.content_modified_at == dt

    def test_json_roundtrip_preserves_value(self):
        dt = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
        ev = _base_event(content_modified_at=dt)
        payload = ev.model_dump(mode="json")
        assert isinstance(payload["content_modified_at"], str)
        restored = ChangeEvent(**payload)
        assert restored.content_modified_at == dt

    def test_json_roundtrip_missing_field_stays_none(self):
        ev = _base_event()
        payload = ev.model_dump(mode="json")
        # pydantic emits the key with value None; bwd-compat path must
        # also work when the key is missing entirely (older producers).
        payload.pop("content_modified_at", None)
        restored = ChangeEvent(**payload)
        assert restored.content_modified_at is None


class TestChunkEmbeddingEntry:
    def test_chunk_accepts_field(self):
        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        c = Chunk(chunk_id="x", content="t", path="p", content_modified_at=dt)
        assert c.content_modified_at == dt

    def test_embedding_entry_accepts_field(self):
        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        e = EmbeddingEntry(
            chunk_id="x",
            vector=[0.0],
            model="m",
            modality="text",
            path="p",
            source_id="s",
            content_modified_at=dt,
        )
        assert e.content_modified_at == dt
        payload = e.model_dump(mode="json")
        assert isinstance(payload["content_modified_at"], str)
        restored = EmbeddingEntry(**payload)
        assert restored.content_modified_at == dt
