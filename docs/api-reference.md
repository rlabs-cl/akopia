# Akopia — HTTP API Reference

**Audience:** developers writing direct HTTP clients against the
concentrador service.

**Source of truth.** The REST layer is implemented with FastAPI in
`concentrador/main.py`. FastAPI auto-generates a machine-readable spec:

- OpenAPI JSON: `GET http://localhost:8080/openapi.json`
- Swagger UI:   `GET http://localhost:8080/docs`
- ReDoc:        `GET http://localhost:8080/redoc`

**This markdown is the narrative layer on top of that spec.** When
the two diverge, the OpenAPI spec wins — it is generated from the
running code. Please file an issue if you spot drift.

## Conventions

- **Base URL:** `http://<host>:<port>` (default `http://localhost:8080`;
  the port comes from `CONCENTRADOR_PORT` in `.env`, default `8080`).
- **Auth:** every endpoint except `GET /health` and `GET /v1/status`
  requires `Authorization: Bearer <AKOPIA_BEARER_TOKEN>`. Missing or
  wrong token → `401`.
- **Content type:** all bodies are JSON (`Content-Type: application/json`).
- **Errors:** FastAPI's default shape:
  ```json
  {"detail": "Source src-git-abc is not registered"}
  ```
  or for validation failures:
  ```json
  {"detail": [{"loc": ["body", "query"], "msg": "field required", "type": "value_error.missing"}]}
  ```
- **Timestamps:** ISO-8601 UTC strings (`2026-04-22T18:30:00+00:00`).
- **No rate limit** in the MVP. The server will happily burst under
  load. See `docs/operations.md` §Scaling for guidance.
- **No pagination cursors.** Endpoints that return lists use
  `limit` / `offset` where relevant; everything else returns the
  entire result set.

## Quick smoke test

```bash
TOKEN=$(grep AKOPIA_BEARER_TOKEN .env | cut -d= -f2)

# no auth required
curl -s http://localhost:8080/health
# {"status":"ok"}

curl -s http://localhost:8080/v1/status | jq
# {"status":"healthy","components":{...},"queue_depth":0,"dead_letter_count":0}

# auth required
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/sources | jq
```

---

## `GET /health`

Liveness probe. Returns `200` if the FastAPI process is up. Does
**not** check downstream services.

| Auth | Request body | Response |
|------|--------------|----------|
| none | none         | `{"status": "ok"}` |

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

Use this in compose healthchecks (as `docker-compose.yml` already does)
and k8s liveness probes. For readiness, prefer `/v1/status`.

---

## `GET /v1/status`

Aggregate health of every dependency + queue backlog.

| Auth | Request body | Response |
|------|--------------|----------|
| none | none         | see below |

Response shape:

```json
{
  "status": "healthy",              // healthy | degraded | unhealthy
  "components": {
    "qdrant": "up",                 // up | down
    "meilisearch": "up",
    "redis": "up"
  },
  "queue_depth": 12,                // length of embedding-jobs stream
  "dead_letter_count": 0            // length of dead-letter stream
}
```

- `healthy` — all three backends up.
- `degraded` — at least one up, at least one down.
- `unhealthy` — all three down.

Watch `queue_depth` to know whether the embedder is keeping up, and
`dead_letter_count` to know whether anything is failing permanently.
See `docs/operations.md` §Monitoring for alert thresholds.

```bash
curl -s http://localhost:8080/v1/status | jq
```

---

## `GET /v1/sources`

List every source registered in the concentrador's registry.

| Auth   | Request body | Response                |
|--------|--------------|-------------------------|
| Bearer | none         | `{"sources": [Source]}` |

`Source` objects are whatever `Source.model_dump()` in
`common/models.py` produces — see the model for the authoritative list
of fields. Typical shape:

```json
{
  "sources": [
    {
      "source_id": "src-git-abc123",
      "type": "git",
      "name": "yourorg/yourrepo",
      "url": "https://github.com/yourorg/yourrepo.git",
      "branch": "main",
      "status": "idle",
      "last_sync": "2026-04-22T10:00:00+00:00",
      "indexed_files": 182,
      "pending_jobs": 0,
      "sync_cron": "*/5 * * * *",
      "file_patterns": ["**/*"]
    }
  ]
}
```

