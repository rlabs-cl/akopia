# Akopia — Operations Manual

**Audience:** someone self-hosting akopia in production or
near-production (a homelab, a small team server, a single-tenant
k8s cluster).

The focus is docker-compose — that is the canonical deployment shape.
Kubernetes manifests live in `k8s/` and are a superset of the
compose topology; the operational concerns below carry over with
minor rewording (`docker compose restart X` ↔ `kubectl rollout
restart deploy/X`).

## 1. End-to-end data flow

```
                                    ┌──────────────────────┐
                         ┌─────────▶│  redis:6379          │
                         │          │  (Streams + k/v)     │
                         │          └──────────────────────┘
                         │                    ▲
                         │                    │
                         │   publish          │  consume
                         │   change-events    │
                         │                    │
┌──────────────────────┐ │          ┌──────────────────────┐
│ plugin-adapter-X     │─┘          │ concentrador         │
│ (git, folder, web)   │            │ :8080 (REST + router)│
│ discover + watch     │            │ – change→extract     │
│ emits ChangeEvent    │            │ – extract→embed      │
└──────────────────────┘            │ – embed→upsert       │
                                    │ – search endpoints   │
                                    └─────────┬────────────┘
                                              │
                          ┌───────────────────┼───────────────────┐
                          │                   │                   │
              extract-jobs│       embedding-jobs│       upsert      │
                          ▼                   ▼                   ▼
            ┌──────────────────────┐  ┌──────────────────┐  ┌──────────────────┐
            │ plugin-extractor-Y   │  │ embeddings :8081 │  │ qdrant :6333     │
            │ (plain, pdf, office) │  │ (text/image emb) │  │ meilisearch:7700 │
            │ ExtractedContent →   │  │  →  EmbeddingResult│  │                  │
            │ extract-results      │  └──────────────────┘  └──────────────────┘
            └──────────────────────┘            │                   ▲
                          │                     │                   │
                          └────────▶ extract-results ───────────────┘
                                   (router chunks + embeds + upserts)

         ┌────────────────┐           ┌──────────────────────┐
         │ mcp-server     │──proxy──▶ │ concentrador :8080   │
         │ :8082 (SSE)    │           │ (search endpoints)   │
         └────────────────┘           └──────────────────────┘

                              dead-letter stream ← failures from any producer
                              drained by dlq_drainer (retry 1/5/15 min)
```

Service names exactly as they appear in `docker-compose.yml`:
`redis`, `qdrant`, `meilisearch`, `concentrador`, `embeddings`,
`mcp-server`, `plugin-adapter-folder`, `plugin-adapter-git`,
`plugin-extractor-plain`.

## 2. Resource sizing

Exact numbers depend on corpus size, modality mix, and whether CLIP
image embedding is enabled. The table below is a starting point; **measure
in your environment and adjust**.

| Corpus (text docs) | Qdrant RAM | Qdrant disk | Meili RAM | Meili disk | Redis RAM | Embedder RAM |
|--------------------|-----------:|------------:|----------:|-----------:|----------:|-------------:|
| 1k docs            | ~300 MiB   | <500 MiB    | ~200 MiB  | <300 MiB   | 64 MiB    | 2-4 GiB      |
| 10k docs           | 600-900 MiB | 1-2 GiB    | 300-500 MiB | 500 MiB-1 GiB | 128 MiB | 2-4 GiB |
| 100k docs          | 2-4 GiB    | 5-10 GiB    | 800 MiB-1.5 GiB | 3-5 GiB | 256 MiB | 4-6 GiB |
| 1M+ docs           | *measure*  | *measure*   | *measure* | *measure*  | 512 MiB+  | 4-8 GiB     |

Notes:

- **Qdrant** scales roughly linearly with `chunks × vector_dim × 4
  bytes` plus index overhead (~1.3×). For a `nomic-embed-text` corpus
  of 100k chunks that's ~300 MiB of vector, ~500 MiB with index.
