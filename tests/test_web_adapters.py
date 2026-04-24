"""Tests for Slice 7 web source adapters.

Covers:
- ``WebSingleAdapter`` conditional-GET + content-hash dedup + inline/url
  content_ref threshold.
- ``WebDeepAdapter`` BFS depth, cycle breaking, robots.txt, rate-limit,
  ``max_pages`` ceiling.

All network I/O is mocked with ``respx`` so the suite is deterministic
and runs offline. Redis I/O uses the ``_FakeRedis`` pattern copied from
``tests/test_base_adapter.py`` — zero external services.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from adapters.web_deep import (
    WebDeepAdapter,
    _canonicalize,
    _extract_links,
    _parse_rate_limit,
)
from adapters.web_single import INLINE_BYTES_THRESHOLD, WebSingleAdapter


# ── Fake Redis (mirrors tests/test_base_adapter.py) ─────────────────


class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []

    async def connect(self) -> None:
        pass

    async def ensure_stream_and_group(self, stream: str, group: str) -> None:
        self.groups.append((stream, group))

    async def publish(self, stream: str, payload: dict) -> None:
        self.published.append((stream, payload))

    async def close(self) -> None:
        pass


# ── web-single ──────────────────────────────────────────────────────


class TestWebSingleAdapter:
    URL = "https://example.com/page"

    def _make(self) -> WebSingleAdapter:
        # Pass a real AsyncClient so respx can intercept.
        client = httpx.AsyncClient()
        adapter = WebSingleAdapter(
            instance_id="demo",
            redis_client=_FakeRedis(),
            client=client,
        )
        adapter._poll_seconds = 0.001   # keep the loop snappy in tests
        return adapter

    @pytest.mark.asyncio
    async def test_configure_requires_url(self):
        a = WebSingleAdapter(instance_id="demo")
        with pytest.raises(ValueError):
            await a.configure({})

    @respx.mock
    @pytest.mark.asyncio
    async def test_first_poll_emits_add_event(self):
        respx.get(self.URL).mock(
            return_value=httpx.Response(
                200,
                text="<html>hello</html>",
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "ETag": 'W/"v1"',
                    "Last-Modified": "Mon, 01 Apr 2026 00:00:00 GMT",
                },
            )
        )
        a = self._make()
        await a.configure({"url": self.URL})
        event = await a._poll_once()
        assert event is not None
        assert event.operation.value == "add"
        assert event.path == self.URL
        assert event.modality.value == "text"
        assert event.content_mime.startswith("text/html")
        # Body is small — should be inlined.
        assert event.content_ref.kind == "inline_bytes"
        decoded = base64.b64decode(event.content_ref.bytes_b64)
        assert decoded == b"<html>hello</html>"
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_unchanged_content_does_not_emit(self):
        route = respx.get(self.URL).mock(
            return_value=httpx.Response(
                200,
                text="<html>same</html>",
                headers={"Content-Type": "text/html"},
            )
        )
        a = self._make()
        await a.configure({"url": self.URL})
        first = await a._poll_once()
        assert first is not None
        # Second fetch returns identical body (server doesn't honour
        # conditional headers but adapter still dedups via hash).
        second = await a._poll_once()
        assert second is None
        assert route.call_count == 2
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_304_not_modified_is_noop(self):
        responses = [
            httpx.Response(
                200,
                text="<html>v1</html>",
                headers={"Content-Type": "text/html", "ETag": 'W/"v1"'},
            ),
            httpx.Response(304),
        ]
        respx.get(self.URL).mock(side_effect=responses)

        a = self._make()
        await a.configure({"url": self.URL})
        first = await a._poll_once()
        assert first is not None
        second = await a._poll_once()
        assert second is None   # 304 must not emit
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_changed_content_emits_modify(self):
        responses = [
            httpx.Response(
                200, text="<html>v1</html>",
                headers={"Content-Type": "text/html", "ETag": 'W/"v1"'},
            ),
            httpx.Response(
                200, text="<html>v2-different</html>",
                headers={"Content-Type": "text/html", "ETag": 'W/"v2"'},
            ),
        ]
        respx.get(self.URL).mock(side_effect=responses)

        a = self._make()
        await a.configure({"url": self.URL})
        first = await a._poll_once()
        second = await a._poll_once()
        assert first.operation.value == "add"
        assert second is not None
        assert second.operation.value == "modify"
        assert first.content_hash != second.content_hash
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_large_body_uses_url_content_ref(self):
        big = b"x" * (INLINE_BYTES_THRESHOLD + 1)
        respx.get(self.URL).mock(
            return_value=httpx.Response(
                200,
                content=big,
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        a = self._make()
        await a.configure({"url": self.URL})
        event = await a._poll_once()
        assert event is not None
        assert event.content_ref.kind == "url"
        assert event.content_ref.url == self.URL
        assert event.size_bytes == len(big)
        await a.close()


# ── web-deep ────────────────────────────────────────────────────────


ROOT = "https://site.test/"
PAGE_A = "https://site.test/a"
PAGE_B = "https://site.test/b"
EXTERNAL = "https://other.test/"


def _html(links: list[str], body: str = "hello") -> str:
    hrefs = "".join(f'<a href="{url}">x</a>' for url in links)
    return f"<html><body>{body}{hrefs}</body></html>"


class TestWebDeepAdapter:
    def _make(
        self,
        *,
        rate_limit: str = "100/s",
        max_pages: int = 10,
        max_depth: int = 3,
        respect_robots: bool = False,
        same_origin_only: bool = True,
    ) -> WebDeepAdapter:
        client = httpx.AsyncClient()
        a = WebDeepAdapter(
            instance_id="site",
            redis_client=_FakeRedis(),
            client=client,
        )
        a._poll_seconds = 0.001
        # configure happens in each test
        return a

    @pytest.mark.asyncio
    async def test_configure_requires_root(self):
        a = WebDeepAdapter(instance_id="site")
        with pytest.raises(ValueError):
            await a.configure({})

    @pytest.mark.asyncio
    async def test_parse_rate_limit(self):
        assert _parse_rate_limit("1/s") == 1.0
        assert _parse_rate_limit("100/s") == pytest.approx(0.01)
        assert _parse_rate_limit("60/min") == 1.0
        with pytest.raises(ValueError):
            _parse_rate_limit("nonsense")
        with pytest.raises(ValueError):
            _parse_rate_limit("1/hour")

    @pytest.mark.asyncio
    async def test_canonicalize_drops_fragment_and_lowercases_host(self):
        assert _canonicalize("HTTPS://Example.COM/a#frag") == "https://example.com/a"
        assert (
            _canonicalize("https://ex.com/a?q=1#top")
            == "https://ex.com/a?q=1"
        )

    @pytest.mark.asyncio
    async def test_extract_links_absolute_and_filtered(self):
        body = (
            b'<a href="/a">x</a>'
            b'<a href="https://other.test/b">y</a>'
            b'<a href="mailto:x@y">z</a>'
            b'<a href="#top">w</a>'
            b'<a href="javascript:void(0)">v</a>'
        ).decode()
        links = _extract_links(body.encode(), base_url="https://site.test/")
        assert "https://site.test/a" in links
        assert "https://other.test/b" in links
        assert all(not link.startswith("mailto:") for link in links)
        assert all("javascript" not in link for link in links)

    @respx.mock
    @pytest.mark.asyncio
    async def test_bfs_crawl_three_pages_with_cycle(self):
        # root → a (depth 1) → b (depth 2) → root (cycle, ignored)
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200,
                text=_html([PAGE_A, EXTERNAL]),
                headers={"Content-Type": "text/html"},
            )
        )
        respx.get(PAGE_A).mock(
            return_value=httpx.Response(
                200,
                text=_html([PAGE_B]),
                headers={"Content-Type": "text/html"},
            )
        )
        respx.get(PAGE_B).mock(
            return_value=httpx.Response(
                200,
                text=_html([ROOT]),   # links back — must not re-crawl root
                headers={"Content-Type": "text/html"},
            )
        )

        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "100/s",
            "max_depth": 3,
            "max_pages": 10,
            "respect_robots": False,
            "same_origin_only": True,
        })

        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            events = [e async for e in a._crawl()]

        paths = [e.path for e in events]
        assert ROOT in paths
        assert PAGE_A in paths
        assert PAGE_B in paths
        assert EXTERNAL not in paths        # same_origin_only
        assert len(paths) == 3              # cycle broken
        # Each event is ADD on first crawl
        assert all(e.operation.value == "add" for e in events)
        # Inline bytes (tiny bodies)
        for e in events:
            assert e.content_ref.kind == "inline_bytes"
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_max_depth_respected(self):
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200, text=_html([PAGE_A]),
                headers={"Content-Type": "text/html"},
            )
        )
        respx.get(PAGE_A).mock(
            return_value=httpx.Response(
                200, text=_html([PAGE_B]),
                headers={"Content-Type": "text/html"},
            )
        )
        page_b_route = respx.get(PAGE_B).mock(
            return_value=httpx.Response(200, text="<html/>",
                                        headers={"Content-Type": "text/html"})
        )

        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "100/s",
            "max_depth": 1,          # root (d0) and PAGE_A (d1) only
            "max_pages": 10,
            "respect_robots": False,
        })
        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            events = [e async for e in a._crawl()]

        assert {e.path for e in events} == {ROOT, PAGE_A}
        assert page_b_route.call_count == 0
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_max_pages_respected(self):
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200, text=_html([PAGE_A, PAGE_B]),
                headers={"Content-Type": "text/html"},
            )
        )
        respx.get(PAGE_A).mock(
            return_value=httpx.Response(200, text="<html/>",
                                        headers={"Content-Type": "text/html"})
        )
        respx.get(PAGE_B).mock(
            return_value=httpx.Response(200, text="<html/>",
                                        headers={"Content-Type": "text/html"})
        )

        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "100/s",
            "max_pages": 2,     # root + 1 child only
            "max_depth": 3,
            "respect_robots": False,
        })
        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            events = [e async for e in a._crawl()]

        assert len(events) == 2
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_robots_txt_disallow_respected(self):
        respx.get("https://site.test/robots.txt").mock(
            return_value=httpx.Response(
                200,
                text="User-agent: *\nDisallow: /a\n",
                headers={"Content-Type": "text/plain"},
            )
        )
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200, text=_html([PAGE_A]),
                headers={"Content-Type": "text/html"},
            )
        )
        blocked = respx.get(PAGE_A).mock(
            return_value=httpx.Response(
                200, text="<html/>",
                headers={"Content-Type": "text/html"},
            )
        )

        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "100/s",
            "max_depth": 2,
            "max_pages": 10,
            "respect_robots": True,
        })
        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            events = [e async for e in a._crawl()]

        paths = [e.path for e in events]
        assert ROOT in paths
        assert PAGE_A not in paths          # blocked by robots
        assert blocked.call_count == 0      # and never actually fetched
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_invokes_sleep(self):
        # Use a *slow* configured limit and mock sleep so the test stays fast.
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200, text=_html([PAGE_A]),
                headers={"Content-Type": "text/html"},
            )
        )
        respx.get(PAGE_A).mock(
            return_value=httpx.Response(
                200, text="<html/>",
                headers={"Content-Type": "text/html"},
            )
        )

        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "1/s",       # 1s spacing — would be slow for real
            "max_depth": 2,
            "max_pages": 10,
            "respect_robots": False,
        })

        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            events = [e async for e in a._crawl()]

        # Second page fetch should have triggered a positive-duration sleep.
        positive_sleeps = [
            call for call in sleep_mock.call_args_list
            if call.args and call.args[0] > 0
        ]
        assert positive_sleeps, "rate limit should have invoked asyncio.sleep(>0)"
        assert len(events) == 2
        await a.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_second_crawl_dedups_unchanged_pages(self):
        # A page that returns byte-identical content on two successive
        # crawls must only emit once.
        respx.get(ROOT).mock(
            return_value=httpx.Response(
                200, text="<html>stable</html>",
                headers={"Content-Type": "text/html"},
            )
        )
        a = self._make()
        await a.configure({
            "root": ROOT,
            "rate_limit": "100/s",
            "max_depth": 1,
            "max_pages": 5,
            "respect_robots": False,
        })
        # Replace asyncio.sleep with an AsyncMock so the rate-limit wait
        # returns instantly. We only assert on *call args*, not duration.
        sleep_mock = AsyncMock(return_value=None)
        with patch("adapters.web_deep.asyncio.sleep", sleep_mock):
            first = [e async for e in a._crawl()]
            second = [e async for e in a._crawl()]
        assert len(first) == 1
        assert len(second) == 0
        await a.close()
