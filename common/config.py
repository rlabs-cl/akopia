"""Centralized configuration for all Akopia services."""
import os


class Config:
    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis.akopia.svc.cluster.local:6379")

    # Qdrant
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://qdrant.akopia.svc.cluster.local:6333")
    QDRANT_API_KEY: str = os.getenv("QDRANT_API_KEY", "")

    # Meilisearch
    MEILISEARCH_URL: str = os.getenv("MEILISEARCH_URL", "http://meilisearch.akopia.svc.cluster.local:7700")
    MEILISEARCH_KEY: str = os.getenv("MEILI_MASTER_KEY", "")

    # Auth. Accepts AKOPIA_BEARER_TOKEN (canonical) or BEARER_TOKEN (legacy).
    BEARER_TOKEN: str = os.getenv("AKOPIA_BEARER_TOKEN", os.getenv("BEARER_TOKEN", ""))

    # Strict auth. When truthy ("1"/"true"/"yes"), services that enforce
    # bearer tokens MUST refuse to start if AKOPIA_BEARER_TOKEN is unset.
    # When falsy (default), services keep permissive-dev behaviour
    # (prominent warning + allow). Set in production.
    STRICT_AUTH: bool = os.getenv("AKOPIA_STRICT_AUTH", "").strip().lower() in (
        "1", "true", "yes", "on",
    )

    # Gitea (self-hosted; operator must supply GITEA_URL pointing at their instance)
    GITEA_URL: str = os.getenv("GITEA_URL", "")
    GITEA_TOKEN: str = os.getenv("GITEA_TOKEN", "")

    # Paths
    GIT_REPOS_PATH: str = os.getenv("GIT_REPOS_PATH", "/data/repos")
    NAS_MOUNT_PATH: str = os.getenv("NAS_MOUNT_PATH", "/mnt/nas")
    PREPROCESS_CACHE_PATH: str = os.getenv("PREPROCESS_CACHE_PATH", "/data/cache")

    # Streams
    STREAM_CHANGE_EVENTS: str = "change-events"
    STREAM_EMBEDDING_JOBS: str = "embedding-jobs"
    STREAM_EMBEDDING_RESULTS: str = "embedding-results"
    STREAM_DEAD_LETTER: str = "dead-letter"

    # Consumer groups
    CG_CONCENTRATOR: str = "cg-concentrator"
    CG_EMBEDDER: str = "cg-embedder"
    CG_UPSERTER: str = "cg-upserter"

    # Embedding
    TEXT_BATCH_SIZE: int = int(os.getenv("TEXT_BATCH_SIZE", "32"))
    IMAGE_BATCH_SIZE: int = int(os.getenv("IMAGE_BATCH_SIZE", "16"))
    MAX_CHUNK_LENGTH: int = int(os.getenv("MAX_CHUNK_LENGTH", "8192"))
    MAX_PENDING_ACK: int = int(os.getenv("MAX_PENDING_ACK", "100"))
    JOB_TIMEOUT_MS: int = int(os.getenv("JOB_TIMEOUT_MS", "300000"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))

    # Qdrant collections
    QDRANT_TEXT_COLLECTION: str = "akopia_text"
    QDRANT_IMAGE_COLLECTION: str = "akopia_image"

    # Meilisearch index
    MEILI_INDEX: str = "akopia_lexical"

    # Redis key prefix for app-owned keys (idempotency, locks, watermarks,
    # snapshots, sources). Centralised so future renames are a 1-line change.
    REDIS_KEY_PREFIX: str = "akopia"
