# Akopia — Configuration Manual

**Audience:** operators setting up akopia against their own data.
**Scope:** everything you can configure through `akopia.yaml` and environment
variables. There is no GUI and there will not be one — this document is
the UX.

## 1. The three-layer mental model

Every deployment has three layers (see `docs/architecture.md` for the
full map):

1. **Sources (Layer 3)** — adapters that watch something and emit
   `ChangeEvent`s. Pluggable. Examples today: `git`, `folder`,
   `web-single`, `web-deep`.
2. **Extractors (Layer 2)** — turn raw bytes into clean text +
   metadata. Pluggable. Examples today: `plain`, `office`, `pdf-text`,
   `html`.
3. **Core (Layer 1)** — router, chunker, embedder, Qdrant,
   Meilisearch, MCP server. Fixed. You configure it, you don't
   replace it.

A valid deployment declares **at least one source** and **at least
one extractor**. The JSON Schema (`common/akopia_schema.json`) enforces
`minItems: 1` on both arrays.

## 2. Where `akopia.yaml` lives and how it's loaded

The loader is `common/config_loader.py` (`ConfigLoader` class, or the
`load_config()` convenience wrapper). Path resolution:

1. Explicit argument passed to `ConfigLoader(path=...)`.
2. The `AKOPIA_CONFIG_PATH` environment variable.
3. Fallback: `./akopia.yaml` in the process CWD.

Load pipeline:

1. Read YAML (`yaml.safe_load`). Top level must be a mapping.
2. Walk the tree and interpolate `${VAR}` / `${VAR:-default}` tokens
   from the OS environment. Missing required var →
   `ConfigError: sources[2].config.token: missing env GITHUB_TOKEN`.
3. Validate against `common/akopia_schema.json` (Draft-7). First 5 errors
   are surfaced with a YAML path pointer.
4. Coerce to the Pydantic `AkopiaConfig` tree in `common/kb_config.py`.

### Environment precedence

Only the embedder config has dual (yaml + env) overrides today. In
`embeddings/main.py::_load_text_backend`:

- Start from env: `EMBEDDER_PROVIDER`, `EMBEDDER_MODEL`,
  `EMBEDDER_QUANTIZED`, `OLLAMA_URL`.
- If `akopia.yaml` loads cleanly, values in
  `core.embeddings.text.{provider,model,quantized,url}` **override**
  the env values.
- If `akopia.yaml` fails to load, env-only config is used and a warning is
  logged.

For everything else (Qdrant URL, Meili URL, Redis URL, bearer token)
the precedence is "whatever is in the process env at start" — see
`common/config.py::Config`. `akopia.yaml` can supply those via env
interpolation (`url: "${QDRANT_URL:-http://qdrant:6333}"`) or the
compose file can set them directly. Both work.

### Env interpolation syntax

```yaml
url: "${QDRANT_URL}"                 # required — startup fails if unset
url: "${QDRANT_URL:-http://qdrant:6333}"  # optional with default
```

The regex lives at the top of `config_loader.py` (`_ENV_PATTERN`).
Defaults may contain any character except `}`.

## 3. Top-level shape

```yaml
version: 1          # must be 1; const-validated by the schema

core:
  storage: { vector: {...}, lexical: {...}, queue: {...} }
  embeddings: { text: {...}, image: {...} }
  router: { max_event_depth: 3, idempotency_ttl: 7d }
  auth: { mode: bearer-static, token: "${AKOPIA_BEARER_TOKEN}" }

sources:            # minItems: 1
  - id: ...
    type: ...
    config: {...}

extractors:         # minItems: 1
  - type: ...
    config: {...}
```

Top-level keys are strict (`additionalProperties: false`). Typos get
caught at startup.

## 4. Sources

Each entry has a unique `id` (free string; appears in every
`ChangeEvent.source_id`), a `type` (must match a registered plugin's
`plugin_id`), and a plugin-specific `config` dict. The plugin
validates its own config in `configure()`.

The types shipped in-tree:

### 4.1 `folder` — local filesystem subtree

Class: `adapters/folder.py::FolderAdapter`.

