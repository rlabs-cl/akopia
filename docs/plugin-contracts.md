# Akopia — Plugin Contracts (RFC)

**Status:** Draft — pending review before implementation.
**Authors:** Ramón Labbé (human) · Claude (drafting assistant).
**Last update:** 2026-04-22.

## Purpose

This document defines the contracts that make akopia a **lego-style
OSS platform**: users assemble a working system by choosing one or more
source adapters, one or more content extractors, and a fixed core. New
sources and extractors are added by third parties without touching the
core.

Two plugin layers are contract-defined here:

- **Layer 3 · Sources** — pluggable. "Where does data live and when did
  it change?" Examples: git, folder, web-single, web-deep, S3.
- **Layer 2 · Extractors** — pluggable. "How do I turn this file into
  indexable text?" Examples: plain, office, pdf-text, ocr, asr, html.

The **Layer 1 · Core** (router + chunker + embedder + Qdrant +
Meilisearch) is intentionally **not pluggable**. Users replacing it
fork the project. This is by design: abstracting over vector stores
adds complexity that the 95% case doesn't need.

## Reading this document

Semaphore markers next to component names across the text:

- 🟢 **existing, works as-is** — no code changes needed
- 🟡 **existing, needs refactor** — code will be modified but the
  concept is already present
- 🔴 **new, build from scratch** — no current implementation

These markers are also in the architecture diagram (see
`architecture.md`). They are the migration roadmap.

## Design principles

1. **Code component vs configuration instance.** A plugin is a *class*
   that knows how to speak its protocol (git, HTTP, filesystem). A
   *configured instance* specifies what resource to connect to (repo
   URL, folder path, credentials, filters). Adding another repo
   doesn't require writing code — it requires adding a YAML entry.

2. **Base class handles transport; plugin handles logic.** Every
   adapter/extractor extends a `Base*` class that encapsulates the
   cross-cutting concerns: Redis stream consumption, event
   publishing, idempotency keys, health probes, graceful shutdown,
   metrics. The plugin author implements a small set of methods
   focused on *their* work (detect changes, extract text, etc.).

3. **At least one of each layer.** A akopia deployment is valid if
   it has at least one source adapter and at least one extractor. You
   can run text-only (just git + plain extractor), image-only (just a
   folder of JPEGs + image extractor), or full multimodal. Running
   zero of either layer makes no sense and is rejected at startup.

4. **Config via `akopia.yaml` + env interpolation.** Single source of
   config truth. Env vars flow in via `${VAR_NAME}` syntax. No
   silent defaults that point at internal infrastructure.

5. **Hot-reload is not in v1.** Restart the affected pod/container when
   config changes. Rationale: users are used to this pattern
   (nginx, postgres), and hot-reload multiplies the edge cases by 10×.

## Data model

The message types that flow through the system. These are Pydantic
models in `common/models.py`. 🟡 Needs minor additions marked below.

### `ChangeEvent` 🟡

Emitted by a source adapter. Describes "something exists or changed at
this source". Extends current model with two new optional fields so
sources that don't have a filesystem path (web scrapers, DB rows) can
pass payload inline.

```python
class ChangeEvent(BaseModel):
    event_id: str                       # uuid, auto-generated
    idempotency_key: str                # sha256 of (source_id, path, hash, modality)
    source_id: str                      # instance id from akopia.yaml
    source_type: str                    # plugin_id: "git", "folder", "web-deep"
    operation: Operation                # add | modify | delete | rename
    path: str                           # logical path (URL for web, file path for fs)
    old_path: Optional[str]             # for rename
    modality: Modality                  # text | image
    content_hash: Optional[str]         # sha256 of bytes (for dedup)
    size_bytes: int
    priority: Priority                  # cron | webhook | manual
    timestamp: str                      # ISO 8601 UTC — when kb *observed* it
    content_modified_at: Optional[datetime]  # when the upstream content
                                        # itself was last modified. Distinct
                                        # from `timestamp`. Populated per
                                        # source type (folder=mtime,
                                        # git=last-commit-for-path,
                                        # web=Last-Modified header).
                                        # Drives search-time `max_age_days`
                                        # hard filter + `freshness_boost`
                                        # soft re-rank. Absence → treated
                                        # as "unknown age" (neutral).

    # NEW FIELDS (🔴 to add):
    content_ref: Optional[ContentRef]   # how the extractor fetches bytes
    content_mime: Optional[str]         # explicit MIME override
    depth: int = 0                      # incremented when extractor emits
                                        # attached content (max 3 to break cycles)
    derived_content: Optional[DerivedContent]  # 🟡 already exists, same role


class ContentRef(BaseModel):
    """How to retrieve the bytes backing this event."""
    kind: Literal["path", "inline_bytes", "inline_text", "url", "object_storage"]
    path: Optional[str] = None          # local fs path (kind=path)
    bytes_b64: Optional[str] = None     # base64 bytes (kind=inline_bytes, <1 MiB)
    text: Optional[str] = None          # already-extracted (kind=inline_text)
    url: Optional[str] = None           # HTTP fetchable (kind=url)
    bucket: Optional[str] = None        # S3/MinIO object key (kind=object_storage)
    key: Optional[str] = None
```

