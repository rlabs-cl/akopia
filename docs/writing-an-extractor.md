# Akopia — Writing a Content Extractor

**Audience:** developers adding a new content format (EPUB, RTF,
LaTeX, Jupyter notebook, a custom binary format, …).

**Running example:** an EPUB extractor.

An extractor answers one question about some byte payload: *"What is
the text inside this thing?"* It receives a `ContentRef` (how to get
the bytes) and a `ChangeEvent` (the event that triggered the
extraction), and returns an `ExtractedContent` (the text plus
metadata).

Everything cross-cutting — fetching bytes, caching results, timeout
enforcement, Redis stream consumption, DLQ routing, metrics — is
handled by `BaseExtractor`. You subclass it and implement two
methods: `configure` and `extract`.

This document mirrors `writing-a-connector.md` but for Layer 2.

## 1. The contract

Exact shape from `common/protocols.py`:

```python
@runtime_checkable
class ContentExtractor(Protocol):
    plugin_id: str

    #: MIME types this extractor claims, e.g. ["application/epub+zip"].
    supported_mime_types: list[str]

    #: File extensions (with leading dot), e.g. [".epub"].
    supported_extensions: list[str]

    #: Higher wins on ties. Built-in extractors use 0 for fallback,
    #: 10-20 for format-specific. Negative = "only me if nothing else".
    priority: int

    @abstractmethod
    async def configure(self, config: dict) -> None: ...

    @abstractmethod
    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent: ...

    async def health(self) -> HealthReport: ...   # default: HEALTHY
    async def close(self) -> None: ...            # default: no-op
```

Responsibilities:

- **`plugin_id`** — class attribute. Matches the `type:` value in
  `akopia.yaml`'s `extractors:` block and the entry-point name in
  `pyproject.toml`. The `BaseExtractor.__init__` raises `TypeError`
  if you forget it.
- **`supported_mime_types` / `supported_extensions`** — how the
  router picks you. At least one must be non-empty; `BaseExtractor`
  checks and raises on both-empty.
- **`priority`** — integer. Existing extractors:
  - `plain` = 0 (fallback)
  - `pdf-text` = 10
  - `office` = 10
  - `html` = 20
  - OCR fallback (future) = -1 (only if text extraction yields nothing)
- **`configure(config)`** — called once at startup with the dict from
  `akopia.yaml`. Validate here; raise on missing/invalid keys.
- **`extract(content_ref, event)`** — do the work. Return an
  `ExtractedContent`. Must be re-entrant (the base runs multiple
  extract() calls concurrently within one process).

## 2. Why subclass `BaseExtractor`

`common/base_extractor.py` handles every boring thing.

- **Byte fetching.** `self._fetch_bytes(content_ref)` returns `bytes`
  regardless of whether the ref is `path`, `inline_bytes`, `url`, or
  `object_storage`. `self._fetch_as_text(content_ref)` decodes UTF-8
  (with a Latin-1 fallback on `UnicodeDecodeError`).
- **Caching.** Results are cached on disk under `self.cache_dir` (default
  `/data/cache/extract`) keyed by `sha256(bytes)`. A cache hit returns
  instantly. Cache invalidates when you bump `version: str` on your
  class (bump it when the output format changes).
- **Timeout.** `self.timeout_seconds` (default 60 s) wraps every
  `extract()` call in `asyncio.wait_for`. Hitting it raises
  `ExtractorTimeout` which the base routes to DLQ.
- **Redis wiring.** Base consumes from `extract-jobs`, publishes to
  `extract-results`, acks on success, routes to `dead-letter` on
  failure — preserving the original job for replay.
- **Metrics.** `self.metrics = {jobs_processed, jobs_cached_hits,
  errors, timeouts}` updated in the hot path.
- **Graceful shutdown.** `SIGTERM` / `SIGINT` → `self._shutdown.set()`.

You write `class FooExtractor(BaseExtractor):` and you get all of
the above.

## 3. Input and output shape

### Inputs

`ContentRef` (`common/models.py`). Five kinds, all handled transparently
by `self._fetch_bytes`:

| `kind`             | Payload field          | Typical source                |
|--------------------|------------------------|-------------------------------|
| `"path"`           | `path: str`            | Git/folder adapter — file on disk inside the container. |
| `"inline_bytes"`   | `bytes_b64: str`       | `web-single` for bodies < 1 MiB. |
| `"inline_text"`    | `text: str`            | Already-extracted text (bypass). |
| `"url"`            | `url: str`             | Anything whose origin lives on HTTP. |
| `"object_storage"` | `bucket: str, key: str`| S3/MinIO sources.             |

`ChangeEvent` has the full context: `source_id`, `path`, `modality`,
`content_hash`, `content_modified_at`, `depth`. **`event.path` is the
logical path** (filename for folder, URL for web); your extractor
uses it to infer format when MIME is absent.

