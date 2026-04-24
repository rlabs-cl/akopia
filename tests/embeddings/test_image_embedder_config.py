"""Tests that the image embedder honors akopia.yaml / env configuration."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_image_state():
    """Ensure each test starts with fresh module globals."""
    from embeddings import main as m
    m.clip_model = None
    m.image_model_name = None
    yield
    m.clip_model = None
    m.image_model_name = None


def _fake_config(provider: str | None, model: str | None):
    image_cfg = SimpleNamespace(provider=provider, model=model, enabled=True)
    embeddings_cfg = SimpleNamespace(image=image_cfg, text=SimpleNamespace())
    core_cfg = SimpleNamespace(embeddings=embeddings_cfg)
    return SimpleNamespace(core=core_cfg)


def test_load_clip_model_uses_config_model():
    """akopia.yaml model should be passed verbatim to fastembed.ImageEmbedding."""
    from embeddings import main as m

    fake_cls = MagicMock(return_value="fake-image-embedder")
    fake_fastembed = SimpleNamespace(ImageEmbedding=fake_cls)

    with patch.object(m, "load_config", create=True), \
         patch("common.config_loader.load_config",
               return_value=_fake_config(provider="fastembed",
                                          model="custom/my-clip-v2")), \
         patch.dict(sys.modules, {"fastembed": fake_fastembed}):
        model = m.load_clip_model()

    fake_cls.assert_called_once_with(model_name="custom/my-clip-v2")
    assert model == "fake-image-embedder"
    assert m.image_model_name == "custom/my-clip-v2"


def test_load_clip_model_defaults_when_no_config(monkeypatch):
    """With no akopia.yaml and no env, the historical default is used."""
    from embeddings import main as m

    def _raise(*_a, **_kw):
        raise FileNotFoundError("akopia.yaml absent in test")

    fake_cls = MagicMock(return_value="default-model")
    fake_fastembed = SimpleNamespace(ImageEmbedding=fake_cls)
    monkeypatch.delenv("IMAGE_EMBEDDER_PROVIDER", raising=False)
    monkeypatch.delenv("IMAGE_EMBEDDER_MODEL", raising=False)

    with patch("common.config_loader.load_config", side_effect=_raise), \
         patch.dict(sys.modules, {"fastembed": fake_fastembed}):
        m.load_clip_model()

    fake_cls.assert_called_once_with(model_name=m.DEFAULT_IMAGE_MODEL)
    assert m.image_model_name == m.DEFAULT_IMAGE_MODEL


def test_load_clip_model_env_override(monkeypatch):
    """Env vars override defaults when akopia.yaml is absent."""
    from embeddings import main as m

    def _raise(*_a, **_kw):
        raise FileNotFoundError("akopia.yaml absent in test")

    fake_cls = MagicMock(return_value="env-model")
    fake_fastembed = SimpleNamespace(ImageEmbedding=fake_cls)
    monkeypatch.setenv("IMAGE_EMBEDDER_PROVIDER", "fastembed")
    monkeypatch.setenv("IMAGE_EMBEDDER_MODEL", "env/override-model")

    with patch("common.config_loader.load_config", side_effect=_raise), \
         patch.dict(sys.modules, {"fastembed": fake_fastembed}):
        m.load_clip_model()

    fake_cls.assert_called_once_with(model_name="env/override-model")


def test_unsupported_provider_raises():
    from embeddings import main as m
    with pytest.raises(ValueError, match="Unsupported image embedder provider"):
        m._build_image_model("does-not-exist", "any")


@pytest.mark.asyncio
async def test_health_reports_image_model():
    """/health should surface the resolved image_model when loaded."""
    from embeddings import main as m

    fake_cls = MagicMock(return_value="loaded")
    fake_fastembed = SimpleNamespace(ImageEmbedding=fake_cls)

    def _raise(*_a, **_kw):
        raise FileNotFoundError("akopia.yaml absent")

    with patch("common.config_loader.load_config", side_effect=_raise), \
         patch.dict(sys.modules, {"fastembed": fake_fastembed}):
        m.load_clip_model()

    health = await m.health()
    assert health["image_model_loaded"] is True
    assert health["image_model"] == m.DEFAULT_IMAGE_MODEL
