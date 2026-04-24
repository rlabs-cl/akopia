"""Layer 2 · Content extractors (Slice 4).

Each extractor class subclasses ``common.base_extractor.BaseExtractor``
and advertises the ``(supported_mime_types, supported_extensions,
priority)`` triplet used by the router (Slice 5) to pick the winner.

Included in Slice 4:

* ``plain``    — passthrough for text/code/markdown/json/yaml/csv (pri 0)
* ``office``   — docx/xlsx/pptx + legacy (pri 10)
* ``pdf-text`` — native-text PDFs via pypdfium2 (pri 10)
* ``html``     — article extraction via trafilatura (pri 20)

Registration via pyproject entry points is Slice 6.
"""
from __future__ import annotations

from extractors.html import HTMLExtractor
from extractors.office import OfficeExtractor
from extractors.pdf_text import PdfTextExtractor
from extractors.plain import PlainExtractor

__all__ = [
    "PlainExtractor",
    "OfficeExtractor",
    "PdfTextExtractor",
    "HTMLExtractor",
]