### Output — `ExtractedContent`

```python
class ExtractedContent(BaseModel):
    source_event_id: str       # MUST be event.event_id
    text: str                  # the extracted text
    pages: Optional[list[Page]]  # for multi-page formats (PDF, PPTX, XLSX)
    structure: Optional[dict]  # free-form structural metadata
    metadata: dict             # {format, page_count, likely_scanned, ...}
    attached: list[ChangeEvent]  # follow-up events (images from inside a PDF)
    extractor_id: str          # self.plugin_id
    extractor_version: str     # self.version (used for cache keying)
    processing_time_ms: int    # auto-filled by the base if you skip it
    timestamp: str             # auto
```

### What **must** flow through (the freshness gotcha)

Adapters stamp `content_modified_at` on every `ChangeEvent`. Downstream
search uses it for `max_age_days` + `freshness_boost`. **The extractor
does not need to touch `content_modified_at` directly** — it is carried
through the pipeline from `ChangeEvent` → chunker (`Chunk.content_modified_at`)
→ `EmbeddingEntry.content_modified_at` → Qdrant/Meili payload.

The one thing you **must not do** is strip or mutate `event` fields
when producing `attached` events (§5). Use the pattern from
`extractors/office.py` — build a new `ChangeEvent` referencing
`event.source_id`, `event.path` as `derived_from`, and set
`depth=event.depth + 1`.

## 4. Worked example — the EPUB extractor

### 4.1 Directory layout

```
extractors/
  epub.py                 # the extractor class
tests/
  test_extractors.py      # add a TestEpubExtractor class
```

In-tree plugins live under `extractors/`. For a third-party plugin
(§8), use your own package name.

### 4.2 Dependencies

EPUB is a zip of HTML files. Two reasonable choices:

- **`ebooklib`** (~150 KiB pure Python) — parses the OPF manifest,
  returns chapters as HTML. Small, no system deps.
- **`unstructured`** (~400 MiB with all extras) — general purpose,
  overkill.

We'll use `ebooklib`. Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
epub = ["ebooklib>=0.18", "beautifulsoup4>=4.12"]
```

`beautifulsoup4` because ebooklib gives us HTML and we need to strip
it to text.

### 4.3 The class

```python
# extractors/epub.py
"""EpubExtractor — .epub / application/epub+zip via ebooklib.

Dependencies (pip packages):
    ebooklib         — parses OPF manifest + chapter XHTML
    beautifulsoup4   — strips chapter HTML to plain text

Produces one Page per chapter (index = reading order, title = chapter
title from the navigation doc when available).
"""
from __future__ import annotations

from typing import Optional

from common.base_extractor import BaseExtractor
from common.models import ChangeEvent, ContentRef, ExtractedContent, Page


class EpubExtractor(BaseExtractor):
    plugin_id = "epub"
    version = "0.1.0"
    priority = 10   # same tier as pdf-text/office — format-specific

    supported_mime_types = [
        "application/epub+zip",
    ]
    supported_extensions = [".epub"]

    async def configure(self, config: dict) -> None:
        cfg = dict(config or {})
        self.include_toc: bool = bool(cfg.get("include_toc", True))

    async def extract(
        self,
        content_ref: ContentRef,
        event: ChangeEvent,
    ) -> ExtractedContent:
        # Lazy imports — only loaded when this extractor runs, so
        # plugins that don't use EPUB don't pay the import cost.
        import io
        from ebooklib import epub, ITEM_DOCUMENT
        from bs4 import BeautifulSoup

        raw = await self._fetch_bytes(content_ref)
        # ebooklib opens from a file path; write to a temp buffer it can read.
        book = epub.read_epub(io.BytesIO(raw))

        pages: list[Page] = []
        text_parts: list[str] = []

        for i, item in enumerate(book.get_items_of_type(ITEM_DOCUMENT)):
            html = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")
            # Strip script/style/nav elements before text extraction.
            for tag in soup(["script", "style", "nav"]):
                tag.decompose()
            chapter_text = soup.get_text(separator="\n", strip=True)

            title: Optional[str] = None
            if soup.title:
                title = soup.title.string or None

            pages.append(Page(index=i, text=chapter_text, title=title))
            text_parts.append(chapter_text)

        full_text = "\n\n".join(text_parts)
        metadata: dict = {
            "format": "epub",
            "chapter_count": len(pages),
        }
        # ebooklib exposes Dublin Core metadata — propagate the common ones.
        try:
            titles = book.get_metadata("DC", "title")
            if titles:
                metadata["title"] = titles[0][0]
            authors = book.get_metadata("DC", "creator")
            if authors:
                metadata["author"] = authors[0][0]
        except Exception:
            # Metadata extraction is best-effort; never let it fail the job.
            pass

        return ExtractedContent(
            source_event_id=event.event_id,
            text=full_text,
            pages=pages,
            metadata=metadata,
            extractor_id=self.plugin_id,
            extractor_version=self.version,
        )


