"""GitAdapter — Git repository source.

Provider-abstracted replacement for the old ``adapter_git`` pod. Speaks
to GitHub, GitLab, and Gitea through a small ``_Provider`` strategy;
everything else (clone, fetch, diff) is vanilla ``git`` subprocess
invocation and therefore provider-agnostic.

Two deployment shapes:

1. ``org: my-org`` — the adapter hits the provider's REST API to
   enumerate every repo visible under the org, then clones each one.
2. ``repos: [url, url, ...]`` — explicit list; no API call needed, no
   token required for public repos. Useful for air-gapped installs or
   when the token scope doesn't include org listing.

``watch()`` clones on first pass (shallow-free so we can diff across
commits), then polls every ``poll_seconds``: ``git fetch`` + ``git diff``
on the branch, one ChangeEvent per changed file, each stamped with the
new ``commit_sha`` and a ``ContentRef(kind="path", ...)`` pointing at
the file on the local clone filesystem. Include/exclude globs are
applied before emission; files larger than ``max_file_bytes`` are
skipped. Modality is inferred by extension via ``_modality_for``.

Non-goals in this class: webhooks, submodules, LFS bandwidth tuning,
git-over-SSH with agent forwarding. The RFC documents the post-launch
roadmap for those.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)


_DEFAULT_POLL_SECONDS = 300
_DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB
_DEFAULT_CACHE_DIR = "/tmp/akopia-repos"
_DEFAULT_CONCURRENCY = 3
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 10


#: Extension → Modality map. Same shape as the folder adapter's, but
#: extended with source-code extensions that are common in git repos.
_MODALITY_BY_EXT: dict[str, Modality] = {
    ".md": Modality.TEXT,
    ".txt": Modality.TEXT,
    ".rst": Modality.TEXT,
    ".py": Modality.TEXT,
    ".js": Modality.TEXT,
    ".ts": Modality.TEXT,
    ".tsx": Modality.TEXT,
    ".jsx": Modality.TEXT,
    ".go": Modality.TEXT,
    ".rs": Modality.TEXT,
    ".java": Modality.TEXT,
    ".rb": Modality.TEXT,
    ".c": Modality.TEXT,
    ".cpp": Modality.TEXT,
    ".h": Modality.TEXT,
    ".hpp": Modality.TEXT,
    ".sh": Modality.TEXT,
    ".json": Modality.TEXT,
    ".yaml": Modality.TEXT,
    ".yml": Modality.TEXT,
    ".toml": Modality.TEXT,
    ".html": Modality.TEXT,
    ".css": Modality.TEXT,
    ".xml": Modality.TEXT,
    ".sql": Modality.TEXT,
    # PDF flows as TEXT after extraction. Audio/video aren't wired
    # end-to-end; extending the enum is step 1 of adding-a-modality.md.
    ".pdf": Modality.TEXT,
    ".jpg": Modality.IMAGE,
    ".jpeg": Modality.IMAGE,
    ".png": Modality.IMAGE,
    ".gif": Modality.IMAGE,
    ".webp": Modality.IMAGE,
}


def _modality_for(path: str) -> Modality:
    ext = Path(path).suffix.lower()
    return _MODALITY_BY_EXT.get(ext, Modality.TEXT)


def _matches_any(path: str, patterns: list[str]) -> bool:
    name = Path(path).name
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


# ── Provider strategy ─────────────────────────────────────────────

class _Provider:
    """Strategy for turning an (org) into a list of clone URLs.

    Subclasses differ only in the REST endpoint and JSON shape of the
    response. They are intentionally thin wrappers around ``httpx``; the
    test suite substitutes them with mocked ``httpx`` calls rather than
    faking the ``_Provider`` itself, so the HTTP contract is exercised.
    """

    name: str = ""
    default_base_url: str = ""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def list_repos_url(self, org: str) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def extract_clone_urls(self, payload: object) -> list[str]:
        """Pull ``clone_url`` (or provider equivalent) out of the JSON."""
        raise NotImplementedError


class _GitHubProvider(_Provider):
    name = "github"
    default_base_url = "https://api.github.com"

    def list_repos_url(self, org: str) -> str:
        return f"{self.base_url}/orgs/{org}/repos?per_page=100"

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def extract_clone_urls(self, payload: object) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [r["clone_url"] for r in payload if "clone_url" in r]


class _GitLabProvider(_Provider):
    name = "gitlab"
    default_base_url = "https://gitlab.com"

    def list_repos_url(self, org: str) -> str:
        # GitLab groups use a path-based URL scheme.
        return f"{self.base_url}/api/v4/groups/{org}/projects?per_page=100"

    def headers(self) -> dict[str, str]:
        h = {}
        if self.token:
            h["PRIVATE-TOKEN"] = self.token
        return h

    def extract_clone_urls(self, payload: object) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [r["http_url_to_repo"] for r in payload if "http_url_to_repo" in r]


class _GiteaProvider(_Provider):
    name = "gitea"
    # Gitea is self-hosted; there is no universal default. Callers must
    # supply ``base_url`` (or ``GITEA_URL``) pointing at their instance.
    default_base_url = ""

    def list_repos_url(self, org: str) -> str:
        return f"{self.base_url}/api/v1/orgs/{org}/repos?limit=50"

    def headers(self) -> dict[str, str]:
        h = {}
        if self.token:
            h["Authorization"] = f"token {self.token}"
        return h

    def extract_clone_urls(self, payload: object) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [r["clone_url"] for r in payload if "clone_url" in r]


_PROVIDERS: dict[str, type[_Provider]] = {
    "github": _GitHubProvider,
    "gitlab": _GitLabProvider,
    "gitea": _GiteaProvider,
}


def _build_provider(name: str, base_url: Optional[str], token: str) -> _Provider:
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown git provider {name!r}; expected one of {sorted(_PROVIDERS)}"
        )
    cls = _PROVIDERS[name]
    return cls(base_url or cls.default_base_url, token)


# ── Git subprocess helpers ────────────────────────────────────────

def _run_git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def _inject_token(url: str, token: str) -> str:
    """Inject a bearer token into an HTTPS clone URL for CI-style auth."""
    if not token or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:  # already authenticated
        return url
    return f"{scheme}://oauth2:{token}@{rest}"


# ── Adapter ───────────────────────────────────────────────────────

class GitAdapter(BaseSourceAdapter):
    """Source adapter for git repositories on any supported provider."""

    plugin_id = "git"

    async def configure(self, config: dict) -> None:
        self.provider_name: str = config.get("provider", "gitea")
        self.base_url: Optional[str] = config.get("base_url")
        self.token: str = config.get("token", "") or ""
        self.org: Optional[str] = config.get("org")
        self.explicit_repos: list[str] = list(config.get("repos") or [])
        self.branch: str = config.get("branch", "main")

        self.include_globs: list[str] = list(config.get("include_globs") or ["*"])
        self.exclude_globs: list[str] = list(config.get("exclude_globs") or [])
        self.poll_seconds: int = int(config.get("poll_seconds", _DEFAULT_POLL_SECONDS))
        self.max_file_bytes: int = int(
            config.get("max_file_bytes", _DEFAULT_MAX_FILE_BYTES)
        )
        self.cache_dir: Path = Path(
            config.get("cache_dir", _DEFAULT_CACHE_DIR)
        )

        concurrency = int(config.get("concurrency", _DEFAULT_CONCURRENCY))
        if concurrency < _MIN_CONCURRENCY or concurrency > _MAX_CONCURRENCY:
            raise ValueError(
                f"GitAdapter config 'concurrency' must be between "
                f"{_MIN_CONCURRENCY} and {_MAX_CONCURRENCY} (got {concurrency})"
            )
        self.concurrency: int = concurrency
        # Bound concurrent clone/fetch git subprocesses across sources.
        # Created here (not lazily) so each configured adapter instance
        # has exactly one semaphore shared across its per-source tasks.
        self._clone_semaphore: asyncio.Semaphore = asyncio.Semaphore(concurrency)

        if not self.org and not self.explicit_repos:
            raise ValueError(
                "GitAdapter config requires either 'org' or 'repos' to be set"
            )

        self._provider = _build_provider(
            self.provider_name, self.base_url, self.token
        )
        # Per-source last-seen commit: {source_id: sha}
        self._watermarks: dict[str, str] = {}

    async def discover(self) -> AsyncIterator[Source]:
        urls: list[str] = list(self.explicit_repos)
        if self.org:
            urls.extend(await self._list_org_repos(self.org))

        # Dedupe while preserving order.
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            name = _repo_name_from_url(url)
            source_id = f"{self.instance_id}:{name}"
            yield Source(
                source_id=source_id,
                type="git",
                name=name,
                url=url,
                branch=self.branch,
            )

    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        repo_path = self._repo_path(source)
        first_pass = True
        while not self._shutdown.is_set():
            if not first_pass:
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self.poll_seconds
                    )
                    break
                except asyncio.TimeoutError:
                    pass
            first_pass = False

            # Gate concurrent clones/fetches with a shared semaphore so
            # the watch tasks spawned one-per-source don't all hit the
            # provider's clone endpoint (and local disk) at once. The
            # subprocess call itself is blocking, so we off-load it to
            # a worker thread — otherwise the event loop stalls and
            # the semaphore is meaningless.
            try:
                logger.info("git adapter: clone/fetch starting repo=%s", source.name)
                async with self._clone_semaphore:
                    await asyncio.to_thread(
                        self._clone_or_fetch, source, repo_path
                    )
                logger.info("git adapter: clone/fetch done repo=%s", source.name)
            except Exception as e:
                logger.error(
                    "git adapter: clone/fetch failed repo=%s source_id=%s: %s",
                    source.name, source.source_id, e,
                )
                continue

            new_head = _run_git(repo_path, "rev-parse", f"origin/{self.branch}").strip()
            prev_head = self._watermarks.get(source.source_id)
            if prev_head == new_head:
                continue

            for change in self._diff(repo_path, prev_head, new_head):
                relpath = change["path"]

                # Respect include / exclude globs.
                if self.exclude_globs and _matches_any(relpath, self.exclude_globs):
                    continue
                if not _matches_any(relpath, self.include_globs):
                    continue

                op = change["op"]
                modality = _modality_for(relpath)
                abs_path = repo_path / relpath

                if op == Operation.DELETE:
                    event = self._make_change_event(
                        path=relpath,
                        operation=op,
                        modality=modality,
                    )
                    event.commit_sha = new_head
                    event.compute_idempotency_key()
                    yield event
                    continue

                # Size check.
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    continue
                if size > self.max_file_bytes:
                    logger.info(
                        "git adapter skipping %s (%d > max %d)",
                        relpath, size, self.max_file_bytes,
                    )
                    continue

                content_hash = _hash_file(abs_path)
                event = self._make_change_event(
                    path=relpath,
                    operation=op,
                    modality=modality,
                    content_hash=content_hash,
                    size_bytes=size,
                    content_ref=ContentRef(kind="path", path=str(abs_path)),
                )
                event.commit_sha = new_head
                # Per-file commit time (last commit touching this path).
                # Precise but subprocess-heavy; acceptable because diffs
                # typically touch few files per poll. Never blocks.
                event.content_modified_at = _last_commit_time_for_path(
                    repo_path, relpath
                )
                # Recompute idempotency with commit_sha in the mix so
                # different commits of the same file don't collide.
                event.compute_idempotency_key()
                yield event

            self._watermarks[source.source_id] = new_head

    async def read(self, source: Source, path: str) -> bytes:
        repo_path = self._repo_path(source)
        abs_path = (repo_path / path).resolve()
        try:
            abs_path.relative_to(repo_path.resolve())
        except ValueError as e:
            raise ValueError(f"path escapes repo: {path}") from e
        return abs_path.read_bytes()

    # ── helpers ───────────────────────────────────────────────────

    def _repo_path(self, source: Source) -> Path:
        # Safe directory name — colons are fine on Linux, but keep it
        # simple.
        safe = source.source_id.replace("/", "_").replace(":", "_")
        return self.cache_dir / safe

    async def _list_org_repos(self, org: str) -> list[str]:
        url = self._provider.list_repos_url(org)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._provider.headers())
            resp.raise_for_status()
            return self._provider.extract_clone_urls(resp.json())

    def _clone_or_fetch(self, source: Source, repo_path: Path) -> None:
        """Ensure repo is cloned and working tree matches ``origin/<branch>``.

        On the polling path we ``fetch`` + hard-reset the working tree so
        subsequent reads (``stat``, ``sha256``) see the latest bytes. A
        plain ``fetch`` would advance ``origin/<branch>`` but leave the
        working tree stale — diff would report an added file whose bytes
        are not yet on disk.
        """
        url = _inject_token(source.url or "", self.token)
        if (repo_path / ".git").exists():
            _run_git(repo_path, "fetch", "origin", self.branch)
            _run_git(repo_path, "reset", "--hard", f"origin/{self.branch}")
            return
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--branch", self.branch, url, str(repo_path)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone {source.url} failed: {result.stderr.strip()}"
            )

    def _diff(
        self, repo_path: Path, prev: Optional[str], new: str
    ) -> list[dict]:
        """Return [{op: Operation, path: str, old_path: str|None}]."""
        if prev is None:
            # First pass: everything in the tree is an ADD.
            output = _run_git(repo_path, "ls-tree", "-r", "--name-only", new).strip()
            if not output:
                return []
            return [
                {"op": Operation.ADD, "path": p}
                for p in output.splitlines() if p
            ]

        output = _run_git(
            repo_path, "diff", "--name-status", prev, new
        ).strip()
        if not output:
            return []

        op_map = {
            "A": Operation.ADD,
            "M": Operation.MODIFY,
            "D": Operation.DELETE,
            "R": Operation.RENAME,
        }
        changes: list[dict] = []
        for line in output.splitlines():
            parts = line.split("\t")
            marker = parts[0][0]  # R100 → R, A → A
            op = op_map.get(marker)
            if op is None:
                continue
            if op is Operation.RENAME and len(parts) >= 3:
                changes.append({"op": op, "path": parts[2], "old_path": parts[1]})
            elif len(parts) >= 2:
                changes.append({"op": op, "path": parts[1]})
        return changes


# ── helpers (module-level) ────────────────────────────────────────

def _repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "repo"


def _last_commit_time_for_path(repo_path: Path, relpath: str) -> Optional[datetime]:
    """Return the committer time of the last commit touching ``relpath``.

    Uses ``git log -1 --format=%cI -- <relpath>``. Parsed as a tz-aware
    datetime. Returns ``None`` on any failure — freshness metadata is
    best-effort per the RFC.
    """
    try:
        out = _run_git(
            repo_path, "log", "-1", "--format=%cI", "--", relpath, check=False
        ).strip()
    except Exception as e:
        logger.debug("git log for %s failed: %s", relpath, e)
        return None
    if not out:
        return None
    try:
        # Python 3.11+ handles ISO 8601 with timezone offset directly.
        return datetime.fromisoformat(out)
    except ValueError:
        logger.debug("git log produced unparseable time for %s: %r", relpath, out)
        return None


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["GitAdapter"]
