"""WebSingleAdapter — poll one URL on a cron and emit diffs.

Deps (installed via ``/tmp/kbvenv/bin/pip install httpx``):
    httpx — async HTTP client with redirect + timeout support.

This adapter re-fetches a single URL at a cron-defined cadence, sends
conditional requests via ``If-None-Match`` + ``If-Modified-Since`` to
avoid re-downloading unchanged pages, and emits a ``ChangeEvent`` only
when the SHA-256 of the body changes.

Non-goals:
- No JavaScript rendering. Server-rendered HTML only. For JS-heavy
  sites (SPAs) use an external renderer like https://firecrawl.dev
  and feed its output into a ``folder`` adapter.
- No HTML-to-text. That's the ``html`` extractor's job (Slice 4). The
  adapter always emits raw bytes, leaving cleanup to the extractor.
- No sitemap.xml discovery — deferred post-launch.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Optional

import httpx

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)

# Inline-body threshold, matches the RFC: bodies below this size are
# embedded into the ChangeEvent as base64; bigger ones use kind="url"
# so downstream extractors can stream them.
INLINE_BYTES_THRESHOLD = 1024 * 1024  # 1 MiB


@dataclass
class _CacheEntry:
    """Per-URL conditional-GET state + last observed content hash."""

    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class _Config:
    url: str
    refresh_cron: str = "0 6 * * *"
    user_agent: str = "akopia/0.1 (+https://github.com/rlabs-cl/akopia)"
    timeout_seconds: int = 30
    follow_redirects: bool = True
    headers: dict = field(default_factory=dict)


def _cron_to_sleep_seconds(cron: str) -> float:
    """Very coarse cron → poll-interval translation.

    We don't evaluate cron schedules (no ``croniter`` in this slice).
    Instead we derive an interval from the minute/hour fields so the
    default ``"0 6 * * *"`` sleeps 24 h and common patterns like
    ``"*/5 * * * *"`` sleep 5 min. Tests inject a short interval by
    passing ``_poll_seconds`` directly on the adapter instance.
    """
    parts = cron.split()
    if len(parts) < 2:
        return 24 * 3600.0
    minute, hour = parts[0], parts[1]
    if minute.startswith("*/"):
        try:
            return max(float(minute[2:]), 1.0) * 60.0
        except ValueError:
            return 300.0
    if hour.startswith("*/"):
        try:
            return max(float(hour[2:]), 1.0) * 3600.0
        except ValueError:
            return 3600.0
    if hour == "*" and minute.isdigit():
        return 3600.0
    # Fixed hour — treat as daily.
    return 24 * 3600.0


class WebSingleAdapter(BaseSourceAdapter):
    """Pull a single URL on cron and emit a ChangeEvent per content change."""

    plugin_id = "web-single"

    def __init__(
        self,
        *,
        instance_id: str,
        redis_client=None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__(instance_id=instance_id, redis_client=redis_client)
        self._cfg: Optional[_Config] = None
        self._cache: dict[str, _CacheEntry] = {}
        self._client: Optional[httpx.AsyncClient] = client
        self._owns_client: bool = client is None
        # Overridable in tests to collapse the cron sleep. When None,
        # the value is derived from ``refresh_cron``.
        self._poll_seconds: Optional[float] = None

    # ── Contract ────────────────────────────────────────────────────

    async def configure(self, config: dict) -> None:
        if "url" not in config or not config["url"]:
            raise ValueError("web-single: 'url' is required")
        self._cfg = _Config(
            url=config["url"],
            refresh_cron=config.get("refresh_cron", "0 6 * * *"),
            user_agent=config.get(
                "user_agent",
                "akopia/0.1 (+https://github.com/rlabs-cl/akopia)",
            ),
            timeout_seconds=int(config.get("timeout_seconds", 30)),
            follow_redirects=bool(config.get("follow_redirects", True)),
            headers=dict(config.get("headers") or {}),
        )
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._cfg.timeout_seconds,
                follow_redirects=self._cfg.follow_redirects,
                headers={"User-Agent": self._cfg.user_agent, **self._cfg.headers},
            )
            self._owns_client = True

    async def discover(self) -> AsyncIterator[Source]:
        assert self._cfg is not None, "configure() must run first"
        # Source.type is now a free string; stamp our plugin_id so it
        # matches akopia.yaml. BaseSourceAdapter also overwrites
        # event.source_type with self.plugin_id, so the two stay aligned.
        yield Source(
            source_id=self.instance_id,
            type=self.plugin_id,
            name=self._cfg.url,
            url=self._cfg.url,
        )

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        assert self._cfg is not None
        interval = (
            self._poll_seconds
            if self._poll_seconds is not None
            else _cron_to_sleep_seconds(self._cfg.refresh_cron)
        )
        while not self._shutdown.is_set():
            event = await self._poll_once()
            if event is not None:
                yield event
            # Sleep but stay responsive to shutdown
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def read(self, source: Source, path: str) -> bytes:
        """Re-fetch the URL. ``path`` is ignored (single-URL adapter)."""
        assert self._client is not None and self._cfg is not None
        resp = await self._client.get(self._cfg.url)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        await super().close()

    # ── Internals ───────────────────────────────────────────────────

    async def _poll_once(self) -> Optional[ChangeEvent]:
        assert self._cfg is not None and self._client is not None
        url = self._cfg.url
        cache = self._cache.get(url, _CacheEntry())

        headers: dict[str, str] = {}
        if cache.etag:
            headers["If-None-Match"] = cache.etag
        if cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified

        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("web-single fetch failed url=%s err=%s", url, e)
            self.metrics["errors"] += 1
            return None

        if resp.status_code == 304:
            # Not Modified — do nothing, keep cache.
            return None
        if resp.status_code >= 400:
            logger.warning(
                "web-single got status=%s for url=%s", resp.status_code, url,
            )
            self.metrics["errors"] += 1
            return None

        body = resp.content
        new_hash = self._sha256(body)
        first_seen = cache.content_hash is None
        if not first_seen and new_hash == cache.content_hash:
            # Server didn't honour conditional headers but content is
            # identical — swallow silently.
            cache.etag = resp.headers.get("ETag", cache.etag)
            cache.last_modified = resp.headers.get("Last-Modified", cache.last_modified)
            self._cache[url] = cache
            return None

        # Content changed (or this is the first fetch).
        cache.etag = resp.headers.get("ETag")
        cache.last_modified = resp.headers.get("Last-Modified")
        cache.content_hash = new_hash
        self._cache[url] = cache

        content_mime = resp.headers.get("Content-Type")
        content_ref = self._make_content_ref(body, url)
        op = Operation.ADD if first_seen else Operation.MODIFY

        event = self._make_change_event(
            path=url,
            operation=op,
            modality=Modality.TEXT,
            content_hash=new_hash,
            size_bytes=len(body),
            content_ref=content_ref,
            content_mime=content_mime,
        )
        event.content_modified_at = _parse_last_modified(
            resp.headers.get("Last-Modified")
        )
        return event

    @staticmethod
    def _make_content_ref(body: bytes, url: str) -> ContentRef:
        if len(body) < INLINE_BYTES_THRESHOLD:
            return ContentRef(
                kind="inline_bytes",
                bytes_b64=base64.b64encode(body).decode("ascii"),
            )
        return ContentRef(kind="url", url=url)


def _parse_last_modified(header_val: Optional[str]) -> Optional[datetime]:
    """Parse an HTTP ``Last-Modified`` header into a tz-aware datetime.

    Falls back to ``datetime.now(utc)`` when the header is missing so
    downstream ranking still has a signal; returns ``None`` only when
    parsing the present header fails, so ingestion doesn't silently
    stamp a stale page as "fresh" if the server returned garbage.
    """
    if not header_val:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(header_val)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # parsedate_to_datetime may return naive for malformed input; coerce.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = ["WebSingleAdapter", "INLINE_BYTES_THRESHOLD"]