**Rationale for `content_ref`:** today the code implicitly assumes the
bytes are on a local filesystem shared between adapter and embeddings.
That breaks for web scrapers (no filesystem) and for multi-node
deployments (different pods, different filesystems). `content_ref`
makes the retrieval strategy explicit and future-proof.

### `ExtractedContent` 🔴

New model. Produced by an extractor. Consumed by the router for
chunking + embedding.

```python
class ExtractedContent(BaseModel):
    source_event_id: str                # the ChangeEvent that triggered this
    text: str                           # unified plain-text output
    pages: Optional[list[Page]] = None  # per-page breakdown (PDFs, slides)
    structure: Optional[dict] = None    # headings/tables/lists; extractor-specific
    metadata: dict = {}                 # title, author, created_at, etc.
    attached: list[ChangeEvent] = []    # embedded images, inline files, etc.
                                        # published back as new ChangeEvents
                                        # with depth += 1


class Page(BaseModel):
    index: int                          # 0-based page/slide/sheet
    text: str
    title: Optional[str] = None         # H1 or slide title
```

**`attached` is the recursion point.** When the PDF extractor finds
embedded images, it includes them as new `ChangeEvent`s with
`content_ref.bytes_b64` set. The router publishes them back to
`change-events` with `depth + 1`. Events at `depth >= 3` are rejected
to prevent cycles.

## Layer 3 contract — `SourceAdapter`

A source adapter connects akopia to an origin of data (git repo,
folder, URL, database) and emits `ChangeEvent`s when something changes.

### Protocol 🔴

```python
from abc import abstractmethod
from typing import AsyncIterator, Protocol, runtime_checkable

@runtime_checkable
class SourceAdapter(Protocol):
    """Plugin contract for a data origin."""

    #: Unique id of this plugin class (e.g., "git", "folder", "web-deep").
    #: Matches the `type:` field in akopia.yaml. Registered via entry points.
    plugin_id: str

    @abstractmethod
    async def configure(self, config: dict) -> None:
        """Validate and store instance config. Raise on missing keys."""

    @abstractmethod
    async def discover(self) -> AsyncIterator["Source"]:
        """Enumerate logical units within this instance. A git adapter
        yields one Source per repo; a folder adapter yields Sources per
        top-level subtree; a web-deep adapter yields one Source per root URL."""

    @abstractmethod
    async def watch(self, source: "Source") -> AsyncIterator[ChangeEvent]:
        """Long-running. Emit ChangeEvent whenever something changes in
        the given source. May run forever (poll loop) or yield-and-pause
        on a webhook-driven source."""

    @abstractmethod
    async def read(self, source: "Source", path: str) -> bytes:
        """On-demand fetch of raw bytes. Used when a downstream extractor
        needs the payload that wasn't inlined in the event."""

    async def health(self) -> HealthStatus:
        """Non-blocking liveness + upstream reachability check. Default
        impl returns HEALTHY; override for upstream-specific checks."""

    async def close(self) -> None:
        """Release connections, flush watermarks, etc. Default is no-op."""
```

### Base class 🔴

`BaseSourceAdapter` in `common/base_adapter.py`. Subclasses only need
to implement the protocol methods. The base handles:

- Redis `change-events` stream publishing
- Idempotency key computation (sha256 of identity fields)
- Prometheus metrics emission: `source_adapter_events_total`,
  `source_adapter_errors_total`, `source_adapter_poll_duration_seconds`
- Graceful shutdown on SIGTERM
- Readiness probe compatible with k8s + docker-compose
- Structured JSON logs with `source_id`, `plugin_id` context

Contract author writes ~50-100 lines; base class is ~250 lines reused
across every adapter.

### Current state