- **Meilisearch** stores snippets (up to ~10 KiB per doc) + an
  inverted index. Roughly 2-3× the raw snippet size on disk.
- **Redis** holds idempotency keys (SHA-256 per unique event) + stream
  messages. 100k events ≈ 50 MiB. Streams are trimmed to ~5 GiB by
  default.
- **Embedder memory is mostly model weights.** Resident footprint is
  independent of corpus size: ~2 GiB for `nomic-embed-text-v1.5`
  quantized, another ~1-2 GiB if CLIP is also loaded. The
  `mem_limit: 8g` in compose is based on a 2026-04-22 OOM incident
  where 4 GiB was not enough with both loaded.

Single-node `docker compose up` comfortably handles ~100k-1M text
chunks on a 16 GiB RAM / 4-core / SSD host. Beyond that, split roles
across nodes (k8s territory).

## 3. Backup and restore

Content is durable in **Qdrant** and **Meilisearch**. Redis holds
in-flight queues and idempotency keys — ephemeral state. If Redis
dies, re-ingestion reconciles (at the cost of some duplicate work);
if Qdrant or Meili die without backups, you re-ingest from the
original sources.

### 3.1 Qdrant — snapshot API

Per-collection snapshot (preferred; atomic, online):

```bash
# Create a snapshot
curl -X POST http://localhost:6333/collections/akopia_text/snapshots
# → {"result":{"name":"akopia_text-12345.snapshot","creation_time":"...","size":...},"status":"ok"}

# List
curl http://localhost:6333/collections/akopia_text/snapshots

# Download the blob
curl -o akopia_text-$(date +%F).snapshot \
  http://localhost:6333/collections/akopia_text/snapshots/akopia_text-12345.snapshot

# Restore (on a clean Qdrant)
curl -X PUT \
  'http://localhost:6333/collections/akopia_text/snapshots/recover' \
  -H "Content-Type: application/json" \
  -d "{\"location\":\"file:///qdrant/snapshots/akopia_text-12345.snapshot\"}"
```

Whole-server snapshot (every collection at once):

```bash
curl -X POST http://localhost:6333/snapshots
# downloads via /snapshots/<name> — same pattern
```

Alternative, lazy: `docker compose stop qdrant && tar czf qdrant.tgz
volumes/qdrant-data && docker compose start qdrant`. Works, but the
cutover window means missing events pile up in `change-events` —
Redis Stream durability saves you, but your `queue_depth` will spike
while you catch up.

### 3.2 Meilisearch — dump API

```bash
# Trigger a dump
curl -X POST http://localhost:7700/dumps \
  -H "Authorization: Bearer $MEILI_MASTER_KEY"
# → {"taskUid": 42, "indexUid": null, "status": "enqueued", ...}

# Poll for completion
curl http://localhost:7700/tasks/42 \
  -H "Authorization: Bearer $MEILI_MASTER_KEY"

# Dumps land in the meili-data volume under dumps/YYYY-MM-DD-HHMMSS.dump
docker compose exec meilisearch ls /meili_data/dumps/

# Copy out
docker cp $(docker compose ps -q meilisearch):/meili_data/dumps/<name>.dump ./
```

Restore: start a fresh Meili with `--import-dump /path/to/dump.dump`.
In compose, edit the service to add `command: ["--import-dump",
"/meili_data/dumps/<name>.dump"]` for one boot, then remove it.

### 3.3 Redis — RDB snapshot

The compose config already has `--save 60 1` (snapshot every 60s if ≥1
key changed) so an RDB file is always being maintained at
`/data/dump.rdb` inside the container.

```bash
docker compose exec redis redis-cli BGSAVE
docker cp $(docker compose ps -q redis):/data/dump.rdb ./redis-$(date +%F).rdb
```

**You usually don't need Redis backups.** Streams are in-flight
work; the `change-events` stream may contain 5 GiB of history but
the concentrador's durable state is in Qdrant/Meili. If you lose
Redis, adapters re-scan on next start and the idempotency keys
rebuild as events flow. The worst-case cost is duplicate embedding
work (you pay CPU/API again), not data loss.

