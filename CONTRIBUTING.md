# Contributing to Akopia

Thanks for wanting to help. This guide covers how to get a dev loop
running, how the architecture is organized, and what we expect from
contributions.

## TL;DR

```bash
git clone https://github.com/rlabs-cl/akopia
cd akopia
cp .env.example .env
cp examples/akopia.yaml.example akopia.yaml
docker compose up --build
```

Then edit a file under `data/docs/`, wait a few seconds, and:

```bash
curl -X POST http://localhost:8080/v1/search/lexical \
  -H "Authorization: Bearer $(grep AKOPIA_BEARER_TOKEN .env | cut -d= -f2)" \
  -H "Content-Type: application/json" \
  -d '{"query":"your term","limit":3}'
```

If that returns your doc — you have a working dev loop. The rest of
this document explains how to change things.

## Project shape

Three layers (see `docs/architecture.md` for the full map):

- **Sources (Layer 3)** — adapters that ingest from somewhere (git,
  folder, web). Live under `adapters/`.
- **Extractors (Layer 2)** — turn bytes into clean text (plain,
  office, pdf-text, html). Live under `extractors/`.
- **Core (Layer 1)** — router + chunker + embedder + MCP server. Live
  under `concentrador/`, `common/`, `embeddings/`, `mcp_server/`.

Storage: Qdrant (vectors) + Meilisearch (lexical) + Redis Streams
(event bus).

All sources and extractors are **plugins** that implement a
`Protocol` from `common/protocols.py`. You don't need to fork the
core to add one — publish an entry point. See `docs/plugin-contracts.md`.

## Common development workflows

### Run tests

```bash
# Host (needs pytest + pytest-asyncio + respx in your env)
pytest tests/ -x

# Or in a disposable container, no host deps:
docker run --rm -v "$(pwd):/app" -w /app python:3.11-slim sh -c \
  "pip install -q -e '.[all-text]' pytest pytest-asyncio pytest-timeout httpx respx && pytest tests/ -x"
```

### Add a source adapter

1. Implement the `SourceAdapter` protocol (see existing: `adapters/folder.py`).
2. Register it via an entry point in `pyproject.toml` under
   `[project.entry-points."akopia.source_adapter"]`.
3. Write tests in `tests/test_<your>_adapter.py` using the patterns
   from `tests/test_folder_adapter.py`.
4. Add a minimal example to `examples/akopia.yaml.example`.

### Add an extractor

Same shape as above but for the `ContentExtractor` protocol. Extractors
read bytes or a path and return clean text + metadata. See
`extractors/plain.py` for the reference.

### Add an embedder backend

Implement the `EmbedderBackend` protocol in `embeddings/backends/`,
register it in `embeddings/backends/__init__.py` `build_backend()`. See
`ollama_backend.py` for a pattern with HTTP-backed models.

### Change a model

- Chunker lives in `common/chunker.py`. Default strategy is `recursive`
  (LangChain-style) with real `tiktoken` token counts and honored
  `overlap_tokens`. If you need a new strategy, add it as a named
  option and keep `recursive` as default.
- Anything that flows over the event bus is a Pydantic model in
  `common/models.py`. Adding fields is backward-compatible; renaming
  or changing types requires a migration plan.

## Code style

- **Python 3.11+**. Use `from __future__ import annotations`.
- **Types everywhere**. `mypy --strict` is a goal; we're not there yet
  but new code should be.
- **Short functions, clear names.** Functions >50 lines deserve a
  second look. Nested helpers > Long Jobs.
- **Pydantic for data on the wire.** Dataclasses for in-memory only.
- **No background work in import-time code.** Use `lifespan` hooks.
- **Logs at INFO for flow, DEBUG for detail, WARNING for recoverable
  issues, ERROR for dropped work + DLQ.** Don't log PII.

## Commit messages

Conventional Commits:

- `feat(scope): …` — new capability
- `fix(scope): …` — bug fix
- `docs(scope): …` — documentation-only
- `refactor(scope): …` — no behavior change
- `test(scope): …` — tests only
- `ci(scope): …` — CI config
- `chore(scope): …` — tooling / deps

Good scopes: `core`, `adapters`, `extractors`, `chunker`, `embedder`,
`router`, `mcp`, `compose`, `k8s`.

Body explains **why** (motivation / user impact) more than **what**
(the diff shows the what).

## Pull requests

Before opening:

- Ran `pytest tests/ -x` and it passes (note any skipped tests and
  why).
- Ran `docker compose up --build` and confirmed the stack still
  ingests a file end-to-end.
- Updated `CHANGELOG.md` under `## [Unreleased]` if user-visible.
- Updated `docs/architecture.md` if you changed the shape of a layer.
- Updated `examples/akopia.yaml.example` if you added a config field.

Checklist in the PR template covers all of the above.

## Reporting bugs

See `.github/ISSUE_TEMPLATE/bug_report.yml` — please fill in the
reproduction steps. Minimal compose-based repros get fixed faster.

## Security

Do **not** open a public issue for security problems. See
[SECURITY.md](SECURITY.md) for the private disclosure channel.

## Licensing

By submitting a contribution you agree it's licensed under the
project's MIT license (see `LICENSE`).
