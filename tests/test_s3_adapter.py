"""Tests for adapters.s3.S3Adapter.

Mirrors tests/test_folder_adapter.py: discover / watch / read are
exercised against a moto ThreadedMotoServer running in-process. We use
the threaded server (rather than the ``mock_aws`` decorator) because
moto's response-mock plumbing trips over aiobotocore's ``raw_headers``
expectations on this version combo; presenting a real HTTP S3 endpoint
sidesteps the issue and exercises the actual aioboto3 wire path. No
external network calls are made.
"""
from __future__ import annotations

import asyncio
import socket

import boto3
import pytest
from moto.server import ThreadedMotoServer

from adapters.s3 import S3Adapter, _modality_for
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


# ── Moto ThreadedMotoServer fixture ────────────────────────────────

_BUCKET = "akopia-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def moto_s3():
    """Start a moto S3 server and yield (endpoint_url, sync_boto3_client).

    The boto3 client is sync (handy for arrange-side fixture writes);
    the S3Adapter under test creates its own aioboto3 client pointed at
    the same endpoint.
    """
    # moto's S3 backend is a module-level singleton; reset before each
    # test so leftover objects from previous tests don't bleed into
    # the listing.
    from moto.s3.models import s3_backends
    s3_backends.reset()

    port = _free_port()
    server = ThreadedMotoServer(port=port)
    server.start()
    endpoint = f"http://127.0.0.1:{port}"
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    s3.create_bucket(Bucket=_BUCKET)
    try:
        yield endpoint, s3
    finally:
        server.stop()
        s3_backends.reset()


# ── Helpers ────────────────────────────────────────────────────────


def _make_adapter(*, instance_id: str = "s3test", redis_client=None) -> S3Adapter:
    return S3Adapter(instance_id=instance_id, redis_client=redis_client)


async def _configure(
    adapter: S3Adapter,
    endpoint: str,
    *,
    prefix: str = "",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_object_bytes: int = 25 * 1024 * 1024,
    poll_seconds: int = 3600,
) -> None:
    await adapter.configure(
        {
            "endpoint_url": endpoint,
            "bucket": _BUCKET,
            "prefix": prefix,
            "access_key": "test",
            "secret_key": "test",
            "region": "us-east-1",
            "use_ssl": False,
            "include": include or ["*"],
            "exclude": exclude or [],
            "poll_seconds": poll_seconds,
            "max_object_bytes": max_object_bytes,
        }
    )


def _put(s3, key: str, body: bytes | str) -> None:
    if isinstance(body, str):
        body = body.encode()
    s3.put_object(Bucket=_BUCKET, Key=key, Body=body)


async def _collect_events(
    adapter: S3Adapter, limit: int | None = None, timeout: float = 5.0
) -> list[ChangeEvent]:
    """Run watch() for one pass and collect the events emitted."""
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

    task = asyncio.create_task(_run())
    # Let the first scan run; then signal shutdown so the loop exits
    # at the next iteration.
    await asyncio.sleep(0.2)
    adapter._shutdown.set()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
    return events


# ── Tests ──────────────────────────────────────────────────────────


class TestS3AdapterConfigure:

    @pytest.mark.asyncio
    async def test_configure_requires_endpoint(self):
        adapter = _make_adapter()
        with pytest.raises(ValueError):
            await adapter.configure(
                {
                    "bucket": _BUCKET,
                    "access_key": "k",
                    "secret_key": "s",
                }
            )

    @pytest.mark.asyncio
    async def test_configure_requires_bucket(self):
        adapter = _make_adapter()
        with pytest.raises(ValueError):
            await adapter.configure(
                {
                    "endpoint_url": "http://x",
                    "access_key": "k",
                    "secret_key": "s",
                }
            )

    @pytest.mark.asyncio
    async def test_configure_requires_credentials(self):
        adapter = _make_adapter()
        with pytest.raises(ValueError):
            await adapter.configure(
                {
                    "endpoint_url": "http://x",
                    "bucket": _BUCKET,
                    "secret_key": "s",
                }
            )


