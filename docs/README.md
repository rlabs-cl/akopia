# Akopia — Documentation Index

Everything here assumes you've cloned the repo and at least skimmed
the top-level `README.md` for the quickstart.

## Start here (by audience)

- **Operators** — want to run and maintain akopia. Read
  [configuration.md](configuration.md) → [operations.md](operations.md)
  → [troubleshooting.md](troubleshooting.md).
- **Integrators** — want to use akopia from an app or AI tool.
  Read [rag-integration.md](rag-integration.md) or
  [mcp-integration.md](mcp-integration.md), with
  [api-reference.md](api-reference.md) as the HTTP reference.
- **Contributors** — want to extend akopia with a new adapter,
  extractor, or modality. Read [architecture.md](architecture.md) →
  [plugin-contracts.md](plugin-contracts.md), then
  [writing-a-connector.md](writing-a-connector.md) or
  [writing-an-extractor.md](writing-an-extractor.md) or
  [adding-a-modality.md](adding-a-modality.md) depending on what
  you're adding.

## Operator manuals

- **[configuration.md](configuration.md)** — every `akopia.yaml` field,
  every env var, every source/extractor config option the in-tree
  plugins accept. Start here if you're setting up akopia
  against your own data.
- **[operations.md](operations.md)** — end-to-end architecture for
  ops, backup/restore, upgrade paths (incl. model swaps), reverse
  proxies (Traefik + Caddy), token rotation, monitoring thresholds,
  scaling limits, security hardening checklist.
- **[sizing.md](sizing.md)** — per-component CPU / RAM / disk
  profile, deployment tiers, embedding provider trade-offs, disk
  growth model, bottleneck order, triggers to step up a tier.
- **[troubleshooting.md](troubleshooting.md)** — symptom → diagnostic
  command → root cause → fix. Covers queue backlog, DLQ replay,
  extractor file-not-found, stale search results, embedding
  slowness, language/model mismatch, and the "Source X not
  registered" UX gap.

## Integrator manuals

- **[rag-integration.md](rag-integration.md)** — primary framing
  doc. akopia as a retrieval layer for your RAG pipeline; the
  three retrieval modes; a ~60-line RAG loop in Python; context
  window management; freshness; language/model considerations;
  anti-patterns.
- **[mcp-integration.md](mcp-integration.md)** — wiring akopia
  into Claude Desktop, Claude Code, Cursor, Continue.dev over MCP.
  Lists every tool the `mcp-server` exposes, config snippets for
  each client, remote-vs-local deployment, troubleshooting.
- **[api-reference.md](api-reference.md)** — every REST endpoint
  with request/response schemas + `curl` examples. Points at the
  live `openapi.json` and `/docs` routes as the machine-readable
  source of truth.

## Contributor manuals

- **[writing-a-connector.md](writing-a-connector.md)** — add a new
  source type (SFTP, Notion, S3, a database CDC feed, …). Walks a
  full example adapter end-to-end and the entry-point wiring that
  lets you ship plugins out-of-tree.
- **[writing-an-extractor.md](writing-an-extractor.md)** — add a
  new content format (EPUB, RTF, LaTeX, a custom binary, …).
  Mirrors `writing-a-connector.md` for Layer 2 plugins.
- **[adding-a-modality.md](adding-a-modality.md)** — add a whole
  new content category (audio, video, 3D, …). Strictly more
  involved than adding an adapter or extractor because it touches
  the router, the embedder service, the Qdrant collection setup,
  and the search endpoints. Includes an explicit PR checklist.

## Reference

- **[architecture.md](architecture.md)** — the three-layer component
  map (Sources · Extractors · Core), deployment shapes, status
  semaphores. The big-picture reference.
- **[plugin-contracts.md](plugin-contracts.md)** — the RFC that
  drove the `SourceAdapter` / `ContentExtractor` protocols and the
  `ChangeEvent` / `ExtractedContent` wire format.

## Where to go after reading

- Adding code? See also the project-root `CONTRIBUTING.md` for
  commit-message style, test expectations, and PR requirements.
- Reporting a security issue? `SECURITY.md` in the repo root.
- Just want to search something? Start with
  `configuration.md` §11 (troubleshooting) or
  `troubleshooting.md` for deeper symptom-based diagnosis.
