# Changelog

All notable changes to Akopia will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] — 2026-04-24

Inaugural release.

### Added
- **Plugin-first core**: `SourceAdapter` + `ContentExtractor` + `EmbedderBackend` Protocols with Python entry-point discovery.
- **Source adapters**: git, folder, web-single, web-deep.
- **Content extractors**: plain, office, pdf-text, html.
- **Hybrid retrieval**: lexical (Meilisearch) + semantic (Qdrant) behind one REST API.
- **Freshness as a retrieval primitive**: `max_age_days` filter + `freshness_boost` rerank.
- **Event-bus pipeline** over Redis Streams with consumer groups, DLQ, idempotency.
- **MCP server** with bearer auth (`AKOPIA_STRICT_AUTH=1` for fail-closed prod) and constant-time token compare.
- **Pluggable embedder backends**: fastembed (CPU) and ollama (external GPU).
- **Parallel ingestion**: concurrent git clones + concurrent embedder batches.
- **docker compose** deploy path.

[Unreleased]: https://github.com/rlabs-cl/akopia/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rlabs-cl/akopia/releases/tag/v0.1.0
