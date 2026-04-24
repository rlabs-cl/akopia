"""Protocol definitions for the two plugin layers.

These are the *contracts* — what a plugin must do to be a akopia
citizen. The implementation-side helpers (transport, retry, metrics,
etc.) live in ``common.base_adapter`` and ``common.base_extractor``.

See ``docs/plugin-contracts.md`` for the RFC that drove these types.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import AsyncIterator, Protocol, runtime_checkable

from common.models import (
    ChangeEvent,
    ContentRef,
    ExtractedContent,
    HealthReport,
    Source,
)


@runtime_checkable
class SourceAdapter(Protocol):
    """Plugin contract: an origin of data.

    Implementations connect to a specific kind of source (git provider,
    filesystem path, URL, database CDC, object store) and emit
    ``ChangeEvent`` objects whenever something appears, changes, or
    disappears. They also expose an on-demand ``read`` for the bytes so
    downstream extractors don't need to know about the source protocol.

    Every plugin class defines ``plugin_id`` (matches the ``type:``
    value in ``akopia.yaml``) and is registered via entry points so that
    ``akopia.yaml`` instances are auto-dispatched to the right class.

    Subclassing ``BaseSourceAdapter`` is the expected way to implement
    this: the base handles bus I/O, idempotency, metrics, shutdown.
    """

    plugin_id: str

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Validate and store instance config. Raise on missing/invalid keys."""

    @abstractmethod
    async def discover(self) -> AsyncIterator[Source]:
        """Enumerate the logical sub-units managed by this instance.

        A git adapter yields one ``Source`` per repo; a folder adapter
        may yield one per top-level subtree; a web-deep adapter yields
        one per root URL. Called once at startup (and on refresh).
        """

    @abstractmethod
    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        """Long-running. Yield ChangeEvents for one source.

        The adapter decides polling cadence, incremental strategy, etc.
        Emits one ``ChangeEvent`` per detected change; the base class
        takes care of publishing to Redis, computing idempotency keys,
        metrics, backpressure. Exit cleanly when ``self._shutdown`` is
        set by the base.
        """

    @abstractmethod
    async def read(self, source: Source, path: str) -> bytes:
        """On-demand fetch of raw bytes for a given path.

        Extractors call this when ``ContentRef.kind == 'path'`` or
        ``'url'`` and they need the payload. Must be idempotent and
        return consistent bytes (the content_hash we stamped in the
        ChangeEvent has to still match).
        """

    async def health(self) -> HealthReport:
        """Non-blocking health probe.

        Default impl returns HEALTHY. Override to check upstream
        reachability (e.g. ``git ls-remote`` or HTTP HEAD).
        """
        from common.models import HealthStatus
        return HealthReport(status=HealthStatus.HEALTHY)

    async def close(self) -> None:
        """Release resources (connections, file handles). Default no-op."""


@runtime_checkable
class ContentExtractor(Protocol):
    """Plugin contract: turn bytes into ExtractedContent.

    Implementations advertise which MIME types and file extensions they
    handle; the router dispatches each event to the highest-priority
    extractor that matches. They receive a ``ContentRef`` (abstracted
    byte source) and a ``ChangeEvent`` (the event that triggered the
    extraction) and return an ``ExtractedContent``.

    Subclass ``BaseExtractor`` to inherit bus I/O, caching, timeout,
    DLQ behaviour.
    """

    plugin_id: str

    #: MIME types this extractor can process, e.g. ["application/pdf"].
    supported_mime_types: list[str]

    #: File extensions (including leading dot) this extractor can
    #: process, e.g. [".pdf"]. Used when MIME type is unknown.
    supported_extensions: list[str]

    #: Higher priority wins on ties. Built-in extractors use 0;
    #: third-party overrides use > 0. Negative priority is a soft
    #: fallback ("use me only if nothing else matches").
    priority: int

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Validate and store config."""

    @abstractmethod
    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        """Turn bytes into extracted text + metadata.

        The content_ref tells you how to get the bytes; the base class
        provides ``self._fetch_bytes(ref)`` and ``self._fetch_as_text(ref)``
        helpers that handle all five ``kind`` values transparently.
        The ``event`` is passed so the extractor can emit attached
        ChangeEvents with correct lineage (``source_id``, ``depth + 1``).
        """

    async def health(self) -> HealthReport:
        from common.models import HealthStatus
        return HealthReport(status=HealthStatus.HEALTHY)

    async def close(self) -> None:
        pass


__all__ = ["SourceAdapter", "ContentExtractor"]