| Adapter | Status | Notes |
|---|---|---|
| `git` | 🟡 | Exists (adapter-git), needs to be refactored to `SourceAdapter` protocol + provider-abstracted (Gitea/GitHub/GitLab). |
| `folder` | 🟡 | Generalised from adapter-nas. Takes NAS out of the repo as a special case; now it's just "watch a path". |
| `web-single` | 🔴 | Pull one URL on cron; re-fetch, diff by content_hash. Uses `httpx` + `trafilatura` for basic HTML cleanup (before handing to html extractor). |
| `web-deep` | 🔴 | BFS crawl with max_depth, robots.txt, rate-limit, visited set. Wrapper on `trafilatura` or `firecrawl` SDK. |
| `s3` | 🔴 post-launch | S3/MinIO object listing + change detection via ETag. |
| `db-live` | 🔴 post-launch | Postgres/MySQL CDC via WAL or trigger-based polling. |

### Example: `folder` adapter (target state)

```python
class FolderAdapter(BaseSourceAdapter):
    plugin_id = "folder"

    async def configure(self, config: dict) -> None:
        self.path = Path(config["path"]).resolve()
        self.include = config.get("include", ["*"])
        self.exclude = config.get("exclude", [])
        self.poll_seconds = config.get("poll_seconds", 300)
        if not self.path.exists():
            raise ConfigError(f"path does not exist: {self.path}")

    async def discover(self) -> AsyncIterator[Source]:
        yield Source(id="root", label=str(self.path))

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        last_state = {}  # path -> mtime
        while not self._shutdown.is_set():
            current = self._scan()  # respects include/exclude globs
            for path, mtime in current.items():
                if last_state.get(path) != mtime:
                    yield self._make_change_event(path, mtime)
            last_state = current
            await asyncio.sleep(self.poll_seconds)

    async def read(self, source: Source, path: str) -> bytes:
        return (self.path / path).read_bytes()
```

~40 lines vs the current adapter-git's ~300. The base class absorbs
the rest.

## Layer 2 contract — `ContentExtractor`

An extractor turns the bytes of a `ChangeEvent` into `ExtractedContent`.
Routing to the right extractor is by MIME type + file extension.

### Protocol 🔴

```python
@runtime_checkable
class ContentExtractor(Protocol):
    plugin_id: str
    supported_mime_types: list[str]         # ["application/pdf"]
    supported_extensions: list[str]         # [".pdf"]
    priority: int = 0                       # higher wins on ties; >0 overrides built-ins

    async def configure(self, config: dict) -> None: ...
    async def extract(self, content_ref: ContentRef, event: ChangeEvent) -> ExtractedContent: ...
    async def health(self) -> HealthStatus: ...
    async def close(self) -> None: ...
```

### Base class 🔴

`BaseExtractor` in `common/base_extractor.py`. Handles:

- Redis `extract-jobs` consumption, `extract-results` publishing
- Fetching `content_ref` for all five `kind` values: `path`, `url`,
  `inline_bytes`, `inline_text`, and `object_storage` (S3 / S3-compatible
  stores like MinIO, Ceph RGW, Wasabi). The `object_storage` path is
  enabled when the optional `[s3]` extra is installed (`pip install
  akopia[s3]`) and the `AKOPIA_S3_ENDPOINT`, `AKOPIA_S3_ACCESS_KEY`,
  `AKOPIA_S3_SECRET_KEY` (and optionally `AKOPIA_S3_REGION`,
  `AKOPIA_S3_USE_SSL`) env vars are set on the extractor pod.
  Credentials are env-resolved on purpose — `ContentRef`s travel
  through Redis, so embedding secrets in them would leak across the
  audit trail. Multi-bucket setups can run separate extractor pods
  with different env values.
- Timeout enforcement (extractors that take >N seconds fail-fast)
- Caching (sha256(content) → extracted text) via `preprocess-cache` PVC
- DLQ publishing on failure with original job preserved
- Metrics: `extractor_jobs_total`, `extractor_latency_seconds`,
  `extractor_failures_total`

### Current state

| Extractor | Status | Notes |
|---|---|---|
| `plain` | 🔴 | Passthrough for text/markdown/json/yaml/csv. Format-aware chunking hints (e.g., CSV splits by row, markdown by heading). |
| `office` | 🔴 | docx/xlsx/pptx/odt/ods/odp. Default impl via `unstructured` library. |
| `pdf-text` | 🔴 | Native-text PDFs (most common case, fast). Via `pypdfium2`. |
| `html` | 🔴 | Clean HTML via `trafilatura`. Used by web-* adapters and when HTML files come from git/folder. |
| `ocr` | 🔴 post-launch | Scanned PDFs, image-with-text. Via `pytesseract`; requires `tesseract` binary; opt-in. |
| `asr` | 🔴 post-launch | Audio/video → transcript. Via `faster-whisper`. |

