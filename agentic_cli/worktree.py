"""Disposable worktree lifecycle, and the containment of files copied into it.

The user's tree is never touched: all agent work happens on ``ac/<run_id>`` in a
throwaway worktree that dies at the end of the run.

The copied-file guards deserve their own note, because they defend against a
*secret leak*, not an inconvenience. The agent commits inside the worktree, so
an un-ignored ``.env`` copied in could be swept up by ``git add -A``,
cherry-picked onto the real branch at merge-back, and pushed. Git's
``info/exclude`` lives in the common dir and is shared across worktrees, so
there is no clean per-worktree exclude to lean on. Two independent guards
instead:

1. **Preflight refusal** (:func:`copy_files`) — refuse to copy anything git is
   not already ignoring.
2. **Commit-content invariant** (:func:`assert_commit_is_clean_of`) — reject any
   commit whose changed-file set intersects ``copy_files``, checked against the
   commit itself rather than against ignore rules.

They are independent on purpose: a ``.gitignore`` edited mid-run cannot open the
hole, because guard 2 never consults ``.gitignore``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

from . import gitx


class WorktreeError(Exception):
    """The worktree could not be created or removed."""


class CopyRefused(Exception):
    """A ``copy_files`` entry is not gitignored, so copying it is unsafe."""


class CopiedFileInCommit(Exception):
    """A commit touches a path that was copied into the worktree."""


def default_root(repo: Path | str) -> Path:
    """Return a stable per-clone sibling directory outside ``.git``.

    Keeping worktrees under the git common directory makes Jest ignore the
    entire checkout. A sibling directory also avoids making the repository
    itself dirty and keeps identically named clones separate.
    """
    repo = Path(repo).resolve()
    identity = sha256(str(gitx.git_common_dir(repo).resolve()).encode()).hexdigest()[:12]
    return repo.parent / ".agentic-cli-worktrees" / f"{repo.name}-{identity}"


def resolve_root(repo: Path | str, configured: str | None = None) -> Path:
    """Resolve an optional root while preserving external-worktree isolation."""
    repo = Path(repo).resolve()
    if not configured:
        return default_root(repo)
    path = Path(configured).expanduser()
    resolved = (path if path.is_absolute() else repo.parent / path).resolve()
    if resolved.is_relative_to(repo):
        raise WorktreeError(
            f"[worktree] root must be outside the repository; got {resolved}. "
            "An external path keeps git status clean and lets Jest discover tests."
        )
    return resolved


def create(repo: Path | str, *, path: Path | str, branch: str, head_sha: str) -> Path:
    """Add a worktree at ``head_sha`` and put it on its own branch.

    Detached-then-branch rather than ``worktree add -b``: it keeps the failure
    mode legible when the branch name is already taken, instead of git picking
    something surprising.
    """
    path = Path(path)
    repo = Path(repo)

    existing = gitx.run(repo, "rev-parse", "--verify", branch, check=False)
    if existing.returncode == 0:
        raise WorktreeError(
            f"branch {branch} already exists; refusing to reuse it for a new run"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        gitx.run(repo, "worktree", "add", "--detach", str(path), head_sha)
    except gitx.GitError as exc:
        raise WorktreeError(str(exc)) from exc

    try:
        gitx.run(path, "switch", "-c", branch)
    except gitx.GitError as exc:
        gitx.run(repo, "worktree", "remove", "--force", str(path), check=False)
        raise WorktreeError(str(exc)) from exc

    return path


def remove(repo: Path | str, path: Path | str, *, branch: str | None = None) -> None:
    """Tear down the worktree and its branch. Copied files die with it."""
    gitx.run(repo, "worktree", "remove", "--force", str(path), check=False)
    if Path(path).exists():
        shutil.rmtree(path, ignore_errors=True)
    if branch:
        gitx.run(repo, "branch", "-D", branch, check=False)
    gitx.run(repo, "worktree", "prune", check=False)


def copy_files(
    repo: Path | str,
    worktree_path: Path | str,
    entries: list[str] | tuple[str, ...],
) -> list[str]:
    """Copy environment files into the worktree, refusing anything git can see.

    Missing entries are skipped silently — ``.env`` is a default, and not every
    repo has one. A *present but un-ignored* entry is a hard refusal.

    Ignore status is checked **in the worktree**, not in the source repo. The two
    can disagree: the worktree is checked out at ``head_sha``, so its
    ``.gitignore`` is whatever that commit contained, while the user's tree may
    have an uncommitted rule. Since the dangerous ``git add -A`` happens in the
    worktree, the worktree's view is the one that decides whether copying is safe.
    """
    repo = Path(repo)
    worktree_path = Path(worktree_path)
    copied: list[str] = []

    for entry in entries:
        source = repo / entry
        if not source.exists():
            continue
        if source.is_dir():
            raise CopyRefused(
                f"refusing to copy directory {entry!r}: [worktree] copy_files accepts "
                "files only. Use setup_command to install dependencies or prepare caches."
            )
        if not gitx.is_ignored(worktree_path, entry):
            raise CopyRefused(
                f"refusing to copy {entry!r} into the worktree: git is not ignoring it "
                f"there, so a `git add -A` could commit it and push it. "
                f"Add {entry!r} to .gitignore and commit that before running."
            )

        destination = worktree_path / entry
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        copied.append(entry)

    return copied


def assert_commit_is_clean_of(
    worktree_path: Path | str,
    sha: str,
    entries: list[str] | tuple[str, ...],
) -> None:
    """Guard 2. Raise if ``sha`` touches any copied path.

    Checked against the commit's own changed-file set, deliberately without
    consulting ``.gitignore`` — that independence is what makes this hold even
    if ignore rules change mid-run.
    """
    touched = set(gitx.commit_files(worktree_path, sha))
    offenders = sorted(touched & set(entries))
    if offenders:
        raise CopiedFileInCommit(
            f"commit {sha[:8]} touches copied file(s) {offenders}, which must never "
            f"enter a commit; these hold local environment data and are not part of "
            f"the change under review"
        )


def run_setup(
    worktree_path: Path | str,
    command: str,
    *,
    timeout_seconds: int = 600,
) -> subprocess.CompletedProcess:
    """Run the configured setup command inside the worktree.

    Returns the result rather than raising: a failed ``uv sync`` is information
    for the caller to report, not an exception to unwind on.
    """
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
