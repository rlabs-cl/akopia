"""WebDeepAdapter — BFS crawl a site, emit ChangeEvents per page.

Deps (installed via ``/tmp/kbvenv/bin/pip install httpx selectolax``):
    httpx       — async HTTP client.
    selectolax  — fast, pure-C HTML parser used *only* to extract
                  outbound ``<a href>`` links so the BFS queue can grow.
                  Chosen over ``beautifulsoup4+lxml`` for (a) much lower
                  memory footprint inside the adapter pod and (b) it's a
                  drop-in pure-Python wheel with no compile step. If the
                  wheel ever breaks, swap for
                  ``beautifulsoup4 + lxml`` — the link-extraction logic
                  is self-contained in ``_extract_links``.

Non-goals:
- No JavaScript rendering. Same caveat as ``web-single``: server-rendered
  HTML only. For SPAs, hand off to https://firecrawl.dev.
- No HTML-to-text. The ``html`` extractor owns that (Slice 4); we emit
  raw bytes and let it clean them.
- No sitemap.xml — post-launch.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)

INLINE_BYTES_THRESHOLD = 1024 * 1024  # 1 MiB

# Safety caps independent of user config.
HARD_MAX_DEPTH = 10
HARD_MAX_PAGES = 10_000


@dataclass
class _Config:
    root: str
    max_depth: int = 3
    max_pages: int = 200
    respect_robots: bool = True
    rate_limit: str = "1/s"
    same_origin_only: bool = True
    user_agent: str = "akopia/0.1 (+https://github.com/rlabs-cl/akopia)"
    timeout_seconds: int = 30
    headers: dict = field(default_factory=dict)
    refresh_cron: str = "0 6 * * *"


def _parse_rate_limit(expr: str) -> float:
    """Return minimum seconds between requests. Format: ``<n>/<s|min>``."""
    try:
        n_str, unit = expr.split("/")
        n = float(n_str)
    except (ValueError, AttributeError):
        raise ValueError(f"rate_limit must be '<n>/<s|min>', got {expr!r}")
    if n <= 0:
        raise ValueError("rate_limit numerator must be positive")
    if unit in ("s", "sec", "second", "seconds"):
        return 1.0 / n
    if unit in ("min", "minute", "minutes", "m"):
        return 60.0 / n
    raise ValueError(f"rate_limit unit must be 's' or 'min', got {unit!r}")


def _canonicalize(url: str) -> str:
    """Drop fragment, lowercase scheme+host, keep path/query intact.

    This is the key used in the visited-set; two URLs that differ only
    by fragment are the same page for crawling purposes.
    """
    clean, _frag = urldefrag(url)
    parsed = urlparse(clean)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    rebuilt = parsed._replace(scheme=scheme, netloc=netloc)
    return rebuilt.geturl()


class WebDeepAdapter(BaseSourceAdapter):
    """BFS crawler over a single site."""

    plugin_id = "web-deep"

    def __init__(
        self,
        *,
        instance_id: str,
        redis_client=None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__(instance_id=instance_id, redis_client=redis_client)
        self._cfg: Optional[_Config] = None
        self._client: Optional[httpx.AsyncClient] = client
        self._owns_client: bool = client is None
        self._seen_hashes: dict[str, str] = {}   # canonical URL → last content hash
        self._robots: Optional[RobotFileParser] = None
        self._robots_checked_for: Optional[str] = None
        self._min_request_spacing: float = 1.0
        self._last_request_at: float = 0.0
        # Overridable in tests to skip the between-crawl sleep.
        self._poll_seconds: Optional[float] = None

    # ── Contract ────────────────────────────────────────────────────

    async def configure(self, config: dict) -> None:
        if "root" not in config or not config["root"]:
            raise ValueError("web-deep: 'root' is required")
        max_depth = int(config.get("max_depth", 3))
        if max_depth > HARD_MAX_DEPTH:
            raise ValueError(
                f"web-deep: max_depth {max_depth} exceeds hard cap {HARD_MAX_DEPTH}"
            )
        max_pages = int(config.get("max_pages", 200))
        if max_pages > HARD_MAX_PAGES:
            raise ValueError(
                f"web-deep: max_pages {max_pages} exceeds hard cap {HARD_MAX_PAGES}"
            )
        self._cfg = _Config(
            root=config["root"],
            max_depth=max_depth,
            max_pages=max_pages,
            respect_robots=bool(config.get("respect_robots", True)),
            rate_limit=config.get("rate_limit", "1/s"),
            same_origin_only=bool(config.get("same_origin_only", True)),
            user_agent=config.get(
                "user_agent",
                "akopia/0.1 (+https://github.com/rlabs-cl/akopia)",
            ),
            timeout_seconds=int(config.get("timeout_seconds", 30)),
            headers=dict(config.get("headers") or {}),
            refresh_cron=config.get("refresh_cron", "0 6 * * *"),
        )
        self._min_request_spacing = _parse_rate_limit(self._cfg.rate_limit)

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._cfg.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self._cfg.user_agent, **self._cfg.headers},
            )
            self._owns_client = True

    async def discover(self) -> AsyncIterator[Source]:
        assert self._cfg is not None
        host = urlparse(self._cfg.root).hostname or self.instance_id
        yield Source(
            source_id=host,
            type=self.plugin_id,
            name=self._cfg.root,
            url=self._cfg.root,
        )

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        assert self._cfg is not None
        interval = (
            self._poll_seconds
            if self._poll_seconds is not None
            # Reuse the same cron translation helper used by web-single.
            else _default_refresh_interval(self._cfg.refresh_cron)
        )
        while not self._shutdown.is_set():
            async for event in self._crawl():
                yield event
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def read(self, source: Source, path: str) -> bytes:
        """Re-fetch a single URL that was previously seen by the crawler."""
        assert self._client is not None
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        await super().close()

    # ── Crawler ─────────────────────────────────────────────────────

    async def _crawl(self) -> AsyncIterator[ChangeEvent]:
        assert self._cfg is not None and self._client is not None

        root_canon = _canonicalize(self._cfg.root)
        root_origin = _origin(root_canon)

        if self._cfg.respect_robots:
            await self._load_robots(root_origin)

        queue: list[tuple[str, int]] = [(root_canon, 0)]
        visited: set[str] = set()
        pages_emitted = 0

        while queue and pages_emitted < self._cfg.max_pages:
            if self._shutdown.is_set():
                return
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            if self._cfg.same_origin_only and _origin(url) != root_origin:
                continue
            if self._cfg.respect_robots and not self._robots_allows(url):
                logger.debug("robots.txt disallows %s", url)
                continue

            await self._rate_limit_wait()

            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as e:
                logger.warning("web-deep fetch failed url=%s err=%s", url, e)
                self.metrics["errors"] += 1
                continue

            if resp.status_code >= 400:
                logger.info("web-deep status=%s url=%s", resp.status_code, url)
                continue

            body = resp.content
            content_hash = self._sha256(body)
            prev_hash = self._seen_hashes.get(url)
            mime = resp.headers.get("Content-Type", "") or ""

            if prev_hash != content_hash:
                op = Operation.ADD if prev_hash is None else Operation.MODIFY
                self._seen_hashes[url] = content_hash
                event = self._make_change_event(
                    path=url,
                    operation=op,
                    modality=Modality.TEXT,
                    content_hash=content_hash,
                    size_bytes=len(body),
                    content_ref=_build_content_ref(body, url),
                    content_mime=mime or None,
                )
                from adapters.web_single import _parse_last_modified
                event.content_modified_at = _parse_last_modified(
                    resp.headers.get("Last-Modified")
                )
                yield event
                pages_emitted += 1

            # Enqueue out-links only for HTML responses + within depth budget.
            if depth + 1 <= self._cfg.max_depth and "html" in mime.lower():
                for link in _extract_links(body, base_url=url):
                    canon = _canonicalize(link)
                    if canon in visited:
                        continue
                    queue.append((canon, depth + 1))

    async def _rate_limit_wait(self) -> None:
        """Ensure ``_min_request_spacing`` seconds elapse between fetches."""
        now = time.monotonic()
        delta = now - self._last_request_at
        if delta < self._min_request_spacing:
            await asyncio.sleep(self._min_request_spacing - delta)
        self._last_request_at = time.monotonic()

    async def _load_robots(self, origin: str) -> None:
        if self._robots_checked_for == origin:
            return
        assert self._client is not None
        self._robots_checked_for = origin
        robots_url = origin.rstrip("/") + "/robots.txt"
        parser = RobotFileParser()
        try:
            resp = await self._client.get(robots_url)
            if resp.status_code >= 400:
                parser.parse([])   # permissive: no robots.txt = allow all
            else:
                parser.parse(resp.text.splitlines())
        except httpx.HTTPError as e:
            logger.info("robots.txt fetch failed origin=%s err=%s", origin, e)
            parser.parse([])
        self._robots = parser

    def _robots_allows(self, url: str) -> bool:
        if self._robots is None:
            return True
        assert self._cfg is not None
        return self._robots.can_fetch(self._cfg.user_agent, url)


# ── Module-level helpers (reuse from web_single where possible) ─────


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _build_content_ref(body: bytes, url: str) -> ContentRef:
    if len(body) < INLINE_BYTES_THRESHOLD:
        return ContentRef(
            kind="inline_bytes",
            bytes_b64=base64.b64encode(body).decode("ascii"),
        )
    return ContentRef(kind="url", url=url)


def _extract_links(body: bytes, *, base_url: str) -> list[str]:
    """Return absolute URLs from every ``<a href>`` in the HTML body.

    Any non-HTTP scheme (mailto:, javascript:, tel:, data:) is dropped.
    """
    try:
        tree = HTMLParser(body)
    except Exception as e:   # pragma: no cover — selectolax is very tolerant
        logger.warning("html parse failed for %s err=%s", base_url, e)
        return []
    out: list[str] = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href")
        if not href:
            continue
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        scheme = urlparse(absolute).scheme
        if scheme not in ("http", "https"):
            continue
        out.append(absolute)
    return out


def _default_refresh_interval(cron: str) -> float:
    # Delegate to the web_single helper so both adapters behave identically.
    from adapters.web_single import _cron_to_sleep_seconds
    return _cron_to_sleep_seconds(cron)


__all__ = [
    "WebDeepAdapter",
    "INLINE_BYTES_THRESHOLD",
    "HARD_MAX_DEPTH",
    "HARD_MAX_PAGES",
]
