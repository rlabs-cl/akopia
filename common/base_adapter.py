"""BaseSourceAdapter — transport + resilience boilerplate for Layer 3 plugins.

This is the "standardised communication in and out" the RFC specifies.
Plugin authors subclass this and implement only the domain-specific
methods from ``SourceAdapter``. Everything below is inherited.

Minimum plugin implementation:

    class FolderAdapter(BaseSourceAdapter):
        plugin_id = "folder"

        async def configure(self, config: dict) -> None:
            self.path = Path(config["path"]).resolve()

        async def discover(self):
            yield Source(source_id="root", type="folder", name=str(self.path))

        async def watch(self, source):
            while not self._shutdown.is_set():
                for p, mtime in self._scan().items():
                    yield self._make_change_event(path=p, ...)
                await asyncio.sleep(self.poll_seconds)

        async def read(self, source, path):
            return (self.path / path).read_bytes()

The base handles publishing events to Redis, metrics, health, shutdown.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from common.config import Config
from common.models import (
    ChangeEvent,
    HealthReport,
    HealthStatus,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)


class BaseSourceAdapter(ABC):
    """Framework skeleton for a SourceAdapter plugin.

    Subclasses set ``plugin_id`` as a class attribute and implement the
    abstract methods. The base class drives the lifecycle:

        1. ``start(config)`` — called once by the runner with the
           instance config from akopia.yaml. Calls ``configure()``,
           starts the Redis publisher, registers signal handlers,
           then enters the discover → watch loop for each source.

        2. The event loop calls ``watch()`` concurrently for every
           source returned by ``discover()``. Every yielded
           ChangeEvent is passed through ``_publish_event`` which
           stamps idempotency key + metrics and writes to Redis.

        3. On SIGTERM/SIGINT, ``_shutdown`` is set and each watch
           loop is expected to exit cleanly. ``close()`` runs after.
    """

    #: Override in subclass. Matches the ``type:`` in akopia.yaml.
    plugin_id: str = ""

    def __init__(self, *, instance_id: str, redis_client=None):
        """
        Args:
            instance_id: The ``id`` field from the akopia.yaml entry
                (e.g. ``company-repos``). Populates ``source_id``
                on emitted ChangeEvents.
            redis_client: Optional injected ``RedisClient``; the
                default constructor pulls from config at start().
        """
        if not self.plugin_id:
            raise TypeError(
                f"{type(self).__name__} must set class attribute plugin_id"
            )
        self.instance_id = instance_id
        self._redis = redis_client
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # Metrics counters (simple in-memory; Prometheus exporter reads them)
        self.metrics = {
            "events_published": 0,
            "errors": 0,
            "discover_calls": 0,
        }

    # ── Public abstract surface (from SourceAdapter Protocol) ──────

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Validate + store instance config."""

    @abstractmethod
    async def discover(self) -> AsyncIterator[Source]:
        """Enumerate sources. Called once at startup."""

    @abstractmethod
    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        """Long-running. Yield ChangeEvents. Exit when ``_shutdown.is_set()``."""

    @abstractmethod
    async def read(self, source: Source, path: str) -> bytes:
        """Fetch raw bytes for one path."""

    async def health(self) -> HealthReport:
        """Override to check upstream reachability."""
        status = HealthStatus.HEALTHY if not self._shutdown.is_set() else HealthStatus.UNHEALTHY
        return HealthReport(
            status=status,
            detail=f"instance_id={self.instance_id}",
            checks={"events_published": str(self.metrics["events_published"])},
        )

    async def close(self) -> None:
        """Release resources. Subclass may override; always call super."""
        if self._redis:
            await self._redis.close()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self, config: dict) -> None:
        """Entry point: configure, connect, dispatch, run forever."""
        await self.configure(config)

        # Lazy-import to avoid heavy deps at module load
        if self._redis is None:
            from common.redis_client import RedisClient
            self._redis = RedisClient(Config.REDIS_URL)
            await self._redis.connect()
        await self._redis.ensure_stream_and_group(
            Config.STREAM_CHANGE_EVENTS, "cg-router"
        )

        self._install_signal_handlers()

        sources: list[Source] = []
        async for s in self.discover():
            sources.append(s)
            self.metrics["discover_calls"] += 1
        if not sources:
            logger.warning(
                "plugin=%s instance=%s discovered zero sources",
                self.plugin_id, self.instance_id,
            )

        # One watch task per source, supervised
        for source in sources:
            task = asyncio.create_task(
                self._watch_loop(source),
                name=f"watch:{self.plugin_id}:{source.source_id}",
            )
            self._tasks.append(task)

        # Wait until shutdown, then cancel pending tasks cleanly
        await self._shutdown.wait()
        logger.info("plugin=%s instance=%s shutdown signalled", self.plugin_id, self.instance_id)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.close()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except NotImplementedError:
                # Windows or certain containers don't support this
                pass

    async def _watch_loop(self, source: Source) -> None:
        """Supervision wrapper around the plugin's ``watch``."""
        backoff = 1.0
        while not self._shutdown.is_set():
            try:
                async for event in self.watch(source):
                    await self._publish_event(event)
                    backoff = 1.0  # reset on successful event
                # watch() returned cleanly (finite source or shutdown)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.metrics["errors"] += 1
                logger.exception(
                    "watch loop crashed plugin=%s source_id=%s — retry in %.1fs",
                    self.plugin_id, source.source_id, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _publish_event(self, event: ChangeEvent) -> None:
        """Stamp the event, serialise, and push to change-events stream.

        The base always overwrites ``source_id`` and ``source_type`` with
        the instance / plugin identity, regardless of what the plugin set.
        This is a consistency invariant: downstream consumers can trust
        that ``source_id`` always matches a akopia.yaml instance id and
        ``source_type`` always matches a registered plugin. Plugins
        that need to discriminate between sub-resources (multi-repo,
        multi-URL) encode that in ``path``, not in source_id.
        """
        event.source_id = self.instance_id
        event.source_type = self.plugin_id
        # Compute idempotency key AFTER stamping identity so it's
        # consistent with the post-stamping values.
        event.compute_idempotency_key()

        payload = event.model_dump(mode="json")
        await self._redis.publish(Config.STREAM_CHANGE_EVENTS, payload)
        self.metrics["events_published"] += 1

    # ── Convenience helpers for subclasses ─────────────────────────

    def _make_change_event(
        self,
        *,
        path: str,
        operation: Operation,
        modality: Modality,
        content_hash: Optional[str] = None,
        size_bytes: int = 0,
        content_ref=None,
        content_mime: Optional[str] = None,
    ) -> ChangeEvent:
        """Factory that fills in source_id + source_type + idempotency."""
        event = ChangeEvent(
            source_id=self.instance_id,
            source_type=self.plugin_id,
            operation=operation,
            path=path,
            modality=modality,
            content_hash=content_hash,
            size_bytes=size_bytes,
            content_ref=content_ref,
            content_mime=content_mime,
        )
        event.compute_idempotency_key()
        return event

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


__all__ = ["BaseSourceAdapter"]