Note: **current modality-specific code in `concentrador/main.py:490-590` (~100 lines of if/elif by modality)** gets deleted. Replaced by routing to extractors via extension/MIME. 🟡 Net simplification of core.

### Example: `plain` extractor

```python
class PlainExtractor(BaseExtractor):
    plugin_id = "plain"
    supported_mime_types = ["text/plain", "text/markdown", "application/json", "text/yaml"]
    supported_extensions = [".txt", ".md", ".rst", ".json", ".yaml", ".toml"]

    async def extract(self, content_ref: ContentRef, event: ChangeEvent) -> ExtractedContent:
        text = await self._fetch_as_text(content_ref)  # base class helper
        return ExtractedContent(
            source_event_id=event.event_id,
            text=text,
            metadata={"format": Path(event.path).suffix.lstrip(".")},
        )
```

~10 lines. The base class hides the Redis / retry / cache plumbing.

## Configuration — `akopia.yaml`

### Schema (top-level)

```yaml
# akopia.yaml v1
version: 1

core:
  storage:
    vector:   { url: ${QDRANT_URL}, collection_prefix: "kb_" }
    lexical:  { url: ${MEILI_URL}, master_key: ${MEILI_MASTER_KEY} }
    queue:    { url: ${REDIS_URL} }

  embeddings:
    text:
      provider: fastembed         # or: tei, openai, ollama
      model: nomic-embed-text-v1.5
      quantized: true             # Q variant; 🟡 saves ~600 MiB
    image:
      enabled: true               # set false if image modality not needed
      provider: fastembed
      model: clip-vit-b-32

  router:
    max_event_depth: 3            # cycle protection on attached content
    idempotency_ttl: 7d

  auth:
    mode: bearer-static           # MVP. Post-launch: api-keys (multi-tenant)
    token: ${AKOPIA_BEARER_TOKEN}

sources:                          # at least one required
  - id: company-docs
    type: folder
    config:
      path: /data/docs
      include: ["*.docx", "*.pdf", "*.md"]
      poll_seconds: 300

  - id: product-repo
    type: git
    config:
      provider: github            # or: gitlab, gitea, bitbucket
      org: acme
      token: ${GITHUB_TOKEN}

  - id: competitor-blog
    type: web-deep
    config:
      root: https://competitor.com/blog
      max_depth: 3
      respect_robots: true

extractors:                       # at least one required; `plain` recommended
  - type: plain
    config: {}

  - type: office
    config:
      include_speaker_notes: true
      max_rows_per_sheet: 10000

  - type: html
    config:
      engine: trafilatura
      strip_nav: true
```

### Env interpolation 🔴

`${VAR}` resolves from OS env at startup. `${VAR:-default}` for
fallbacks. If a variable is missing and no default is provided,
startup fails with a clear error pointing to the offending key.
No silent empty-string substitution.

### Validation 🔴

JSON Schema shipped in `common/akopia_schema.json`. Used at:

- Startup: validate before spinning up adapters.
- CLI: `kb validate akopia.yaml` for pre-flight check.
- CI: generate docs from schema, verify examples match.

## Plugin registration

Plugins register via Python **entry points** in `pyproject.toml`:

```toml
[project.entry-points."akopia.source_adapter"]
git         = "akopia_adapters.git:GitAdapter"
folder      = "akopia_adapters.folder:FolderAdapter"
web-single  = "akopia_adapters.web_single:WebSingleAdapter"
web-deep    = "akopia_adapters.web_deep:WebDeepAdapter"

[project.entry-points."akopia.content_extractor"]
plain   = "akopia_extractors.plain:PlainExtractor"
office  = "akopia_extractors.office:OfficeExtractor"
pdf-text = "akopia_extractors.pdf_text:PdfTextExtractor"
html    = "akopia_extractors.html:HTMLExtractor"
```

Third-party plugin publishing (e.g. `akopia-adapter-notion`) just
needs its own `pyproject.toml` with an entry point pointing at the
class. `pip install akopia-adapter-notion` makes it available;
`type: notion` in akopia.yaml activates an instance.

## Deployment modes & mandatory components

The platform supports three tiers. All three run the same Python code;
the difference is packaging and persistence.