| Field            | Type       | Required | Default  | Notes |
|------------------|-----------|----------|----------|-------|
| `path`           | str       | yes      | —        | Absolute path inside the container. Must exist and be a directory. |
| `include`        | list[str] | no       | `["*"]`  | fnmatch globs; matched against basename and relative path. |
| `exclude`        | list[str] | no       | `[]`     | Same matching rule. Applied before include. |
| `poll_seconds`   | int       | no       | `300`    | Interval between full tree scans. |
| `max_file_bytes` | int       | no       | `26214400` (25 MiB) | Files above this are skipped with an INFO log. |

Minimal:

```yaml
- id: local-docs
  type: folder
  config:
    path: /data/docs
```

Full:

```yaml
- id: policies
  type: folder
  config:
    path: /mnt/nas/policies
    include: ["*.md", "*.pdf", "docs/**"]
    exclude: ["drafts/**", "*.tmp"]
    poll_seconds: 120
    max_file_bytes: 52428800   # 50 MiB
```

Modality is inferred by extension (`_MODALITY_BY_EXT` in
`adapters/folder.py`). `content_modified_at` is the file's POSIX
`st_mtime`, coerced to tz-aware UTC.

### 4.2 `git` — git repository (GitHub / GitLab / Gitea)

Class: `adapters/git.py::GitAdapter`.

| Field             | Type      | Required | Default       | Notes |
|-------------------|-----------|----------|---------------|-------|
| `provider`        | str       | no       | `gitea`       | One of `github`, `gitlab`, `gitea`. |
| `base_url`        | str       | no       | per-provider  | Override for self-hosted instances. |
| `token`           | str       | no       | `""`          | Personal access token. Env-interpolated. |
| `org`             | str       | yes *    | —             | Org/group whose repos will be listed via provider API. |
| `repos`           | list[str] | yes *    | `[]`          | Explicit clone URLs. Bypasses the listing API (useful air-gapped or for public repos). |
| `branch`          | str       | no       | `main`        | Tracked branch. |
| `include_globs`   | list[str] | no       | `["*"]`       | fnmatch globs. |
| `exclude_globs`   | list[str] | no       | `[]`          | Applied before include. |
| `poll_seconds`    | int       | no       | `300`         | Interval between `git fetch` calls. |
| `max_file_bytes`  | int       | no       | 25 MiB        | |
| `cache_dir`       | str       | no       | `/tmp/akopia-repos` | Where clones live inside the container. |
| `concurrency`     | int       | no       | `3`           | Max parallel `git clone`/`git fetch` subprocesses for this adapter instance. Valid range: `1`–`10`. Values outside the range raise at `configure()`. Sensible upper bound: stays friendly to Gitea/GitHub/GitLab under default rate limits; bump only if you own the server. |

\* One of `org` or `repos` must be set; the adapter raises at
  `configure()` otherwise.

Minimal (explicit repo, public):

```yaml
- id: product-docs
  type: git
  config:
    repos:
      - https://github.com/acme/docs.git
    branch: main
```

Full (org scan, private):

```yaml
- id: acme-repos
  type: git
  config:
    provider: github
    org: acme
    token: "${GITHUB_TOKEN}"
    branch: main
    include_globs: ["*.md", "*.py", "docs/**"]
    exclude_globs: ["vendor/**", "node_modules/**"]
    poll_seconds: 600
```

`content_modified_at` is the committer time of the last commit
touching the file (`git log -1 --format=%cI`). Precise but
subprocess-heavy.

### 4.3 `web-single` — poll one URL on a cron

Class: `adapters/web_single.py::WebSingleAdapter`. `plugin_id =
"web-single"` (note the hyphen).

| Field              | Type    | Required | Default | Notes |
|--------------------|---------|----------|---------|-------|
| `url`              | str     | yes      | —       | The URL to poll. |
| `refresh_cron`     | str     | no       | `0 6 * * *` | Coarse cron → interval translation; see `_cron_to_sleep_seconds`. |
| `user_agent`       | str     | no       | `akopia/0.1 (...)` | Sent on every request. |
| `timeout_seconds`  | int     | no       | `30`    | httpx request timeout. |
| `follow_redirects` | bool    | no       | `true`  | |
| `headers`          | dict    | no       | `{}`    | Extra headers (e.g. auth for an API). |

