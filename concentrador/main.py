"""Akopia Concentrador - API REST + Upsert Manager + Stream Orchestrator."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config
from common.models import (
    ChangeEvent, EmbeddingJob, EmbeddingResult, EmbeddingModality,
    ExtractedContent,
    Source, SourceStatus, Priority, Chunk,
)
from common.redis_client import RedisClient
from common.chunker import chunk_text, get_chunker_config
from concentrador.index_manager import IndexManager, UnsupportedModalityError
from concentrador.router import Router, extractor_routing_enabled

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("concentrador")

redis_client = RedisClient()
index_manager: Optional[IndexManager] = None

# Module-level Router (stateless, safe to share across consumers).
# Extractor discovery happens lazily via default_registry the first
# time pick_extractor() is invoked.
_router = Router()


def _router_use_extractors() -> bool:
    """Runtime check wrapper so tests can monkeypatch."""
    return extractor_routing_enabled()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global index_manager
    await redis_client.connect()

    # Ensure streams and consumer groups
    await redis_client.ensure_stream_and_group(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR)
    await redis_client.ensure_stream_and_group(Config.STREAM_EMBEDDING_JOBS, Config.CG_EMBEDDER)
    await redis_client.ensure_stream_and_group(Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER)
    await redis_client.ensure_stream_and_group(Config.STREAM_DEAD_LETTER, "cg-ops")
    # New streams for the extractor layer (Slice 5 refactor). Always
    # created so that flipping AKOPIA_ROUTER_USE_EXTRACTORS at runtime
    # doesn't require a topology change.
    await redis_client.ensure_stream_and_group("extract-jobs", "cg-router-extract-dispatch")
    await redis_client.ensure_stream_and_group("extract-results", "cg-router-extract")

    index_manager = IndexManager(
        qdrant_url=Config.QDRANT_URL,
        qdrant_api_key=Config.QDRANT_API_KEY,
        meili_url=Config.MEILISEARCH_URL,
        meili_key=Config.MEILISEARCH_KEY,
    )
    await index_manager.initialize()

    # Start background tasks
    task_change = asyncio.create_task(change_event_consumer())
    task_result = asyncio.create_task(embedding_result_consumer())
    # Extract-results consumer runs unconditionally: even if the feature
    # flag is off today, extract-results that land in the stream should
    # still be drained rather than accumulated.
    task_extract = asyncio.create_task(extract_result_consumer())
    # DLQ drainer — retries with exponential backoff up to 3 attempts.
    task_dlq = asyncio.create_task(dlq_drainer())

    logger.info(
        "Concentrador started (extractor_routing=%s)",
        "on" if _router_use_extractors() else "off",
    )
    yield

    task_change.cancel()
    task_result.cancel()
    task_extract.cancel()
    task_dlq.cancel()
    await redis_client.close()


app = FastAPI(
    title="Akopia Concentrador API",
    version="1.0.0",
    lifespan=lifespan,
)


# Strict-auth: fail closed at import time when production asked for it
# but no token was supplied. Same contract as the MCP server so both
# enforcement layers go dark together, not one before the other.
if Config.STRICT_AUTH and not Config.BEARER_TOKEN:
    raise RuntimeError(
        "AKOPIA_STRICT_AUTH=1 but AKOPIA_BEARER_TOKEN is unset. "
        "Either configure AKOPIA_BEARER_TOKEN, or unset AKOPIA_STRICT_AUTH "
        "for permissive-dev mode. Refusing to start (fail closed)."
    )


# --- Auth ---

async def verify_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, detail="Missing Bearer token")
    token = auth.split(" ", 1)[1]
    # Constant-time compare prevents byte-level timing side-channels
    # against ``Config.BEARER_TOKEN``.
    expected = Config.BEARER_TOKEN or ""
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, detail="Invalid token")


# --- Request/Response models ---

class CreateGitSourceReq(BaseModel):
    name: str
    git_url: str
    branch: str = "main"
    sync_cron: str = "*/5 * * * *"
    file_patterns: list[str] = Field(default_factory=lambda: ["**/*"])


class CreateFolderSourceReq(BaseModel):
    name: str
    folder_path: str
    sync_cron: str = "*/10 * * * *"
    file_patterns: list[str] = Field(default_factory=lambda: ["**/*"])


class SemanticSearchReq(BaseModel):
    query: str
    modality: Optional[str] = None
    repo: Optional[str] = None
    path_prefix: Optional[str] = None
    top_k: int = 10
    score_threshold: float = 0.0
    # Freshness controls (2026-04-22). Both default off for bwd compat.
    max_age_days: Optional[int] = None   # hard filter; drop older docs
    freshness_boost: float = 0.0         # soft re-rank weight 0.0–1.0


class LexicalSearchReq(BaseModel):
    query: str
    repo: Optional[str] = None
    path_prefix: Optional[str] = None
    modality: Optional[str] = None
    limit: int = 20
    offset: int = 0
    max_age_days: Optional[int] = None
    freshness_boost: float = 0.0


class RAGAskReq(BaseModel):
    """RAG query request."""
    question: str = Field(..., description="Natural language question")
    repos: Optional[list[str]] = Field(None, description="Filter by repo names (e.g. ['yourorg/yourrepo'])")
    path_prefix: Optional[str] = Field(None, description="Filter by path prefix")
    max_context_chars: int = Field(6000, description="Max total chars of context to return (controls context window usage)")
    top_k: int = Field(15, description="Candidate results to retrieve before re-ranking")
    semantic_weight: float = Field(0.7, description="Weight for semantic vs lexical (0-1)")
    max_age_days: Optional[int] = Field(None, description="Hard filter: skip docs older than N days")
    freshness_boost: float = Field(0.0, description="Soft re-rank: β∈[0,1] for freshness weight")


class RAGAskResponse(BaseModel):
    """Curated context ready for LLM consumption."""
    context: str = Field(..., description="Assembled context with source references")
    sources: list[dict] = Field(default_factory=list, description="Source files used")
    stats: dict = Field(default_factory=dict, description="Retrieval stats")


# --- API Endpoints ---

@app.get("/v1/status")
async def get_status():
    qdrant_ok = await index_manager.health_check_qdrant()
    meili_ok = await index_manager.health_check_meili()
    redis_ok = True
    try:
        await redis_client.r.ping()
    except Exception:
        redis_ok = False

    queue_depth = await redis_client.stream_len(Config.STREAM_EMBEDDING_JOBS)
    dl_count = await redis_client.stream_len(Config.STREAM_DEAD_LETTER)

    all_ok = qdrant_ok and meili_ok and redis_ok
    some_ok = qdrant_ok or meili_ok or redis_ok

    return {
        "status": "healthy" if all_ok else ("degraded" if some_ok else "unhealthy"),
        "components": {
            "qdrant": "up" if qdrant_ok else "down",
            "meilisearch": "up" if meili_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
        "queue_depth": queue_depth,
        "dead_letter_count": dl_count,
    }


@app.get("/v1/stats", dependencies=[Depends(verify_token)])
async def get_stats():
    """Operator-facing index stats: doc counts + freshness.

    Designed for cheap polling from admin dashboards (Atalaya's
    /v1/akopia/instances panel reads this to populate Docs / Last sync /
    Latency / Q (24h) columns). Independent from /health and /v1/status,
    which focus on liveness rather than content.
    """
    return await index_manager.stats()


@app.get("/v1/sources", dependencies=[Depends(verify_token)])
async def list_sources():
    sources = await redis_client.list_sources()
    return {"sources": sources}


@app.post("/v1/sources", status_code=201, dependencies=[Depends(verify_token)])
async def create_source(request: Request):
    body = await request.json()
    source_type = body.get("type", "git")

    if source_type == "git":
        req = CreateGitSourceReq(**body)
        source_id = f"src-git-{uuid.uuid4().hex[:6]}"
        source = Source(
            source_id=source_id,
            type="git",
            name=req.name,
            url=req.git_url,
            branch=req.branch,
            sync_cron=req.sync_cron,
            file_patterns=req.file_patterns,
        )
    elif source_type == "folder":
        req = CreateFolderSourceReq(**body)
        source_id = f"src-nas-{uuid.uuid4().hex[:6]}"
        source = Source(
            source_id=source_id,
            type="folder",
            name=req.name,
            folder_path=req.folder_path,
            sync_cron=req.sync_cron,
            file_patterns=req.file_patterns,
        )
    else:
        raise HTTPException(400, detail=f"Unknown source type: {source_type}")

    # Check for duplicate name
    existing = await redis_client.list_sources()
    if any(s["name"] == source.name for s in existing):
        raise HTTPException(409, detail=f"Source with name '{source.name}' already exists")

    await redis_client.register_source(source.model_dump())
    return source.model_dump()


@app.delete("/v1/sources/{source_id}/index", dependencies=[Depends(verify_token)])
async def purge_source_index(source_id: str):
    """Purge every indexed chunk/doc belonging to ``source_id`` from both
    Qdrant and Meilisearch. Useful when a source was contaminated, the
    model/tokenizer changed, or a repo was reshaped.

    Note: this only removes what's *indexed*. The adapter that owns
    ``source_id`` keeps an in-memory "already seen" set that this
    endpoint cannot touch (process isolation). To actually re-index,
    restart the adapter process after calling this — on its next scan
    it will treat every file as new and emit ADD events.
    """
    source = await redis_client.get_source(source_id)
    if not source:
        raise HTTPException(404, detail=f"Source {source_id} not registered")
    counts = await index_manager.delete_by_source(source_id)
    return {
        "source_id": source_id,
        "purged": counts,
        "next_step": (
            f"restart the adapter process for {source_id} (e.g. "
            f"`docker compose restart plugin-adapter-<name>`) so its in-memory "
            "state resets and it re-emits ADD for every file on the next scan"
        ),
    }


@app.post("/v1/sources/{source_id}/reindex", dependencies=[Depends(verify_token)])
async def reindex_source(source_id: str):
    """Convenience wrapper: purge + trigger_sync. Same caveat as
    ``DELETE /v1/sources/{source_id}/index`` — the adapter's in-memory
    state is NOT reset; a restart is still required for a full rebuild.
    The trigger_sync event is best-effort and only covers adapters that
    react to manual sync triggers.
    """
    source = await redis_client.get_source(source_id)
    if not source:
        raise HTTPException(404, detail=f"Source {source_id} not registered")
    counts = await index_manager.delete_by_source(source_id)

    locked = await redis_client.acquire_lock(source_id, ttl=1800)
    sync_id = f"reindex-{uuid.uuid4().hex[:8]}" if locked else None
    if locked:
        await redis_client.update_source_status(source_id, SourceStatus.DETECTING.value)
        await redis_client.publish(
            Config.STREAM_CHANGE_EVENTS,
            {
                "type": "trigger_sync",
                "source_id": source_id,
                "sync_id": sync_id,
                "priority": "manual",
                "reason": "reindex",
            },
        )

    return JSONResponse(
        status_code=202,
        content={
            "source_id": source_id,
            "purged": counts,
            "sync_id": sync_id,
            "sync_triggered": bool(locked),
            "next_step": (
                "restart the adapter process for a full rebuild — the "
                "trigger_sync is best-effort and only covers adapters that "
                "react to manual sync events; most adapters need a process "
                "restart to clear their 'already seen' set."
            ),
        },
    )


@app.post("/v1/sources/{source_id}/sync", dependencies=[Depends(verify_token)])
async def trigger_sync(source_id: str):
    source = await redis_client.get_source(source_id)
    if not source:
        raise HTTPException(404, detail=f"Source {source_id} not found")

    locked = await redis_client.acquire_lock(source_id, ttl=1800)
    if not locked:
        raise HTTPException(409, detail=f"Sync already in progress for {source_id}")

    sync_id = f"sync-{uuid.uuid4().hex[:8]}"
    await redis_client.update_source_status(source_id, SourceStatus.DETECTING.value)

    # Publish a manual trigger event to change-events
    trigger = {
        "type": "trigger_sync",
        "source_id": source_id,
        "sync_id": sync_id,
        "priority": "manual",
    }
    await redis_client.publish(Config.STREAM_CHANGE_EVENTS, trigger)

    return JSONResponse(
        status_code=202,
        content={"sync_id": sync_id, "status": "detecting", "priority": "manual"},
    )


@app.post("/v1/search/semantic", dependencies=[Depends(verify_token)])
async def search_semantic(req: SemanticSearchReq):
    start = time.monotonic()
    results = await index_manager.search_semantic(
        query=req.query,
        modality=req.modality,
        repo=req.repo,
        path_prefix=req.path_prefix,
        top_k=req.top_k,
        score_threshold=req.score_threshold,
        max_age_days=req.max_age_days,
        freshness_boost=req.freshness_boost,
    )
    elapsed = int((time.monotonic() - start) * 1000)
    return {"results": results, "total": len(results), "query_time_ms": elapsed}


@app.post("/v1/search/lexical", dependencies=[Depends(verify_token)])
async def search_lexical(req: LexicalSearchReq):
    start = time.monotonic()
    results = await index_manager.search_lexical(
        query=req.query,
        repo=req.repo,
        path_prefix=req.path_prefix,
        modality=req.modality,
        limit=req.limit,
        offset=req.offset,
        max_age_days=req.max_age_days,
        freshness_boost=req.freshness_boost,
    )
    elapsed = int((time.monotonic() - start) * 1000)
    return {"results": results, "total": len(results), "query_time_ms": elapsed}


@app.get("/v1/files", dependencies=[Depends(verify_token)])
async def list_files(
    source_id: Optional[str] = None,
    path_prefix: Optional[str] = None,
    modality: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    results = await index_manager.list_files(
        source_id=source_id,
        path_prefix=path_prefix,
        modality=modality,
        limit=limit,
        offset=offset,
    )
    return {"files": results, "total": len(results)}



@app.post("/v1/rag/ask", dependencies=[Depends(verify_token)])
async def rag_ask(req: RAGAskReq):
    """RAG endpoint: hybrid search + re-rank + context assembly.
    
    Returns curated context ready for LLM interpretation.
    Does NOT call an LLM - the caller (Claude, GPT, etc.) interprets the context.
    """
    import time as _time
    start = _time.monotonic()

    # --- 1. Hybrid search: semantic + lexical in parallel ---
    semantic_task = index_manager.search_semantic(
        query=req.question,
        repo=req.repos[0] if req.repos and len(req.repos) == 1 else None,
        top_k=req.top_k,
        score_threshold=0.25,
        max_age_days=req.max_age_days,
        freshness_boost=req.freshness_boost,
    )
    lexical_task = index_manager.search_lexical(
        query=req.question,
        repo=req.repos[0] if req.repos and len(req.repos) == 1 else None,
        limit=req.top_k,
        max_age_days=req.max_age_days,
        freshness_boost=req.freshness_boost,
    )
    semantic_results, lexical_results = await asyncio.gather(
        semantic_task, lexical_task, return_exceptions=True
    )
    if isinstance(semantic_results, Exception):
        logger.error("RAG semantic search failed: %s", semantic_results)
        semantic_results = []
    if isinstance(lexical_results, Exception):
        logger.error("RAG lexical search failed: %s", lexical_results)
        lexical_results = []

    # --- 2. Merge and de-duplicate by chunk_id/path ---
    seen = {}
    for r in semantic_results:
        key = r.get("chunk_id") or f"{r.get('source_id')}:{r.get('path')}"
        if key not in seen:
            seen[key] = {
                **r,
                "semantic_score": r.get("score", 0),
                "lexical_score": 0.0,
            }

    for r in lexical_results:
        key = r.get("doc_id") or f"{r.get('source_id')}:{r.get('path')}"
        if key in seen:
            seen[key]["lexical_score"] = 0.5  # Presence boost
        else:
            seen[key] = {
                **r,
                "semantic_score": 0.0,
                "lexical_score": 0.5,
            }

    # --- 3. Re-rank: weighted combination ---
    w_sem = req.semantic_weight
    w_lex = 1.0 - w_sem
    ranked = []
    for item in seen.values():
        combined = (item["semantic_score"] * w_sem) + (item["lexical_score"] * w_lex)
        item["combined_score"] = combined
        ranked.append(item)

    ranked.sort(key=lambda x: x["combined_score"], reverse=True)

    # --- 4. Filter by repos if multiple specified ---
    if req.repos and len(req.repos) > 1:
        ranked = [r for r in ranked if any(
            repo in r.get("path", "") or repo in r.get("repo", "") or repo in r.get("source_id", "")
            for repo in req.repos
        )]

    # --- 5. Assemble context with token budget ---
    context_parts = []
    sources_used = []
    total_chars = 0

    # Group by file path for coherent context
    file_groups = {}
    for item in ranked:
        path = item.get("path", "unknown")
        if path not in file_groups:
            file_groups[path] = []
        file_groups[path].append(item)

    for path, chunks in file_groups.items():
        if total_chars >= req.max_context_chars:
            break

        best_chunk = max(chunks, key=lambda x: x.get("combined_score", 0))
        snippet = best_chunk.get("snippet", "").strip()
        if not snippet:
            continue

        # Truncate snippet if needed
        remaining = req.max_context_chars - total_chars
        if len(snippet) > remaining:
            snippet = snippet[:remaining] + "..."

        source_id = best_chunk.get("source_id", "")
        score = best_chunk.get("combined_score", 0)

        context_parts.append(f"### {path} (score: {score:.2f})\n{snippet}")
        sources_used.append({
            "path": path,
            "source_id": source_id,
            "score": round(score, 3),
            "chars": len(snippet),
        })
        total_chars += len(snippet) + len(path) + 20

    context = "\n\n".join(context_parts) if context_parts else "No relevant context found for this query."

    elapsed_ms = int((_time.monotonic() - start) * 1000)

    return RAGAskResponse(
        context=context,
        sources=sources_used,
        stats={
            "semantic_hits": len(semantic_results) if not isinstance(semantic_results, Exception) else 0,
            "lexical_hits": len(lexical_results) if not isinstance(lexical_results, Exception) else 0,
            "merged_candidates": len(seen),
            "sources_in_context": len(sources_used),
            "context_chars": total_chars,
            "query_time_ms": elapsed_ms,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Background: consume ChangeEvents and produce EmbeddingJobs ---

async def change_event_consumer():
    """Consume ChangeEvents from adapters, batch them, produce EmbeddingJobs."""
    consumer = "concentrador-0"
    while True:
        try:
            messages = await redis_client.consume(
                Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, consumer,
                count=50, block_ms=10000,
            )
            if not messages:
                continue

            batch_id = str(uuid.uuid4())
            for msg_id, data in messages:
                try:
                    # Skip trigger_sync messages (handled differently)
                    if data.get("type") == "trigger_sync":
                        await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)
                        continue

                    event = ChangeEvent(**data)

                    # Idempotency check
                    if event.idempotency_key and await redis_client.is_processed(
                        Config.STREAM_CHANGE_EVENTS, event.idempotency_key
                    ):
                        await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)
                        continue

                    # Handle deletes
                    if event.operation.value == "delete":
                        await index_manager.delete_by_path(event.source_id, event.path)
                        await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)
                        if event.idempotency_key:
                            await redis_client.mark_processed(Config.STREAM_CHANGE_EVENTS, event.idempotency_key)
                        continue

                    # Check backpressure
                    queue_len = await redis_client.stream_len(Config.STREAM_EMBEDDING_JOBS)
                    if queue_len > 10000:
                        logger.warning("Backpressure: queue at %d, pausing", queue_len)
                        await asyncio.sleep(5)
                        continue

                    # ── Router path (Slice 5) ──────────────────────────
                    # When AKOPIA_ROUTER_USE_EXTRACTORS=1, dispatch to the
                    # extractor layer. The EmbeddingJob is produced later
                    # by extract_result_consumer after the extractor
                    # returns an ExtractedContent. When the flag is off,
                    # fall through to the legacy modality-switch path.
                    if _router_use_extractors():
                        extract_job = _router.build_extract_job(event)
                        if extract_job is not None:
                            await redis_client.publish("extract-jobs", extract_job)
                            await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)
                            if event.idempotency_key:
                                await redis_client.mark_processed(
                                    Config.STREAM_CHANGE_EVENTS, event.idempotency_key,
                                )
                            continue
                        # build_extract_job returned None — either the
                        # event is image/delete/depth-exceeded, or no
                        # extractor is registered. Fall through so the
                        # legacy path still handles it (e.g. images go
                        # straight to CLIP via the image modality
                        # branch below).

                    # Legacy path: create embedding jobs inline based on modality
                    jobs = create_embedding_jobs(event, batch_id)
                    for job in jobs:
                        await redis_client.publish(
                            Config.STREAM_EMBEDDING_JOBS, job.model_dump()
                        )

                    await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)
                    if event.idempotency_key:
                        await redis_client.mark_processed(Config.STREAM_CHANGE_EVENTS, event.idempotency_key)

                except Exception as e:
                    logger.error("Error processing change event: %s", e, exc_info=True)
                    await redis_client.ack(Config.STREAM_CHANGE_EVENTS, Config.CG_CONCENTRATOR, msg_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Change event consumer error: %s", e, exc_info=True)
            await asyncio.sleep(5)


def _image_path_for(event: ChangeEvent) -> Optional[str]:
    """Resolve the absolute filesystem path the embedder should read.

    Only ``ContentRef(kind="path")`` is supported today — the git and
    folder adapters both produce this shape. Returns ``None`` for any
    other kind (inline_bytes / url / object_storage) so the caller can
    log a skip without risking a stale event.path that might or might
    not exist inside the embeddings container.
    """
    ref = event.content_ref
    if ref is None:
        return None
    if ref.kind != "path" or not ref.path:
        return None
    return ref.path


def create_embedding_jobs(event: ChangeEvent, batch_id: str) -> list[EmbeddingJob]:
    """Convert a ChangeEvent into one or more EmbeddingJobs."""
    jobs = []

    if event.modality.value == "text":
        # Text content. PDFs arrive as TEXT after the extractor runs;
        # any legacy OCR stored in derived_content is preferred if set.
        content = ""
        if event.derived_content and event.derived_content.ocr_text:
            content = event.derived_content.ocr_text
        # For text files, content comes via the adapter embedding the file directly
        # The adapter should include content in derived_content or we read from cache
        if not content:
            # Placeholder - adapter should set derived_content.ocr_text for PDFs
            # For text files, this path means content should be in the event metadata
            content = f"[file: {event.path}]"

        ccfg = get_chunker_config()
        chunks = chunk_text(
            content,
            max_tokens=ccfg.chunk_size_tokens,
            overlap_tokens=ccfg.overlap_tokens,
            strategy=ccfg.strategy,
            source_id=event.source_id,
            path=event.path,
        )
        if chunks:
            chunk_models = []
            for c in chunks:
                cm = Chunk(**c)
                cm.content_modified_at = event.content_modified_at
                chunk_models.append(cm)
            job = EmbeddingJob(
                batch_id=batch_id,
                idempotency_key=event.idempotency_key,
                source_id=event.source_id,
                modality=EmbeddingModality.TEXT,
                chunks=chunk_models,
                priority=event.priority,
            )
            jobs.append(job)

    if event.modality.value == "image":
        # Image branch: emit an EmbeddingJob whose ``content`` is the
        # absolute filesystem path the embeddings pod will pass to
        # ``fastembed.ImageEmbedding.embed(paths)``.
        #
        # Contract: the adapter must set ``event.content_ref`` with
        # ``kind="path"`` (the git + folder adapters both do). The path
        # must resolve inside the embeddings container — docker-compose
        # mounts the ``git-repos`` volume and ``./data/docs`` read-only
        # on the embeddings service exactly for this reason. Without
        # those mounts the embedder will raise ``FileNotFoundError``
        # and the job is routed to the DLQ (loud, not silent).
        #
        # Events without a path-kind content_ref are skipped (inline
        # bytes/url modes aren't wired for images yet). Skipping here
        # is intentional: the file is still indexed at text level if
        # another pipeline picks it up, and re-enabling is a config
        # change (add a new ContentRef kind to _image_path_for).
        image_path = _image_path_for(event)
        if image_path is None:
            logger.warning(
                "skipping image job for %s: no usable ContentRef (kind=path required); "
                "source=%s",
                event.path, event.source_id,
            )
        else:
            job = EmbeddingJob(
                batch_id=batch_id,
                idempotency_key=event.idempotency_key,
                source_id=event.source_id,
                modality=EmbeddingModality.IMAGE,
                chunks=[Chunk(
                    chunk_id=f"{event.source_id}:{event.path}:0",
                    content=image_path,    # absolute path on embeddings pod
                    path=event.path,       # logical path in the source
                    chunk_index=0,
                    total_chunks=1,
                    content_modified_at=event.content_modified_at,
                )],
                priority=event.priority,
            )
            jobs.append(job)

    # Audio/video used to have transcript + keyframe branches here, but
    # were never wired end-to-end. Adding them back is the job of the
    # checklist in docs/adding-a-modality.md.

    return jobs


# --- Background: consume EmbeddingResults and upsert to indices ---

async def embedding_result_consumer():
    """Consume EmbeddingResults and upsert into Qdrant + Meilisearch."""
    consumer = "upserter-0"
    while True:
        try:
            messages = await redis_client.consume(
                Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER, consumer,
                count=20, block_ms=10000,
            )
            if not messages:
                continue

            for msg_id, data in messages:
                try:
                    result = EmbeddingResult(**data)

                    if result.status == "failure":
                        # Check retry count from the original job
                        logger.error("Embedding failed for job %s: %s", result.job_id, result.error)
                        await redis_client.publish(Config.STREAM_DEAD_LETTER, data)
                        await redis_client.ack(Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER, msg_id)
                        continue

                    # Upsert embeddings into Qdrant + Meilisearch.
                    # UnsupportedModalityError is a per-event failure —
                    # send the whole result to the DLQ for inspection
                    # rather than crashing the consumer (loud + contained).
                    unsupported: Optional[UnsupportedModalityError] = None
                    for emb in result.embeddings:
                        try:
                            await index_manager.upsert(emb)
                        except UnsupportedModalityError as ue:
                            unsupported = ue
                            logger.error(
                                "Unsupported modality in embedding result (job=%s chunk=%s): %s",
                                result.job_id, emb.chunk_id, ue,
                            )
                            break

                    if unsupported is not None:
                        await redis_client.publish(
                            Config.STREAM_DEAD_LETTER,
                            {**data, "error": f"unsupported_modality: {unsupported}"},
                        )
                        await redis_client.ack(
                            Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER, msg_id,
                        )
                        continue

                    await redis_client.ack(Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER, msg_id)
                    logger.info(
                        "Upserted %d embeddings for job %s in %dms",
                        len(result.embeddings), result.job_id, result.processing_time_ms,
                    )

                except Exception as e:
                    logger.error("Error processing embedding result: %s", e, exc_info=True)
                    await redis_client.ack(Config.STREAM_EMBEDDING_RESULTS, Config.CG_UPSERTER, msg_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Embedding result consumer error: %s", e, exc_info=True)
            await asyncio.sleep(5)


# --- Background: consume ExtractResults and produce EmbeddingJobs (Slice 5) ---

async def extract_result_consumer():
    """Consume extract-results, chunk + publish EmbeddingJobs, republish attached.

    Runs unconditionally — if no extractor has been enabled, the stream
    stays empty and this loop just sleeps. Active only when
    ``AKOPIA_ROUTER_USE_EXTRACTORS=1`` on the producers (adapters continue
    emitting ChangeEvents regardless; the router in change_event_consumer
    decides whether to dispatch to extract-jobs).
    """
    consumer = "router-extract-0"
    group = "cg-router-extract"
    while True:
        try:
            messages = await redis_client.consume(
                "extract-results", group, consumer,
                count=20, block_ms=10000,
            )
            if not messages:
                continue
            for msg_id, payload in messages:
                try:
                    if not payload.get("ok", True):
                        # Extractor already routed to DLQ; just ack.
                        await redis_client.ack("extract-results", group, msg_id)
                        continue
                    event = ChangeEvent(**payload["event"]) if "event" in payload else None
                    if event is None:
                        # Legacy shape used by BaseExtractor: only event_id + result
                        # In that case we can't rehydrate the full event context,
                        # so we log + skip. Producer will be fixed in a follow-up
                        # to always include the event payload.
                        logger.warning(
                            "extract-result missing full event; id=%s",
                            payload.get("event_id"),
                        )
                        await redis_client.ack("extract-results", group, msg_id)
                        continue
                    extracted = ExtractedContent(**payload["result"])

                    # Cycle guard — router already checks at dispatch time
                    # but belt-and-suspenders in case an attached chain
                    # slipped through.
                    if event.exceeds_max_depth():
                        await redis_client.ack("extract-results", group, msg_id)
                        continue

                    # 1) Republish any attached ChangeEvents for recursive processing.
                    for child in _router.repoint_attached(extracted.attached, event):
                        await redis_client.publish(
                            Config.STREAM_CHANGE_EVENTS, child.model_dump(mode="json"),
                        )

                    # 2) Build + publish EmbeddingJobs for the main payload.
                    jobs = _router.build_embedding_jobs(event, extracted)
                    for job in jobs:
                        await redis_client.publish(
                            Config.STREAM_EMBEDDING_JOBS, job.model_dump(mode="json"),
                        )

                    await redis_client.ack("extract-results", group, msg_id)
                    if event.idempotency_key:
                        await redis_client.mark_processed(
                            Config.STREAM_CHANGE_EVENTS, event.idempotency_key,
                        )
                    logger.info(
                        "Router: extract→embed produced %d job(s) for event=%s (attached=%d)",
                        len(jobs), event.event_id, len(extracted.attached),
                    )
                except Exception as e:
                    logger.error(
                        "extract_result_consumer: error processing msg_id=%s: %s",
                        msg_id, e, exc_info=True,
                    )
                    await redis_client.ack("extract-results", group, msg_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("extract_result_consumer loop error: %s", e, exc_info=True)
            await asyncio.sleep(5)


# --- Background: DLQ drainer (Slice 5) ---

async def dlq_drainer():
    """Background worker that retries transient DLQ entries with backoff.

    Inspects ``dead-letter`` messages, reads ``retry_count``, decides:
      * retry_count < 3: republish to originating stream with
        ``retry_count += 1`` after an exponential backoff (1, 5, 15 min).
      * retry_count >= 3: leave in DLQ (terminal). Drainer still
        consumes the message via the ``cg-ops-drainer`` group so it's
        off the pending list; the message remains visible to the ops
        dashboard via ``stream_len(dead-letter)``.

    Safe to run even when extractors are off — DLQ entries from the
    legacy embedding path (existing behaviour) are also retried.
    """
    consumer = "dlq-drainer-0"
    group = "cg-ops-drainer"
    try:
        await redis_client.ensure_stream_and_group(Config.STREAM_DEAD_LETTER, group)
    except Exception:
        pass
    backoff = {0: 60, 1: 300, 2: 900}  # seconds for retry attempt index
    while True:
        try:
            messages = await redis_client.consume(
                Config.STREAM_DEAD_LETTER, group, consumer,
                count=10, block_ms=30000,
            )
            if not messages:
                continue
            for msg_id, payload in messages:
                try:
                    original = payload.get("original") or payload
                    source_stream = payload.get("stream") or Config.STREAM_EMBEDDING_JOBS
                    retry_count = int(original.get("retry_count", 0)) if isinstance(original, dict) else 0
                    if retry_count >= 3:
                        # Terminal; leave for manual inspection.
                        await redis_client.ack(Config.STREAM_DEAD_LETTER, group, msg_id)
                        continue
                    wait_s = backoff.get(retry_count, 900)
                    await asyncio.sleep(wait_s)
                    if isinstance(original, dict):
                        original["retry_count"] = retry_count + 1
                    await redis_client.publish(source_stream, original)
                    await redis_client.ack(Config.STREAM_DEAD_LETTER, group, msg_id)
                    logger.info(
                        "DLQ retry: stream=%s attempt=%d",
                        source_stream, retry_count + 1,
                    )
                except Exception as e:
                    logger.error("dlq_drainer: error on msg_id=%s: %s", msg_id, e, exc_info=True)
                    await redis_client.ack(Config.STREAM_DEAD_LETTER, group, msg_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("dlq_drainer loop error: %s", e, exc_info=True)
            await asyncio.sleep(10)
