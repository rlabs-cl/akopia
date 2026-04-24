"""`/embed` endpoint request-model handling.

Before this slice, ``POST /embed`` with ``model="image"`` passed the
request ``text`` string as a filesystem path straight to
``fastembed.ImageEmbedding.embed`` and failed with an opaque
``FileNotFoundError`` deep inside the worker thread. Now it returns a
clear 400 that points the caller at the adapter-driven ingestion path.

The tests build a throwaway FastAPI app that mounts only the
``embed_query`` route, bypassing the real ``embeddings.main.app``
lifespan (which would try to connect to Redis). The endpoint function
under test is the real one — we're verifying its behaviour, not the
app wiring.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from embeddings.main import embed_query


def _client() -> TestClient:
    app = FastAPI()
    app.post("/embed")(embed_query)
    return TestClient(app)


def test_image_model_returns_400():
    client = _client()
    resp = client.post("/embed", json={"text": "a diagram", "model": "image"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not supported" in detail
    # Must tell the caller what to do instead (adapter path) so the
    # error message is actionable.
    assert "adapter" in detail


def test_unknown_model_returns_400():
    client = _client()
    resp = client.post("/embed", json={"text": "x", "model": "mystery"})
    assert resp.status_code == 400
    assert "mystery" in resp.json()["detail"]


def test_text_model_calls_embed_texts():
    """Happy path still works — regression guard for the if/else shape."""
    async def fake_embed(texts):
        assert texts == ["hello"]
        return [[0.1, 0.2, 0.3]]

    with patch("embeddings.main.embed_texts", side_effect=fake_embed):
        client = _client()
        resp = client.post("/embed", json={"text": "hello", "model": "text"})
    assert resp.status_code == 200
    assert resp.json() == {"vector": [0.1, 0.2, 0.3]}
