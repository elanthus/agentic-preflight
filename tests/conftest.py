"""Shared fixtures.

Git fixtures drive a real ``git`` binary. The product under test *is* git
semantics — worktrees, merge-bases, cherry-picks, hook stdin protocol — so
mocking git would mean testing our idea of git rather than git. Everything is
made deterministic with fixed identity and timestamp environment instead.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
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
    # Real-push tests invoke the installed hook, which resolves
    # ``agentic-preflight`` through PATH. Pin that lookup to the same environment
    # running pytest so an unrelated user installation cannot test older code.
    python_bin = str(Path(sys.executable).parent)
    current_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", os.pathsep.join(part for part in (python_bin, current_path) if part))


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def write(repo: Path | str, relpath: str, content: str) -> Path:
    path = Path(repo) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def home_env(path: Path | str) -> dict[str, str]:
    """Environment that redirects ``Path.home()`` at a temporary directory.

    ``HOME`` alone is not enough. ``Path.home()`` consults ``USERPROFILE`` on
    Windows, so a test that sets only ``HOME`` there does not redirect anything
    — it reads and writes the developer's real home directory, installing
    skills into it and asserting against whatever happens to be there already.
    """
    return {"HOME": str(path), "USERPROFILE": str(path)}


def set_home(monkeypatch, path: Path | str) -> None:
    """The :func:`home_env` redirection, applied to this process."""
    for name, value in home_env(path).items():
        monkeypatch.setenv(name, value)


def access_entries(path: Path) -> list[str]:
    """A Windows file's DACL entries, one per granted principal.

    Every ``icacls`` entry has the shape ``PRINCIPAL:(rights)``, so ``:(`` is
    what identifies one. Splitting on the first colon instead counts the drive
    letter of the path that ``icacls`` prints on its own first line.
    """
    listing = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if ":(" in line]


def assert_owner_only(path: Path) -> None:
    """Assert the platform's expression of "readable by the owner and nobody else".

    On Windows the mode bits carry no permissions, so the DACL is read back:
    exactly one entry may remain, and none of it may be inherited.
    """
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    entries = access_entries(path)
    assert len(entries) == 1, f"expected a single access entry, got: {entries}"
    assert "(I)" not in entries[0], f"inherited access survived: {entries}"


def _symlinks_available() -> bool:
    """Whether this process may create symlinks.

    Probed rather than assumed from the platform: Windows permits it under
    Developer Mode or elevation and refuses otherwise, so the answer is a
    property of the machine and not of ``sys.platform``.
    """
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "probe"
        try:
            probe.symlink_to(directory)
        except (OSError, NotImplementedError):
            return False
    return True


def _git_records_symlinks() -> bool:
    """Whether git stores a symlink *as* a symlink in this environment.

    A separate question from whether the filesystem allows one. Git for Windows
    sets ``core.symlinks=false`` unless the installer enabled them, and then
    commits a symlink as an ordinary file holding its target path — so a test
    about symlink-to-file *type changes* sees no type change at all. GitHub's
    Windows runners are exactly this case: elevated enough to create symlinks,
    configured not to record them.

    Probed under the same config isolation the suite runs with, since that is
    what decides the answer.
    """
    if not SYMLINKS_AVAILABLE:
        return False

    env = {**os.environ, **DETERMINISTIC_ENV}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            subprocess.run(
                ["git", "init", "-q", str(root)], capture_output=True, check=True, env=env
            )
            (root / "target.txt").write_text("target\n", encoding="utf-8")
            (root / "link.txt").symlink_to("target.txt")
            subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True, env=env)
            staged = subprocess.run(
                ["git", "ls-files", "-s", "link.txt"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return False
    # Mode 120000 is git's symlink mode; 100644 means it stored a plain file.
    return staged.startswith("120000")


SYMLINKS_AVAILABLE = _symlinks_available()
GIT_RECORDS_SYMLINKS = _git_records_symlinks()

requires_symlinks = pytest.mark.skipif(
    not SYMLINKS_AVAILABLE,
    reason="creating symlinks requires Developer Mode or elevation on Windows",
)

requires_git_symlinks = pytest.mark.skipif(
    not GIT_RECORDS_SYMLINKS,
    reason="git is not configured to record symlinks as symlinks",
)

requires_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32",
    reason="making a file unreadable by mode bits has no effect on Windows",
)

# The POSIX kill path cannot be forced on Windows the way other platform
# branches can: ``signal.SIGKILL`` does not exist there, so exercising it would
# mean inventing the constant and testing a fiction. The Windows branch has its
# own tests instead, so both paths stay covered.
requires_posix_signals = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="process groups and SIGKILL are POSIX-only",
)

# Its counterpart. The two kill paths are defined under a ``sys.platform``
# guard, so neither can be forced on the other platform — each is covered by
# the CI leg that actually runs it.
requires_windows = pytest.mark.skipif(
    sys.platform != "win32",
    reason="taskkill and process-creation flags are Windows-only",
)


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
