"""delete_by_source and reindex endpoint shape tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from concentrador.index_manager import IndexManager


@pytest.mark.asyncio
async def test_delete_by_source_returns_counts(monkeypatch):
    im = IndexManager("http://q", "", "http://m", "")

    class Resp:
        def __init__(self, j): self._j = j
        def json(self): return self._j

    call_log: list[tuple[str, dict]] = []

    async def fake_post(url, json=None, headers=None):  # noqa: A002
        call_log.append((url, json or {}))
        if "/points/count" in url:
            return Resp({"result": {"count": 42}})
        if "/search" in url:
            return Resp({"estimatedTotalHits": 7})
        return Resp({})

    monkeypatch.setattr(im._http, "post", AsyncMock(side_effect=fake_post))

    counts = await im.delete_by_source("example-org")

    assert counts["qdrant_before"]["akopia_text"] == 42
    assert counts["qdrant_before"]["akopia_image"] == 42
    assert counts["meili_before"] == 7

    # 2x count + 2x delete on qdrant, 1x search + 1x delete on meili = 6 calls
    assert len(call_log) == 6
    # Every qdrant call carries the source_id filter.
    qdrant_calls = [c for c in call_log if "/collections/" in c[0]]
    for _, body in qdrant_calls:
        assert body["filter"]["must"][0]["match"]["value"] == "example-org"
