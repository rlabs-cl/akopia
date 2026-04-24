"""GitAdapter captures per-file last-commit time via ``git log -1``.

Builds a real local repo with two commits (one per file, at distinct
commit timestamps) so the helper reports file-scoped — not repo-HEAD —
times. Uses subprocess against the real git binary; ``pygit2`` isn't
already a dep and the subprocess path is what production runs.

The top-level GitAdapter lifecycle tests live in tests/test_git_adapter.py
which is skipped in CI (see the task's --ignore flag). This file tests
only the time helper so it runs everywhere.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from adapters.git import _last_commit_time_for_path


def _run(cwd: Path, *args: str, env: dict | None = None) -> str:
    full_env = {
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if env:
        full_env.update(env)
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=30, env=full_env,
    )
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def repo_with_two_commits(tmp_path: Path) -> Path:
    """Repo where a.txt was committed at T1 and b.txt at T2 (> T1)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")

    # Commit a.txt at fixed date T1 = 2024-01-01T00:00:00Z
    (repo / "a.txt").write_text("a")
    _run(repo, "add", "a.txt")
    _run(
        repo, "commit", "-q", "-m", "add a",
        env={
            "GIT_AUTHOR_DATE": "2024-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2024-01-01T00:00:00+0000",
        },
    )

    # Commit b.txt at fixed date T2 = 2025-06-15T12:00:00Z
    (repo / "b.txt").write_text("b")
    _run(repo, "add", "b.txt")
    _run(
        repo, "commit", "-q", "-m", "add b",
        env={
            "GIT_AUTHOR_DATE": "2025-06-15T12:00:00+0000",
            "GIT_COMMITTER_DATE": "2025-06-15T12:00:00+0000",
        },
    )
    return repo


class TestLastCommitTimeForPath:
    def test_returns_per_file_times(self, repo_with_two_commits: Path):
        t_a = _last_commit_time_for_path(repo_with_two_commits, "a.txt")
        t_b = _last_commit_time_for_path(repo_with_two_commits, "b.txt")

        assert t_a is not None and t_b is not None
        # Both tz-aware.
        assert t_a.tzinfo is not None
        assert t_b.tzinfo is not None
        # a.txt was committed earlier than b.txt — this is the whole
        # point of per-file times vs. repo-HEAD heuristic.
        assert t_a < t_b
        # Values match the pinned commit dates exactly.
        assert t_a == datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        assert t_b == datetime.fromisoformat("2025-06-15T12:00:00+00:00")

    def test_returns_none_for_unknown_path(self, repo_with_two_commits: Path):
        assert _last_commit_time_for_path(
            repo_with_two_commits, "does-not-exist.txt"
        ) is None

    def test_returns_none_outside_repo(self, tmp_path: Path):
        # Not a git repo → git log errors; helper swallows and returns None.
        assert _last_commit_time_for_path(tmp_path, "whatever.txt") is None
