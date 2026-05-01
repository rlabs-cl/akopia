"""Index Manager - handles Qdrant + Meilisearch operations."""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from common.config import Config
from common.models import EmbeddingEntry


# Freshness re-rank decay constant. fresh_score = exp(-age_days / TAU)
# with TAU=180 days gives ~half-life of 125 days.
FRESHNESS_DECAY_DAYS = 180.0
# Neutral score for docs missing content_modified_at (pre-feature ingests).
FRESHNESS_NEUTRAL = 0.5

logger = logging.getLogger("concentrador.index_manager")


# Modalities the upsert path knows how to route. Kept next to
# IndexManager so adding a new modality is a one-stop edit: extend this
# tuple and add the collection routing branch in ``IndexManager.upsert``.
SUPPORTED_UPSERT_MODALITIES: tuple[str, ...] = ("text", "image", "audio_transcript", "video_transcript")


class UnsupportedModalityError(ValueError):
    """Raised by IndexManager.upsert when the modality has no index wiring.

    The event should be routed to the DLQ rather than silently falling
    back to ``akopia_text`` (which would cause mixed-modality corruption in
    the text collection).
    """


class IndexManager:
    """Manages Qdrant and Meilisearch indices."""

    def __init__(
        self,
        qdrant_url: str,
        qdrant_api_key: str,
        meili_url: str,
        meili_key: str,
    ):
        self.qdrant_url = qdrant_url.rstrip("/")
        self.qdrant_headers = {"api-key": qdrant_api_key} if qdrant_api_key else {}
        self.meili_url = meili_url.rstrip("/")
        self.meili_headers = {"Authorization": f"Bearer {meili_key}"} if meili_key else {}
        self._http = httpx.AsyncClient(timeout=30)

    async def initialize(self) -> None:
        """Create collections/indices if they don't exist."""
        await self._ensure_qdrant_collection(Config.QDRANT_TEXT_COLLECTION, 768)
        await self._ensure_qdrant_collection(Config.QDRANT_IMAGE_COLLECTION, 512)
        await self._ensure_meili_index()
        logger.info("Index manager initialized")

    async def _ensure_qdrant_collection(self, name: str, dim: int) -> None:
        try:
            resp = await self._http.get(
                f"{self.qdrant_url}/collections/{name}",
                headers=self.qdrant_headers,
            )
            if resp.status_code == 200:
                logger.info("Qdrant collection %s exists", name)
                return
        except Exception:
            pass

        body = {
            "vectors": {"size": dim, "distance": "Cosine"},
            "optimizers_config": {"memmap_threshold": 20000},
            "on_disk_payload": True,
        }
        resp = await self._http.put(
            f"{self.qdrant_url}/collections/{name}",
            json=body,
            headers=self.qdrant_headers,
        )
        resp.raise_for_status()
        logger.info("Created Qdrant collection %s (%dd)", name, dim)

        # Create payload indices
        for field in ["source_id", "repo", "path", "modality", "derived_from", "chunk_id"]:
            await self._http.put(
                f"{self.qdrant_url}/collections/{name}/index",
                json={"field_name": field, "field_schema": "keyword"},
                headers=self.qdrant_headers,
            )

    async def _ensure_meili_index(self) -> None:
        try:
            resp = await self._http.get(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}",
                headers=self.meili_headers,
            )
            if resp.status_code == 200:
                logger.info("Meilisearch index %s exists", Config.MEILI_INDEX)
                # Update settings anyway
                await self._update_meili_settings()
                return
        except Exception:
            pass

        resp = await self._http.post(
            f"{self.meili_url}/indexes",
            json={"uid": Config.MEILI_INDEX, "primaryKey": "doc_id"},
            headers=self.meili_headers,
        )
        logger.info("Created Meilisearch index %s: %s", Config.MEILI_INDEX, resp.status_code)
        await self._update_meili_settings()

    async def _update_meili_settings(self) -> None:
        settings = {
            "searchableAttributes": ["snippet", "path", "repo"],
            "filterableAttributes": [
                "source_id", "repo", "path", "modality", "derived_from",
                "content_modified_ts",
            ],
            "sortableAttributes": ["last_indexed", "content_modified_ts"],
            "displayedAttributes": [
                "doc_id", "source_id", "repo", "path", "modality",
                "snippet", "derived_from", "last_indexed",
                "content_modified_at", "content_modified_ts",
            ],
            "typoTolerance": {
                "enabled": True,
                "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8},
            },
        }
        await self._http.patch(
            f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/settings",
            json=settings,
            headers=self.meili_headers,
        )

    # --- Upsert ---

    async def upsert(self, emb: EmbeddingEntry) -> None:
        """Upsert a single embedding into Qdrant and Meilisearch.

        Raises ``UnsupportedModalityError`` if ``emb.modality`` has no
        collection wired below. Silent fallback to ``akopia_text`` (the
        pre-2026-04 behaviour) would cause mixed-modality corruption
        when a new modality is plumbed through adapters + extractors
        without being added here — loud failure per-event is safer.
        """
        # Determine collection
        if emb.modality == "image":
            collection = Config.QDRANT_IMAGE_COLLECTION
        elif emb.modality in ("text", "audio_transcript", "video_transcript"):
            collection = Config.QDRANT_TEXT_COLLECTION
        else:
            raise UnsupportedModalityError(
                f"Unsupported modality {emb.modality!r}: add a collection + "
                "branch in IndexManager.upsert, or coerce to Modality.TEXT "
                "in the extractor."
            )

        # Point ID from chunk_id (deterministic)
        point_id = self._chunk_id_to_int(emb.chunk_id)

        # Qdrant upsert
        payload = {
            "chunk_id": emb.chunk_id,
            "source_id": emb.source_id,
            "repo": emb.repo or "",
            "path": emb.path,
            "modality": emb.modality,
            "derived_from": emb.derived_from or "",
            "snippet": emb.snippet[:4000] if emb.snippet else "",
            "model": emb.model,
        }
        # Freshness metadata: store epoch seconds (int) for O(1) range
        # filters plus ISO 8601 string for human readability. Only
        # populated when the adapter captured an mtime — otherwise the
        # keys are omitted so range filters exclude the doc by default.
        if emb.content_modified_at is not None:
            ts_epoch = int(emb.content_modified_at.timestamp())
            payload["content_modified_ts"] = ts_epoch
            payload["content_modified_at"] = emb.content_modified_at.isoformat()
        body = {
            "points": [{
                "id": point_id,
                "vector": emb.vector,
                "payload": payload,
            }]
        }
        try:
            resp = await self._http.put(
                f"{self.qdrant_url}/collections/{collection}/points",
                json=body,
                headers=self.qdrant_headers,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Qdrant upsert failed for %s: %s", emb.chunk_id, e)
            raise

        # Meilisearch upsert (text modalities only)
        if emb.modality != "image":
            doc = {
                "doc_id": hashlib.md5(emb.chunk_id.encode()).hexdigest(),
                "source_id": emb.source_id,
                "repo": emb.repo or "",
                "path": emb.path,
                "modality": emb.modality,
                "snippet": emb.snippet[:10000] if emb.snippet else "",
                "derived_from": emb.derived_from or "",
                "last_indexed": emb.model,
            }
            if emb.content_modified_at is not None:
                ts_epoch = int(emb.content_modified_at.timestamp())
                doc["content_modified_ts"] = ts_epoch
                doc["content_modified_at"] = emb.content_modified_at.isoformat()
            try:
                resp = await self._http.post(
                    f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/documents",
                    json=[doc],
                    headers=self.meili_headers,
                )
            except Exception as e:
                logger.error("Meilisearch upsert failed for %s: %s", emb.chunk_id, e)

    # --- Search ---

    async def search_semantic(
        self,
        query: str,
        modality: Optional[str] = None,
        repo: Optional[str] = None,
        path_prefix: Optional[str] = None,
        top_k: int = 10,
        score_threshold: float = 0.0,
        max_age_days: Optional[int] = None,
        freshness_boost: float = 0.0,
    ) -> list[dict]:
        """Semantic search requires embedding the query first.
        This is done via the embedding service endpoint.
        For now, we call Qdrant directly with a pre-computed vector.
        The MCP server or client provides the query embedding.
        """
        # We need to embed the query - call the embedding service.
        # URL is configurable via EMBEDDINGS_URL so compose / k8s / remote
        # deployments all work without patching source.
        import os
        embeddings_url = os.getenv(
            "EMBEDDINGS_URL",
            "http://embeddings:8081",
        )
        try:
            resp = await self._http.post(
                f"{embeddings_url}/embed",
                json={"text": query, "model": "text"},
                timeout=10,
            )
            resp.raise_for_status()
            vector = resp.json()["vector"]
        except Exception as e:
            logger.error("Failed to embed query: %s", e)
            return []

        # Build Qdrant filter
        must = []
        if modality:
            must.append({"key": "modality", "match": {"value": modality}})
        if repo:
            must.append({"key": "repo", "match": {"value": repo}})
        # Hard freshness filter — excludes docs missing content_modified_ts
        # (range on an absent key does not match in Qdrant).
        cutoff_epoch = _cutoff_epoch(max_age_days)
        if cutoff_epoch is not None:
            must.append({
                "key": "content_modified_ts",
                "range": {"gte": cutoff_epoch},
            })

        # When a freshness_boost is requested we over-fetch so re-ranking
        # has material to work with. Capped at 200 to keep the Qdrant
        # request bounded.
        qdrant_limit = top_k
        if freshness_boost > 0.0:
            qdrant_limit = min(max(top_k * 4, 20), 200)

        body = {
            "vector": vector,
            "limit": qdrant_limit,
            "score_threshold": score_threshold,
            "with_payload": True,
            "filter": {"must": must} if must else None,
        }

        collection = Config.QDRANT_IMAGE_COLLECTION if modality == "image" else Config.QDRANT_TEXT_COLLECTION

        try:
            resp = await self._http.post(
                f"{self.qdrant_url}/collections/{collection}/points/search",
                json=body,
                headers=self.qdrant_headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Qdrant search failed: %s", e)
            return []

        results = []
        for hit in data.get("result", []):
            payload = hit.get("payload", {})
            if path_prefix and not payload.get("path", "").startswith(path_prefix):
                continue
            results.append({
                "chunk_id": payload.get("chunk_id", ""),
                "source_id": payload.get("source_id", ""),
                "repo": payload.get("repo", ""),
                "path": payload.get("path", ""),
                "modality": payload.get("modality", ""),
                "snippet": payload.get("snippet", ""),
                "score": hit.get("score", 0),
                "derived_from": payload.get("derived_from") or None,
                "content_modified_at": payload.get("content_modified_at"),
                "content_modified_ts": payload.get("content_modified_ts"),
            })

        if freshness_boost > 0.0:
            results = _apply_freshness_boost(results, freshness_boost)
            results = results[:top_k]

        return results

    async def search_lexical(
        self,
        query: str,
        repo: Optional[str] = None,
        path_prefix: Optional[str] = None,
        modality: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        max_age_days: Optional[int] = None,
        freshness_boost: float = 0.0,
    ) -> list[dict]:
        """BM25 search via Meilisearch."""
        filters = []
        if repo:
            filters.append(f'repo = "{repo}"')
        if modality:
            filters.append(f'modality = "{modality}"')
        if path_prefix:
            filters.append(f'path STARTS WITH "{path_prefix}"')  # Meili doesn't support this natively
        cutoff_epoch = _cutoff_epoch(max_age_days)
        if cutoff_epoch is not None:
            filters.append(f"content_modified_ts >= {cutoff_epoch}")

        meili_limit = limit
        if freshness_boost > 0.0:
            meili_limit = min(max(limit * 4, 20), 200)

        body = {
            "q": query,
            "limit": meili_limit,
            "offset": offset,
            "attributesToHighlight": ["snippet"],
        }
        if filters:
            body["filter"] = " AND ".join(filters)

        try:
            resp = await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/search",
                json=body,
                headers=self.meili_headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Meilisearch search failed: %s", e)
            return []

        results = []
        for hit in data.get("hits", []):
            results.append({
                "doc_id": hit.get("doc_id", ""),
                "source_id": hit.get("source_id", ""),
                "repo": hit.get("repo", ""),
                "path": hit.get("path", ""),
                "modality": hit.get("modality", ""),
                "snippet": hit.get("snippet", ""),
                "derived_from": hit.get("derived_from") or None,
                "highlights": hit.get("_formatted", {}).get("snippet", ""),
                "content_modified_at": hit.get("content_modified_at"),
                "content_modified_ts": hit.get("content_modified_ts"),
            })

        if freshness_boost > 0.0:
            # Meili's BM25 score isn't in the response payload by default;
            # use rank-derived scores (1.0 for rank 0 down to ~0 at end)
            # when mixing with freshness so rankings can shift.
            n = len(results) or 1
            for i, r in enumerate(results):
                r["score"] = 1.0 - (i / n)
            results = _apply_freshness_boost(results, freshness_boost)
            results = results[:limit]

        return results

    async def list_files(
        self,
        source_id: Optional[str] = None,
        path_prefix: Optional[str] = None,
        modality: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List indexed files via Meilisearch."""
        filters = []
        if source_id:
            filters.append(f'source_id = "{source_id}"')
        if modality:
            filters.append(f'modality = "{modality}"')

        body = {
            "q": path_prefix or "*",
            "limit": limit,
            "offset": offset,
        }
        if filters:
            body["filter"] = " AND ".join(filters)

        try:
            resp = await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/search",
                json=body,
                headers=self.meili_headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("Meilisearch list files failed: %s", e)
            return []

        return [
            {
                "path": hit.get("path", ""),
                "source_id": hit.get("source_id", ""),
                "modality": hit.get("modality", ""),
                "doc_id": hit.get("doc_id", ""),
            }
            for hit in data.get("hits", [])
        ]

    # --- Delete ---

    async def delete_by_path(self, source_id: str, path: str) -> None:
        """Delete all points/documents for a given path + source."""
        filter_body = {
            "filter": {
                "must": [
                    {"key": "source_id", "match": {"value": source_id}},
                    {"key": "path", "match": {"value": path}},
                ]
            }
        }

        for collection in [Config.QDRANT_TEXT_COLLECTION, Config.QDRANT_IMAGE_COLLECTION]:
            try:
                await self._http.post(
                    f"{self.qdrant_url}/collections/{collection}/points/delete",
                    json=filter_body,
                    headers=self.qdrant_headers,
                )
            except Exception as e:
                logger.error("Qdrant delete failed for %s:%s in %s: %s", source_id, path, collection, e)

        # Also delete derived items
        derived_filter = {
            "filter": {
                "must": [
                    {"key": "source_id", "match": {"value": source_id}},
                    {"key": "derived_from", "match": {"value": path}},
                ]
            }
        }
        for collection in [Config.QDRANT_TEXT_COLLECTION, Config.QDRANT_IMAGE_COLLECTION]:
            try:
                await self._http.post(
                    f"{self.qdrant_url}/collections/{collection}/points/delete",
                    json=derived_filter,
                    headers=self.qdrant_headers,
                )
            except Exception as e:
                logger.error("Qdrant delete derived failed: %s", e)

        # Meilisearch delete
        try:
            await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/documents/delete",
                json={"filter": f'source_id = "{source_id}" AND (path = "{path}" OR derived_from = "{path}")'},
                headers=self.meili_headers,
            )
        except Exception as e:
            logger.error("Meilisearch delete failed: %s", e)

    async def delete_by_source(self, source_id: str) -> dict:
        """Delete every indexed doc (chunks + derived) for a given source_id.

        Returns counts of what was found before deletion so callers can
        report how much was purged. Useful as the delete half of a
        reindex flow: after this returns, the caller must cause the
        adapter that owns `source_id` to re-emit ADD events for every
        path it watches (typically by restarting the adapter process —
        adapters hold the "already seen" set in memory and won't
        re-emit on their own).
        """
        source_filter = {"must": [{"key": "source_id", "match": {"value": source_id}}]}

        counts = {"qdrant_before": {}, "meili_before": 0}

        for collection in [Config.QDRANT_TEXT_COLLECTION, Config.QDRANT_IMAGE_COLLECTION]:
            try:
                resp = await self._http.post(
                    f"{self.qdrant_url}/collections/{collection}/points/count",
                    json={"filter": source_filter, "exact": True},
                    headers=self.qdrant_headers,
                )
                counts["qdrant_before"][collection] = resp.json().get("result", {}).get("count", 0)
            except Exception as e:
                logger.warning("Qdrant count failed for %s in %s: %s", source_id, collection, e)
                counts["qdrant_before"][collection] = None

        for collection in [Config.QDRANT_TEXT_COLLECTION, Config.QDRANT_IMAGE_COLLECTION]:
            try:
                await self._http.post(
                    f"{self.qdrant_url}/collections/{collection}/points/delete",
                    json={"filter": source_filter},
                    headers=self.qdrant_headers,
                )
            except Exception as e:
                logger.error("Qdrant delete_by_source failed for %s in %s: %s", source_id, collection, e)

        try:
            resp = await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/search",
                json={"filter": f'source_id = "{source_id}"', "limit": 0},
                headers=self.meili_headers,
            )
            counts["meili_before"] = resp.json().get("estimatedTotalHits", 0)
        except Exception as e:
            logger.warning("Meili count failed for %s: %s", source_id, e)
            counts["meili_before"] = None

        try:
            await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/documents/delete",
                json={"filter": f'source_id = "{source_id}"'},
                headers=self.meili_headers,
            )
        except Exception as e:
            logger.error("Meilisearch delete_by_source failed for %s: %s", source_id, e)

        return counts

    # --- Stats (operator dashboards / Atalaya admin UI) -------------

    async def stats(self) -> dict[str, Any]:
        """Return operator-facing index stats.

        Composition:
        * `docs.qdrant.<collection>` — Qdrant point counts per
          collection (vector docs, exact count via points/count).
        * `docs.meili` — Meilisearch document count.
        * `docs.total` — sum across the two stores.
        * `last_index_at` — most recent ``content_modified_at`` we've
          seen indexed (best-effort; ``None`` when no docs).
        * `errors` — list of stores that failed the probe (so the
          caller can degrade gracefully without seeing a 500).

        Designed to be cheap (no full scans). Each Qdrant call is
        ``points/count`` with no filter; Meili is ``search`` with
        ``limit:0`` which returns ``estimatedTotalHits`` without
        scanning the index.
        """
        out: dict[str, Any] = {
            "docs": {"qdrant": {}, "meili": 0, "total": 0},
            "last_index_at": None,
            "errors": [],
        }
        total = 0
        for collection in (
            Config.QDRANT_TEXT_COLLECTION,
            Config.QDRANT_IMAGE_COLLECTION,
        ):
            try:
                resp = await self._http.post(
                    f"{self.qdrant_url}/collections/{collection}/points/count",
                    json={"exact": True},
                    headers=self.qdrant_headers,
                    timeout=5,
                )
                count = int(resp.json().get("result", {}).get("count", 0))
                out["docs"]["qdrant"][collection] = count
                total += count
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"qdrant:{collection}:{exc}")
                out["docs"]["qdrant"][collection] = None
        try:
            resp = await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/search",
                json={"limit": 0},
                headers=self.meili_headers,
                timeout=5,
            )
            meili_n = int(resp.json().get("estimatedTotalHits", 0))
            out["docs"]["meili"] = meili_n
            total += meili_n
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"meili:{exc}")
            out["docs"]["meili"] = None
        out["docs"]["total"] = total

        # Best-effort latest-indexed timestamp: peek at the freshest
        # text document via Meili sort. Skipped silently if the index
        # has no docs or sorting fails.
        try:
            resp = await self._http.post(
                f"{self.meili_url}/indexes/{Config.MEILI_INDEX}/search",
                json={
                    "limit": 1,
                    "sort": ["content_modified_at:desc"],
                    "attributesToRetrieve": ["content_modified_at"],
                },
                headers=self.meili_headers,
                timeout=5,
            )
            hits = resp.json().get("hits") or []
            if hits and hits[0].get("content_modified_at"):
                out["last_index_at"] = hits[0]["content_modified_at"]
        except Exception:  # noqa: BLE001
            pass
        return out

    # --- Health checks ---

    async def health_check_qdrant(self) -> bool:
        try:
            resp = await self._http.get(
                f"{self.qdrant_url}/healthz", headers=self.qdrant_headers, timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def health_check_meili(self) -> bool:
        try:
            resp = await self._http.get(
                f"{self.meili_url}/health", headers=self.meili_headers, timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _chunk_id_to_int(chunk_id: str) -> int:
        """Convert chunk_id string to deterministic positive integer for Qdrant point ID."""
        h = hashlib.md5(chunk_id.encode()).hexdigest()
        return int(h[:16], 16)


# ── Freshness helpers (module-level for testability) ──────────────

def _cutoff_epoch(max_age_days: Optional[int]) -> Optional[int]:
    """Compute the epoch-seconds cutoff for a ``max_age_days`` window.

    ``None`` passes through unchanged so callers can wrap the result in
    ``if cutoff is not None: ...`` without an extra guard around the
    parameter.
    """
    if max_age_days is None:
        return None
    if max_age_days < 0:
        return None
    now = datetime.now(timezone.utc)
    return int(now.timestamp()) - int(max_age_days) * 86400


def _apply_freshness_boost(results: list[dict], beta: float) -> list[dict]:
    """Re-rank ``results`` by a convex combination of vector + fresh score.

    ``score_final = (1 - β) * score + β * fresh_score``

    where ``fresh_score = exp(-age_days / 180)`` when the result has
    ``content_modified_ts``, else ``FRESHNESS_NEUTRAL`` (0.5). Re-sorts
    in place and returns the same list for fluent chaining.
    """
    beta = max(0.0, min(1.0, beta))
    now_epoch = datetime.now(timezone.utc).timestamp()
    for r in results:
        base = float(r.get("score", 0.0) or 0.0)
        ts = r.get("content_modified_ts")
        if ts is None:
            fresh = FRESHNESS_NEUTRAL
        else:
            age_days = max(0.0, (now_epoch - float(ts)) / 86400.0)
            fresh = math.exp(-age_days / FRESHNESS_DECAY_DAYS)
        r["fresh_score"] = fresh
        r["score_final"] = (1.0 - beta) * base + beta * fresh
    results.sort(key=lambda r: r.get("score_final", 0.0), reverse=True)
    return results
