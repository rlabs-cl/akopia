# Sizing & Resource Guidance

How much CPU, RAM, and disk does Akopia need? The honest answer: it
depends on corpus size, embedding provider, and how much ingestion
concurrency you turn on. This page gives you concrete numbers from our
own measurements plus linear projections you can sanity-check against
your hardware.

> **Measure-twice-cut-once.** Every table here is guidance. Instrument
> your own deployment — `docker stats` is enough to spot a component
> pinning a resource. Overprovision initially, tune once you see the
> shape of your traffic.

## Baseline we measured

The numbers in this page are grounded in a reference run we did
against our own corpus:

- 13 git repositories (small-to-medium, mixed markdown + code + YAML).
- **8 607 chunks** after chunking, fully ingested.
- Qdrant + Meilisearch + Redis on the same host as the plugins.
- Embeddings via external Ollama on a GPU host (`nomic-embed-text`, 768-d).
- `docker compose up` from a cold start to "all searches green": **3 min 52 s**
  after enabling clone concurrency + embedder batching.

Treat this as a data point, not a promise. Your mileage will differ
with source types, file sizes, extractor mix, and provider choice.

## Per-component resource profile

Each row lists **idle** usage (service up, no ingestion), **at-scale**
usage projected for 100 000 chunks, CPU requirement, persistent disk,
and notes on what drives the numbers.

| Component | RAM idle | RAM @ 100k chunks | CPU | Disk | Notes |
|---|---|---|---|---|---|
| **Qdrant** | 150 MB | 1–2 GB | 1 core | ~750 MB | HNSW graph stays in RAM; `on_disk_payload: true` (default compose) pushes payload to disk and keeps RAM tight. |
| **Meilisearch** | 100 MB | ~500 MB | 1 core | ~500 MB | Memory-mapped. RAM usage tracks the hot working set — full-corpus scans briefly touch more. |
| **Redis** | 50 MB | 50–100 MB | <0.1 core | transient | Event bus + idempotency keys only. Streams drain continuously; steady-state size stays small. |
| **Concentrador** | 180 MB | 200–250 MB | 0.5 core | — | FastAPI async API + router. Flat memory profile; CPU scales with request + event throughput. |
| **Embeddings (fastembed, CPU)** | 500 MB | ~800 MB | 2–4 cores | — | Model loaded in-process; embeddings are CPU-bound. Default for the zero-deps quickstart. |
| **Embeddings (Ollama client)** | 120 MB | 150 MB | 0.3 core | — | Just an HTTP client to your GPU host; negligible on the Akopia pod. See the GPU note below. |
| **MCP server** | 100 MB | 120 MB | <0.1 core | — | JSON-RPC proxy over the concentrador; thin. |
| **Plugin adapter-git** | 200 MB | 250 MB | 0.3 core | 1–10 GB | Disk is dominated by repo clones (`git-repos` volume). Memory grows transiently during clone bursts. |
| **Plugin adapter-folder** | 120 MB | 150 MB | 0.1 core | — | Near-zero overhead; watches a mounted directory. |
| **Plugin adapter-web-\*** | 180 MB | 220 MB | 0.3 core | — | Network IO + HTML buffer; bounded by `rate_limit_per_sec` and `max_depth`. |
| **Plugin extractor-plain** | 120 MB | 180 MB | 0.5–1 core | — | CPU-bound per file, one at a time today — next bottleneck to parallelise. |
| **Plugin extractor-office** | 400 MB | 500 MB | 1–2 cores | — | docx/xlsx/pptx libraries are heavy; load-on-first-use amortises. |
| **Plugin extractor-pdf-text** | 350 MB | 450 MB | 1–2 cores | — | `pypdfium2` keeps fixed memory per worker; native-text PDFs only. |
| **Plugin extractor-html** | 200 MB | 280 MB | 0.5 core | — | `trafilatura` is mostly CPU; well-behaved memory. |

Totals for a typical "compose default" stack (concentrador + embeddings
+ MCP + adapter-git + adapter-folder + extractor-plain + Qdrant + Meili
+ Redis):

- **Idle:** ≈ 1.6 GB RAM, ~3 CPU cores available-but-barely-used, ~2 GB disk.
- **Steady at 100 000 chunks:** ≈ 4–5 GB RAM, 4–6 cores under ingestion
  bursts, 10–15 GB disk (including cloned repos).

## Deployment tiers

Pick the row that matches your workload:

| Tier | CPU | RAM | Disk | Fits |
|---|---|---|---|---|
| **Laptop / POC** | 4 cores | 8 GB | 20 GB SSD | <100 k chunks, single developer, fastembed CPU. |
| **Homelab single-host** | 8 cores | 16 GB | 100 GB SSD | <1 M chunks, small team, fastembed OR Ollama on a separate GPU box. |
| **Production single-host** | 16 cores | 32 GB | 500 GB NVMe | <5 M chunks, steady production traffic, Ollama/TEI external. |
| **Beyond ≈ 5 M chunks** | multi-host | — | — | Split Qdrant + embeddings + plugins across machines (k8s or compose-over-ssh). Ship the `k8s/` manifests as a starting point; Helm is roadmap. |