__all__ = ["EpubExtractor"]
```

Patterns to note:

- **Lazy imports.** `ebooklib` is imported inside `extract()`, not at
  module top. This keeps container boot fast when EPUB isn't used.
  Look at `extractors/pdf_text.py` — it imports `pypdfium2` inside
  `extract()` for the same reason.
- **`source_event_id=event.event_id`** — mandatory. The router pairs
  extract results back to change events via this field.
- **`extractor_id` / `extractor_version`** — used by the cache key so
  a version bump invalidates old cached outputs.
- **Metadata is best-effort.** `ebooklib.get_metadata` can raise on
  malformed OPF; swallow it with a try/except. The text extraction
  matters, the metadata is a nice-to-have.
- **`pages: list[Page]`** — chapters map naturally onto pages.
  Downstream chunker chunks each page independently, so chapter
  boundaries are respected.

### 4.4 Register the entry point

`pyproject.toml`:

```toml
[project.entry-points."akopia.content_extractor"]
plain    = "extractors.plain:PlainExtractor"
office   = "extractors.office:OfficeExtractor"
pdf-text = "extractors.pdf_text:PdfTextExtractor"
html     = "extractors.html:HTMLExtractor"
epub     = "extractors.epub:EpubExtractor"       # ← new
```

The group name `akopia.content_extractor` comes from
`common/registry.py::CONTENT_EXTRACTOR_GROUP`. The entry-point name
(`epub`) must match `plugin_id`.

Rebuild the editable install so `importlib.metadata` picks it up:

```bash
pip install -e '.[epub]'           # installs ebooklib + beautifulsoup4
```

For docker: rebuild the `Dockerfile.plugin` image (the plugin
container base). Compose will pick up the change on next
`docker compose up --build`.

### 4.5 `akopia.yaml` — opt-in

Extractors are declared in the top-level `extractors:` array:

```yaml
extractors:
  - type: plain
    config: {}
  - type: epub
    config:
      include_toc: true
```

Once declared, the router auto-dispatches `.epub` files (or any file
with MIME `application/epub+zip`) to your extractor. **The router
picks the highest-priority extractor whose `supported_mime_types` or
`supported_extensions` matches.** Priority ties are broken by order.

**Registration vs activation clarification.** Entry points define
*which extractors exist*. The `extractors:` block in `akopia.yaml` defines
*which of them are active for this deployment*. An entry-point-registered
extractor that isn't listed in `akopia.yaml` is never invoked.

## 5. Attached content (follow-up events)

Some extractors find content inside content: a PDF contains embedded
images you want OCRed, a zip contains separate files, an EPUB contains
diagrams. Emit them as `attached` `ChangeEvent`s; the router republishes
them with `depth += 1` (capped at `ChangeEvent.MAX_DEPTH = 3` to
prevent cycles).

```python
# inside extract(), after processing the main text:
attached: list[ChangeEvent] = []
for img_idx, img_bytes in _enumerate_images(book):
    attached.append(ChangeEvent(
        source_id=event.source_id,
        source_type=event.source_type,
        operation=event.operation,
        path=f"{event.path}#image-{img_idx}",       # synthetic path
        modality=Modality.IMAGE,
        content_hash=hashlib.sha256(img_bytes).hexdigest(),
        size_bytes=len(img_bytes),
        content_ref=ContentRef(
            kind="inline_bytes",
            bytes_b64=base64.b64encode(img_bytes).decode("ascii"),
        ),
        content_modified_at=event.content_modified_at,  # inherit freshness
        depth=event.depth + 1,                          # router-enforced
    ))

return ExtractedContent(
    source_event_id=event.event_id,
    text=full_text,
    pages=pages,
    attached=attached,
    ...
)
```

Do **not** mutate `event.depth` in place — build a new event.

## 6. Tests

Pattern from `tests/test_extractors.py`. Every new extractor adds a
`TestFooExtractor` class. Fixtures are synthesised at test time — no
binary blobs in the repo.

```python
# tests/test_extractors.py (add to existing file)

