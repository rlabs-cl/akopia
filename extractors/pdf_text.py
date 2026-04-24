"""PdfTextExtractor — native-text PDF via pypdfium2.

Dependencies (pip packages):
    pypdfium2 — pure-python binding to PDFium (the Chrome PDF engine).
                Ships a prebuilt wheel, no system libs. Fast and
                well-maintained; ideal for text-native PDFs.

Scanned/image-only PDFs surface with <50 chars of extracted text — we
stamp ``metadata['likely_scanned'] = True`` so the Tier 2 OCR extractor
(Slice-N) can pick them up on re-queue.
"""
from __future__ import annotations

from common.base_extractor import BaseExtractor
from common.models import ChangeEvent, ContentRef, ExtractedContent, Page


# If the total extracted text is below this threshold we flag the PDF as
# likely scanned (probably image-only and needs OCR).
_LIKELY_SCANNED_CHAR_THRESHOLD = 50


class PdfTextExtractor(BaseExtractor):
    plugin_id = "pdf-text"
    version = "0.1.0"
    priority = 10

    supported_mime_types = ["application/pdf"]
    supported_extensions = [".pdf"]

    async def configure(self, config: dict) -> None:
        self._config = dict(config or {})

    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        import pypdfium2 as pdfium

        raw = await self._fetch_bytes(content_ref)
        pdf = pdfium.PdfDocument(raw)
        try:
            pages: list[Page] = []
            text_parts: list[str] = []
            for i in range(len(pdf)):
                page = pdf[i]
                textpage = page.get_textpage()
                try:
                    page_text = textpage.get_text_range() or ""
                finally:
                    textpage.close()
                    page.close()
                pages.append(Page(index=i, text=page_text))
                text_parts.append(page_text)
        finally:
            pdf.close()

        full_text = "\n".join(text_parts)
        metadata: dict = {"format": "pdf", "page_count": len(pages)}
        if len(full_text.strip()) < _LIKELY_SCANNED_CHAR_THRESHOLD:
            metadata["likely_scanned"] = True

        return ExtractedContent(
            source_event_id=event.event_id,
            text=full_text,
            pages=pages,
            metadata=metadata,
            extractor_id=self.plugin_id,
            extractor_version=self.version,
        )


__all__ = ["PdfTextExtractor"]
