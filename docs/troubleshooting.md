# Akopia — Troubleshooting

**Audience:** operators + integrators when something is wrong.

Format: **Symptom → Diagnostic → Root cause → Fix.** Every entry gives
the exact command to run.

Quick reference — the three endpoints you will type most often:

```bash
TOKEN=$(grep AKOPIA_BEARER_TOKEN .env | cut -d= -f2)

# system health
curl -s http://localhost:8080/v1/status | jq

# registered sources
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/sources | jq

# dead-letter inspection
docker compose exec redis redis-cli XRANGE dead-letter - + COUNT 20
```

---

## 1. `queue_depth` keeps rising

**Symptom.**
```bash
$ curl -s http://localhost:8080/v1/status | jq .queue_depth
12403    # and climbing
```

**Diagnose.**
```bash
# Is the embedder even running?
docker compose ps embeddings
# Is it stuck on something?
docker compose logs embeddings --tail 100
# How busy is its CPU?
docker stats --no-stream | grep embeddings
```

**Root causes (in order of likelihood):**

1. Embedder is down or crash-looping (model load failure, OOM).
2. Embedder is up but saturated — CPU pinned at 100% on one core,
   fastembed is single-threaded per request.
3. Large ingestion just started (normal; drain will happen).
4. `change-events` stream full — check `redis-cli XLEN change-events`.
   If `>100k`, the router is the bottleneck.

**Fix.**

- *Embedder down:* `docker compose restart embeddings`, watch logs.
  If it OOMs, the compose default of 8 GiB is usually enough; if
  you added CLIP on a small host, raise `mem_limit` or split text
  and image embedders.
- *Embedder saturated:* scale out. In compose,
  `docker compose up -d --scale embeddings=2` — they share the
  `cg-embedder` consumer group and load-balance automatically.
- *Normal drain:* watch it. Rate is typically 5-50 docs/sec on CPU.
- *Router bottleneck:* `docker compose logs concentrador --tail 100`
  for errors; restart if it's stuck on one message (rare but happens).

Backpressure safety net: the router pauses for 5s when
`queue_depth > 10_000`. So worst case it plateaus; it doesn't
take down the adapter.

---

## 2. `dead_letter_count > 0` — how to inspect and replay

**Symptom.**
```bash
$ curl -s http://localhost:8080/v1/status | jq .dead_letter_count
17
```

**Diagnose.**
```bash
# Read the 20 most recent DLQ entries.
docker compose exec redis redis-cli XRANGE dead-letter - + COUNT 20
```

A typical entry:

```
1) 1) "1745406812345-0"
   2) 1) "job_id"
      2) "4fa82c..."
      3) "status"
      4) "failure"
      5) "error"
      6) "extractor timed out after 60s"
      7) "original"
      8) "{...original EmbeddingJob payload...}"
      9) "stream"
     10) "embedding-jobs"
```

Error shapes you will see:

- `extractor timed out after 60s` — a pathological file blew past the
  extractor timeout. Find the file in the preserved payload's `chunks[0].path`,
  confirm it's corrupt / huge / malformed.
- `Error loading model` — embedder was misconfigured or the model
  download failed partway. Happens on first boot with a fresh volume.
- `httpx.ConnectError` — backend (Qdrant or Meili) wasn't reachable
  at the moment of the upsert. Usually transient; the drainer retries
  automatically (1, 5, 15 min backoff, 3 attempts total).
- `Validation error` — upstream producer shipped malformed JSON.
  Usually a bug in a new adapter/extractor you just added.

**Root cause.** The DLQ drainer already retried 3 times and gave up.
Whatever is in the DLQ now needs human action.

**Fix — replay after you've fixed the root cause.**

```bash
# Re-publish one entry back to its originating stream.
docker compose exec redis sh -c '
  redis-cli XRANGE dead-letter 1745406812345-0 1745406812345-0 | \
  redis-cli --rdb-stream-entry... '   # ugly; prefer the python helper below
```

Cleaner: use a one-liner Python script:

```bash
docker compose exec concentrador python -c '
import json, asyncio
from common.redis_client import RedisClient
async def main():
    r = RedisClient(); await r.connect()
    msgs = await r.r.xrange("dead-letter", "-", "+", count=100)
    for msg_id, data in msgs:
        original = data.get("original")
        stream = data.get("stream", "embedding-jobs")
        if original:
            await r.r.xadd(stream, {"data": original})
            await r.r.xdel("dead-letter", msg_id)
asyncio.run(main())'
```

Once fixed, verify:

```bash
curl -s http://localhost:8080/v1/status | jq .dead_letter_count
# → 0
```

---

## 3. Adapter crash-loops with `missing env X`

**Symptom.**
```
$ docker compose logs plugin-adapter-git --tail 20
ConfigError: sources[0].config.token: missing env GITHUB_TOKEN
plugin-adapter-git exited with code 1
plugin-adapter-git exited with code 1      # restart loop
```

**Diagnose.**
```bash
# What env does the adapter actually see?
docker compose exec plugin-adapter-git env | grep -E 'TOKEN|URL'
# What does .env on the host look like?
grep -E 'TOKEN=' .env
```

**Root cause.** `akopia.yaml` references `"${GITHUB_TOKEN}"` (or `GITEA_TOKEN`,
or any other) but the variable is unset or empty in `.env`.

**Fix.**

```bash
# Edit .env to add the missing variable.
echo 'GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxx' >> .env

# Recreate the adapter (not just restart — restart doesn't re-read .env).
docker compose up -d --force-recreate plugin-adapter-git
```

Tokens referenced by `${FOO}` fail fast; `${FOO:-default}` falls back.
If you don't want the adapter to require a token, use the fallback
form in `akopia.yaml` or make the source public.

---

## 4. Extractor can't find files that the adapter cloned

**Symptom.**
```
$ docker compose logs plugin-extractor-plain --tail 30
FileNotFoundError: /tmp/akopia-repos/acme/docs/README.md
```

**Diagnose.**
```bash
# Is the git-repos volume mounted into the extractor?
docker compose config | grep -A 5 plugin-extractor-plain
# Compare with the adapter:
docker compose config | grep -A 5 plugin-adapter-git
```

**Root cause.** The git adapter clones into the `git-repos` named
volume at `/tmp/akopia-repos`. Extractors need the **same** volume mounted
(read-only is fine) so they can resolve `ContentRef(kind="path",
path="/tmp/akopia-repos/...")`. A common mistake: only the adapter has the
volume. We actually hit this exact bug.

**Fix.** Add the shared volume to every extractor service in
`docker-compose.yml`:

```yaml
  plugin-extractor-plain:
    volumes:
      - ./akopia.yaml:/app/akopia.yaml:ro
      - ./data/docs:/data/docs:ro
      - git-repos:/tmp/akopia-repos:ro       # ← this line
```

Then:

```bash
docker compose up -d --force-recreate plugin-extractor-plain
```

The `:ro` is deliberate — extractors should not be able to mutate the
clones the adapter maintains.

---

## 5. Qdrant OOM during mass ingestion

**Symptom.**
```
$ docker compose logs qdrant --tail 20
[... crash ...]
$ docker compose ps qdrant
# exited with code 137 (OOMKilled)
```

**Diagnose.**
```bash
# What did it grow to?
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' | grep qdrant
# Collection size right now.
curl -s http://localhost:6333/collections/akopia_text | jq
```

**Root cause.** Qdrant builds in-memory indices while ingesting;
during mass inserts it may briefly need 2-3× the steady-state
footprint. On small hosts this exceeds whatever cgroup limit docker
has applied.

**Fix.**

- *Immediate:* raise the limit. Add to `qdrant` in `docker-compose.yml`:
  ```yaml
    mem_limit: 4g     # or higher for larger corpora
  ```
- *Structural:* rate-limit ingestion. The adapter's `poll_seconds`
  controls how much it emits per cycle; raise it during initial
  backfill, lower it for steady state.
- *Tune Qdrant's flush:* set env `QDRANT__STORAGE__OPTIMIZERS__DEFAULT_SEGMENT_NUMBER=2`
  on the `qdrant` service to reduce per-segment overhead. Restart.

---

## 6. Results look stale after updating a source

