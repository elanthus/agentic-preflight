"""Shared fixtures.

Git fixtures drive a real ``git`` binary. The product under test *is* git
semantics — worktrees, merge-bases, cherry-picks, hook stdin protocol — so
mocking git would mean testing our idea of git rather than git. Everything is
made deterministic with fixed identity and timestamp environment instead.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

DETERMINISTIC_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


@pytest.fixture(autouse=True)
def deterministic_git_env(monkeypatch):
    for key, value in DETERMINISTIC_ENV.items():
        monkeypatch.setenv(key, value)


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def write(repo: Path, relpath: str, content: str) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def commit_all(repo: Path, message: str) -> str:
    git("add", "-A", cwd=repo)
    git("commit", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A repo on ``main`` with one base commit and no working-tree changes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    write(repo, "README.md", "# demo\n\nA demo project.\n")
    write(repo, "src/app.py", "def greet(name):\n    return f'hi {name}'\n")
    # Committed, not just written: worktrees are checked out at a commit, so an
    # uncommitted ignore rule is invisible where it matters.
    write(repo, ".gitignore", ".env\n")
    commit_all(repo, "base commit")
    return repo


@pytest.fixture
def feature_repo(tmp_repo: Path) -> Path:
    """``tmp_repo`` with a ``feature/x`` branch holding one commit ahead of main."""
    git("switch", "-c", "feature/x", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name, loud=False):\n    return f'hi {name}'\n")
    commit_all(tmp_repo, "add loud flag")
    return tmp_repo


@pytest.fixture
def bare_remote(tmp_path: Path, tmp_repo: Path) -> Path:
    """A bare remote wired up as ``origin`` so push and hook paths run for real."""
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=tmp_repo)
    git("push", "-u", "origin", "main", cwd=tmp_repo)
    return remote