The adapter sends conditional `If-None-Match` / `If-Modified-Since`
and emits a `ChangeEvent` only when the SHA-256 of the body actually
changes. Bodies below 1 MiB are inlined as base64 in the event
(`ContentRef(kind="inline_bytes")`); larger bodies use
`ContentRef(kind="url")`.

```yaml
- id: changelog
  type: web-single
  config:
    url: https://example.com/releases.html
    refresh_cron: "0 */2 * * *"
```

### 4.4 `web-deep` — BFS crawl one site

Class: `adapters/web_deep.py::WebDeepAdapter`. `plugin_id = "web-deep"`.

| Field              | Type | Required | Default | Notes |
|--------------------|------|----------|---------|-------|
| `root`             | str  | yes      | —       | Entry URL. |
| `max_depth`        | int  | no       | `3`     | Hard cap: `HARD_MAX_DEPTH = 10`. |
| `max_pages`        | int  | no       | `200`   | Hard cap: `HARD_MAX_PAGES = 10000`. |
| `respect_robots`   | bool | no       | `true`  | |
| `rate_limit`       | str  | no       | `1/s`   | Format `<n>/<s\|min>`. |
| `same_origin_only` | bool | no       | `true`  | Drops outbound links at the origin boundary. |
| `user_agent`       | str  | no       | `akopia/0.1 (...)` | |
| `timeout_seconds`  | int  | no       | `30`    | |
| `headers`          | dict | no       | `{}`    | |
| `refresh_cron`     | str  | no       | `0 6 * * *` | Sleeps between successive crawls. |

```yaml
- id: handbook
  type: web-deep
  config:
    root: https://handbook.acme.com/
    max_depth: 4
    max_pages: 500
    rate_limit: 2/s
```

JavaScript-rendered pages are out of scope (no headless browser).

## 5. Extractors

Extractors are auto-wired by MIME type + file extension. The router
picks the **highest-priority** match; ties are broken by `priority:`
on the class. You declare one `extractors:` entry per extractor type
you want active, and pass any config it accepts.

| `type`     | Class                                        | Handles                           | Priority | Config fields |
|------------|----------------------------------------------|-----------------------------------|----------|---------------|
| `plain`    | `extractors/plain.py::PlainExtractor`        | `.md .txt .rst .json .yaml .toml .csv .tsv .log .py .js .ts .go .rs .java .c .cpp .h .sh` (incl. `text/plain`, `text/markdown`, `application/json`, `text/csv`, `text/tab-separated-values`) | 0  | none |
| `html`     | `extractors/html.py::HTMLExtractor`          | `.html .htm` (`text/html`, `application/xhtml+xml`) | 20 | `strip_nav` (bool, `true`), `preserve_code_blocks` (bool, `true`), `include_tables` (bool, `true`) |
| `pdf-text` | `extractors/pdf_text.py::PdfTextExtractor`   | `.pdf` (`application/pdf`)        | 10       | *(none today; `likely_scanned` auto-stamped for <50 extracted chars)* |
| `office`   | `extractors/office.py::OfficeExtractor`      | `.docx .doc .xlsx .xls .pptx .ppt .odt .ods .odp` + their MIME types | 10 | `max_rows_per_sheet` (int, `10000`), `include_speaker_notes` (bool, `true`) |

Minimal:

```yaml
extractors:
  - type: plain
    config: {}
```

Full:

```yaml
extractors:
  - type: plain
    config: {}
  - type: html
    config:
      strip_nav: true
      preserve_code_blocks: true
      include_tables: true
  - type: pdf-text
    config: {}
  - type: office
    config:
      include_speaker_notes: true
      max_rows_per_sheet: 20000
```

Keep `plain` in the list. It's the fallback for arbitrary text.

### HTML-specific notes

Powered by `trafilatura`. The `strip_nav` flag sets `favor_precision`
on extraction — enable it for news/blog pages to drop nav/footer
boilerplate; disable on content-heavy pages where precision over-prunes.

### PDF-specific notes