Upper bounds are soft — they're the point where *one* machine starts
to feel constrained, not where Akopia breaks. Qdrant in particular
scales well past these rough numbers; the constraint is usually
RAM for the HNSW graph.

## Embedding provider trade-off

The single biggest sizing dial is **where you embed**.

| Provider | Akopia pod cost | Throughput | When to pick |
|---|---|---|---|
| `fastembed` (in-process, CPU) | +500 MB RAM, 2–4 CPU cores | ~30–80 chunks/s per core | Quickstart, laptops, small corpora, fully offline. |
| `ollama` (external HTTP, GPU) | +20 MB RAM, negligible CPU on the Akopia pod | 200–1000 chunks/s depending on GPU | Homelab+ with a dedicated GPU host, or cloud GPU box. `nomic-embed-text` fits in ~600 MB VRAM. |
| `tei` / `openai` (roadmap) | negligible | high | Post-1.0. |

With Ollama, the GPU VRAM requirement is driven by the embedder model,
not by corpus size. `nomic-embed-text` (768-d) runs comfortably on
any GPU with **2–4 GB of free VRAM**. Keep `OLLAMA_KEEP_ALIVE=30m` or
longer so the model stays resident between ingestion bursts — a cold
load costs seconds per first-query.

## Network IO profile

- **Ingestion initial:** bursty. Git clones + external-web fetches
  dominate. With `concurrency: 3` on the git adapter, plan for 3
  parallel clone streams + associated upstream API calls
  (Gitea/GitHub/GitLab).
- **Ingestion steady state:** near-zero. Adapters poll on their cron
  and only push deltas (hash-based dedup).
- **Search:** small JSON round-trips (< 5 KB request, 1–50 KB response
  depending on chunk sizes and `top_k`). Trivial to any modern network.
- **MCP:** JSON-RPC over HTTP, same order of magnitude as search.

## Disk growth model

Rough linear model for the text-only corpus:

- **Qdrant**: ~7.5 KB per chunk (768-d vector + payload + HNSW
  overhead, `on_disk_payload` on). 1 M chunks ≈ 7.5 GB.
- **Meilisearch**: ~5 KB per document (inverted index + filterable
  attributes). 1 M docs ≈ 5 GB.
- **Redis**: bounded by stream retention (default: drain on ACK).
  Set `stream-node-max-entries` if you need XRANGE history.
- **git-repos volume** (adapter cache): size of cloned repos on disk;
  each repo clones once, then `git fetch` for deltas.

Plan for **~15 KB per chunk total across Qdrant + Meili + payload
duplication**, plus whatever your raw-source storage needs.

## Scaling the bottleneck: what fills up first

In our reference run with parallelisation enabled, the order in which
components hit their ceiling as the corpus grows:

1. **Extractor-plain** (serial per-file today) — first to become the
   steady-state bottleneck above ~10 k files per ingestion burst.
   Parallelising it is a roadmap item.
2. **Qdrant HNSW memory** — growth is linear with the vector count;
   it takes millions of chunks to matter, but when it does it matters
   abruptly. Watch `process_resident_memory_bytes`.
3. **Disk IOPS on the Qdrant volume** — less of an issue on NVMe; on
   spinning disks you'll feel it during rebuilds or large upserts.
4. **Ollama GPU VRAM** — only if you run multiple large models on one
   card. One embedder model is fine.

Nothing above is a hard wall; each can be scaled by throwing hardware
or horizontal instances at it.

## What you can ignore

- **RAM for Python plugin overhead.** Each plugin pod adds ~150 MB of
  base Python overhead; don't sweat it — that's why the table totals
  above already include plugin rows.
- **CPU for Redis.** Redis is almost free on modern hardware for the
  sizes we push through it.
- **"Right-sizing" the MCP server.** It's a proxy; it doesn't need
  tuning.

## When to step up

Concrete triggers that justify moving to the next tier:

- `queue_depth` in `/v1/status` consistently growing during
  ingestion windows → embedder/extractor CPU-starved.
- `dead_letter_count > 0` from "request timeout" errors → backend
  slowness, usually Qdrant under memory pressure.
- `docker stats` shows a single container at 100 % CPU for minutes
  continuously during ingest → parallelise that component or give it
  more cores.
- Ingest of a "typical" small corpus takes >10× what our reference
  run did → the baseline assumption is off; instrument first.

If nothing is pegged, don't pre-optimise. Akopia is boring on purpose.