**Symptom.** You pushed new content to a repo / folder and search
still returns the old version, even after `POST .../sync`.

**Diagnose.**
```bash
# What does the adapter think the current state is?
docker compose logs plugin-adapter-git --tail 50 | grep -E 'ADD|MODIFY|DELETE'
# Is content_modified_at flowing through?
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/v1/search/lexical \
  -H "Content-Type: application/json" \
  -d '{"query":"<your-term>","limit":1}' | jq '.results[0].content_modified_at'
```

**Root cause.** The adapter keeps an in-memory "already seen" set
keyed by `(path, mtime, size)` or `(path, commit_sha)`. `POST
.../reindex` purges the indices but **does not** reset that in-memory
set. So the adapter re-scans, sees "same mtime, already seen, skip,"
and emits nothing. Meanwhile the index is empty until an actual new
event triggers re-ingestion. (This is documented in `concentrador/main.py::purge_source_index`.)

**Fix — the documented recipe:**

```bash
# 1. Purge the index.
curl -X DELETE http://localhost:8080/v1/sources/src-git-abc/index \
  -H "Authorization: Bearer $TOKEN"

# 2. Restart the adapter — this clears its in-memory state.
docker compose restart plugin-adapter-git

# 3. Adapter's next scan treats every file as new, emits ADD events.
# Watch queue_depth.
watch -n 2 'curl -s http://localhost:8080/v1/status | jq .queue_depth'
```

The `/v1/sources/{id}/reindex` endpoint does steps 1+2 in one call —
but **still** requires the restart in step 2. The response body
literally says `"next_step": "restart the adapter process..."`. Read it.

---

## 7. Embeddings very slow

**Symptom.** `queue_depth` drains at 0.5-2 docs/sec instead of the
expected 5-50.

**Diagnose.**
```bash
# Check the embedding provider.
docker compose exec embeddings env | grep -E 'EMBEDDER|OLLAMA'
# CPU usage during ingestion.
docker stats --no-stream | grep embeddings
# Is it GPU or CPU fastembed?
docker compose logs embeddings --tail 50 | grep -iE 'onnx|provider|cuda'
```

**Root cause — two usual suspects:**

- **fastembed on CPU, small host.** Pure-CPU ONNX on a 2-core VM is
  genuinely slow. fastembed runs single-threaded per request by
  default.
- **Ollama URL unreachable or wrong model.** If `OLLAMA_URL` points
  at a host-network Ollama on a GPU box, but the container can't
  reach it, requests fall back on timeouts (look for `httpx.ReadTimeout`
  in the logs).

**Fix.**

- *CPU fastembed:* keep it if you can live with it. Otherwise:
  - Run Ollama on a machine with a GPU (even a laptop 3060 is ~20x
    faster than CPU fastembed).
  - Set `OLLAMA_URL=http://host.docker.internal:11434` in `.env` on
    the same machine as akopia, or the LAN IP for remote.
  - Change `akopia.yaml`:
    ```yaml
    core:
      embeddings:
        text:
          provider: ollama
          model: bge-m3          # or nomic-embed-text
          url: "${OLLAMA_URL}"
    ```
  - Restart: `docker compose restart embeddings concentrador`.
- *Ollama unreachable:* verify connectivity —
  `docker compose exec embeddings curl -s http://host.docker.internal:11434/api/tags`.
  If that hangs, the docker host's firewall is blocking or
  `extra_hosts: host.docker.internal:host-gateway` isn't taking
  effect (Linux only).

First request after embedder restart always pays a ~30-90s model-load
cost. That's normal; subsequent requests are fast.

---

## 8. Semantic search returns nothing for obvious matches

**Symptom.** `search_semantic` returns empty or near-empty results for
a query that should clearly match an indexed document.

**Diagnose.**
```bash
# Does lexical find it?
curl -s -H "Authorization: Bearer $TOKEN" \
  -X POST http://localhost:8080/v1/search/lexical \
  -H "Content-Type: application/json" \
  -d '{"query":"<your-term>","limit":5}' | jq '.results | length'

# Does the collection have vectors at all?
curl -s http://localhost:6333/collections/akopia_text | jq '.result.points_count'
```