`pdf-text` uses `pypdfium2` and produces one `Page` per PDF page. If
the total extracted text is under 50 characters, the extractor stamps
`metadata.likely_scanned = true`. The forthcoming OCR extractor (not
in the current release) will pick up those events for re-processing.

## 6. Chunker

Lives in `common/chunker.py`. Configurable through `akopia.yaml`
(`core.chunker.*`) as of 0.2 — defaults match the previous hard-coded
behaviour. The router and legacy `create_embedding_jobs` path both
resolve the effective config via `get_chunker_config()` (process-cached
`lru_cache`).

```yaml
core:
  chunker:
    strategy: recursive        # recursive | paragraph
    chunk_size_tokens: 512
    overlap_tokens: 50
```

- **`strategy`** — `"recursive"` (default, LangChain-style separator
  walk) or `"paragraph"` (legacy greedy accumulator).
- **`chunk_size_tokens`** — hard upper bound on tokens per emitted
  chunk. Default: 512.
- **`overlap_tokens`** — tokens of previous chunk prepended to each
  subsequent chunk. Default: 50.
- **Env overrides** — `AKOPIA_CHUNK_STRATEGY`, `AKOPIA_CHUNK_SIZE_TOKENS`,
  `AKOPIA_CHUNK_OVERLAP_TOKENS`. Useful for per-deploy tweaks without
  editing `akopia.yaml`.
- **Tokenizer** — `tiktoken.get_encoding("cl100k_base")` when
  available, else a `chars/4` approximation. Token counts are real
  when `tiktoken` is installed (`pyproject.toml` pins
  `tiktoken>=0.7,<1.0`).

The recursive splitter walks separators `["\n\n", "\n", ". ", " ",
""]` in order, merging greedily while under budget and descending
into finer separators only if a merged block is still oversized.

## 7. Embeddings

### Text

```yaml
core:
  embeddings:
    text:
      provider: fastembed               # fastembed | ollama
      model: nomic-embed-text-v1.5
      quantized: true                   # fastembed only
      # url: "${OLLAMA_URL}"            # ollama only
```

- `fastembed` (default) — `embeddings/backends/fastembed_backend.py`.
  ONNX runtime in the embedding pod, CPU-friendly. 768-d vectors
  (`_DIM = 768`) for `nomic-embed-text-v1.5`. First use downloads
  the model to `/root/.cache/fastembed` inside the container.
- `ollama` — `embeddings/backends/ollama_backend.py`. HTTP calls out
  to an Ollama server you run yourself. Set `url:` in yaml or
  `OLLAMA_URL` in env. Use `http://host.docker.internal:11434` to
  reach an Ollama on the docker host.

Unknown `provider` values raise at startup (`build_backend()` in
`embeddings/backends/__init__.py`).

#### Throughput knob: `EMBEDDER_BATCH_SIZE`

The embedder consumes Redis-stream messages in batches and processes the
whole batch in parallel via `asyncio.gather`. `EMBEDDER_BATCH_SIZE`
(default `10`, valid range `1`–`50`) controls how many `EmbeddingJob`s
are pulled per `XREADGROUP` call. Larger batches reduce per-job consume
overhead (`block_ms=5000`) and keep a GPU-backed Ollama saturated by
issuing N concurrent HTTP POSTs to `/api/embed`. Values outside the
range are clamped with a warning. Start at the default; raise only if
the embedder is underutilised (see `docker compose logs embeddings`
for per-job ms) and the backend can absorb the parallelism.

Each job still has its own `idempotency_key`, so gather-style parallel
dispatch preserves exactly-once semantics. Acks happen per-message
inside the batch, so partial failures don't block siblings.

### Image

```yaml
core:
  embeddings:
    image:
      enabled: false                    # set true to spin up CLIP
      # provider: fastembed
      # model: clip-vit-b-32
```

CLIP (ViT-B/32) via `fastembed.ImageEmbedding` is the only option
today — `embeddings/main.py::load_clip_model` hardcodes
`Qdrant/clip-ViT-B-32-vision` regardless of yaml. The `provider` /
`model` fields are accepted by the schema but ignored at runtime;
making the image embedder pluggable is a post-launch item.

## 8. Freshness

