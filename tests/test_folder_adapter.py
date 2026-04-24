"""Tests for adapters.folder.FolderAdapter (Slice 3).

Exercises discover / watch / read against a real temp filesystem so the
glob and mtime logic is the production logic. The FakeRedis and
one-shot watch trick mirror tests/test_base_adapter.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapters.folder import FolderAdapter, _MODALITY_BY_EXT, _modality_for
from common.models import (
    ChangeEvent,
    Modality,
    Operation,
)
from common.registry import PluginRegistry


# ── Reused FakeRedis pattern (see tests/test_base_adapter.py) ──────

class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def ensure_stream_and_group(self, stream: str, group: str) -> None:
        self.groups.append((stream, group))

    async def publish(self, stream: str, payload: dict) -> None:
        self.published.append((stream, payload))

    async def close(self) -> None:
        self.closed = True


# ── Helpers ────────────────────────────────────────────────────────

async def _collect_events(
    adapter: FolderAdapter, limit: int | None = None, timeout: float = 2.0
) -> list[ChangeEvent]:
    """Run watch() for one pass and collect the events emitted.

    Sets poll_seconds high so we only get the first pass; then signals
    shutdown to unblock the loop (it checks _shutdown on entry).
    """
    source = None
    async for s in adapter.discover():
        source = s
        break
    assert source is not None

    events: list[ChangeEvent] = []

    async def _run():
        async for ev in adapter.watch(source):
            events.append(ev)
            if limit is not None and len(events) >= limit:
                adapter._shutdown.set()
                return
        # Watch finished on its own.

    task = asyncio.create_task(_run())
    # Let the first scan run, then signal shutdown so the `while not
    # _shutdown` loop exits cleanly at the next iteration.
    await asyncio.sleep(0.05)
    adapter._shutdown.set()
    await asyncio.wait_for(task, timeout=timeout)
    return events


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_bytes(content)


# ── Tests ──────────────────────────────────────────────────────────

class TestFolderAdapter:

    @pytest.mark.asyncio
    async def test_configure_requires_path(self):
        adapter = FolderAdapter(instance_id="t")
        with pytest.raises(ValueError):
            await adapter.configure({})

    @pytest.mark.asyncio
    async def test_configure_rejects_missing_path(self, tmp_path):
        adapter = FolderAdapter(instance_id="t")
        with pytest.raises(ValueError):
            await adapter.configure({"path": str(tmp_path / "does-not-exist")})

    @pytest.mark.asyncio
    async def test_discover_yields_single_source(self, tmp_path):
        adapter = FolderAdapter(instance_id="t")
        await adapter.configure({"path": str(tmp_path)})
        sources = [s async for s in adapter.discover()]
        assert len(sources) == 1
        assert sources[0].type == "folder"
        assert sources[0].source_id == "t"

    @pytest.mark.asyncio
    async def test_watch_emits_adds(self, tmp_path):
        _write(tmp_path / "a.md", "hello")
        _write(tmp_path / "sub" / "b.txt", "world")

        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({"path": str(tmp_path), "poll_seconds": 3600})

        events = await _collect_events(adapter)
        paths = sorted(e.path for e in events)
        ops = {e.operation for e in events}
        assert ops == {Operation.ADD}
        assert paths == ["a.md", "sub/b.txt"]
        # content_hash + size + content_ref all stamped
        for e in events:
            assert e.content_hash and len(e.content_hash) == 64
            assert e.size_bytes > 0
            assert e.content_ref is not None
            assert e.content_ref.kind == "path"

    @pytest.mark.asyncio
    async def test_watch_emits_modify_then_delete(self, tmp_path):
        f = tmp_path / "doc.md"
        _write(f, "v1")

        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({"path": str(tmp_path), "poll_seconds": 3600})

        # Pass 1: ADD
        events = await _collect_events(adapter)
        assert [e.operation for e in events] == [Operation.ADD]
        original_hash = events[0].content_hash

        # Prepare pass 2: overwrite (MODIFY)
        adapter._shutdown = asyncio.Event()  # reset shutdown
        # Ensure mtime_ns changes even on ultra-fast filesystems.
        import os, time
        time.sleep(0.01)
        _write(f, "v2 longer content")
        os.utime(f, ns=(time.time_ns(), time.time_ns()))

        events = await _collect_events(adapter)
        assert [e.operation for e in events] == [Operation.MODIFY]
        assert events[0].content_hash != original_hash

        # Pass 3: delete
        adapter._shutdown = asyncio.Event()
        f.unlink()
        events = await _collect_events(adapter)
        assert [e.operation for e in events] == [Operation.DELETE]
        assert events[0].path == "doc.md"

    @pytest.mark.asyncio
    async def test_include_exclude_globs(self, tmp_path):
        _write(tmp_path / "keep.md", "k")
        _write(tmp_path / "skip.log", "s")
        _write(tmp_path / "also.md", "a")
        _write(tmp_path / "node_modules" / "x.md", "x")

        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({
            "path": str(tmp_path),
            "include": ["*.md"],
            "exclude": ["node_modules/*", "node_modules/*/*"],
            "poll_seconds": 3600,
        })
        events = await _collect_events(adapter)
        paths = sorted(e.path for e in events)
        assert paths == ["also.md", "keep.md"]

    @pytest.mark.asyncio
    async def test_max_file_bytes_enforced(self, tmp_path):
        _write(tmp_path / "small.md", "hi")
        _write(tmp_path / "big.md", "x" * 5000)

        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({
            "path": str(tmp_path),
            "poll_seconds": 3600,
            "max_file_bytes": 100,
        })
        events = await _collect_events(adapter)
        paths = [e.path for e in events]
        assert "small.md" in paths
        assert "big.md" not in paths

    @pytest.mark.asyncio
    async def test_read_returns_bytes(self, tmp_path):
        _write(tmp_path / "r.md", b"bytes!")
        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({"path": str(tmp_path)})
        source = None
        async for s in adapter.discover():
            source = s
        assert await adapter.read(source, "r.md") == b"bytes!"

    @pytest.mark.asyncio
    async def test_read_rejects_path_escape(self, tmp_path):
        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({"path": str(tmp_path)})
        source = None
        async for s in adapter.discover():
            source = s
        with pytest.raises(ValueError):
            await adapter.read(source, "../outside.md")

    @pytest.mark.asyncio
    async def test_start_publishes_via_base(self, tmp_path):
        _write(tmp_path / "a.md", "hello")
        redis = _FakeRedis()
        adapter = FolderAdapter(instance_id="fs", redis_client=redis)

        # One-shot: override watch by setting poll ceiling via shutdown
        # from a sidecar task.
        async def _stopper():
            await asyncio.sleep(0.2)
            adapter._shutdown.set()
        stopper = asyncio.create_task(_stopper())
        await adapter.start({"path": str(tmp_path), "poll_seconds": 3600})
        await stopper

        published = [p for s, p in redis.published if s == "change-events"]
        assert len(published) == 1
        assert published[0]["source_id"] == "fs"
        assert published[0]["source_type"] == "folder"
        assert published[0]["idempotency_key"] != ""

    def test_modality_map(self):
        assert _modality_for("x.md") is Modality.TEXT
        assert _modality_for("x.py") is Modality.TEXT
        # PDFs flow as TEXT once extracted — no Modality.PDF anymore.
        assert _modality_for("x.pdf") is Modality.TEXT
        assert _modality_for("x.jpg") is Modality.IMAGE
        # Audio/video aren't wired end-to-end; unknown defaults to TEXT.
        assert _modality_for("x.mp3") is Modality.TEXT
        assert _modality_for("x.mp4") is Modality.TEXT
        # Unknown defaults to TEXT by design.
        assert _modality_for("x.unknown") is Modality.TEXT
        # Sanity: map is non-empty.
        assert ".md" in _MODALITY_BY_EXT

    def test_registry_manual_registration(self):
        r = PluginRegistry()
        r.register_source_adapter("folder", FolderAdapter)
        assert r.get_source_adapter_class("folder") is FolderAdapter
