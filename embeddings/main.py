"""Akopia Embedding Service — consumes EmbeddingJobs, produces EmbeddingResults.

Text embedding is pluggable via the ``EmbedderBackend`` protocol. Pick the
backend through ``core.embeddings.text.provider`` in akopia.yaml (currently
``fastembed`` default, or ``ollama`` for an external HTTP-served embedder).

Image embedding is also pluggable through ``core.embeddings.image.provider``
(``fastembed`` is the only provider shipped today; add more by extending
``_build_image_model``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config
from common.models import EmbeddingJob, EmbeddingResult, EmbeddingEntry
from common.redis_client import RedisClient
from embeddings.backends import EmbedderBackend, build_backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("embeddings")

redis_client = RedisClient()
text_backend: Optional[EmbedderBackend] = None
clip_model = None  # image embedder instance (fastembed.ImageEmbedding today)
image_model_name: Optional[str] = None  # resolved model id, exposed via /health

DEFAULT_IMAGE_PROVIDER = "fastembed"
DEFAULT_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"

# Batch size for the Redis XREADGROUP in the consumer loop. Each message
# is a full EmbeddingJob. We gather() the whole batch so the backend
# (Ollama in prod) can service them in parallel — Ollama queues HTTP
# POSTs internally, so N concurrent calls keeps the GPU saturated
# without the bookkeeping of cross-job chunk merging.
_DEFAULT_EMBEDDER_BATCH_SIZE = 10
_MIN_EMBEDDER_BATCH_SIZE = 1
_MAX_EMBEDDER_BATCH_SIZE = 50


def _resolve_batch_size() -> int:
    """Clamp EMBEDDER_BATCH_SIZE to [1, 50]; fall back to default on
    any parse error or out-of-range value with a warning."""
    raw = os.getenv("EMBEDDER_BATCH_SIZE")
    if not raw:
        return _DEFAULT_EMBEDDER_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "EMBEDDER_BATCH_SIZE=%r is not an int; falling back to default %d",
            raw, _DEFAULT_EMBEDDER_BATCH_SIZE,
        )
        return _DEFAULT_EMBEDDER_BATCH_SIZE
    if value < _MIN_EMBEDDER_BATCH_SIZE or value > _MAX_EMBEDDER_BATCH_SIZE:
        logger.warning(
            "EMBEDDER_BATCH_SIZE=%d is out of range [%d, %d]; clamping",
            value, _MIN_EMBEDDER_BATCH_SIZE, _MAX_EMBEDDER_BATCH_SIZE,
        )
        value = max(_MIN_EMBEDDER_BATCH_SIZE, min(_MAX_EMBEDDER_BATCH_SIZE, value))
    return value


def _load_text_backend() -> EmbedderBackend:
    """Read provider config from akopia.yaml (if present) or env, and build backend."""
    global text_backend
    if text_backend is not None:
        return text_backend

    provider = os.getenv("EMBEDDER_PROVIDER")
    model: Optional[str] = os.getenv("EMBEDDER_MODEL")
    quantized_env = os.getenv("EMBEDDER_QUANTIZED")
    quantized: Optional[bool] = None
    if quantized_env is not None:
        quantized = quantized_env.lower() in ("1", "true", "yes")
    url: Optional[str] = os.getenv("OLLAMA_URL")

    # akopia.yaml takes precedence over env when available.
    try:
        from common.config_loader import load_config
        cfg = load_config()
        tcfg = cfg.core.embeddings.text
        if tcfg.provider:
            provider = tcfg.provider
        if tcfg.model:
            model = tcfg.model
        if tcfg.quantized is not None:
            quantized = tcfg.quantized
        extra_url = getattr(tcfg, "url", None)
        if extra_url:
            url = extra_url
    except Exception as e:
        logger.info("akopia.yaml not loaded (%s); using env-only config", e)

    provider = provider or "fastembed"
    text_backend = build_backend(
        provider,
        model=model,
        quantized=quantized,
        url=url,
    )
    logger.info(
        "text embedder ready: provider=%s model=%s url=%s",
        provider, text_backend.model_id(), url or "n/a",
    )
    return text_backend


def _resolve_image_config() -> tuple[str, str]:
    """Resolve (provider, model) for the image embedder.

    Precedence: akopia.yaml (``core.embeddings.image.provider/model``) >
    env (``IMAGE_EMBEDDER_PROVIDER`` / ``IMAGE_EMBEDDER_MODEL``) >
    hard-coded defaults (fastembed + CLIP ViT-B/32).

    Mirrors the pattern used by ``_load_text_backend``.
    """
    provider = os.getenv("IMAGE_EMBEDDER_PROVIDER")
    model = os.getenv("IMAGE_EMBEDDER_MODEL")

    try:
        from common.config_loader import load_config
        cfg = load_config()
        icfg = cfg.core.embeddings.image
        if getattr(icfg, "provider", None):
            provider = icfg.provider
        if getattr(icfg, "model", None):
            model = icfg.model
    except Exception as e:
        logger.info("akopia.yaml not loaded for image config (%s); using env/defaults", e)

    return provider or DEFAULT_IMAGE_PROVIDER, model or DEFAULT_IMAGE_MODEL


def _build_image_model(provider: str, model: str):
    """Instantiate the image embedder for ``provider``.

    Single place to add new providers later — keep each branch as a
    thin wrapper over the third-party constructor. Raises ValueError
    for unknown providers rather than silently falling back.
    """
    if provider == "fastembed":
        from fastembed import ImageEmbedding
        return ImageEmbedding(model_name=model)
    raise ValueError(
        f"Unsupported image embedder provider: {provider!r}. "
        "Supported: 'fastembed'. Add a branch in embeddings.main._build_image_model."
    )


def load_clip_model():
    """Lazy-load the image embedding model, respecting akopia.yaml config.

    Kept the ``load_clip_model`` name for backwards compat — today the
    default is still CLIP ViT-B/32 but swapping the model is a config
    change, not a code change.
    """
    global clip_model, image_model_name
    if clip_model is None:
        provider, model = _resolve_image_config()
        logger.info("Loading image embedder: provider=%s model=%s", provider, model)
        clip_model = _build_image_model(provider, model)
        image_model_name = model
        logger.info("Image embedder ready (%s)", model)
    return clip_model


async def embed_texts(texts: list[str]) -> list[list[float]]:
    backend = _load_text_backend()
    return await backend.embed(texts)


def embed_images_sync(paths: list[str]) -> list[list[float]]:
    model = load_clip_model()
    return [emb.tolist() for emb in model.embed(paths)]


async def embed_images(paths: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(embed_images_sync, paths)


async def process_job(job: EmbeddingJob) -> EmbeddingResult:
    start = time.monotonic()
    try:
        if job.modality.value == "image":
            paths = [c.content for c in job.chunks]
            vectors = await embed_images(paths)
            # load_clip_model populates image_model_name on first use.
            model_name = image_model_name or _resolve_image_config()[1]
        else:
            texts = [c.content for c in job.chunks]
            vectors = await embed_texts(texts)
            model_name = _load_text_backend().model_id()

        entries = []
        for chunk, vector in zip(job.chunks, vectors):
            entries.append(EmbeddingEntry(
                chunk_id=chunk.chunk_id,
                vector=vector,
                model=model_name,
                modality=job.modality.value,
                path=chunk.path,
                repo=chunk.repo,
                source_id=job.source_id,
                derived_from=job.derived_from,
                snippet=chunk.content[:4000],
                content_modified_at=chunk.content_modified_at,
            ))

        elapsed = int((time.monotonic() - start) * 1000)
        return EmbeddingResult(
            job_id=job.job_id,
            batch_id=job.batch_id,
            idempotency_key=job.idempotency_key,
            status="success",
            embeddings=entries,
            processing_time_ms=elapsed,
        )

    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.error("Embedding failed for job %s: %s", job.job_id, e, exc_info=True)
        return EmbeddingResult(
            job_id=job.job_id,
            batch_id=job.batch_id,
            idempotency_key=job.idempotency_key,
            status="failure",
            error=str(e),
            processing_time_ms=elapsed,
        )


async def _process_msg(msg_id: str, data: dict) -> None:
    """Handle one Redis-stream message end-to-end: parse → dedupe → embed
    → publish result → mark-processed → ack.

    Acks in every branch (success, idempotent-skip, failure) so a poison
    job never stalls the consumer group. Failures get ack'd with an
    ``exc_info`` log — by that point ``process_job`` has already trapped
    the embedding-side exception and emitted a ``status="failure"``
    result, so loss here is narrow (a parse error or a Redis hiccup,
    both visible in logs).
    """
    try:
        job = EmbeddingJob(**data)

        if await redis_client.is_processed(
            Config.STREAM_EMBEDDING_JOBS, job.idempotency_key
        ):
            await redis_client.ack(
                Config.STREAM_EMBEDDING_JOBS, Config.CG_EMBEDDER, msg_id,
            )
            return

        result = await process_job(job)

        await redis_client.publish(
            Config.STREAM_EMBEDDING_RESULTS,
            result.model_dump(),
        )
        await redis_client.mark_processed(
            Config.STREAM_EMBEDDING_JOBS, job.idempotency_key
        )
        await redis_client.ack(
            Config.STREAM_EMBEDDING_JOBS, Config.CG_EMBEDDER, msg_id,
        )

        logger.info(
            "Processed job %s: %d chunks, %dms, model=%s",
            job.job_id, len(job.chunks), result.processing_time_ms,
            result.embeddings[0].model if result.embeddings else "N/A",
        )
    except Exception as e:
        logger.error("Error processing job: %s", e, exc_info=True)
        await redis_client.ack(
            Config.STREAM_EMBEDDING_JOBS, Config.CG_EMBEDDER, msg_id,
        )


async def consumer_loop():
    consumer = "embedder-0"
    await redis_client.ensure_stream_and_group(
        Config.STREAM_EMBEDDING_JOBS, Config.CG_EMBEDDER
    )
    batch_size = _resolve_batch_size()
    logger.info("Embedding consumer started (batch_size=%d)", batch_size)

    while True:
        try:
            messages = await redis_client.consume(
                Config.STREAM_EMBEDDING_JOBS,
                Config.CG_EMBEDDER,
                consumer,
                count=batch_size,
                block_ms=5000,
            )
            if not messages:
                continue

            # Process the whole batch in parallel. Each coroutine owns
            # its own ack (inside _process_msg), so partial success is
            # representable: if job 3 of 10 blows up, jobs 1,2,4..10
            # still ack and ship. return_exceptions=True guards the
            # loop against a bug in _process_msg itself — we'd rather
            # log and continue than crash the whole consumer.
            await asyncio.gather(
                *[_process_msg(msg_id, data) for msg_id, data in messages],
                return_exceptions=True,
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Consumer loop error: %s", e, exc_info=True)
            await asyncio.sleep(5)


# --- HTTP API for query embedding ---

class EmbedRequest(BaseModel):
    text: str
    model: str = "text"  # "text" or "image"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await redis_client.connect()
    # Pre-load the text backend at startup so first request isn't penalised
    # and misconfiguration surfaces immediately in healthchecks.
    try:
        _load_text_backend()
    except Exception:
        logger.exception("Failed to pre-load text backend")
    task = asyncio.create_task(consumer_loop())
    logger.info("Embedding service started")
    yield
    task.cancel()
    await redis_client.close()


app = FastAPI(title="KB Embedding Service", version="1.0.0", lifespan=lifespan)


@app.post("/embed")
async def embed_query(req: EmbedRequest):
    """Embed a single query string.

    ``model="text"`` embeds ``req.text`` directly — used by the
    concentrador to turn a search query into a vector for Qdrant.

    ``model="image"`` is **not supported** on this endpoint and
    returns HTTP 400. Image embedding requires file bytes, not a text
    query; the previous implementation silently passed ``req.text`` as
    a path to ``fastembed.ImageEmbedding.embed`` and blew up with an
    opaque FileNotFoundError. To index images, emit a ChangeEvent via
    a source adapter — the concentrador's image branch of
    create_embedding_jobs will route it through the
    embedding-jobs stream.

    (To search images *by text*, use ``search_semantic`` with
    ``modality="image"`` — the server will embed the text via CLIP's
    text tower at the Qdrant level.)
    """
    if req.model == "text":
        vectors = await embed_texts([req.text])
        return {"vector": vectors[0]}
    if req.model == "image":
        raise HTTPException(
            status_code=400,
            detail=(
                "image embedding via /embed is not supported. "
                "Ingest images through a source adapter (git or folder); "
                "the concentrador routes them to the embedding-jobs stream. "
                "See docs/architecture.md §6.1 for the shared-volume contract."
            ),
        )
    raise HTTPException(
        status_code=400,
        detail=f"unknown model={req.model!r}; expected 'text' or 'image'",
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "text_backend_loaded": text_backend is not None,
        "text_model": text_backend.model_id() if text_backend else None,
        "image_model_loaded": clip_model is not None,
        "image_model": image_model_name if clip_model is not None else None,
    }
