"""PlainExtractor — passthrough for text-like payloads.

Dependencies: none beyond the stdlib. This extractor is the fallback for
anything the router can decode as UTF-8 text (markdown, code, json,
yaml, csv/tsv, plain .txt, .log).

Handled MIME/extensions deliberately exclude ``text/html`` — HTML files
are routed to ``HTMLExtractor`` (higher priority) so boilerplate stripping
happens before chunking.
"""
from __future__ import annotations

from pathlib import Path

from common.base_extractor import BaseExtractor
from common.models import ChangeEvent, ContentRef, ExtractedContent


class PlainExtractor(BaseExtractor):
    plugin_id = "plain"
    version = "0.1.0"
    priority = 0

    supported_mime_types = [
        "text/plain",
        "text/markdown",
        "application/json",
        "text/yaml",
        "text/x-yaml",
        "text/csv",
        "text/tab-separated-values",
    ]
    supported_extensions = [
        ".txt", ".md", ".rst",
        ".json", ".yaml", ".toml",
        ".csv", ".tsv", ".log",
        ".py", ".js", ".ts", ".go", ".rs",
        ".java", ".c", ".cpp", ".h", ".sh",
    ]

    _TABULAR_DELIMS = {".csv": ",", ".tsv": "\t"}

    async def configure(self, config: dict) -> None:
        # No required config; store whatever was passed for future options.
        self._config = dict(config or {})

    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        text = await self._fetch_as_text(content_ref)

        suffix = Path(event.path or "").suffix.lower()
        structure: dict | None = None
        if suffix in self._TABULAR_DELIMS:
            structure = {
                "format": "tabular",
                "delimiter": self._TABULAR_DELIMS[suffix],
            }

        metadata: dict = {}
        if suffix:
            metadata["format"] = suffix.lstrip(".")

        return ExtractedContent(
            source_event_id=event.event_id,
            text=text,
            structure=structure,
            metadata=metadata,
            extractor_id=self.plugin_id,
            extractor_version=self.version,
        )


__all__ = ["PlainExtractor"]
