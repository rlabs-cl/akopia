"""Entry point for the adapter-s3 pod.

Reads ``S3_*`` env vars (matching the pattern used by other Akopia
deployments — env-driven via ConfigMap + Secret, no akopia.yaml mounted)
and runs an ``S3Adapter`` instance forever. SIGTERM / SIGINT trigger a
clean shutdown via the BaseSourceAdapter lifecycle.

Mirrors the role of ``akopia run adapter <id>`` from
``scripts/akopia_cli.py`` but for the env-only deployment shape — the
two paths share the same adapter implementation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# Ensure the akopia repo is on PYTHONPATH when this script is invoked
# directly inside the container (``python -m scripts.run_adapter_s3``).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.s3 import S3Adapter  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("adapter-s3")


def _config_from_env() -> dict:
    """Build the adapter config dict from S3_* env vars.

    Mirrors the env surface other Akopia adapter pods expose
    (ConfigMap + Secret). Kept inline here rather than on
    ``adapters.s3`` so the library stays decoupled from the
    deployment shape.
    """
    use_ssl_raw = os.environ.get("S3_USE_SSL")
    return {
        "endpoint_url": os.environ.get("S3_ENDPOINT_URL", ""),
        "bucket": os.environ.get("S3_BUCKET", ""),
        "prefix": os.environ.get("S3_PREFIX", ""),
        "access_key": os.environ.get("S3_ACCESS_KEY", ""),
        "secret_key": os.environ.get("S3_SECRET_KEY", ""),
        "region": os.environ.get("S3_REGION", "us-east-1"),
        "use_ssl": (
            use_ssl_raw.lower() in ("1", "true", "yes")
            if use_ssl_raw is not None
            else True
        ),
        "poll_seconds": int(os.environ.get("S3_POLL_SECONDS", "300")),
        "max_object_bytes": int(
            os.environ.get("S3_MAX_OBJECT_BYTES", str(25 * 1024 * 1024))
        ),
    }


def main() -> int:
    instance_id = os.environ.get("S3_INSTANCE_ID", "atalaya-closures")
    config = _config_from_env()
    missing = [
        k for k in ("endpoint_url", "bucket", "access_key", "secret_key")
        if not config.get(k)
    ]
    if missing:
        logger.error(
            "adapter-s3 missing required env (S3_ENDPOINT_URL/S3_BUCKET/"
            "S3_ACCESS_KEY/S3_SECRET_KEY); missing=%s",
            missing,
        )
        return 2

    logger.info(
        "adapter-s3 starting instance=%s endpoint=%s bucket=%s prefix=%s "
        "poll_seconds=%s",
        instance_id, config["endpoint_url"], config["bucket"],
        config.get("prefix") or "(none)", config["poll_seconds"],
    )
    adapter = S3Adapter(instance_id=instance_id)
    try:
        asyncio.run(adapter.start(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
