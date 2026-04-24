"""Plugin discovery via Python entry points.

Third-party plugins register themselves in their own ``pyproject.toml``:

    [project.entry-points."akopia.source_adapter"]
    notion = "akopia_notion:NotionAdapter"

    [project.entry-points."akopia.content_extractor"]
    epub = "akopia_epub:EPUBExtractor"

``pip install akopia-adapter-notion`` makes the plugin discoverable.
This module resolves the entry point name (``notion``, ``epub``) to the
class object so the runner can instantiate it.

Rationale: avoid hardcoded plugin lists in core code; let the Python
packaging ecosystem be the source of truth. Works identically in
docker-compose (plugins baked into image) and in k8s.
"""
from __future__ import annotations

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SOURCE_ADAPTER_GROUP = "akopia.source_adapter"
CONTENT_EXTRACTOR_GROUP = "akopia.content_extractor"


class PluginRegistry:
    """Discovers installed plugins and maps ``type:`` → class.

    Intended lifecycle: instantiate once at startup, call ``scan()``,
    then use ``get_source_adapter_class()`` / ``get_extractor_class()``
    to resolve akopia.yaml entries.
    """

    def __init__(self) -> None:
        self._sources: dict[str, type] = {}
        self._extractors: dict[str, type] = {}
        self._scanned = False

    def scan(self) -> None:
        """Load all registered entry points. Idempotent."""
        if self._scanned:
            return
        self._sources = self._load_group(SOURCE_ADAPTER_GROUP)
        self._extractors = self._load_group(CONTENT_EXTRACTOR_GROUP)
        self._scanned = True
        logger.info(
            "plugin registry: %d source adapter(s), %d extractor(s)",
            len(self._sources), len(self._extractors),
        )

    def _load_group(self, group: str) -> dict[str, type]:
        result: dict[str, type] = {}
        eps: Iterable[EntryPoint] = entry_points(group=group)
        for ep in eps:
            try:
                cls = ep.load()
            except Exception as e:  # pragma: no cover — env-specific
                logger.error(
                    "failed to load entry point %s:%s → %s: %s",
                    group, ep.name, ep.value, e,
                )
                continue
            # Sanity: class attr plugin_id must match entry-point name
            plugin_id = getattr(cls, "plugin_id", None)
            if plugin_id and plugin_id != ep.name:
                logger.warning(
                    "entry point name=%r does not match class plugin_id=%r (%s); "
                    "using entry-point name",
                    ep.name, plugin_id, cls.__name__,
                )
            result[ep.name] = cls
        return result

    # ── Public lookup API ─────────────────────────────────────────

    def list_source_adapters(self) -> list[str]:
        self.scan()
        return sorted(self._sources.keys())

    def list_extractors(self) -> list[str]:
        self.scan()
        return sorted(self._extractors.keys())

    def get_source_adapter_class(self, type_name: str) -> type:
        self.scan()
        try:
            return self._sources[type_name]
        except KeyError:
            raise PluginNotFound(
                f"source adapter {type_name!r} not registered. "
                f"Available: {self.list_source_adapters()}"
            ) from None

    def get_extractor_class(self, type_name: str) -> type:
        self.scan()
        try:
            return self._extractors[type_name]
        except KeyError:
            raise PluginNotFound(
                f"content extractor {type_name!r} not registered. "
                f"Available: {self.list_extractors()}"
            ) from None

    # ── Manual registration (useful for tests and in-repo plugins) ──

    def register_source_adapter(self, name: str, cls: type) -> None:
        self.scan()
        self._sources[name] = cls

    def register_extractor(self, name: str, cls: type) -> None:
        self.scan()
        self._extractors[name] = cls


class PluginNotFound(LookupError):
    """Raised when akopia.yaml references a type_name that isn't registered."""


#: Module-level default registry. Most callers use this; tests make
#: their own instances to avoid cross-test pollution.
default_registry = PluginRegistry()


__all__ = ["PluginRegistry", "PluginNotFound", "default_registry",
           "SOURCE_ADAPTER_GROUP", "CONTENT_EXTRACTOR_GROUP"]
