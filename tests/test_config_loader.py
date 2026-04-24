"""Tests for the akopia.yaml config loader (Slice 2).

Covers schema validation, env interpolation, and the typed model output.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from common.config_loader import ConfigError, ConfigLoader
from common.akopia_config import AkopiaConfig


def _write(tmp_path: Path, body: str) -> Path:
    """Write a akopia.yaml under tmp_path and return its path."""
    path = tmp_path / "akopia.yaml"
    path.write_text(textwrap.dedent(body).lstrip())
    return path


# ---------- minimal valid fixtures ----------

MINIMAL_YAML = """
version: 1
core:
  storage:
    vector:  { url: "http://qdrant:6333" }
    lexical: { url: "http://meilisearch:7700", master_key: "abc" }
    queue:   { url: "redis://redis:6379" }
  embeddings:
    text:  { provider: fastembed, model: nomic-embed-text-v1.5, quantized: true }
    image: { enabled: false }
  router:
    max_event_depth: 3
    idempotency_ttl: 7d
  auth:
    mode: bearer-static
    token: "sekret"
sources:
  - id: local-docs
    type: folder
    config:
      path: /data/docs
extractors:
  - type: plain
    config: {}
"""


def test_valid_minimal_config_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_YAML)
    cfg = ConfigLoader(path=path, env={}).load()

    assert isinstance(cfg, AkopiaConfig)
    assert cfg.version == 1
    assert len(cfg.sources) == 1
    assert cfg.sources[0].id == "local-docs"
    assert cfg.sources[0].type == "folder"
    assert cfg.sources[0].config["path"] == "/data/docs"
    assert len(cfg.extractors) == 1
    assert cfg.extractors[0].type == "plain"
    assert cfg.core.embeddings.image.enabled is False


# ---------- missing required top-level fields ----------

def test_missing_version_rejected(tmp_path: Path) -> None:
    bad = MINIMAL_YAML.replace("version: 1\n", "")
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(path=path, env={}).load()
    msg = str(exc.value)
    assert "version" in msg


def test_zero_sources_rejected(tmp_path: Path) -> None:
    bad = textwrap.dedent(
        """
        version: 1
        core:
          storage:
            vector:  { url: "http://qdrant:6333" }
            lexical: { url: "http://meilisearch:7700" }
            queue:   { url: "redis://redis:6379" }
          embeddings:
            text:  { provider: fastembed }
            image: { enabled: false }
          router:
            max_event_depth: 3
            idempotency_ttl: 7d
          auth:
            mode: bearer-static
        sources: []
        extractors:
          - type: plain
            config: {}
        """
    ).lstrip()
    path = tmp_path / "akopia.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(path=path, env={}).load()
    assert "sources" in str(exc.value)


def test_zero_extractors_rejected(tmp_path: Path) -> None:
    bad = MINIMAL_YAML.replace(
        "extractors:\n  - type: plain\n    config: {}\n",
        "extractors: []\n",
    )
    path = _write(tmp_path, bad)
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(path=path, env={}).load()
    assert "extractors" in str(exc.value)


# ---------- env interpolation ----------

def test_env_var_required_interpolation(tmp_path: Path) -> None:
    body = MINIMAL_YAML.replace(
        'token: "sekret"', 'token: "${AKOPIA_BEARER_TOKEN}"'
    )
    path = _write(tmp_path, body)
    cfg = ConfigLoader(path=path, env={"AKOPIA_BEARER_TOKEN": "real-token"}).load()
    assert cfg.core.auth.token == "real-token"


def test_env_var_default_used_when_missing(tmp_path: Path) -> None:
    body = MINIMAL_YAML.replace(
        'url: "http://qdrant:6333"',
        'url: "${QDRANT_URL:-http://qdrant:6333}"',
    )
    path = _write(tmp_path, body)
    cfg = ConfigLoader(path=path, env={}).load()
    assert cfg.core.storage.vector.url == "http://qdrant:6333"


def test_env_var_default_overridden_when_set(tmp_path: Path) -> None:
    body = MINIMAL_YAML.replace(
        'url: "http://qdrant:6333"',
        'url: "${QDRANT_URL:-http://qdrant:6333}"',
    )
    path = _write(tmp_path, body)
    cfg = ConfigLoader(
        path=path, env={"QDRANT_URL": "http://prod-qdrant:6333"}
    ).load()
    assert cfg.core.storage.vector.url == "http://prod-qdrant:6333"


def test_missing_env_without_default_errors_with_path(tmp_path: Path) -> None:
    body = MINIMAL_YAML.replace(
        'token: "sekret"', 'token: "${AKOPIA_BEARER_TOKEN}"'
    )
    path = _write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(path=path, env={}).load()
    msg = str(exc.value)
    assert "AKOPIA_BEARER_TOKEN" in msg
    assert "auth.token" in msg  # YAML path pointer


# ---------- unknown fields ----------

def test_unknown_top_level_field_rejected(tmp_path: Path) -> None:
    body = MINIMAL_YAML + "gremlin: yes\n"
    path = _write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        ConfigLoader(path=path, env={}).load()
    assert "gremlin" in str(exc.value) or "additional" in str(exc.value).lower()


# ---------- example RFC config ----------

RFC_YAML = """
version: 1
core:
  storage:
    vector:   { url: "${QDRANT_URL:-http://qdrant:6333}", collection_prefix: "kb_" }
    lexical:  { url: "${MEILI_URL:-http://meilisearch:7700}", master_key: "${MEILI_MASTER_KEY}" }
    queue:    { url: "${REDIS_URL:-redis://redis:6379}" }
  embeddings:
    text:  { provider: fastembed, model: nomic-embed-text-v1.5, quantized: true }
    image: { enabled: false }
  router:
    max_event_depth: 3
    idempotency_ttl: 7d
  auth:
    mode: bearer-static
    token: "${AKOPIA_BEARER_TOKEN}"
