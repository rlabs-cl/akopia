"""IndexManager.upsert must raise loudly for unsupported modalities."""
from __future__ import annotations

import pytest

from common.models import EmbeddingEntry
from concentrador.index_manager import IndexManager, UnsupportedModalityError


def _entry(modality: str) -> EmbeddingEntry:
    return EmbeddingEntry(
        chunk_id=f"test:{modality}:0",
        vector=[0.0] * 8,
        model="test-model",
        modality=modality,
        path="/x",
        source_id="src",
        snippet="",
    )


@pytest.mark.asyncio
async def test_upsert_rejects_unknown_modality():
    # 'pdf' is the canonical "used to be a modality, now isn't" case.
    im = IndexManager("http://q", "", "http://m", "")
    with pytest.raises(UnsupportedModalityError) as exc:
        await im.upsert(_entry("pdf"))
    msg = str(exc.value)
    assert "pdf" in msg
    assert "IndexManager.upsert" in msg


@pytest.mark.asyncio
async def test_upsert_rejects_garbage_modality():
    im = IndexManager("http://q", "", "http://m", "")
    with pytest.raises(UnsupportedModalityError):
        await im.upsert(_entry("not-a-modality"))
