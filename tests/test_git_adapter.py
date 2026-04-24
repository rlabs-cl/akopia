"""Tests for adapters.git.GitAdapter (Slice 3).

Strategy: create a local bare repo + a seeding working-clone, then
point the adapter at the bare repo (file:// URL) so clone/fetch/diff
run the real git binary against real objects — no network, no flakes.

Provider abstraction is tested separately by mocking the HTTP call with
``respx`` so we never hit github.com.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from adapters.git import (
    GitAdapter,
    _GitHubProvider,
    _GitLabProvider,
    _GiteaProvider,
    _build_provider,
    _repo_name_from_url,
    _modality_for,
)
from common.models import ChangeEvent, Modality, Operation
from common.registry import PluginRegistry


# ── Reused FakeRedis pattern (see tests/test_base_adapter.py) ──────

class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def ensure_stream_and_group(self, stream: str, group: str) -> None:
        self.groups.append((stream, group))

    async def publish(self, stream: str, payload: dict) -> None:
        self.published.append((stream, payload))

    async def close(self) -> None:
        self.closed = True


# ── Repo fixture builder ───────────────────────────────────────────

def _run(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class _RepoFixture:
    """Bundle of bare repo path + seed working clone path."""

    def __init__(self, bare: Path, seed: Path):
        self.bare = bare
        self.seed = seed

    @property
    def url(self) -> str:
        return f"file://{self.bare}"


@pytest.fixture
def bare_repo(tmp_path: Path) -> _RepoFixture:
    """Create a bare repo + working seed clone with initial commit.

    Layout:
        tmp_path/bare        — bare repo (URL for the adapter)
        tmp_path/seed        — working clone used only to seed commits
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(bare), str(seed)],
        check=True, capture_output=True,
    )
    _run(seed, "checkout", "-B", "main")

    (seed / "README.md").write_text("# hello\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "intro.md").write_text("intro\n")
    _run(seed, "add", "-A")
    _run(seed, "commit", "-m", "initial")
    _run(seed, "push", "-u", "origin", "main")

    return _RepoFixture(bare, seed)


async def _collect_events(
    adapter: GitAdapter, source, timeout: float = 5.0
) -> list[ChangeEvent]:
    """Drive watch() for one pass and collect events."""
    events: list[ChangeEvent] = []

    async def _run_watch():
        async for ev in adapter.watch(source):
            events.append(ev)

    task = asyncio.create_task(_run_watch())
    await asyncio.sleep(0.1)
    adapter._shutdown.set()
    await asyncio.wait_for(task, timeout=timeout)
    return events


# ── Tests ──────────────────────────────────────────────────────────

class TestGitAdapterProviders:
    def test_default_provider_is_gitea(self):
        # Gitea is self-hosted — no universal default. Callers supply base_url.
        p = _build_provider("gitea", None, "tok")
        assert isinstance(p, _GiteaProvider)
        assert p.base_url == ""

    def test_github_provider_url(self):
        p = _build_provider("github", None, "abc")
        assert p.list_repos_url("acme") == (
            "https://api.github.com/orgs/acme/repos?per_page=100"
        )
        assert p.headers()["Authorization"] == "Bearer abc"

    def test_gitlab_provider_url(self):
        p = _build_provider("gitlab", None, "abc")
        assert "gitlab.com/api/v4/groups/acme/projects" in p.list_repos_url("acme")
        assert p.headers()["PRIVATE-TOKEN"] == "abc"

    def test_gitea_provider_url(self):
        p = _build_provider("gitea", "https://git.example", "abc")
        assert p.list_repos_url("acme") == (
            "https://git.example/api/v1/orgs/acme/repos?limit=50"
        )

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            _build_provider("bitbucket", None, "")

    def test_repo_name_from_url(self):
        assert _repo_name_from_url("https://x/foo/bar.git") == "bar"
        assert _repo_name_from_url("https://x/foo/bar") == "bar"
        assert _repo_name_from_url("file:///tmp/bare/") == "bare"

    def test_modality(self):
        assert _modality_for("x.py") is Modality.TEXT
        # PDFs flow as TEXT after extraction.
        assert _modality_for("x.pdf") is Modality.TEXT
        assert _modality_for("x.png") is Modality.IMAGE


class TestGitAdapterProviderDiscovery:
    """Provider abstraction exercised via mocked HTTP."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_github_provider_hits_github_api(self):
        route = respx.get(
            "https://api.github.com/orgs/acme/repos"
        ).mock(return_value=httpx.Response(
            200, json=[
                {"clone_url": "https://github.com/acme/repo-one.git"},
                {"clone_url": "https://github.com/acme/repo-two.git"},
            ],
        ))

        adapter = GitAdapter(instance_id="gh")
        await adapter.configure({
            "provider": "github",
            "token": "ghp_fake",
            "org": "acme",
        })
        sources = [s async for s in adapter.discover()]

        assert route.called
        # Authorization header shape: Bearer token
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer ghp_fake"
        assert [s.name for s in sources] == ["repo-one", "repo-two"]
        for s in sources:
            assert s.type == "git"

    @pytest.mark.asyncio
    @respx.mock
    async def test_gitea_provider_hits_gitea_api(self):
        respx.get(
            "https://git.lan/api/v1/orgs/rlabs/repos"
        ).mock(return_value=httpx.Response(
            200, json=[{"clone_url": "https://git.lan/rlabs/foo.git"}],
        ))

        adapter = GitAdapter(instance_id="gt")
        await adapter.configure({
            "provider": "gitea",
            "base_url": "https://git.lan",
            "token": "t",
            "org": "rlabs",
        })
        sources = [s async for s in adapter.discover()]
        assert [s.name for s in sources] == ["foo"]

    @pytest.mark.asyncio
    async def test_explicit_repos_no_api_call_needed(self, tmp_path):
        # No respx mock → any HTTP call would fail. Proves discover()
        # with an explicit repos list does not call the provider.
        adapter = GitAdapter(instance_id="x")
        await adapter.configure({
            "provider": "github",
            "repos": ["https://example/foo.git", "https://example/bar.git"],
        })
        sources = [s async for s in adapter.discover()]
        assert sorted(s.name for s in sources) == ["bar", "foo"]

    @pytest.mark.asyncio
    async def test_configure_requires_org_or_repos(self):
        adapter = GitAdapter(instance_id="x")
        with pytest.raises(ValueError):
            await adapter.configure({"provider": "github"})


class TestGitAdapterWatch:
    """End-to-end watch behaviour against a real local bare repo."""

    @pytest.mark.asyncio
    async def test_first_pass_emits_add_for_every_file(
        self, bare_repo: _RepoFixture, tmp_path: Path
    ):
        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
        })
        source = None
        async for s in adapter.discover():
            source = s
        events = await _collect_events(adapter, source)

        paths = sorted(e.path for e in events)
        assert paths == ["README.md", "docs/intro.md"]
        assert all(e.operation is Operation.ADD for e in events)
        assert all(e.commit_sha for e in events)
        assert all(e.content_ref and e.content_ref.kind == "path" for e in events)

    @pytest.mark.asyncio
    async def test_second_pass_detects_modify_and_delete(
        self, bare_repo: _RepoFixture, tmp_path: Path
    ):
        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
        })
        source = None
        async for s in adapter.discover():
            source = s

        # Pass 1: ADDs (two files).
        events1 = await _collect_events(adapter, source)
        assert len(events1) == 2

        # Make a second commit in the seed clone and push.
        seed = bare_repo.seed
        (seed / "README.md").write_text("# updated\n")
        (seed / "docs" / "intro.md").unlink()
        (seed / "new.md").write_text("new\n")
        _run(seed, "add", "-A")
        _run(seed, "commit", "-m", "update")
        _run(seed, "push", "origin", "main")

        # Reset shutdown for pass 2.
        adapter._shutdown = asyncio.Event()
        events2 = await _collect_events(adapter, source)

        by_path = {e.path: e for e in events2}
        assert by_path["README.md"].operation is Operation.MODIFY
        assert by_path["docs/intro.md"].operation is Operation.DELETE
        assert by_path["new.md"].operation is Operation.ADD

    @pytest.mark.asyncio
    async def test_include_exclude_globs(
        self, bare_repo: _RepoFixture, tmp_path: Path
    ):
        # Add a .log file to the seed, push.
        seed = bare_repo.seed
        (seed / "noisy.log").write_text("noise")
        (seed / "keep.md").write_text("keep")
        _run(seed, "add", "-A")
        _run(seed, "commit", "-m", "add log")
        _run(seed, "push", "origin", "main")

        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
            "include_globs": ["*.md"],
            "exclude_globs": ["docs/*"],
        })
        source = None
        async for s in adapter.discover():
            source = s

        events = await _collect_events(adapter, source)
        paths = sorted(e.path for e in events)
        # Only top-level .md files survive after include=["*.md"] and
        # exclude=["docs/*"]. README.md, keep.md.
        assert "README.md" in paths
        assert "keep.md" in paths
        assert "noisy.log" not in paths
        assert "docs/intro.md" not in paths

    @pytest.mark.asyncio
    async def test_max_file_bytes_enforced(
        self, bare_repo: _RepoFixture, tmp_path: Path
    ):
        seed = bare_repo.seed
        (seed / "huge.md").write_text("x" * 50_000)
        _run(seed, "add", "-A")
        _run(seed, "commit", "-m", "huge")
        _run(seed, "push", "origin", "main")

        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "max_file_bytes": 1000,
            "cache_dir": str(tmp_path / "cache"),
        })
        source = None
        async for s in adapter.discover():
            source = s
        events = await _collect_events(adapter, source)
        paths = [e.path for e in events]
        assert "huge.md" not in paths
        assert "README.md" in paths

    @pytest.mark.asyncio
    async def test_read_returns_bytes(self, bare_repo: _RepoFixture, tmp_path: Path):
        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
        })
        source = None
        async for s in adapter.discover():
            source = s

        # First trigger a clone via a watch pass so the repo exists.
        await _collect_events(adapter, source)

        data = await adapter.read(source, "README.md")
        assert data == b"# hello\n"

    @pytest.mark.asyncio
    async def test_publishes_via_base_with_fake_redis(
        self, bare_repo: _RepoFixture, tmp_path: Path
    ):
        redis = _FakeRedis()
        adapter = GitAdapter(instance_id="demo", redis_client=redis)

        async def _stopper():
            await asyncio.sleep(0.3)
            adapter._shutdown.set()
        stopper = asyncio.create_task(_stopper())
        await adapter.start({
            "provider": "gitea",
            "repos": [bare_repo.url],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
        })
        await stopper

        published = [p for s, p in redis.published if s == "change-events"]
        assert len(published) >= 2
        for ev in published:
            assert ev["source_id"] == "demo"
            assert ev["source_type"] == "git"
            assert ev["idempotency_key"] != ""
            assert ev["commit_sha"]

    def test_registry_manual_registration(self):
        r = PluginRegistry()
        r.register_source_adapter("git", GitAdapter)
        assert r.get_source_adapter_class("git") is GitAdapter


class TestGitAdapterConcurrency:
    """Semaphore-gated concurrent clones + config validation."""

    @pytest.mark.asyncio
    async def test_concurrency_default_is_three(self):
        adapter = GitAdapter(instance_id="x")
        await adapter.configure({"repos": ["https://example/a.git"]})
        assert adapter.concurrency == 3

    @pytest.mark.asyncio
    async def test_concurrency_out_of_range_raises(self):
        adapter = GitAdapter(instance_id="x")
        with pytest.raises(ValueError, match="concurrency"):
            await adapter.configure({
                "repos": ["https://example/a.git"],
                "concurrency": 0,
            })
        adapter2 = GitAdapter(instance_id="x")
        with pytest.raises(ValueError, match="concurrency"):
            await adapter2.configure({
                "repos": ["https://example/a.git"],
                "concurrency": 99,
            })

    @pytest.mark.asyncio
    async def test_clones_run_concurrently_bounded_by_semaphore(
        self, tmp_path: Path, monkeypatch,
    ):
        """Five repos, concurrency=2 → observed in-flight clones never
        exceeds 2, and at least 2 clones overlap in time (proving they're
        not serial). Uses a barrier + counter inside a monkeypatched
        ``_clone_or_fetch`` so the test is deterministic without real
        git processes.
        """
        import threading

        in_flight = 0
        max_in_flight = 0
        total_calls = 0
        lock = threading.Lock()
        ever_overlapped = threading.Event()
        # Barrier-style gate: each call bumps in_flight, sleeps briefly
        # so siblings have time to overlap, then decrements.

        def fake_clone(self, source, repo_path):
            nonlocal in_flight, max_in_flight, total_calls
            with lock:
                in_flight += 1
                total_calls += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
                if in_flight >= 2:
                    ever_overlapped.set()
            # Hold the semaphore slot long enough for overlaps.
            import time as _t
            _t.sleep(0.15)
            with lock:
                in_flight -= 1

        monkeypatch.setattr(GitAdapter, "_clone_or_fetch", fake_clone)

        adapter = GitAdapter(instance_id="demo")
        await adapter.configure({
            "repos": [f"https://example/repo{i}.git" for i in range(5)],
            "branch": "main",
            "poll_seconds": 3600,
            "cache_dir": str(tmp_path / "cache"),
            "concurrency": 2,
        })
        sources = [s async for s in adapter.discover()]
        assert len(sources) == 5

        # Drive one watch-pass per source concurrently. Each watch() in
        # GitAdapter calls _clone_or_fetch under the semaphore. We stop
        # each coroutine after the clone completes by setting shutdown,
        # so we don't also need to fake diff output — _run_git on a
        # non-existent repo will raise and the exception is caught
        # inside watch() (the logger.error path), loop exits on next
        # shutdown check.
        async def _drive(src):
            # Pull a single event if any, then stop.
            async for _ev in adapter.watch(src):
                break

        async def _stopper():
            # Give the semaphore time to dispatch initial batch.
            await asyncio.sleep(0.6)
            adapter._shutdown.set()

        tasks = [asyncio.create_task(_drive(s)) for s in sources]
        tasks.append(asyncio.create_task(_stopper()))
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )

        assert total_calls == 5, f"expected 5 clones, got {total_calls}"
        assert max_in_flight <= 2, (
            f"semaphore breached: saw {max_in_flight} concurrent clones, "
            "expected ≤ 2"
        )
        assert ever_overlapped.is_set(), (
            "clones never overlapped — they ran serially despite "
            "concurrency=2 (regression)"
        )
