"""Tests for common.registry (Slice 1)."""
from __future__ import annotations

import pytest

from common.registry import PluginNotFound, PluginRegistry


class _FakeAdapter:
    plugin_id = "fake-source"


class _FakeExtractor:
    plugin_id = "fake-extractor"
    supported_extensions = [".fake"]


class TestPluginRegistry:
    def test_scan_idempotent(self):
        r = PluginRegistry()
        r.scan()
        first = r.list_source_adapters()
        r.scan()
        assert r.list_source_adapters() == first

    def test_manual_registration_source(self):
        r = PluginRegistry()
        r.register_source_adapter("fake-source", _FakeAdapter)
        assert "fake-source" in r.list_source_adapters()
        assert r.get_source_adapter_class("fake-source") is _FakeAdapter

    def test_manual_registration_extractor(self):
        r = PluginRegistry()
        r.register_extractor("fake-extractor", _FakeExtractor)
        assert "fake-extractor" in r.list_extractors()
        assert r.get_extractor_class("fake-extractor") is _FakeExtractor

    def test_missing_source_raises_plugin_not_found(self):
        r = PluginRegistry()
        with pytest.raises(PluginNotFound) as exc:
            r.get_source_adapter_class("does-not-exist")
        # Error message should list what IS available to help the user
        assert "does-not-exist" in str(exc.value)

    def test_missing_extractor_raises_plugin_not_found(self):
        r = PluginRegistry()
        with pytest.raises(PluginNotFound):
            r.get_extractor_class("does-not-exist")