**Root causes (by frequency):**

1. **Empty collection.** `points_count == 0` — the embedder never
   successfully upserted anything. Check `dead_letter_count` and the
   embeddings logs.
2. **Model mismatch.** You changed `core.embeddings.text.model` but
   never purged — the collection has vectors from the old model and
   queries embed with the new one. Cosine similarity in mixed spaces
   is random. Fix: follow §6 + `docs/operations.md` §4.2.
3. **Language mismatch.** Corpus is Chinese, model is
   `nomic-embed-text-v1.5` (English-first). Switch to a multilingual
   model like `bge-m3`. See `docs/rag-integration.md` §7.
4. **Idempotency cache poisoned.** If you indexed with a buggy extractor,
   fixed the bug, but the idempotency key still matches — the router
   skips the re-emitted event. Purge via `DELETE .../index` + adapter
   restart (§6 pattern).

**Fix.** Map symptom to the likely cause above. If lexical works but
semantic doesn't, it's almost always (1) or (2).

---

## 9. Lexical returns results but semantic doesn't (or vice versa)

**Symptom.** `/v1/search/lexical` returns hits; `/v1/search/semantic`
returns none (or inverse).

**Diagnose.**
```bash
# Collection sizes should be comparable.
curl -s http://localhost:6333/collections/akopia_text | jq '.result.points_count'
curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  http://localhost:7700/indexes/akopia_lexical/stats | jq '.numberOfDocuments'
```

**Root cause.** Partial pipeline failure. The upserter writes to Qdrant
*and* Meili sequentially (`concentrador/index_manager.py::upsert`). If
one backend was down when a batch was written, you get asymmetry.

**Fix.**

- *Meili populated, Qdrant empty* — embedder failed between
  "generate embedding" and "upsert"; Qdrant was probably down. Check
  DLQ (§2), replay.
- *Qdrant populated, Meili empty* — Meili was down or out of disk.
  Much rarer. Check `docker compose logs meilisearch`; if healthy,
  re-upsert by running the same replay as §2.
- *Both populated but symmetry off* — some specific batches failed.
  DLQ will tell you which. Replay.

---

## 10. A specific term returns 0 hits via the concentrador but exists in Meili

**Symptom.** Your query for `authentik` (or any specific term) via
`/v1/search/lexical` returns no results, but you're sure the term
exists in an indexed doc.

**Diagnose — query Meili directly, bypassing the concentrador:**

```bash
curl -s -X POST http://localhost:7700/indexes/akopia_lexical/search \
  -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q":"authentik","limit":3}' | jq '.hits | length'
```

**Root cause — three options:**

1. **Direct Meili returns hits, concentrador returns 0.** A filter on
   the concentrador's side is dropping them. Check:
   - Did you pass `repo`, `path_prefix`, or `modality` accidentally?
   - Is `max_age_days` set small in a client you forgot about?
   - Did something slip a `source_id` filter in that doesn't exist?
2. **Direct Meili also returns 0.** The term isn't indexed.
   - Extractor didn't process the file — check
     `docker compose logs plugin-extractor-<type> --tail 100`.
   - Adapter never saw the file — `include_globs` / `file_patterns`
     too narrow?
3. **Direct Meili returns 0 only in JSON searchableAttributes.**
   Meilisearch's default searchable attributes are `["snippet",
   "path", "repo"]`. If the match is in a field not in that list, it
   won't be found. Check `concentrador/index_manager.py` for the
   declared attributes.

**Fix.** Work from 1 → 2 → 3. Direct Meili queries are the
canonical diagnostic — concentrador is a thin layer on top.

---

## 11. `docker compose up` fails with Redis password mismatch

**Symptom.**
```
$ docker compose up
[...]
concentrador  | redis.exceptions.AuthenticationError: AUTH <password> called without any password configured for the default user
# or
concentrador  | ConnectionError: Error 22 connecting to redis:6379. Invalid argument.
```

**Diagnose.**
```bash
# What REDIS_URL do services see?
docker compose config | grep REDIS_URL
# Does the redis container have auth configured?
docker compose exec redis redis-cli CONFIG GET requirepass
```

**Root cause.** Usually `.env` drift — two `.env` files (host + compose
working dir), or someone set `REDIS_PASSWORD` in one but not the
other, or the compose `command:` for Redis has `--requirepass` but the
clients don't know the password.

**Fix.** Reconcile. The default compose config does **not** require a
password for Redis — all clients use `redis://redis:6379` (no auth).
If you added auth, add it consistently:

```yaml
# docker-compose.yml
redis:
  command: redis-server --requirepass ${REDIS_PASSWORD} --save 60 1

x-kb-env: &kb-env
  REDIS_URL: "redis://:${REDIS_PASSWORD}@redis:6379"
```

Then `docker compose down -v redis ; docker compose up -d`. The `-v
redis` wipes the redis data volume — only necessary if the server was
started without auth and you're adding it now; otherwise it refuses
to apply the new password.

---

## 12. `/v1/sources/{id}/reindex` says "Source X not registered"

**Symptom.**
```
$ curl -X POST http://localhost:8080/v1/sources/src-git-abc/reindex \
    -H "Authorization: Bearer $TOKEN"
{"detail": "Source src-git-abc not registered"}
```

…but `docker compose ps` shows `plugin-adapter-git` happily running
and ingesting, and search returns hits for chunks tagged with
`source_id=src-git-abc`.

**Diagnose.**
```bash
# Does the concentrador's registry know about it?
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8080/v1/sources | jq '.sources[].source_id'
# Does the adapter's akopia.yaml reference it?
grep -A 3 'src-git-abc' akopia.yaml
```

**Root cause — this is a known UX gap.** Adapter plugins and the
concentrador's source registry are two separate things:

- **Adapters** read `akopia.yaml`, decide which sources to watch, emit
  events with whatever `source_id` they choose. They don't register
  themselves with the concentrador.
- **Concentrador's registry** (Redis hash `sources:`) is populated by
  `POST /v1/sources`. Endpoints like `/reindex`, `/sync`, and
  `DELETE .../index` consult *this registry* to know the source
  exists.

Result: ingestion works without registration (because adapters don't
need the registry), but operational endpoints fail.

**Fix — register the source the concentrador-API way after starting
the adapter:**

```bash
# For a git source
curl -X POST http://localhost:8080/v1/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"git","name":"acme/docs","git_url":"https://github.com/acme/docs.git"}'
# Response gives you source_id — but it's a freshly-generated ID,
# NOT the one the adapter uses.
```

Wrinkle: the registry auto-generates `src-git-<6-random-hex>` on
create, which won't match the adapter's self-chosen ID. Until this is
unified (tracked as an issue), the workaround is to register the
source **first** with a known name, note the returned `source_id`,
and ensure your adapter's `akopia.yaml` either uses that ID or treats
them as aliases.

For the default in-tree adapters (git, folder), the adapter uses the
`source_id` it generates; to operate on it via `/reindex`, set up a
registry entry whose fields mirror it, or accept that the ops
endpoints won't work for that source and use the manual fallback:

```bash
# Manual purge (bypass the registry check):
curl -X POST http://localhost:6333/collections/akopia_text/points/delete \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"source_id","match":{"value":"src-git-abc"}}]}}'

# Meili:
curl -X POST http://localhost:7700/indexes/akopia_lexical/documents/delete \
  -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"filter":"source_id = \"src-git-abc\""}'

# Then restart the adapter per §6.
```

This gap is on the cleanup list before the public GitHub release; if
it is still present when you read this, please file an issue.

---

## 13. MCP client connects but sees no tools

See `docs/mcp-integration.md` §7 — full MCP-specific troubleshooting
lives there.

## 14. Still stuck?

- Re-read `docs/architecture.md` §10 "Failure modes" — it has the
  long-form versions of most of the above.
- Run `curl -s http://localhost:8080/v1/status | jq` and screenshot
  it when you file an issue.
- Attach `docker compose logs --tail 200` filtered to the suspect
  service.
- Sanitize tokens before posting publicly.

Issue tracker: [github.com/rlabs-cl/akopia/issues](https://github.com/rlabs-cl/akopia/issues).