sources:
  - id: local-docs
    type: folder
    config:
      path: /data/docs
      include: ["*.md", "*.pdf"]
extractors:
  - type: plain
    config: {}
"""


def test_rfc_example_loads_with_env(tmp_path: Path) -> None:
    path = _write(tmp_path, RFC_YAML)
    cfg = ConfigLoader(
        path=path,
        env={"MEILI_MASTER_KEY": "k", "AKOPIA_BEARER_TOKEN": "t"},
    ).load()
    assert cfg.core.storage.vector.url == "http://qdrant:6333"
    assert cfg.core.storage.lexical.master_key == "k"
    assert cfg.core.auth.token == "t"


# ---------- roundtrip ----------

def test_roundtrip_load_and_revalidate(tmp_path: Path) -> None:
    path = _write(tmp_path, MINIMAL_YAML)
    env: dict[str, str] = {}
    cfg1 = ConfigLoader(path=path, env=env).load()

    # Serialize the model back to a plain dict and run it through the
    # schema validator again via a second loader on a fresh tempfile.
    # exclude_none: Optional-None fields would trip `type: string` checks
    # in the JSON schema.
    dumped = cfg1.model_dump(exclude_none=True)

    import yaml

    path2 = tmp_path / "roundtrip.yaml"
    path2.write_text(yaml.safe_dump(dumped, sort_keys=False))
    cfg2 = ConfigLoader(path=path2, env=env).load()

    assert cfg1 == cfg2


# ---------- path resolution ----------

def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        ConfigLoader(path=tmp_path / "does-not-exist.yaml", env={}).load()


def test_env_var_path_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, MINIMAL_YAML)
    # Supply env dict directly so we don't rely on process env:
    loader = ConfigLoader(env={"AKOPIA_CONFIG_PATH": str(path)})
    cfg = loader.load()
    assert cfg.version == 1
