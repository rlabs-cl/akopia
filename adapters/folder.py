"""FolderAdapter — filesystem path source.

Replaces the out-of-tree ``adapter-nas`` pod with an in-tree plugin that
watches any filesystem path (local disk, NFS mount, CIFS mount, bind-
mounted volume). All NAS-specific plumbing lives at the infrastructure
layer (how the mount is wired into the pod); from the platform's POV
there is no such thing as a "NAS" — it's just a folder.

Behaviour:

1. ``discover()`` yields a single ``Source`` rooted at ``path``.
2. ``watch()`` polls the tree every ``poll_seconds``, applies include /
   exclude globs, and maintains a ``{path: (mtime_ns, size)}`` map to
   detect add / modify / delete. sha256 content hash is computed lazily
   only for add / modify events (delete doesn't need bytes).
3. ``read()`` returns the file's bytes.

Modality detection is by extension against a small, explicit constant
map (``_MODALITY_BY_EXT``). Unknown extensions default to TEXT because
it is the only modality whose extractor tolerates arbitrary bytes
(lossy fallback is better than silently dropping the file).

Config keys:

- ``path``           (str, required)
- ``include``        (list[str], default ``["*"]``)
- ``exclude``        (list[str], default ``[]``)
- ``poll_seconds``   (int, default 300)
- ``max_file_bytes`` (int, default 25 MiB) — files above this are skipped
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)


#: Extension → Modality. Kept small and explicit on purpose; the router
#: decides which extractor runs for each modality. Anything not in the
#: map defaults to TEXT (see ``_modality_for``).
_MODALITY_BY_EXT: dict[str, Modality] = {
    # Text-ish
    ".md": Modality.TEXT,
    ".txt": Modality.TEXT,
    ".py": Modality.TEXT,
    ".json": Modality.TEXT,
    ".yaml": Modality.TEXT,
    ".yml": Modality.TEXT,
    # PDF flows as TEXT once extracted (pdf-text extractor emits text).
    ".pdf": Modality.TEXT,
    # Images
    ".jpg": Modality.IMAGE,
    ".jpeg": Modality.IMAGE,
    ".png": Modality.IMAGE,
    ".gif": Modality.IMAGE,
    ".webp": Modality.IMAGE,
    # Audio/video are not wired end-to-end; extending Modality is step 1
    # of docs/adding-a-modality.md.
}

_DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB
_DEFAULT_POLL_SECONDS = 300


def _modality_for(path: str) -> Modality:
    ext = Path(path).suffix.lower()
    return _MODALITY_BY_EXT.get(ext, Modality.TEXT)


def _file_mtime_utc(path: Path) -> Optional[datetime]:
    """Return the file's last-modified timestamp as a tz-aware UTC datetime.

    Returns ``None`` on any OS error — freshness metadata is best-effort
    and must never block the adapter.
    """
    try:
        ns = path.stat().st_mtime_ns
    except OSError:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def _matches_any(relpath: str, patterns: list[str]) -> bool:
    """Match if any glob pattern (basename or full path) matches."""
    name = Path(relpath).name
    for pat in patterns:
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


class FolderAdapter(BaseSourceAdapter):
    """Polling source adapter for a local filesystem subtree."""

    plugin_id = "folder"

    async def configure(self, config: dict) -> None:
        if "path" not in config:
            raise ValueError("FolderAdapter config missing required key 'path'")
        self.root: Path = Path(config["path"]).resolve()
        if not self.root.exists():
            raise ValueError(f"FolderAdapter path does not exist: {self.root}")
        if not self.root.is_dir():
            raise ValueError(f"FolderAdapter path is not a directory: {self.root}")

        self.include: list[str] = list(config.get("include") or ["*"])
        self.exclude: list[str] = list(config.get("exclude") or [])
        self.poll_seconds: int = int(config.get("poll_seconds", _DEFAULT_POLL_SECONDS))
        self.max_file_bytes: int = int(
            config.get("max_file_bytes", _DEFAULT_MAX_FILE_BYTES)
        )

        # Per-source last-seen state: {relpath: (mtime_ns, size)}
        self._state: dict[str, tuple[int, int]] = {}

    async def discover(self) -> AsyncIterator[Source]:
        yield Source(
            source_id=self.instance_id,
            type="folder",
            name=str(self.root),
            folder_path=str(self.root),
        )

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        # First pass runs immediately; subsequent passes wait poll_seconds.
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

            current = self._scan()

            # Detect adds + modifies
            for relpath, (mtime, size) in current.items():
                prev = self._state.get(relpath)
                if prev is None:
                    event = self._event_for_upsert(
                        relpath, size, Operation.ADD
                    )
                    if event is not None:
                        yield event
                elif prev != (mtime, size):
                    event = self._event_for_upsert(
                        relpath, size, Operation.MODIFY
                    )
                    if event is not None:
                        yield event

            # Detect deletes
            gone = set(self._state.keys()) - set(current.keys())
            for relpath in gone:
                yield self._make_change_event(
                    path=relpath,
                    operation=Operation.DELETE,
                    modality=_modality_for(relpath),
                    size_bytes=0,
                )

            self._state = current

    async def read(self, source: Source, path: str) -> bytes:
        abs_path = (self.root / path).resolve()
        # Guard against ``..`` escape.
        try:
            abs_path.relative_to(self.root)
        except ValueError as e:
            raise ValueError(f"path escapes root: {path}") from e
        return abs_path.read_bytes()

    # ── helpers ───────────────────────────────────────────────────

    def _scan(self) -> dict[str, tuple[int, int]]:
        """Walk ``self.root`` and return {relpath: (mtime_ns, size)}."""
        result: dict[str, tuple[int, int]] = {}
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.root))
            if self.exclude and _matches_any(rel, self.exclude):
                continue
            if not _matches_any(rel, self.include):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > self.max_file_bytes:
                logger.info(
                    "folder adapter skipping %s (%d bytes > max %d)",
                    rel, st.st_size, self.max_file_bytes,
                )
                continue
            result[rel] = (st.st_mtime_ns, st.st_size)
        return result

    def _event_for_upsert(
        self, relpath: str, size: int, op: Operation
    ) -> ChangeEvent | None:
        """Build an ADD/MODIFY event with a lazily-computed sha256 hash."""
        abs_path = self.root / relpath
        try:
            content_hash = self._hash_file(abs_path)
        except OSError as e:
            logger.warning("folder adapter could not hash %s: %s", relpath, e)
            return None
        # Freshness metadata: file's own mtime. Failure is non-fatal.
        content_modified_at = _file_mtime_utc(abs_path)
        event = self._make_change_event(
            path=relpath,
            operation=op,
            modality=_modality_for(relpath),
            content_hash=content_hash,
            size_bytes=size,
            content_ref=ContentRef(kind="path", path=str(abs_path)),
        )
        event.content_modified_at = content_modified_at
        return event

    @staticmethod
    def _file_mtime_utc_static(path: Path) -> Optional[datetime]:
        return _file_mtime_utc(path)

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


__all__ = ["FolderAdapter"]