**Note.** This endpoint lists sources *known to the concentrador's
Redis registry*. Adapter plugins maintain their own in-memory "already
seen" state independently. A source configured in `akopia.yaml` that no
one has ever registered via `POST /v1/sources` will appear as
indexed documents *without* an entry here. See `docs/troubleshooting.md`
§12 for the workaround.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/sources | jq
```

---

## `POST /v1/sources`

Register a new source in the concentrador's registry. Two body shapes
depending on `type`:

### Git

```json
{
  "type": "git",
  "name": "acme/docs",
  "git_url": "https://github.com/acme/docs.git",
  "branch": "main",                          // default "main"
  "sync_cron": "*/5 * * * *",                // default */5 min
  "file_patterns": ["**/*.md", "docs/**"]    // default ["**/*"]
}
```

### Folder

```json
{
  "type": "folder",
  "name": "local-docs",
  "folder_path": "/data/docs",
  "sync_cron": "*/10 * * * *",               // default */10 min
  "file_patterns": ["**/*"]
}
```

Response: `201 Created` with the full `Source` model:

```json
{
  "source_id": "src-git-3f2a1b",
  "type": "git",
  "name": "acme/docs",
  "url": "https://github.com/acme/docs.git",
  "branch": "main",
  ...
}
```

Duplicate name → `409 Conflict`. Unknown `type` → `400`.

```bash
curl -X POST http://localhost:8080/v1/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"git","name":"acme/docs","git_url":"https://github.com/acme/docs.git"}'
```

**Important.** Registering a source here does **not** cause an adapter
to start watching it. Adapter plugins are spawned by docker-compose
and read `akopia.yaml`; this endpoint populates the metadata registry the
search-path endpoints consult. Typical ingestion flow:

1. Edit `akopia.yaml` to declare the source.
2. `docker compose up -d plugin-adapter-<name>` spawns the adapter.
3. Call `POST /v1/sources` so the concentrador knows about the
   `source_id` the adapter is about to stamp on events.

This split is a known UX sharp edge — see `docs/troubleshooting.md` §12.

---

## `POST /v1/sources/{source_id}/sync`

Trigger an immediate sync of a registered source.

| Auth   | Request body | Response  |
|--------|--------------|-----------|
| Bearer | none         | `202`     |

Acquires a 30-minute distributed lock on the `source_id`; a second
concurrent call returns `409 Conflict`. Publishes a `trigger_sync`
event to `change-events` so the adapter owning the source picks it up.
The adapter decides whether/how to honor it — most adapters support
it, the git adapter honors it by running an immediate `git fetch`.

Response:

```json
{"sync_id": "sync-1a2b3c4d", "status": "detecting", "priority": "manual"}
```

Errors:

- `404` — `source_id` not registered (via `POST /v1/sources`).
- `409` — sync already in progress for this source.

```bash
curl -X POST http://localhost:8080/v1/sources/src-git-abc123/sync \
  -H "Authorization: Bearer $TOKEN"
```

---

## `POST /v1/sources/{source_id}/reindex`

Purge every indexed chunk/doc for `source_id` from Qdrant+Meili **and**
trigger a sync. Convenience wrapper over `DELETE .../index` +
`POST .../sync` with a single lock.

| Auth   | Request body | Response |
|--------|--------------|----------|
| Bearer | none         | `202`    |

```json
{
  "source_id": "src-git-abc123",
  "purged": {"qdrant": 182, "meili": 182},
  "sync_id": "reindex-9a8b7c6d",
  "sync_triggered": true,
  "next_step": "restart the adapter process for a full rebuild — ..."
}
```

Errors: `404` if the source is not registered.

**Caveat.** The adapter keeps an in-memory "already seen" set that
this endpoint cannot touch. For a full rebuild, restart the adapter
process after calling this:

```bash
curl -X POST http://localhost:8080/v1/sources/src-git-abc123/reindex \
  -H "Authorization: Bearer $TOKEN"