### 3.4 Suggested cron

```bash
# /etc/cron.d/akopia-backup — daily at 03:00
0 3 * * * root /opt/akopia-backup.sh >> /var/log/akopia-backup.log 2>&1
```

`/opt/akopia-backup.sh`: trigger Qdrant snapshots + Meili dump, copy
latest of each into `/backups/YYYY-MM-DD/`, rotate older than 30
days. Test the restore path quarterly — backups you never restore
are theatre.

## 4. Upgrade path

### 4.1 Routine version bumps

```bash
cd /opt/akopia
git pull
docker compose pull          # for upstream images (qdrant, meili, redis)
docker compose up --build -d # rebuild plugin containers from the repo
```

Expect ~30-60 s of `queue_depth` accumulation during the rolling
restart; the router resumes draining once `concentrador` is up.

### 4.2 Changing the embedding model (BREAKING)

This is the most common "I need to do ops" scenario and it has a
specific sequence. **Out-of-order execution leaves stale vectors in
Qdrant that can never be replaced** (the new model produces different
`chunk_id`s).

```bash
# 1. Update akopia.yaml — change core.embeddings.text.model or provider
vim akopia.yaml

# 2. Restart the embedder FIRST so it is running on the new model
#    when the next step's queue drains.
docker compose restart embeddings

# 3. Purge every source's index (loop or do the one you care about).
curl -X DELETE \
  http://localhost:8080/v1/sources/src-git-abc/index \
  -H "Authorization: Bearer $TOKEN"

# 4. Recreate the Qdrant collection with the new vector dim IF the new
#    model has a different dim. The concentrador auto-creates with the
#    dim it reads from the embedder on startup, so:
#      - If dim unchanged (e.g. nomic → nomic-quant): skip.
#      - If dim changed (e.g. 768 → 1024):
docker compose exec qdrant curl -X DELETE \
  http://localhost:6333/collections/akopia_text
docker compose restart concentrador     # recreates collection

# 5. Restart adapters so their in-memory "already seen" sets reset
#    and they re-emit ADD events on the next scan.
docker compose restart plugin-adapter-git plugin-adapter-folder

# 6. Watch queue_depth drain.
watch -n 2 'curl -s http://localhost:8080/v1/status | jq ".queue_depth,.dead_letter_count"'
```

Step 5 is the piece people forget. `DELETE .../index` empties the
indices but adapters still think "I already indexed this file,
nothing to do." See `docs/troubleshooting.md` §6.

### 4.3 Changing an extractor (e.g. `plain` → `html` for a docs tree)

Extractors are auto-dispatched by MIME+extension. To re-process files
with a different extractor version (e.g. you bumped `extractors/plain.py::version`
from `"0.1.0"` to `"0.2.0"` for better chunk boundaries):

1. `DELETE /v1/sources/<id>/index` for affected sources.
2. `docker compose restart plugin-extractor-<name> plugin-adapter-<name>`.
3. Watch `queue_depth`.

## 5. Reverse proxy + TLS

Production exposure: put the concentrador (`:8080`) and MCP server
(`:8082`) behind a proxy that terminates TLS and ideally gates auth.
Keep Qdrant (`:6333`), Meili (`:7700`), and Redis (`:6379`) **unpublished**
— they have no business being reachable from outside the compose
network. Remove their `ports:` blocks before going to production.

### 5.1 Traefik

```yaml
# docker-compose.override.yml
services:
  traefik:
    image: traefik:v3.1
    restart: unless-stopped
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.le.acme.email=you@example.com
      - --certificatesresolvers.le.acme.storage=/le/acme.json
      - --certificatesresolvers.le.acme.tlschallenge=true
    ports: ["80:80", "443:443"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - traefik-le:/le

  concentrador:
    labels:
      - traefik.enable=true
      - traefik.http.routers.kb-api.rule=Host(`kb.example.com`)
      - traefik.http.routers.kb-api.entrypoints=websecure
      - traefik.http.routers.kb-api.tls.certresolver=le
      - traefik.http.services.kb-api.loadbalancer.server.port=8080

  mcp-server:
    labels:
      - traefik.enable=true
      - traefik.http.routers.kb-mcp.rule=Host(`kb-mcp.example.com`)
      - traefik.http.routers.kb-mcp.entrypoints=websecure
      - traefik.http.routers.kb-mcp.tls.certresolver=le
      - traefik.http.services.kb-mcp.loadbalancer.server.port=8082

volumes:
  traefik-le:
```

