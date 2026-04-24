"""HTMLExtractor — article extraction from raw HTML via trafilatura.

Dependencies (pip packages):
    trafilatura — state-of-the-art boilerplate removal (nav/ads/footer).
                  Exposes both ``extract`` (main text) and ``extract_metadata``
                  (title, author, date, sitename, language). Higher
                  quality than readability-lxml on modern article pages.

Priority 20 — wins over ``plain`` (priority 0) for .html/.htm files so
boilerplate is stripped before chunking. Raw-HTML-only; pptx/docx bodies
are owned by OfficeExtractor regardless of any embedded HTML fragments.
"""
from __future__ import annotations

from common.base_extractor import BaseExtractor
from common.models import ChangeEvent, ContentRef, ExtractedContent


class HTMLExtractor(BaseExtractor):
    plugin_id = "html"
    version = "0.1.0"
    priority = 20

    supported_mime_types = ["text/html", "application/xhtml+xml"]
    supported_extensions = [".html", ".htm"]

    async def configure(self, config: dict) -> None:
        cfg = dict(config or {})
        self.strip_nav: bool = bool(cfg.get("strip_nav", True))
        self.preserve_code_blocks: bool = bool(cfg.get("preserve_code_blocks", True))
        self.include_tables: bool = bool(cfg.get("include_tables", True))

    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        import trafilatura

        html = await self._fetch_as_text(content_ref)

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=self.include_tables,
            include_formatting=self.preserve_code_blocks,
            favor_precision=self.strip_nav,
        ) or ""

        metadata: dict = {"format": "html"}
        try:
            meta = trafilatura.extract_metadata(html)
        except Exception:
            meta = None
        if meta is not None:
            as_dict = meta.as_dict() if hasattr(meta, "as_dict") else {}
            for key in ("title", "author", "date", "sitename", "language"):
                val = as_dict.get(key)
                if val:
                    metadata[key] = val

        return ExtractedContent(
            source_event_id=event.event_id,
            text=text,
            metadata=metadata,
            extractor_id=self.plugin_id,
            extractor_version=self.version,
        )


__all__ = ["HTMLExtractor"]