docker compose restart plugin-adapter-git
```

See `docs/operations.md` §Upgrade-path and `docs/troubleshooting.md` §6
for the gory details.

---

## `DELETE /v1/sources/{source_id}/index`

Delete every chunk/doc in Qdrant+Meili tagged with `source_id`. Useful
when:

- Model or tokenizer changed (old vectors are worthless).
- A repo was re-shaped upstream.
- A source was misconfigured and fed garbage into the index.

| Auth   | Request body | Response |
|--------|--------------|----------|
| Bearer | none         | `200`    |

```json
{
  "source_id": "src-git-abc123",
  "purged": {"qdrant": 182, "meili": 182},
  "next_step": "restart the adapter process for src-git-abc123 ..."
}
```

Does **not** remove the source from the registry (use a direct Redis
`HDEL source:src-git-abc123` for that — no REST endpoint today).
Does **not** reset the adapter's in-memory state. See
`docs/troubleshooting.md` §6.

```bash
curl -X DELETE http://localhost:8080/v1/sources/src-git-abc123/index \
  -H "Authorization: Bearer $TOKEN"
```

---

## `POST /v1/search/lexical`

BM25 search via Meilisearch. Best for exact terms / identifiers /
error codes / product SKUs.

### Request

```json
{
  "query": "AKOPIA_ROUTER_USE_EXTRACTORS",      // required
  "repo": "akopia",                    // optional
  "path_prefix": "docs/",                   // optional
  "modality": "text",                       // text | audio_transcript | video_transcript
  "limit": 20,                              // default 20
  "offset": 0,                              // default 0
  "max_age_days": 365,                      // optional hard filter
  "freshness_boost": 0.15                   // optional soft rerank, [0,1]
}
```

### Response

```json
{
  "results": [
    {
      "doc_id": "src-git-abc:docs/configuration.md:0",
      "source_id": "src-git-abc",
      "repo": "akopia",
      "path": "docs/configuration.md",
      "modality": "text",
      "snippet": "Route change-events through the plugin extractor path ...",
      "derived_from": null,
      "highlights": "<em>AKOPIA_ROUTER_USE_EXTRACTORS</em>=1 ...",
      "content_modified_at": "2026-04-22T10:00:00+00:00",
      "content_modified_ts": 1745316000
    }
  ],
  "total": 4,
  "query_time_ms": 18
}
```

`highlights` uses Meili's HTML highlight markers around hit terms.
Dedupe by `path` if you are building a file-level UI.

```bash
curl -X POST http://localhost:8080/v1/search/lexical \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"refund policy","limit":5}'
```

---

## `POST /v1/search/semantic`

Cosine-similarity vector search via Qdrant. Best for meaning /
paraphrase / natural-language queries.

### Request

```json
{
  "query": "how do we handle late-arriving webhooks?",  // required
  "modality": "text",                                    // optional
  "repo": "akopia",                                 // optional
  "path_prefix": "docs/",                                // optional
  "top_k": 10,                                           // default 10
  "score_threshold": 0.25,                               // default 0.0
  "max_age_days": 365,                                   // optional hard filter
  "freshness_boost": 0.15                                // optional soft rerank
}
```

### Response

```json
{
  "results": [
    {
      "chunk_id": "src-git-abc:docs/webhooks.md:2",
      "source_id": "src-git-abc",
      "repo": "akopia",
      "path": "docs/webhooks.md",
      "modality": "text",
      "snippet": "Adapters that expose webhooks must persist watermarks ...",
      "score": 0.82,
      "derived_from": null,
      "content_modified_at": "2026-04-22T10:00:00+00:00",
      "content_modified_ts": 1745316000
    }
  ],
  "total": 7,
  "query_time_ms": 42
}
```

`score` is cosine similarity in `[0, 1]`. `0.25` is a reasonable
floor for "this might be relevant"; above `0.6` is usually a strong
match.

```bash
curl -X POST http://localhost:8080/v1/search/semantic \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"dead letter queue retry logic","top_k":5}'
```

---

## `POST /v1/rag/ask`

Hybrid retrieval (semantic + lexical in parallel), merge, re-rank, and
assemble a context string ready to feed an LLM.

**This endpoint does not call an LLM.** It returns pre-formatted
context and source list. The caller's job is to prompt the LLM with
that context. See `docs/rag-integration.md` §4.

### Request

```json
{
  "question": "how do refunds work?",          // required
  "repos": ["yourorg/policies"],               // optional list
  "path_prefix": "docs/",                      // optional
  "max_context_chars": 6000,                   // default 6000
  "top_k": 15,                                 // default 15
  "semantic_weight": 0.7,                      // default 0.7 (lexical weight = 1 - this)
  "max_age_days": 365,                         // optional hard filter
  "freshness_boost": 0.15                      // optional soft rerank
}
```

### Response

```json
{
  "context": "### docs/refunds.md (score: 0.81)\nRefunds are issued within 14 days ...\n\n### docs/billing.md (score: 0.73)\n...",
  "sources": [
    {"path": "docs/refunds.md", "source_id": "src-git-abc", "score": 0.812, "chars": 1840},
    {"path": "docs/billing.md", "source_id": "src-git-abc", "score": 0.726, "chars": 1020}
  ],
  "stats": {
    "semantic_hits": 10,
    "lexical_hits": 12,
    "merged_candidates": 17,
    "sources_in_context": 2,
    "context_chars": 2880,
    "query_time_ms": 184
  }
}
```

Ranking: semantic score and a lexical "presence boost" (fixed at 0.5
when lexical returns a hit) are combined via `semantic_weight`. Results
are then grouped by `path` (one chunk per file: the top scorer) and
concatenated until the `max_context_chars` budget is hit.

```bash
curl -X POST http://localhost:8080/v1/rag/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"how are embeddings batched?","top_k":10}'
```

---

## `GET /v1/files`

List indexed files (chunks, actually — one row per indexed chunk).
Use this to discover what is in the index from a file perspective.

### Query parameters

| Param         | Type   | Default | Notes                                 |
|---------------|--------|---------|---------------------------------------|
| `source_id`   | string | —       | Filter to one source.                 |
| `path_prefix` | string | —       | Filter by path prefix.                |
| `modality`    | string | —       | `text`, `image`, etc.                 |
| `limit`       | int    | 50      | Max rows returned.                    |
| `offset`      | int    | 0       | Pagination cursor.                    |

### Response

```json
{
  "files": [
    {"path": "docs/configuration.md", "source_id": "src-git-abc", ...}
  ],
  "total": 1
}
```

`total` is the length of `files` in the current page — not the total
number of matches in the index. If you need an accurate count, keep
paging with `offset` until you get fewer than `limit` rows.

```bash
curl -s "http://localhost:8080/v1/files?source_id=src-git-abc&limit=10" \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## Error catalogue

