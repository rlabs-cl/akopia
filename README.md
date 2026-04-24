# Akopia

[![status](https://img.shields.io/badge/status-alpha-orange)](CHANGELOG.md)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Plug. Sync. Serve. — the headless retrieval plane for your AI stack.** RAG infrastructure
with freshness-aware hybrid search, plugin-first extensions, and MCP
integration. Pluggable **source adapters** (git, folder, web) and
**content extractors** (plain/office/PDF/HTML) on top of a fixed core:
**Qdrant** (vector) + **Meilisearch** (lexical) + **Redis** (event
bus). An **MCP server** exposes search to any MCP-capable agent.

Built lego-style: the core is the table, adapters and extractors are the
pieces you snap on. Install only what you need.

## Quickstart (5 minutes, docker compose)

```bash
git clone https://github.com/rlabs-cl/akopia
cd akopia

# 1. Configure
cp examples/akopia.yaml.example akopia.yaml      # edit: point `sources:` at your data
cp .env.example .env                           # edit: set AKOPIA_BEARER_TOKEN + MEILI_MASTER_KEY
mkdir -p data/docs && cp ~/some-notes.md data/docs/    # default folder source

# 2. Run
docker compose up --build -d

# 3. Verify
curl -fsS http://localhost:8080/health    # concentrador
curl -fsS http://localhost:8081/health    # embeddings
curl -fsS http://localhost:8082/health    # MCP server

# 4a. Query via HTTP (concentrador REST API — fastest smoke test)
curl -X POST http://localhost:8080/v1/search/semantic \
  -H "Authorization: Bearer $AKOPIA_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query": "retrieval augmented generation", "top_k": 5}'
```

**4b. Query via MCP (from an agent).** The MCP server on `:8082` speaks
JSON-RPC over SSE, not REST — wire it into Claude Desktop, Claude Code,
Cursor, or Continue.dev with your bearer token. One-page setup per
client in [docs/mcp-integration.md](docs/mcp-integration.md).

Tools exposed by the MCP server: `search_semantic`, `search_lexical`,
`search_images_by_text`, `list_sources`, `add_git_source`, `trigger_sync`,
`get_file`, `get_status`.

## Concepts

- **Source adapter** — *where content lives.* One plugin per source type
  (git, folder, web-single, web-deep, …). Subclasses
  [`BaseSourceAdapter`](common/base_adapter.py) and yields `ChangeEvent`s.
- **Content extractor** — *how to turn bytes into text.* One plugin per
  MIME family (plain, office, pdf-text, html, …). Subclasses
  [`BaseExtractor`](common/base_extractor.py) and emits `ExtractedContent`.
- **Router** ([concentrador/router.py](concentrador/router.py)) —
  dispatches `ChangeEvent` → extractor by MIME/extension, then chunks
  `ExtractedContent` into `EmbeddingJob`s (one per page when the extractor
  produces pages — keeps slide/sheet citations accurate).
- **DLQ drainer** — retries transient failures with exponential backoff
  (1 min / 5 min / 15 min, max 3 attempts) before flagging terminal.
- **akopia.yaml** — single config file. Validated against a JSON Schema.
  Supports `${VAR}` / `${VAR:-default}` env interpolation.

See [docs/architecture.md](docs/architecture.md) for the layered view with
component sizing and [docs/plugin-contracts.md](docs/plugin-contracts.md)
for the full protocol specs.

## Add your own plugin

Write a class, set `plugin_id`, register via entry points, `pip install .` —
that's the whole contract. No fork required.

```python
# akopia_myadapter/__init__.py
from common.base_adapter import BaseSourceAdapter
from common.models import ChangeEvent, Modality, Operation, Source

class MyAdapter(BaseSourceAdapter):
    plugin_id = "my-source"

    async def configure(self, config: dict) -> None:
        self.api_key = config["api_key"]

    async def discover(self):
        yield Source(source_id="root", type="my-source", name="My system")

    async def watch(self, source):
        while not self._shutdown.is_set():
            for item in await self._fetch_changes():
                yield self._make_change_event(
                    path=item.path, operation=Operation.ADD,
                    modality=Modality.TEXT,
                )
            await asyncio.sleep(60)

    async def read(self, source, path):
        return await self._fetch_bytes(path)
```

```toml
# akopia_myadapter/pyproject.toml
[project.entry-points."akopia.source_adapter"]
my-source = "akopia_myadapter:MyAdapter"
```

Now `my-source` is a valid `type:` in `akopia.yaml`. See
[docs/plugin-contracts.md](docs/plugin-contracts.md) for the full
`SourceAdapter` / `ContentExtractor` protocols.

## What ships in core

| Kind       | Plugin      | Notes                                           |
|------------|-------------|-------------------------------------------------|
| Adapter    | `git`       | GitHub / GitLab / Gitea, provider-abstracted    |
| Adapter    | `folder`    | Local filesystem, include/exclude globs         |
| Adapter    | `web-single`| Poll one URL on cron, ETag / Last-Modified      |
| Adapter    | `web-deep`  | BFS crawl, depth limit, robots.txt, rate limit  |
| Extractor  | `plain`     | Text / markdown / JSON / YAML / CSV / source    |
| Extractor  | `office`    | docx / xlsx / pptx / odt / ods / odp            |
| Extractor  | `pdf-text`  | Native-text PDFs (`pypdfium2`)                  |
| Extractor  | `html`      | Article extraction (`trafilatura`)              |

Image ingestion works when the adapter and the embeddings service share
a volume (the default compose wires this). OCR (scanned PDFs) and ASR
(audio/video) are planned as extractor plugins.

## Embedding providers

Text embeddings are pluggable via `core.embeddings.text.provider` in
`akopia.yaml`. Default is fastembed (in-process, CPU, zero external deps).
For throughput, point at an external Ollama server and keep compute
off the Akopia pod.

```yaml
# akopia.yaml — default fastembed (quickstart)
core:
  embeddings:
    text:
      provider: fastembed
      model: nomic-embed-text-v1.5
      quantized: true

# akopia.yaml — external Ollama
core:
  embeddings:
    text:
      provider: ollama
      url: "${OLLAMA_URL}"       # e.g. http://host.docker.internal:11434
      model: nomic-embed-text
```

Set `OLLAMA_URL` in `.env` (compose forwards it via the `x-akopia-env`
anchor and adds `host.docker.internal:host-gateway` so it resolves on
Linux). The embeddings service auto-detects batch `/api/embed` support
and falls back to per-text `/api/embeddings` on older Ollama versions.
`tei` and `openai` providers are planned for 0.2.0.

## Install as a library (without compose)

Akopia isn't on PyPI — install directly from the repo:

```bash
pip install "akopia[all-text,fastembed] @ git+https://github.com/rlabs-cl/akopia.git"
# or just the pieces you want:
pip install "akopia[plain,folder,fastembed] @ git+https://github.com/rlabs-cl/akopia.git"      # minimal
pip install "akopia[office,pdf,html] @ git+https://github.com/rlabs-cl/akopia.git"             # extra extractors
```

Validate a config, run a plugin standalone:

```bash
akopia validate                                   # validates ./akopia.yaml
akopia run adapter local-docs                     # runs a source by id
akopia run extractor plain                        # runs an extractor by type
```

## Deployment

**Docker Compose** (this README) is the supported deploy path for 0.1.0 —
laptops, homelabs, single-host production. It's a single
`docker compose up --build` from clone to first query.

Kubernetes manifests live under [k8s/](k8s/) as a reference for operators
who need them, but they're not part of the supported public surface yet.
Helm and operator packaging are on the roadmap for post-1.0.

## Development

```bash
git clone https://github.com/rlabs-cl/akopia && cd akopia
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all-text,fastembed,mcp,dev]"
pytest                                           # full test suite
```

See [CHANGELOG.md](CHANGELOG.md) for the release history. Issues and PRs
welcome at <https://github.com/rlabs-cl/akopia/issues>.

## License

MIT — see [LICENSE](LICENSE).
