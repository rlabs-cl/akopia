"""Tests for BaseExtractor._fetch_bytes() with kind='object_storage'.

PR #1 added the `S3Adapter` source plugin which emits events with
``ContentRef(kind="object_storage", bucket=..., key=...)``. Without a
matching consumer-side fetch path in BaseExtractor, those events end up
in the dead-letter queue. This test suite exercises the fetch path
end-to-end against a moto ThreadedMotoServer (same pattern used by the
S3Adapter test in PR #1) so the actual aioboto3 wire path is exercised
without any external network call.

Credentials are deliberately read from env vars (AKOPIA_S3_ENDPOINT /
AKOPIA_S3_ACCESS_KEY / AKOPIA_S3_SECRET_KEY / AKOPIA_S3_REGION /
AKOPIA_S3_USE_SSL) rather than embedded in the ContentRef — events
travel through Redis and putting secrets in them would leak across the
audit trail.
"""
from __future__ import annotations

import socket

import boto3
import pytest
from moto.server import ThreadedMotoServer

from common.base_extractor import BaseExtractor
from common.models import ChangeEvent, ContentRef, ExtractedContent


# ── A minimal concrete BaseExtractor for testing ───────────────────


class _StubExtractor(BaseExtractor):
    """Bare-minimum subclass that lets us call ``_fetch_bytes`` directly."""

    plugin_id = "stub"
    supported_extensions = [".bin"]

    async def configure(self, config: dict) -> None:  # pragma: no cover - trivial
        return None

    async def extract(  # pragma: no cover - not exercised here
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        return ExtractedContent(source_event_id=event.event_id, text="")


def _make_extractor() -> _StubExtractor:
    # cache_dir=None to skip filesystem cache; redis_client=object() so the
    # base init doesn't try to connect.
    return _StubExtractor(cache_dir=None, redis_client=object())


# ── moto ThreadedMotoServer fixture ────────────────────────────────


_BUCKET = "akopia-extract-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def moto_s3():
    """Start a moto S3 server and yield (endpoint_url, sync_boto3_client).

    The boto3 client is sync (handy for arrange-side fixture writes);
    the BaseExtractor under test creates its own aioboto3 client pointed
    at the same endpoint via the AKOPIA_S3_* env vars.
    """
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


@pytest.fixture
def s3_env(moto_s3, monkeypatch):
    """Wire AKOPIA_S3_* env vars to the moto endpoint."""
    endpoint, s3 = moto_s3
    monkeypatch.setenv("AKOPIA_S3_ENDPOINT", endpoint)
    monkeypatch.setenv("AKOPIA_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("AKOPIA_S3_SECRET_KEY", "test")
    monkeypatch.setenv("AKOPIA_S3_REGION", "us-east-1")
    monkeypatch.setenv("AKOPIA_S3_USE_SSL", "false")
    return endpoint, s3


# ── Tests ──────────────────────────────────────────────────────────


class TestFetchBytesObjectStorage:
    async def test_round_trip_small_json(self, s3_env):
        """Upload a small JSON object and fetch it via _fetch_bytes."""
        _, s3 = s3_env
        body = b'{"hello": "world", "n": 42}'
        s3.put_object(Bucket=_BUCKET, Key="dir/sample.json", Body=body)

        ex = _make_extractor()
        ref = ContentRef(
            kind="object_storage", bucket=_BUCKET, key="dir/sample.json"
        )

        got = await ex._fetch_bytes(ref)

        assert got == body

    async def test_missing_env_raises_runtime_error(self, monkeypatch):
        """No AKOPIA_S3_* env vars set → RuntimeError with a clear message."""
        for name in (
            "AKOPIA_S3_ENDPOINT",
            "AKOPIA_S3_ACCESS_KEY",
            "AKOPIA_S3_SECRET_KEY",
        ):
            monkeypatch.delenv(name, raising=False)

        ex = _make_extractor()
        ref = ContentRef(
            kind="object_storage", bucket=_BUCKET, key="anything"
        )

        with pytest.raises(RuntimeError, match="AKOPIA_S3_ENDPOINT"):
            await ex._fetch_bytes(ref)