class TestEpubExtractor:
    def _make_epub_bytes(self) -> bytes:
        from ebooklib import epub
        import io

        book = epub.EpubBook()
        book.set_identifier("test-123")
        book.set_title("The Test Book")
        book.set_language("en")
        book.add_author("Test Author")

        c1 = epub.EpubHtml(title="Ch 1", file_name="ch1.xhtml", lang="en")
        c1.content = "<h1>Chapter 1</h1><p>hello world</p>"
        c2 = epub.EpubHtml(title="Ch 2", file_name="ch2.xhtml", lang="en")
        c2.content = "<h1>Chapter 2</h1><p>second chapter</p>"
        book.add_item(c1)
        book.add_item(c2)

        book.toc = (c1, c2)
        book.spine = ["nav", c1, c2]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        buf = io.BytesIO()
        epub.write_epub(buf, book)
        return buf.getvalue()

    async def test_extract_multi_chapter(self, tmp_path):
        ex = await _make(EpubExtractor)
        p = tmp_path / "book.epub"
        p.write_bytes(self._make_epub_bytes())
        ref = ContentRef(kind="path", path=str(p))
        ev = _event("book.epub")

        result = await ex.extract(ref, ev)

        assert "hello world" in result.text
        assert "second chapter" in result.text
        assert result.extractor_id == "epub"
        assert result.extractor_version == "0.1.0"
        assert result.metadata["format"] == "epub"
        assert result.metadata["chapter_count"] == 2
        assert len(result.pages) == 2
```

Run just your suite:

```bash
pytest tests/test_extractors.py::TestEpubExtractor -x
```

`_make` and `_event` are already defined at the top of the file.

## 7. Running end-to-end in docker-compose

Copy one of the `plugin-extractor-*` services:

```yaml
  plugin-extractor-epub:
    build:
      context: .
      dockerfile: Dockerfile.plugin
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_healthy
    environment:
      <<: *kb-env
    volumes:
      - ./akopia.yaml:/app/akopia.yaml:ro
      - ./data/docs:/data/docs:ro
      - git-repos:/tmp/akopia-repos:ro
    command: ["run", "extractor", "epub"]
```

The `run extractor <type>` command is wired in `scripts/kb_cli.py`;
the runner instantiates your class via the plugin registry and calls
`.start(config)`.

**Shared volumes matter.** The `git-repos` volume is mounted read-only
into extractor containers exactly because the git adapter writes
clones into it and extractors need to read those files when a
`ContentRef(kind="path", path="/tmp/akopia-repos/...")` arrives. If you
forget the volume mount, your extractor crashes on `FileNotFoundError`.
See `docs/troubleshooting.md` §4.

Ship the new optional-dep via Docker:

```dockerfile
# Dockerfile.plugin (or a derived image for your extractor)
RUN pip install --no-cache-dir 'akopia[epub]'
```

## 8. Packaging as a third-party plugin (out-of-tree)

You can publish an extractor as a standalone package without touching
akopia's repo. Minimal `pyproject.toml`:

```toml
[project]
name = "akopia-extractor-epub"
version = "0.1.0"
dependencies = [
  "akopia",
  "ebooklib>=0.18",
  "beautifulsoup4>=4.12",
]

[project.entry-points."akopia.content_extractor"]
epub = "akopia_epub:EpubExtractor"
```

Install into the same Python env as akopia (`pip install
akopia-extractor-epub` in the `Dockerfile.plugin` image, or
extend the image with your own layer). The registry picks it up on
next startup — no core changes.

A reusable container pattern:

```dockerfile
FROM akopia/plugin:latest
COPY . /opt/akopia-extractor-epub
RUN pip install --no-cache-dir /opt/akopia-extractor-epub
```

Reference it from `docker-compose.yml` using that image name.

## 9. Debugging

- **Container logs** — `docker compose logs plugin-extractor-epub
  --tail 100`. `BaseExtractor` logs plugin + event id on every error.
  Look for `extract() timed out` (raise `timeout_seconds`) and
  `extraction failed: …` (your code bug).
- **Queue depth** — `curl -s localhost:8080/v1/status | jq`. If
  `queue_depth` grows and your extractor is under-subscribed, it's
  probably stuck on one bad file; check `dead_letter_count`.
- **Dead-letter stream** — failed extractions land in
  `dead-letter`:

  ```bash
  docker compose exec redis redis-cli XRANGE dead-letter - +
  ```

  The payload includes the original `extract-jobs` message so you
  can replay after fixing the extractor.
- **Local unit test** — for fast iteration, build the
  `ExtractedContent` via `await ex.extract(ref, ev)` in a test and
  assert on fields. Don't spin up the full compose stack until the
  unit is green. The pattern in `tests/test_extractors.py` handles
  the `cache_dir=None, redis_client=object()` boilerplate for you
  via `_make(cls)`.

## 10. See also

- `docs/configuration.md` §5 — operator view of the `extractors:`
  block.
- `docs/plugin-contracts.md` — RFC behind the Protocol definitions.
- `docs/writing-a-connector.md` — the adapter-side equivalent of this
  document.
- `docs/adding-a-modality.md` — when "new format" really means "new
  modality" (audio, video) and touches the router + embedder too.
- `common/protocols.py` — authoritative contracts.
- `common/base_extractor.py` — what you inherit.
- `extractors/plain.py` — shortest full implementation.
- `extractors/pdf_text.py` — extractor with multi-page output.
- `extractors/office.py` — extractor branching on suffix.