Freshness is per-search, not per-deployment. Two controls live on
every search request:

- **`max_age_days`** — hard filter. Documents with
  `content_modified_at` older than `now - max_age_days` are dropped
  before ranking. Docs missing `content_modified_at` (pre-feature
  ingests) are kept (neutral).
- **`freshness_boost`** — soft re-rank weight in `[0, 1]`. Combined
  with the underlying similarity via
  `fresh_score = exp(-age_days / 180)` and
  `final = (1 - beta) * sim + beta * fresh_score`
  (`concentrador/index_manager.py::_apply_freshness_boost`). Docs
  without `content_modified_at` get a neutral `0.5`.

How age is measured depends on the source:

- `folder` → `Path.stat().st_mtime_ns`, UTC.
- `git` → last commit committer time for that specific path.
- `web-single` → `Last-Modified` header (or `now()` if missing; only
  `None` on a present-but-malformed header).
- `web-deep` → same pattern as `web-single`.

The value is carried unchanged through `ChangeEvent` →
`ExtractedContent`'s chunker → `Chunk.content_modified_at` →
`EmbeddingEntry.content_modified_at` → the Qdrant/Meili payload.

There is no global override in `akopia.yaml` today — callers specify
`max_age_days` / `freshness_boost` per request on
`/v1/search/semantic`, `/v1/search/lexical`, or `/v1/rag/ask`.

## 9. Authentication

```yaml
core:
  auth:
    mode: bearer-static
    token: "${AKOPIA_BEARER_TOKEN}"
```

`bearer-static` is the only mode in MVP. The concentrador reads
`Config.BEARER_TOKEN` (`common/config.py`), which accepts
`AKOPIA_BEARER_TOKEN` (canonical) or `BEARER_TOKEN` (legacy). Requests
must carry `Authorization: Bearer <token>`; the check is in
`concentrador/main.py::verify_token` and applies to every endpoint
except `/v1/status` and `/health`.

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it in `.env`:

```
AKOPIA_BEARER_TOKEN=<your-token>
```

The mcp_server proxies the same token; your MCP client sends it as
the bearer on every call.

## 10. Tokens for private sources

docker-compose's `x-kb-env` block pipes these into every plugin
container:

- `GITHUB_TOKEN`
- `GITLAB_TOKEN`
- `GITEA_TOKEN`

Set them in `.env`, reference them from `akopia.yaml` with
`"${GITHUB_TOKEN}"`. They only need to be set for the provider you
actually use — the interpolation is only attempted on the plugin
instance whose config references the var (other plugins see their
own values fine).

Public repos don't need a token. Use the `repos: [...]` form instead
of `org: ...` to skip the listing API entirely.

## 11. Troubleshooting

| Symptom | First check |
|---|---|
| A source isn't ingesting | `docker compose logs plugin-adapter-<name> --tail 100` — look for `configure` errors (missing env, bad path). Then `GET /v1/status` — verify `queue_depth` is moving. |
| Docs ingest but don't appear in search | `GET /v1/status` for `dead_letter_count > 0`, then `docker compose logs embeddings --tail 100` for timeouts or model-load failures. |
| Embeddings are very slow | Switch to `ollama` with a GPU-backed box, or keep `fastembed` and increase the embeddings replica count. The first embedding after startup always pays the model-load cost (~30 s). |
| Results feel stale | Add `"max_age_days": 90` to requests, or `"freshness_boost": 0.3` for a softer re-rank. If `content_modified_at` is missing from recent results, check that the adapter is stamping it (see §8). |
| `ConfigError: sources[0].config.token: missing env GITHUB_TOKEN` | The referenced env var isn't set inside the container. Add to `.env` and `docker compose up -d --force-recreate`. |
| `ConfigError: ... additionalProperties ...` | You added a typo at a strict level (top-level or inside `core`). The path pointer in the message tells you where. |

## 12. See also

- `docs/architecture.md` — the big-picture component map and the
  status semaphores used elsewhere.
- `docs/plugin-contracts.md` — the RFC behind the plugin system.
- `docs/writing-a-connector.md` — add a new source type.
- `docs/adding-a-modality.md` — extend the pipeline with e.g. audio.