| Status | When                                                           |
|--------|----------------------------------------------------------------|
| `400`  | Unknown source `type` on `POST /v1/sources`.                   |
| `401`  | Missing / wrong bearer token.                                  |
| `404`  | `source_id` not registered for sync / reindex / purge.         |
| `409`  | Duplicate source name; sync already in progress.               |
| `422`  | Pydantic validation failure on request body (FastAPI default). |
| `500`  | Unhandled exception — check `docker compose logs concentrador`. |

## Idempotency

The search endpoints are safe to retry freely (pure reads).

The sync / reindex endpoints acquire a 30-minute distributed lock via
Redis — a second call while a sync is in-flight returns `409`. Once
the adapter finishes, the lock is released (by the adapter on
success, or by the TTL on crash).

`POST /v1/sources` is **not** idempotent: a second call with the same
`name` returns `409 Conflict` because source IDs are randomly
generated per call. If you need idempotent provisioning, look up first
via `GET /v1/sources` and branch on existence.

## Streaming

No endpoint streams today. `/v1/rag/ask` is synchronous. See
`docs/rag-integration.md` §9 for how to get streamed tokens out of
your LLM anyway.

## See also

- `docs/rag-integration.md` — using these endpoints from your own code.
- `docs/mcp-integration.md` — the same operations exposed over MCP.
- `docs/configuration.md` §9 — bearer-token configuration.
- `docs/operations.md` — production deployment concerns.
- `concentrador/main.py` — authoritative implementation.
- `http://localhost:8080/openapi.json` — machine-readable spec.
