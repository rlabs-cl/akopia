"""FolderAdapter stamps ``content_modified_at`` from the file's mtime.

Uses a real temp dir with ``os.utime`` to pin the mtime precisely, then
asserts the emitted ChangeEvent's ``content_modified_at`` matches
(within ~1 ms to absorb nanosecond→float conversion).
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.folder import FolderAdapter, _file_mtime_utc
from common.models import ChangeEvent, Operation


# ── Reused FakeRedis pattern ────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def connect(self) -> None: ...
    async def ensure_stream_and_group(self, *a, **kw) -> None: ...
    async def publish(self, stream: str, payload: dict) -> None:
        self.published.append((stream, payload))
    async def close(self) -> None: ...


async def _collect_one(adapter: FolderAdapter, timeout: float = 2.0) -> list[ChangeEvent]:
    source = None
    async for s in adapter.discover():
        source = s
        break
    events: list[ChangeEvent] = []

    async def _run():
        async for ev in adapter.watch(source):
            events.append(ev)
    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    adapter._shutdown.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
    return events


class TestFolderMtime:
    def test_file_mtime_utc_returns_aware(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("x")
        dt = _file_mtime_utc(p)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_file_mtime_utc_none_on_missing(self, tmp_path):
        p = tmp_path / "does-not-exist"
        assert _file_mtime_utc(p) is None

    @pytest.mark.asyncio
    async def test_adapter_stamps_mtime(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("hello")
        # Pin mtime to a known epoch (2024-06-15T12:00:00Z).
        target_epoch = 1718452800
        os.utime(f, (target_epoch, target_epoch))

        adapter = FolderAdapter(instance_id="fs", redis_client=_FakeRedis())
        await adapter.configure({"path": str(tmp_path), "poll_seconds": 3600})
        events = await _collect_one(adapter)

        assert len(events) == 1
        ev = events[0]
        assert ev.operation is Operation.ADD
        assert ev.content_modified_at is not None
        assert ev.content_modified_at.tzinfo is not None
        # Allow ~1s tolerance (float mtime quantisation on some FSes).
        expected = datetime.fromtimestamp(target_epoch, tz=timezone.utc)
        delta = abs((ev.content_modified_at - expected).total_seconds())
        assert delta < 1.0, f"mtime drift too large: {delta}s"

    @pytest.mark.asyncio
    async def test_modify_updates_mtime(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("v1")
        os.utime(f, (1_700_000_000, 1_700_000_000))

        adapter = FolderAdapter(instance_id="fs")
        await adapter.configure({"path": str(tmp_path), "poll_seconds": 3600})
        pass1 = await _collect_one(adapter)
        assert pass1[0].content_modified_at.timestamp() == pytest.approx(
            1_700_000_000, abs=1.0
        )

        # Second pass: overwrite with a newer mtime.
        adapter._shutdown = asyncio.Event()
        f.write_text("v2 longer")
        os.utime(f, (1_800_000_000, 1_800_000_000))
        pass2 = await _collect_one(adapter)
        assert pass2[0].operation is Operation.MODIFY
        assert pass2[0].content_modified_at.timestamp() == pytest.approx(
            1_800_000_000, abs=1.0
        )