class TestS3Adapter:

    @pytest.mark.asyncio
    async def test_discover_yields_single_source_with_uri(self, moto_s3):
        endpoint, _ = moto_s3
        adapter = _make_adapter()
        await _configure(adapter, endpoint, prefix="incidents/")
        sources = [s async for s in adapter.discover()]
        assert len(sources) == 1
        assert sources[0].source_id == "s3test"
        assert sources[0].type == "s3"
        assert sources[0].url == f"s3://{_BUCKET}/incidents/"
        assert sources[0].name == f"s3://{_BUCKET}/incidents/"

    @pytest.mark.asyncio
    async def test_discover_no_prefix_uri(self, moto_s3):
        endpoint, _ = moto_s3
        adapter = _make_adapter()
        await _configure(adapter, endpoint)
        sources = [s async for s in adapter.discover()]
        assert sources[0].url == f"s3://{_BUCKET}"

    @pytest.mark.asyncio
    async def test_watch_emits_adds(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "a.md", "hello")
        _put(s3, "sub/b.txt", "world")

        adapter = _make_adapter()
        await _configure(adapter, endpoint)
        events = await _collect_events(adapter)

        paths = sorted(e.path for e in events)
        ops = {e.operation for e in events}
        assert ops == {Operation.ADD}
        assert paths == ["a.md", "sub/b.txt"]
        for e in events:
            assert e.content_hash and len(e.content_hash) == 64
            assert e.size_bytes > 0
            assert e.content_ref is not None
            assert e.content_ref.kind == "object_storage"
            assert e.content_ref.bucket == _BUCKET
            assert e.content_modified_at is not None
            assert e.content_modified_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_watch_emits_modify_on_etag_change(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "doc.md", "v1")

        adapter = _make_adapter()
        await _configure(adapter, endpoint)
        pass1 = await _collect_events(adapter)
        assert [e.operation for e in pass1] == [Operation.ADD]
        original_hash = pass1[0].content_hash

        adapter._shutdown = asyncio.Event()
        _put(s3, "doc.md", "v2 longer content")
        pass2 = await _collect_events(adapter)
        assert [e.operation for e in pass2] == [Operation.MODIFY]
        assert pass2[0].content_hash != original_hash

    @pytest.mark.asyncio
    async def test_watch_emits_delete_when_object_disappears(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "doc.md", "v1")

        adapter = _make_adapter()
        await _configure(adapter, endpoint)
        pass1 = await _collect_events(adapter)
        assert [e.operation for e in pass1] == [Operation.ADD]

        adapter._shutdown = asyncio.Event()
        s3.delete_object(Bucket=_BUCKET, Key="doc.md")
        pass2 = await _collect_events(adapter)
        assert [e.operation for e in pass2] == [Operation.DELETE]
        assert pass2[0].path == "doc.md"

    @pytest.mark.asyncio
    async def test_include_exclude_globs(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "keep.md", "k")
        _put(s3, "skip.log", "s")
        _put(s3, "also.md", "a")
        _put(s3, "node_modules/x.md", "x")

        adapter = _make_adapter()
        await _configure(
            adapter,
            endpoint,
            include=["*.md"],
            exclude=["node_modules/*", "node_modules/*/*"],
        )
        events = await _collect_events(adapter)
        paths = sorted(e.path for e in events)
        assert paths == ["also.md", "keep.md"]

    @pytest.mark.asyncio
    async def test_prefix_strips_relative_paths(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "incidents/2026/inc-1.json", "{}")
        _put(s3, "incidents/2026/inc-2.json", "{}")
        _put(s3, "other/ignored.json", "{}")

        adapter = _make_adapter()
        await _configure(adapter, endpoint, prefix="incidents/")
        events = await _collect_events(adapter)
        paths = sorted(e.path for e in events)
        assert paths == ["2026/inc-1.json", "2026/inc-2.json"]
        for e in events:
            assert e.content_ref.key.startswith("incidents/")

    @pytest.mark.asyncio
    async def test_max_object_bytes_skips_oversized(self, moto_s3, caplog):
        endpoint, s3 = moto_s3
        _put(s3, "small.md", "hi")
        _put(s3, "big.md", "x" * 5000)

        adapter = _make_adapter()
        await _configure(adapter, endpoint, max_object_bytes=100)
        with caplog.at_level("WARNING"):
            events = await _collect_events(adapter)
        paths = [e.path for e in events]
        assert "small.md" in paths
        assert "big.md" not in paths
        assert any("big.md" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_read_returns_object_bytes(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "incidents/r.json", b"bytes!")

        adapter = _make_adapter()
        await _configure(adapter, endpoint, prefix="incidents/")
        source = None
        async for s in adapter.discover():
            source = s
        assert await adapter.read(source, "r.json") == b"bytes!"

    @pytest.mark.asyncio
    async def test_read_skips_when_oversized(self, moto_s3, caplog):
        endpoint, s3 = moto_s3
        _put(s3, "big.md", b"x" * 5000)

        adapter = _make_adapter()
        await _configure(adapter, endpoint, max_object_bytes=100)
        source = None
        async for s in adapter.discover():
            source = s
        with caplog.at_level("WARNING"):
            body = await adapter.read(source, "big.md")
        assert body == b""
        assert any("big.md" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_start_publishes_via_base(self, moto_s3):
        endpoint, s3 = moto_s3
        _put(s3, "a.md", "hello")

        redis = _FakeRedis()
        adapter = S3Adapter(instance_id="s3fs", redis_client=redis)

        async def _stopper():
            await asyncio.sleep(0.5)
            adapter._shutdown.set()

        stopper = asyncio.create_task(_stopper())
        await adapter.start(
            {
                "endpoint_url": endpoint,
                "bucket": _BUCKET,
                "access_key": "test",
                "secret_key": "test",
                "region": "us-east-1",
                "use_ssl": False,
                "poll_seconds": 3600,
            }
        )
        await stopper

        published = [p for s, p in redis.published if s == "change-events"]
        assert len(published) == 1
        assert published[0]["source_id"] == "s3fs"
        assert published[0]["source_type"] == "s3"
        assert published[0]["idempotency_key"] != ""
        assert published[0]["content_ref"]["kind"] == "object_storage"

    def test_modality_map(self):
        assert _modality_for("incidents/x.json") is Modality.TEXT
        assert _modality_for("x.md") is Modality.TEXT
        assert _modality_for("x.pdf") is Modality.TEXT
        assert _modality_for("x.png") is Modality.IMAGE
        assert _modality_for("x.unknown") is Modality.TEXT

    def test_registry_manual_registration(self):
        r = PluginRegistry()
        r.register_source_adapter("s3", S3Adapter)
        assert r.get_source_adapter_class("s3") is S3Adapter
