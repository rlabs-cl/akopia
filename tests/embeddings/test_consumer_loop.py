"""Batch-consume + concurrent job processing in ``embeddings.main``.

The consumer used to pull 1 Redis message at a time and process it
serially. Now it pulls up to ``EMBEDDER_BATCH_SIZE`` (default 10) per
XREADGROUP and hands the whole batch to ``asyncio.gather``. These tests
lock in that contract without spinning up Redis or a real backend:

* Batch size respects ``EMBEDDER_BATCH_SIZE`` (range clamp + default).
* ``_process_msg`` handles success, idempotent-skip, and poison-job
  branches with per-message acks.
* Processing 5 jobs from one batch runs them concurrently — the test
  uses an ``asyncio.Barrier`` so all 5 coroutines must rendezvous
  before any completes. If the loop was still serial the barrier would
  deadlock and the test would hit its timeout.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from embeddings import main as emb_main
from embeddings.main import (
    _DEFAULT_EMBEDDER_BATCH_SIZE,
    _MAX_EMBEDDER_BATCH_SIZE,
    _process_msg,
    _resolve_batch_size,
    consumer_loop,
)


def _job_payload(job_id: str, idem: str) -> dict:
    """Minimal dict shape that EmbeddingJob(**data) will accept."""
    return {
        "job_id": job_id,
        "batch_id": "b",
        "idempotency_key": idem,
        "source_id": "src-x",
        "modality": "text",
        "chunks": [{
            "chunk_id": f"{job_id}:0",
            "content": "hello world",
            "path": "a.md",
            "chunk_index": 0,
            "total_chunks": 1,
        }],
    }


# ── _resolve_batch_size ────────────────────────────────────────────

class TestResolveBatchSize:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("EMBEDDER_BATCH_SIZE", raising=False)
        assert _resolve_batch_size() == _DEFAULT_EMBEDDER_BATCH_SIZE

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("EMBEDDER_BATCH_SIZE", "25")
        assert _resolve_batch_size() == 25

    def test_out_of_range_clamped(self, monkeypatch):
        monkeypatch.setenv("EMBEDDER_BATCH_SIZE", "999")
        assert _resolve_batch_size() == _MAX_EMBEDDER_BATCH_SIZE
        monkeypatch.setenv("EMBEDDER_BATCH_SIZE", "0")
        assert _resolve_batch_size() == 1

    def test_bad_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("EMBEDDER_BATCH_SIZE", "not-a-number")
        assert _resolve_batch_size() == _DEFAULT_EMBEDDER_BATCH_SIZE


# ── _process_msg: per-message ack semantics ────────────────────────

class _FakeRedisClient:
    """Minimal stand-in for ``RedisClient`` exercised by _process_msg."""

    def __init__(self, processed_keys=()):
        self._processed_keys = set(processed_keys)
        self.published: list[tuple[str, dict]] = []
        self.marked: list[str] = []
        self.acked: list[str] = []

    async def is_processed(self, stream, key):
        return key in self._processed_keys

    async def mark_processed(self, stream, key):
        self.marked.append(key)

    async def publish(self, stream, payload):
        self.published.append((stream, payload))

    async def ack(self, stream, group, msg_id):
        self.acked.append(msg_id)


@pytest.mark.asyncio
async def test_process_msg_success_path(monkeypatch):
    fake = _FakeRedisClient()
    monkeypatch.setattr(emb_main, "redis_client", fake)

    async def fake_embed(texts):
        return [[0.0, 1.0] for _ in texts]

    with patch("embeddings.main.embed_texts", side_effect=fake_embed), \
         patch("embeddings.main._load_text_backend") as fake_backend:
        fake_backend.return_value.model_id = lambda: "m"
        await _process_msg("msg-1", _job_payload("j1", "k1"))

    assert fake.acked == ["msg-1"]
    assert fake.marked == ["k1"]
    assert fake.published  # an EmbeddingResult was published


@pytest.mark.asyncio
async def test_process_msg_skips_idempotent_job(monkeypatch):
    fake = _FakeRedisClient(processed_keys={"k-dup"})
    monkeypatch.setattr(emb_main, "redis_client", fake)

    await _process_msg("msg-2", _job_payload("j2", "k-dup"))

    # Idempotent duplicate: ack'd, nothing published, nothing marked.
    assert fake.acked == ["msg-2"]
    assert fake.published == []
    assert fake.marked == []


@pytest.mark.asyncio
async def test_process_msg_poison_still_acks(monkeypatch):
    fake = _FakeRedisClient()
    monkeypatch.setattr(emb_main, "redis_client", fake)

    # Missing required field → pydantic raises inside EmbeddingJob(**data)
    await _process_msg("msg-3", {"not": "a job"})

    assert fake.acked == ["msg-3"]
    assert fake.published == []
    assert fake.marked == []


# ── consumer_loop: batch pulls + concurrent processing ─────────────

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_consumer_processes_batch_concurrently(monkeypatch):
    """5 jobs in one batch must all hit the backend concurrently.

    Uses an ``asyncio.Barrier(5)`` inside the fake ``embed_texts`` —
    each coroutine must arrive before any can proceed. If the consumer
    were still serial, the first call would wait forever and the test
    would timeout. Passing proves gather-style parallelism.
    """
    n = 5
    barrier = asyncio.Barrier(n)
    call_count = 0
    lock = asyncio.Lock()

    async def fake_embed(texts):
        nonlocal call_count
        async with lock:
            call_count += 1
        # Rendezvous point: all 5 coroutines must be here.
        await barrier.wait()
        return [[0.0, 1.0] for _ in texts]

    # Build a scripted Redis client. First consume() returns a batch of
    # 5 messages, subsequent calls return [] so the loop idles and we
    # can cancel cleanly.
    consume_calls: list[int] = []

    class _ScriptedRedis:
        def __init__(self):
            self._batches = [
                [(f"mid-{i}", _job_payload(f"j{i}", f"k{i}")) for i in range(n)],
            ]
            self.acked: list[str] = []
            self.published: list[tuple[str, dict]] = []

        async def ensure_stream_and_group(self, *a, **kw):
            pass

        async def consume(self, stream, group, consumer, count, block_ms):
            consume_calls.append(count)
            if self._batches:
                return self._batches.pop(0)
            # Idle loop — yield control so cancel() can land.
            await asyncio.sleep(0.05)
            return []

        async def is_processed(self, stream, key):
            return False

        async def mark_processed(self, stream, key):
            pass

        async def publish(self, stream, payload):
            self.published.append((stream, payload))

        async def ack(self, stream, group, msg_id):
            self.acked.append(msg_id)

    scripted = _ScriptedRedis()
    monkeypatch.setattr(emb_main, "redis_client", scripted)
    monkeypatch.setenv("EMBEDDER_BATCH_SIZE", "10")

    with patch("embeddings.main.embed_texts", side_effect=fake_embed), \
         patch("embeddings.main._load_text_backend") as fake_backend:
        fake_backend.return_value.model_id = lambda: "m"
        task = asyncio.create_task(consumer_loop())
        # Wait for all 5 to reach the barrier (they'll finish together).
        # If anything is serial, this hangs and pytest-timeout kills it.
        for _ in range(100):
            if call_count >= n:
                break
            await asyncio.sleep(0.05)
        # Give the gather a moment to finalize (publish + ack).
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count == n, f"expected {n} concurrent calls, got {call_count}"
    assert consume_calls and consume_calls[0] == 10, (
        "consumer must pull EMBEDDER_BATCH_SIZE per round-trip; "
        f"first consume count was {consume_calls[:1]}"
    )
    assert len(scripted.acked) == n, "every job in the batch must ack"
    assert len(scripted.published) == n, "every job must publish a result"