The bearer-token check is done by the concentrador itself
(`verify_token` in `concentrador/main.py`), so Traefik only has to
route. For the MCP server (which does **not** enforce auth inline —
see `docs/mcp-integration.md` §4) you want an extra layer — use the
`basicauth` or `forwardauth` middleware, or front with Cloudflare
Access / Authelia.

### 5.2 Caddy

```caddy
# /etc/caddy/Caddyfile
kb.example.com {
    reverse_proxy localhost:8080
}

kb-mcp.example.com {
    # MCP server has no inline auth — enforce at the proxy.
    @unauthorized not header Authorization "Bearer <YOUR-SHARED-TOKEN>"
    respond @unauthorized 401

    reverse_proxy localhost:8082 {
        # SSE needs request buffering disabled.
        flush_interval -1
    }
}
```

Caddy does TLS automatically (Let's Encrypt). The `flush_interval -1`
on the MCP route is mandatory — without it Caddy buffers SSE and the
MCP client hangs waiting for the initial handshake.

## 6. Token rotation

### Zero-downtime rotation — bearer token

The bearer token is read once at process start (`Config.BEARER_TOKEN`).
There is no hot-reload path today. The "zero-downtime" rotation is:

1. Add the new token alongside the old one in your auth proxy (if you
   have a proxy terminating auth before the concentrador — most don't).
2. Update all clients to use the new token.
3. Update `.env` on the host.
4. `docker compose restart concentrador mcp-server`. ~10-30 s
   unavailability on the API; long-lived MCP SSE sessions drop and
   auto-reconnect.
5. Remove the old token from the proxy.

In practice most operators accept a 30-second window of 401s during
step 4. Plan it for a low-traffic hour.

### Meili master key

`MEILI_MASTER_KEY`: same as above — change `.env`, restart `meilisearch`
and `concentrador`. Meilisearch data is unaffected by the key change.

### Upstream tokens (`GITHUB_TOKEN`, `GITEA_TOKEN`, etc.)

Change `.env`, `docker compose restart plugin-adapter-git`. The adapter
is the only service that needs the token; the concentrador and
embedder do not.

## 7. Monitoring and observability

### 7.1 What to watch

| Signal                        | Where                              | Alert when                                           |
|-------------------------------|------------------------------------|------------------------------------------------------|
| `queue_depth`                 | `GET /v1/status`                   | Growing monotonically for >10 min (embedder stuck).  |
| `dead_letter_count`           | `GET /v1/status`                   | > 0 for > 1 hour (retries exhausted; needs hands).   |
| Container health              | `docker compose ps` / k8s probes   | Anything not `healthy`.                              |
| Qdrant collection count       | `GET :6333/collections/akopia_text`    | Shrinks without a deliberate purge (data loss).      |
| Meili doc count               | `GET :7700/indexes/akopia_lexical/stats` | Same.                                              |
| Embedder latency              | `docker compose logs embeddings`   | p95 > 5s (model load stuck or GPU oversubscribed).   |
| Adapter "watch loop crashed"  | `docker compose logs plugin-adapter-*` | Any occurrence; bases retry with backoff but recurring means upstream issue. |

### 7.2 Thresholds in practice

- **`queue_depth < 100`** is normal at idle.
- **`queue_depth` spike of 1-10k** is normal after a large repo is
  added — let it drain.
- **`queue_depth` persistently >10k** triggers backpressure in
  `change_event_consumer` (it sleeps 5 s and retries). If you see
  this alongside a pinned embedder CPU, scale out the embedder
  container (raise replica count in compose / k8s).
- **`dead_letter_count > 0`** means the DLQ drainer gave up after 3
  attempts. Inspect the DLQ content:
  ```bash
  docker compose exec redis redis-cli XRANGE dead-letter - + COUNT 20
  ```
  See `docs/troubleshooting.md` §2 for typical payloads and replay
  recipes.

### 7.3 Logs

All services log to stdout in plain text (not JSON — the refactor
mentioned in `docs/architecture.md` §3 is in progress but unshipped).
Useful one-liners:

```bash
# Everything, follow
docker compose logs -f --tail 50

# Only errors
docker compose logs --tail 500 | grep -iE 'error|warning|crash|oom'

# One service
docker compose logs concentrador --tail 200 -f
```

### 7.4 Prometheus

**Not shipped.** The architecture doc mentions Prometheus as a
forward-looking goal. There is no `/metrics` endpoint yet; don't
expect scrapes to work. When observability lands it will be
Prometheus + OpenTelemetry spans; for now, `GET /v1/status` is the
only programmatic health signal.

## 8. Scaling limits

Single-node `docker compose` comfortably handles:

- ~1M text chunks in Qdrant+Meili (at ~1.5 GiB Qdrant RAM, ~1 GiB
  Meili disk).
- ~10 source adapters running concurrently.
- Sustained ingestion of ~10-50 docs/sec (embedder-bound; CPU
  fastembed is the bottleneck).
- Search latency p95 < 100 ms at the above scale.

When to move to k8s (or at least split the compose across hosts):

- Corpus > 1M chunks with CLIP enabled (image embedder RAM starts
  fighting text embedder RAM).
- You want horizontal embedder replicas (compose can run N copies of
  `embeddings` but they will all hit the same Redis group — works
  fine, but at k8s scale you want proper HPA).
- You need zero-downtime upgrades (compose can't do rolling deploys).
- Multi-tenant or multi-team isolation.

## 9. Security hardening checklist

For anything exposed beyond a single-user laptop:

- [ ] **Remove `ports:` from `qdrant`, `meilisearch`, `redis`** in
      docker-compose. They only need to be reachable inside the
      compose network.
- [ ] **Rotate `AKOPIA_BEARER_TOKEN` away from the `.env.example` default.**
      The default is literally `changeme-dev-only`; tools that scan
      GitHub for leaked tokens find it in 20 minutes.
- [ ] **Rotate `MEILI_MASTER_KEY` away from its default** for the same
      reason.
- [ ] **Front the exposed ports (`8080`, `8082`) with TLS** via
      Traefik, Caddy, Nginx, or Cloudflare Tunnel. The concentrador
      does not speak HTTPS natively.
- [ ] **Gate `:8082` with a reverse-proxy auth check** (see §5) — the
      MCP server does not enforce auth inline.
- [ ] **Scope adapter tokens.** A `GITHUB_TOKEN` with `repo:read` on
      one org is enough; don't hand over a full-scope classic PAT.
- [ ] **Keep `git-repos` volume private.** It contains clones of every
      watched repo, including private ones. It is mounted read-only
      into extractors but is read-write for the git adapter.
- [ ] **Set `MEILI_ENV=production`** (currently `development` in the
      default compose, which disables some safety checks).
- [ ] **Configure persistent volumes with restore-tested backups**
      (§3). "Restore-tested" is the operative word.
- [ ] **Restrict who can `docker compose` on the host.** Docker group
      membership is effectively root.
- [ ] **Keep the image base layers patched.** `docker compose pull`
      monthly.

## 10. See also

- `docs/architecture.md` — component map and status semaphores.
- `docs/configuration.md` — every env var and `akopia.yaml` field.
- `docs/api-reference.md` — endpoint-level documentation.
- `docs/troubleshooting.md` — specific failure modes with fixes.
- `docker-compose.yml` — authoritative service topology.
- `k8s/` — manifests for the kubernetes deployment.