| Component | Solo-dev (compose) | Single-host (compose) | K8s |
|---|---|---|---|
| Qdrant 🟢 | container + named volume | container + host-mounted volume | Deployment + PVC |
| Meilisearch 🟢 | container + named volume | container + host-mounted volume | Deployment + PVC |
| Redis 🟢 | container + named volume | container + host-mounted volume | Deployment + PVC |
| Router (Core) 🟡 | in concentrador | same | StatefulSet (1 replica) |
| Embedder — text 🟡 | in concentrador | separate container | Deployment (scale horizontally) |
| Embedder — image 🟡 | optional | optional | optional Deployment |
| SourceAdapter(s) 🟡 | at least one | at least one | one Deployment per adapter type |
| ContentExtractor(s) 🔴 | at least `plain` | as many as needed | one Deployment per extractor type |

**Text-only deployments** omit image embedder, ocr, asr, and image-specific
adapters. The `akopia.yaml` `embeddings.image.enabled: false` makes the
platform skip those pipelines entirely.

**Image-only deployments** (rare, e.g. a stock-photo search index) do
the inverse. Valid config, fewer containers running.

## Testing strategy

### Contract tests 🔴

`tests/contract/source_adapter.py` defines a generic suite every
`SourceAdapter` must pass. Parameterised on the plugin id:

- `test_configure_missing_key_fails`
- `test_discover_yields_at_least_one`
- `test_watch_emits_change_event_on_add`
- `test_watch_emits_on_modify`
- `test_watch_emits_on_delete`
- `test_idempotency_key_is_deterministic`
- `test_health_returns_quickly`
- `test_graceful_shutdown_closes_resources`

Same pattern for `tests/contract/content_extractor.py`. Any plugin
author runs `pytest --archetype kb-source-adapter --plugin=git` and
gets certification that their plugin meets the contract.

### Integration tests 🟡

End-to-end smoke: compose up → create source → wait for embedding →
run semantic query → assert result. Runs in CI against the matrix
`{text-only, text+image, full-multimodal}`.

## Migration plan (semaphore summary)

### 🟢 Keep as-is

- Qdrant, Meilisearch, Redis (deployment + manifests ✅)
- MCP server (pure proxy, contract doesn't touch it)
- Backing services config **once PVCs are made explicit** — currently
  `emptyDir` in manifest, needs fix independent of this RFC

### 🟡 Refactor (behaviour preserved, shape changed)

- `concentrador/main.py` — replace 100 lines of modality-if-elif with
  router + extractor dispatch
- `common/models.py` — add `content_ref`, `ExtractedContent`,
  `ContentRef`, depth field
- `embeddings/main.py` — already fine, becomes one of N embedder pods
  addressable by modality
- `adapter_git/main.py` → `adapters/git.py`, extends `BaseSourceAdapter`
- `adapter_nas` → `adapters/folder.py` (generalised)

### 🔴 Build from scratch

- `common/base_adapter.py`, `common/base_extractor.py`
- `akopia_extractors/{plain,office,pdf_text,html}.py`
- `adapters/web_single.py`, `adapters/web_deep.py`
- `akopia/config/loader.py` + `akopia_schema.json`
- Plugin registry via entry points
- `docker-compose.yml` for tier 1 & tier 2 deployments
- Contract test suites
- Full quickstart `README.md`

## Open questions (resolve before implementation)

1. **Object storage for inline content.** Should `ContentRef.kind=url`
   fetch directly (simple, leaks URLs to the extractor) or proxy via
   core (safer, one more hop)? Lean: proxy via core for v1.

2. **Cache invalidation for re-indexing.** If I edit an extractor's
   chunking logic, should existing indexed content automatically
   re-extract? Proposal: content hash + `extractor_version` composite
   key in Qdrant payload; `kb reindex --since extractor_version=X`
   CLI command post-launch.

3. **Per-source retention.** Should a source be able to say "drop my
   data after 90 days"? Not in v1. Single `kb purge --source-id X` CLI
   command covers the use case manually.

4. **Auth model for multi-tenant**. Currently single bearer token.
   Post-launch: pgbouncer-style API keys with per-key rate limits and
   per-key source visibility. Out of scope for this RFC.

## References

- Architecture diagram with semaphore markers: `docs/architecture.md`
- Memo M4 target: `portfolio-2026-04/sections/05_recommendation.md` §M4
- AgentGuard archetypes used to generate + validate this refactor:
  - `software_architecture` for this RFC and the architecture doc
  - `hexagonal-api` for the implementation phase (ports/adapters
    scaffold + test harness)
