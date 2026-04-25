"""S3Adapter — S3 / S3-compatible object storage source.

Reads documents from any S3-compatible bucket (AWS S3, MinIO, Ceph
RGW, Wasabi, Backblaze B2 with S3 API, …) and emits ``ChangeEvent``
records on the same change-events stream as the rest of Akopia. The
sister project Atalaya writes incident closure JSON files to a MinIO
bucket; configuring this adapter against that bucket lets Akopia index
those closures and surface them on later semantic searches. Anyone
with documents in S3 (data lake, drop bucket, exports from another
tool) gets the same ingestion path for free.

Behaviour mirrors :class:`adapters.folder.FolderAdapter` semantically:

1. ``discover()`` yields a single ``Source`` rooted at
   ``s3://{bucket}/{prefix}``.
2. ``watch()`` polls the bucket every ``poll_seconds``, paginates
   through ``list_objects_v2`` under ``prefix``, applies include /
   exclude globs over the relative key, and maintains a
   ``{relkey: (last_modified, size, etag)}`` map to detect
   add / modify / delete. The S3 ``ETag`` is the primary identity
   signal (S3 returns the MD5 of the object body for non-multipart
   uploads); ``last_modified + size`` are kept as a fallback because
   multipart uploads use a different ETag scheme that still changes on
   every overwrite. The ``sha256`` content hash is computed lazily
   only on add / modify by downloading the object once.
3. ``read()`` downloads the object's bytes via ``get_object``. Honours
   ``max_object_bytes`` (skip with WARNING).

Modality detection is by extension, copying ``_MODALITY_BY_EXT`` from
``adapters.folder`` so behaviour is identical regardless of where the
bytes live.

Why ``aioboto3``: the adapter loop is async; spinning up sync boto3
calls in a thread pool would block the event loop on each poll. We
use ``aioboto3`` for ``list_objects_v2``, ``get_object``, and
``head_object``. The session is constructed once at ``configure()``
time but the client context manager is opened per-operation — this
matches the Atalaya MinIO wrapper and avoids stale credentials in
long-running processes.

Config keys:

- ``endpoint_url``     (str, required) — e.g. ``http://minio:9000``
                         or ``https://s3.amazonaws.com``
- ``bucket``           (str, required)
- ``prefix``           (str, default ``""``) — only ingest objects
                         whose key starts with this prefix
- ``access_key``       (str, required)
- ``secret_key``       (str, required)
- ``region``           (str, default ``"us-east-1"``) — boto3 needs a
                         region even for MinIO
- ``use_ssl``          (bool, default ``True``)
- ``include``          (list[str], default ``["*"]``) — globs over
                         the key relative to ``prefix``
- ``exclude``          (list[str], default ``[]``)
- ``poll_seconds``     (int, default 300)
- ``max_object_bytes`` (int, default 25 MiB) — objects above this are
                         skipped with a WARNING
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import AsyncIterator, Optional

import aioboto3

from adapters.folder import _MODALITY_BY_EXT  # re-use the same map
from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)


_DEFAULT_MAX_OBJECT_BYTES = 25 * 1024 * 1024  # 25 MiB
_DEFAULT_POLL_SECONDS = 300
_DEFAULT_REGION = "us-east-1"


def _modality_for(key: str) -> Modality:
    """Map an object key extension to a Modality.

    Re-uses the FolderAdapter table so the routing decision doesn't
    depend on storage backend. Unknown extensions default to TEXT
    (lossy fallback is better than silently dropping the object).
    """
    ext = PurePosixPath(key).suffix.lower()
    return _MODALITY_BY_EXT.get(ext, Modality.TEXT)


def _matches_any(relkey: str, patterns: list[str]) -> bool:
    """Match if any glob pattern (basename or full key) matches."""
    name = PurePosixPath(relkey).name
    for pat in patterns:
        if fnmatch.fnmatch(relkey, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def _strip_etag(etag: Optional[str]) -> Optional[str]:
    """S3 returns ETag wrapped in double quotes — strip them."""
    if etag is None:
        return None
    return etag.strip('"')


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly naive datetime into a tz-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class S3Adapter(BaseSourceAdapter):
    """Polling source adapter for S3 / S3-compatible buckets."""

    plugin_id = "s3"

    async def configure(self, config: dict) -> None:
        for required in ("endpoint_url", "bucket", "access_key", "secret_key"):
            if not config.get(required):
                raise ValueError(
                    f"S3Adapter config missing required key {required!r}"
                )

        self.endpoint_url: str = str(config["endpoint_url"])
        self.bucket: str = str(config["bucket"])
        self.prefix: str = str(config.get("prefix") or "")
        self.access_key: str = str(config["access_key"])
        self.secret_key: str = str(config["secret_key"])
        self.region: str = str(config.get("region") or _DEFAULT_REGION)
        self.use_ssl: bool = bool(config.get("use_ssl", True))

        self.include: list[str] = list(config.get("include") or ["*"])
        self.exclude: list[str] = list(config.get("exclude") or [])
        self.poll_seconds: int = int(
            config.get("poll_seconds", _DEFAULT_POLL_SECONDS)
        )
        self.max_object_bytes: int = int(
            config.get("max_object_bytes", _DEFAULT_MAX_OBJECT_BYTES)
        )

        # Per-source last-seen state: {relkey: (last_modified, size, etag)}.
        # ETag is primary identity, mtime+size is the fallback (multipart
        # uploads use a different ETag scheme).
        self._state: dict[str, tuple[Optional[datetime], int, Optional[str]]] = {}

        # aioboto3 session — re-created cheaply, but stored once so
        # tests can monkey-patch _session_factory without touching env.
        self._session = aioboto3.Session(
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
        )

        logger.info(
            "s3 adapter configured bucket=%s prefix=%s endpoint=%s",
            self.bucket, self.prefix, self.endpoint_url,
        )

    # ── Internal client factory (overridable for tests) ────────────

    def _client(self):
        """Open a fresh aioboto3 S3 client context manager.

        Returns the unentered async-context-manager so callers do
        ``async with self._client() as s3:``. Tests can monkey-patch
        this to inject a stub client.
        """
        return self._session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            use_ssl=self.use_ssl,
            verify=self.use_ssl,
        )

    # ── Adapter surface ────────────────────────────────────────────

    async def discover(self) -> AsyncIterator[Source]:
        uri = f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"
        yield Source(
            source_id=self.instance_id,
            type=self.plugin_id,
            name=uri,
            url=uri,
        )

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        """Poll the bucket and yield ChangeEvents on add/modify/delete."""
        first_pass = True
        while not self._shutdown.is_set():
            if not first_pass:
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self.poll_seconds
                    )
                    break  # shutdown signalled during wait
                except asyncio.TimeoutError:
                    pass
            first_pass = False

            logger.debug(
                "s3 adapter polling bucket=%s prefix=%s",
                self.bucket, self.prefix,
            )
            try:
                current = await self._scan()
            except Exception:
                # _watch_loop in the base class will log + back off.
                raise

            # Adds + modifies (yielded inside helper because read may
            # need an open client for the hash download).
            for relkey, (last_modified, size, etag) in current.items():
                prev = self._state.get(relkey)
                if prev is None:
                    event = await self._event_for_upsert(
                        relkey, size, last_modified, etag, Operation.ADD
                    )
                    if event is not None:
                        yield event
                else:
                    prev_lm, prev_size, prev_etag = prev
                    changed = (
                        (etag is not None and etag != prev_etag)
                        or (last_modified != prev_lm)
                        or (size != prev_size)
                    )
                    if changed:
                        event = await self._event_for_upsert(
                            relkey, size, last_modified, etag, Operation.MODIFY
                        )
                        if event is not None:
                            yield event

            # Deletes.
            gone = set(self._state.keys()) - set(current.keys())
            for relkey in gone:
                yield self._make_change_event(
                    path=relkey,
                    operation=Operation.DELETE,
                    modality=_modality_for(relkey),
                    size_bytes=0,
                )

            self._state = current

    async def read(self, source: Source, path: str) -> bytes:
        """Download the object at ``path`` (relative to ``prefix``).

        Honours ``max_object_bytes``: oversized objects log a WARNING
        and return ``b""`` instead of materialising the body.
        """
        key = self._abs_key(path)
        async with self._client() as s3:
            head = await s3.head_object(Bucket=self.bucket, Key=key)
            size = int(head.get("ContentLength") or 0)
            if size > self.max_object_bytes:
                logger.warning(
                    "s3 adapter skipping %s (%d bytes > max %d)",
                    key, size, self.max_object_bytes,
                )
                return b""
            resp = await s3.get_object(Bucket=self.bucket, Key=key)
            body = await resp["Body"].read()
        return body

    # ── Helpers ────────────────────────────────────────────────────

    def _abs_key(self, relkey: str) -> str:
        """Join ``prefix`` and a relative key into an absolute S3 key."""
        if not self.prefix:
            return relkey
        if self.prefix.endswith("/"):
            return f"{self.prefix}{relkey}"
        return f"{self.prefix}/{relkey}"

    def _relkey(self, abskey: str) -> str:
        """Strip ``prefix`` (and a trailing ``/``) from an absolute key."""
        if not self.prefix:
            return abskey
        pfx = self.prefix if self.prefix.endswith("/") else self.prefix + "/"
        if abskey.startswith(pfx):
            return abskey[len(pfx):]
        if abskey == self.prefix:
            return ""
        return abskey

    async def _scan(
        self,
    ) -> dict[str, tuple[Optional[datetime], int, Optional[str]]]:
        """List all objects under ``prefix``; return relkey → (mtime, size, etag).

        Applies include / exclude globs and the ``max_object_bytes``
        cap (oversized objects are filtered out the same way the
        FolderAdapter filters oversized files).
        """
        result: dict[str, tuple[Optional[datetime], int, Optional[str]]] = {}
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            kwargs = {"Bucket": self.bucket}
            if self.prefix:
                kwargs["Prefix"] = self.prefix
            async for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents") or []:
                    abskey = obj.get("Key") or ""
                    if not abskey or abskey.endswith("/"):
                        continue  # skip pseudo-folder markers
                    relkey = self._relkey(abskey)
                    if not relkey:
                        continue
                    if self.exclude and _matches_any(relkey, self.exclude):
                        continue
                    if not _matches_any(relkey, self.include):
                        continue
                    size = int(obj.get("Size") or 0)
                    if size > self.max_object_bytes:
                        logger.warning(
                            "s3 adapter skipping %s (%d bytes > max %d)",
                            abskey, size, self.max_object_bytes,
                        )
                        continue
                    last_modified = _to_utc(obj.get("LastModified"))
                    etag = _strip_etag(obj.get("ETag"))
                    result[relkey] = (last_modified, size, etag)
        return result

    async def _event_for_upsert(
        self,
        relkey: str,
        size: int,
        last_modified: Optional[datetime],
        etag: Optional[str],
        op: Operation,
    ) -> ChangeEvent | None:
        """Build an ADD/MODIFY event with a lazily-computed sha256 hash.

        Downloads the object body once to compute the hash. Returns
        ``None`` if the body cannot be retrieved (logged as a WARNING)
        — keeping watch() resilient to transient S3 errors instead of
        nuking the whole poll cycle.
        """
        abskey = self._abs_key(relkey)
        try:
            content_hash = await self._hash_object(abskey)
        except Exception as e:
            logger.warning("s3 adapter could not hash %s: %s", abskey, e)
            return None
        event = self._make_change_event(
            path=relkey,
            operation=op,
            modality=_modality_for(relkey),
            content_hash=content_hash,
            size_bytes=size,
            content_ref=ContentRef(
                kind="object_storage",
                bucket=self.bucket,
                key=abskey,
            ),
        )
        event.content_modified_at = last_modified
        return event

    async def _hash_object(self, abskey: str) -> str:
        """Stream the object body and return its sha256 hex digest."""
        h = hashlib.sha256()
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self.bucket, Key=abskey)
            body = resp["Body"]
            # aiobotocore StreamingBody supports async ``read`` and
            # async iteration; read in 64 KiB chunks to stay friendly
            # to large objects without requiring iter_chunks support.
            while True:
                chunk = await body.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()


__all__ = ["S3Adapter"]
