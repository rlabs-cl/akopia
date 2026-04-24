"""BaseExtractor — transport + resilience boilerplate for Layer 2 plugins.

Plugin authors subclass this and implement ``extract`` (and ``configure``).
The base handles: fetching bytes from any ContentRef kind, caching by
content hash, timeout enforcement, Redis stream consumption, DLQ
publishing on failure, metrics, graceful shutdown.

Minimum plugin implementation:

    class PlainExtractor(BaseExtractor):
        plugin_id = "plain"
        supported_mime_types = ["text/plain", "text/markdown"]
        supported_extensions = [".txt", ".md"]
        priority = 0

        async def extract(self, content_ref, event):
            text = await self._fetch_as_text(content_ref)
            return ExtractedContent(
                source_event_id=event.event_id,
                text=text,
                extractor_id=self.plugin_id,
            )
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import signal
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from common.config import Config
from common.models import (
    ChangeEvent,
    ContentRef,
    ExtractedContent,
    HealthReport,
    HealthStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CACHE_DIR = "/data/cache/extract"


class ExtractorTimeout(Exception):
    """Raised when an extract() call exceeds the configured timeout."""


class BaseExtractor(ABC):
    """Framework skeleton for a ContentExtractor plugin."""

    #: Override in subclass.
    plugin_id: str = ""
    supported_mime_types: list[str] = []
    supported_extensions: list[str] = []
    priority: int = 0

    #: Semver of the extractor's output format; bump on breaking change
    #: so re-index can target old versions.
    version: str = "0.1.0"

    def __init__(self, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
                 redis_client=None):
        if not self.plugin_id:
            raise TypeError(
                f"{type(self).__name__} must set class attribute plugin_id"
            )
        if not (self.supported_mime_types or self.supported_extensions):
            raise TypeError(
                f"{type(self).__name__} must declare at least one of "
                "supported_mime_types or supported_extensions"
            )
        self.timeout_seconds = timeout_seconds
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._redis = redis_client
        self._shutdown = asyncio.Event()
        self.metrics = {
            "jobs_processed": 0,
            "jobs_cached_hits": 0,
            "errors": 0,
            "timeouts": 0,
        }

    # ── Abstract surface ──────────────────────────────────────────

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Validate + store config. Raise on missing keys."""

    @abstractmethod
    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        """Core of the plugin: bytes → ExtractedContent.

        Use ``self._fetch_bytes(content_ref)`` or
        ``self._fetch_as_text(content_ref)`` to avoid dealing with the
        five ContentRef kinds directly.
        """

    async def health(self) -> HealthReport:
        return HealthReport(
            status=HealthStatus.HEALTHY,
            detail=f"plugin={self.plugin_id}",
            checks={k: str(v) for k, v in self.metrics.items()},
        )

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self, config: dict) -> None:
        await self.configure(config)
        if self._redis is None:
            from common.redis_client import RedisClient
            self._redis = RedisClient(Config.REDIS_URL)
            await self._redis.connect()
        await self._redis.ensure_stream_and_group(
            "extract-jobs", f"cg-extract-{self.plugin_id}"
        )
        self._install_signal_handlers()
        await self._consume_loop()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except NotImplementedError:
                pass

    async def _consume_loop(self) -> None:
        """Read extract-jobs, process, publish extract-results."""
        group = f"cg-extract-{self.plugin_id}"
        consumer = f"extractor-{self.plugin_id}-0"
        while not self._shutdown.is_set():
            try:
                messages = await self._redis.consume(
                    "extract-jobs", group, consumer,
                    count=5, block_ms=5000,
                )
                for msg_id, data in messages or []:
                    try:
                        event = ChangeEvent(**data.get("event", {}))
                        content_ref = ContentRef(**data["content_ref"])
                        result = await self._process_with_timeout(content_ref, event)
                        await self._publish_result(event, result, ok=True)
                    except asyncio.TimeoutError:
                        self.metrics["timeouts"] += 1
                        await self._publish_dlq(data, "timeout")
                    except Exception as e:
                        self.metrics["errors"] += 1
                        logger.exception(
                            "extractor=%s failed on event=%s",
                            self.plugin_id, data.get("event", {}).get("event_id"),
                        )
                        await self._publish_dlq(data, str(e))
                    finally:
                        await self._redis.ack("extract-jobs", group, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("extractor=%s consume loop error: %s", self.plugin_id, e)
                await asyncio.sleep(5)

    # ── Content retrieval helpers ─────────────────────────────────

    async def _fetch_bytes(self, ref: ContentRef) -> bytes:
        """Resolve any ContentRef kind to raw bytes."""
        if ref.kind == "path":
            return Path(ref.path).read_bytes()
        if ref.kind == "inline_bytes":
            return base64.b64decode(ref.bytes_b64)
        if ref.kind == "inline_text":
            return ref.text.encode("utf-8")
        if ref.kind == "url":
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                r = await client.get(ref.url)
                r.raise_for_status()
                return r.content
        if ref.kind == "object_storage":
            # TODO: S3/MinIO via boto3. Not in slice 1.
            raise NotImplementedError("object_storage fetch not yet supported")
        raise ValueError(f"unknown ContentRef kind: {ref.kind}")

    async def _fetch_as_text(self, ref: ContentRef, encoding: str = "utf-8") -> str:
        """Shortcut when the extractor knows the body is already text."""
        if ref.kind == "inline_text":
            return ref.text
        return (await self._fetch_bytes(ref)).decode(encoding, errors="replace")

    # ── Core processing ───────────────────────────────────────────

    async def _process_with_timeout(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        """Run extract() with a hard deadline + cache check."""
        cache_key = None
        if self.cache_dir and event.content_hash:
            cache_key = f"{self.plugin_id}:{self.version}:{event.content_hash}"
            cached = self._cache_get(cache_key)
            if cached is not None:
                self.metrics["jobs_cached_hits"] += 1
                return cached

        t0 = time.monotonic()
        result = await asyncio.wait_for(
            self.extract(content_ref, event),
            timeout=self.timeout_seconds,
        )
        result.processing_time_ms = int((time.monotonic() - t0) * 1000)
        # Stamp provenance even if plugin forgot
        result.extractor_id = result.extractor_id or self.plugin_id
        result.extractor_version = result.extractor_version or self.version
        if cache_key:
            self._cache_put(cache_key, result)
        self.metrics["jobs_processed"] += 1
        return result

    # ── Cache (content-hash keyed, plugin + version scoped) ───────

    def _cache_get(self, key: str) -> Optional[ExtractedContent]:
        if not self.cache_dir:
            return None
        path = self.cache_dir / hashlib.sha256(key.encode()).hexdigest()[:32]
        if not path.exists():
            return None
        try:
            return ExtractedContent(**json.loads(path.read_text()))
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _cache_put(self, key: str, value: ExtractedContent) -> None:
        if not self.cache_dir:
            return
        path = self.cache_dir / hashlib.sha256(key.encode()).hexdigest()[:32]
        try:
            path.write_text(value.model_dump_json())
        except Exception as e:
            logger.warning("cache write failed for %s: %s", self.plugin_id, e)

    # ── Publish ───────────────────────────────────────────────────

    async def _publish_result(
        self,
        event: ChangeEvent,
        result: ExtractedContent,
        *,
        ok: bool,
    ) -> None:
        payload = {
            "event_id": event.event_id,
            "event": event.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "ok": ok,
        }
        await self._redis.publish("extract-results", payload)

    async def _publish_dlq(self, original: dict, error: str) -> None:
        payload = {
            "stream": "extract-jobs",
            "extractor_id": self.plugin_id,
            "error": error,
            "original": original,
        }
        await self._redis.publish("dead-letter", payload)


__all__ = ["BaseExtractor", "ExtractorTimeout"]
