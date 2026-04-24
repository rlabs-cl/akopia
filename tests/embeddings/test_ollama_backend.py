"""Unit tests for OllamaBackend HTTP contract."""
import pytest
import respx
from httpx import Response

from embeddings.backends.ollama_backend import OllamaBackend


@pytest.mark.asyncio
@respx.mock
async def test_batch_endpoint_preferred():
    route = respx.post("http://ollama:11434/api/embed").mock(
        return_value=Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
    )
    backend = OllamaBackend(model="nomic-embed-text", url="http://ollama:11434")
    vectors = await backend.embed(["hello"])
    assert route.called
    assert vectors == [[0.1, 0.2, 0.3]]
    assert backend.dim() == 3


@pytest.mark.asyncio
@respx.mock
async def test_fallback_to_legacy_on_404():
    respx.post("http://ollama:11434/api/embed").mock(
        return_value=Response(404, json={"error": "not found"})
    )
    legacy = respx.post("http://ollama:11434/api/embeddings").mock(
        return_value=Response(200, json={"embedding": [0.5, 0.6]})
    )
    backend = OllamaBackend(url="http://ollama:11434")
    vectors = await backend.embed(["a", "b"])
    assert legacy.call_count == 2
    assert vectors == [[0.5, 0.6], [0.5, 0.6]]


def test_model_id_default():
    b = OllamaBackend()
    assert b.model_id() == "nomic-embed-text"


def test_url_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://env-host:11434")
    b = OllamaBackend()
    assert b._url == "http://env-host:11434"
