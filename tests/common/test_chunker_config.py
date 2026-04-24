"""Tests that chunk_text honors get_chunker_config() + akopia.yaml wiring."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from common import chunker
from common.chunker import (
    ChunkerConfig,
    chunk_text,
    get_chunker_config,
    reset_chunker_config,
)
from common.config_loader import ConfigLoader


MINIMAL_YAML = """
version: 1
core:
  storage:
    vector:  {{ url: "http://qdrant:6333" }}
    lexical: {{ url: "http://meilisearch:7700" }}
    queue:   {{ url: "redis://redis:6379" }}
  embeddings:
    text:  {{ provider: fastembed, model: m, quantized: true }}
    image: {{ enabled: false }}
  router:
    max_event_depth: 3
    idempotency_ttl: 7d
  auth:
    mode: bearer-static
    token: "x"
  chunker:
    strategy: {strategy}
    chunk_size_tokens: {size}
    overlap_tokens: {overlap}
sources:
  - id: docs
    type: folder
    config: {{}}
extractors:
  - type: plain
    config: {{}}
"""


@pytest.fixture(autouse=True)
def _reset():
    reset_chunker_config()
    yield
    reset_chunker_config()


def test_defaults_without_config():
    cfg = get_chunker_config()
    assert cfg.strategy == "recursive"
    assert cfg.chunk_size_tokens == 512
    assert cfg.overlap_tokens == 50


def test_env_override(monkeypatch):
    monkeypatch.setenv("AKOPIA_CHUNK_STRATEGY", "paragraph")
    monkeypatch.setenv("AKOPIA_CHUNK_SIZE_TOKENS", "256")
    monkeypatch.setenv("AKOPIA_CHUNK_OVERLAP_TOKENS", "10")
    reset_chunker_config()

    cfg = get_chunker_config()
    assert cfg.strategy == "paragraph"
    assert cfg.chunk_size_tokens == 256
    assert cfg.overlap_tokens == 10


def test_kb_yaml_config(tmp_path: Path, monkeypatch):
    body = MINIMAL_YAML.format(strategy="paragraph", size=128, overlap=20)
    path = tmp_path / "akopia.yaml"
    path.write_text(textwrap.dedent(body))
    monkeypatch.setenv("AKOPIA_CONFIG_PATH", str(path))
    # Make sure no stray env overrides are in play.
    monkeypatch.delenv("AKOPIA_CHUNK_STRATEGY", raising=False)
    monkeypatch.delenv("AKOPIA_CHUNK_SIZE_TOKENS", raising=False)
    monkeypatch.delenv("AKOPIA_CHUNK_OVERLAP_TOKENS", raising=False)

    # Sanity: loader reads the chunker section at all.
    loaded = ConfigLoader(path=path, env={}).load()
    assert loaded.core.chunker.strategy == "paragraph"
    assert loaded.core.chunker.chunk_size_tokens == 128
    assert loaded.core.chunker.overlap_tokens == 20

    reset_chunker_config()
    cfg = get_chunker_config()
    assert cfg.strategy == "paragraph"
    assert cfg.chunk_size_tokens == 128
    assert cfg.overlap_tokens == 20


def test_chunk_text_uses_config_when_args_none():
    """If chunk_text() is called without size/overlap/strategy, it must
    resolve them from get_chunker_config, not the old 512/50/recursive
    hard-codes."""
    fake = ChunkerConfig(strategy="recursive", chunk_size_tokens=40, overlap_tokens=0)
    with patch.object(chunker, "get_chunker_config", return_value=fake):
        text = "word " * 500
        chunks = chunk_text(text, path="x.md")
    # With 40-token cap, we expect multiple chunks (500 words ≈ 500 tokens).
    assert len(chunks) > 1


def test_chunk_text_respects_explicit_override():
    """Explicit args still win over config (per-call override path)."""
    fake = ChunkerConfig(strategy="recursive", chunk_size_tokens=40, overlap_tokens=0)
    with patch.object(chunker, "get_chunker_config", return_value=fake):
        chunks_big = chunk_text("word " * 500, path="x.md", max_tokens=10_000)
    # max_tokens=10000 keeps whole text in one chunk, overriding config.
    assert len(chunks_big) == 1
