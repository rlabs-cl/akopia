"""Redis client with Streams helpers for Akopia."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

import redis.asyncio as aioredis

from .config import Config

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Fallback JSON encoder for types pydantic ``model_dump()`` leaves native.

    Covers the types we actually emit on the wire — datetime (freshness
    metadata) primarily. Any other unexpected type falls through to
    ``TypeError`` so we notice new gaps instead of silently stringifying.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class RedisClient:
    """Async Redis client with Streams support."""

    def __init__(self, url: str | None = None):
        self._url = url or Config.REDIS_URL
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            self._url, decode_responses=True, max_connections=20
        )
        await self._redis.ping()
        logger.info("Connected to Redis at %s", self._url)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    @property
    def r(self) -> aioredis.Redis:
        assert self._redis is not None, "Redis not connected"
        return self._redis

    # --- Streams ---

    async def ensure_stream_and_group(self, stream: str, group: str) -> None:
        try:
            await self.r.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("Created consumer group %s on stream %s", group, stream)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        # Use a permissive encoder so callers can publish ``model_dump()``
        # output without forcing ``mode='json'`` at every call site. This
        # guards against silent failures when a new field (e.g. a pydantic
        # ``datetime``) is added to any wire-format model. ISO 8601 is the
        # canonical textual form for datetimes in this codebase.
        msg_id = await self.r.xadd(
            stream, {"data": json.dumps(data, default=_json_default)}
        )
        return msg_id

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 5000,
    ) -> list[tuple[str, dict]]:
        results = await self.r.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        parsed = []
        if results:
            for _stream_name, messages in results:
                for msg_id, fields in messages:
                    try:
                        data = json.loads(fields.get("data", "{}"))
                        parsed.append((msg_id, data))
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON in message %s", msg_id)
                        await self.r.xack(stream, group, msg_id)
        return parsed

    async def ack(self, stream: str, group: str, msg_id: str) -> None:
        await self.r.xack(stream, group, msg_id)

    async def stream_len(self, stream: str) -> int:
        try:
            return await self.r.xlen(stream)
        except Exception:
            return 0

    # --- Idempotency ---

    async def is_processed(self, stream: str, key: str) -> bool:
        return await self.r.sismember(f"{Config.REDIS_KEY_PREFIX}:idempotent:{stream}", key)

    async def mark_processed(self, stream: str, key: str, ttl: int = 86400) -> None:
        set_key = f"{Config.REDIS_KEY_PREFIX}:idempotent:{stream}"
        await self.r.sadd(set_key, key)
        await self.r.expire(set_key, ttl)

    # --- Locks ---

    async def acquire_lock(self, source_id: str, ttl: int = 1800) -> bool:
        return await self.r.set(
            f"{Config.REDIS_KEY_PREFIX}:lock:{source_id}", "1", nx=True, ex=ttl
        )

    async def release_lock(self, source_id: str) -> None:
        await self.r.delete(f"{Config.REDIS_KEY_PREFIX}:lock:{source_id}")

    # --- Watermarks / Snapshots ---

    async def get_watermark(self, source_id: str) -> Optional[str]:
        return await self.r.get(f"{Config.REDIS_KEY_PREFIX}:watermark:{source_id}")

    async def set_watermark(self, source_id: str, value: str) -> None:
        await self.r.set(f"{Config.REDIS_KEY_PREFIX}:watermark:{source_id}", value)

    async def get_snapshot(self, source_id: str) -> dict[str, str]:
        return await self.r.hgetall(f"{Config.REDIS_KEY_PREFIX}:snapshot:{source_id}") or {}

    async def set_snapshot(self, source_id: str, snapshot: dict[str, str]) -> None:
        key = f"{Config.REDIS_KEY_PREFIX}:snapshot:{source_id}"
        if snapshot:
            await self.r.delete(key)
            await self.r.hset(key, mapping=snapshot)
        else:
            await self.r.delete(key)

    # --- Sources registry ---

    @property
    def _sources_key(self) -> str:
        return f"{Config.REDIS_KEY_PREFIX}:sources"

    async def register_source(self, source: dict) -> None:
        await self.r.hset(self._sources_key, source["source_id"], json.dumps(source))

    async def get_source(self, source_id: str) -> Optional[dict]:
        raw = await self.r.hget(self._sources_key, source_id)
        return json.loads(raw) if raw else None

    async def list_sources(self) -> list[dict]:
        raw = await self.r.hgetall(self._sources_key)
        return [json.loads(v) for v in raw.values()]

    async def update_source_status(self, source_id: str, status: str) -> None:
        raw = await self.r.hget(self._sources_key, source_id)
        if raw:
            source = json.loads(raw)
            source["status"] = status
            await self.r.hset(self._sources_key, source_id, json.dumps(source))
