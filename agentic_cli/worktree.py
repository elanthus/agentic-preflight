"""Validation checkout lifecycle, and containment of local environment files.

In-place runs use the caller's already-dedicated checkout. Isolated runs happen
on ``ac/<run_id>``: strict worktrees die at the end of a run, while the reusable
runner is reset and detached while retaining ignored caches for its next lease.

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


def acquire_reusable(
    repo: Path | str, *, path: Path | str, branch: str, head_sha: str
) -> Path:
    """Create or lease the repository's single persistent validation runner.

    A released runner is always detached. Finding it on a branch means a prior
    run may still own commits there, so acquisition refuses instead of guessing
    that the branch is disposable.
    """
    repo = Path(repo)
    path = Path(path)
    if not path.exists():
        return create(repo, path=path, branch=branch, head_sha=head_sha)

    if not (path / ".git").is_file():
        raise WorktreeError(
            f"reusable runner path exists but is not a linked worktree: {path}"
        )
    current = gitx.current_branch(path)
    if current != "HEAD":
        raise WorktreeError(
            f"reusable runner {path} is still leased on branch {current!r}; "
            "run `agentic-cli status` and finish, abort, or recover that run first"
        )
    if gitx.run(repo, "rev-parse", "--verify", branch, check=False).returncode == 0:
        raise WorktreeError(
            f"branch {branch} already exists; refusing to reuse it for a new run"
        )

    # Non-ignored leftovers are never caches. Ignored directories deliberately
    # survive so dependency and build caches do not churn between validations.
    gitx.run(path, "reset", "--hard", head_sha)
    gitx.run(path, "clean", "-fd")
    gitx.run(path, "switch", "-c", branch, head_sha)
    return path


def release_reusable(
    repo: Path | str,
    path: Path | str,
    *,
    branch: str | None,
    copied_files: list[str] | tuple[str, ...] = (),
) -> None:
    """Release a persistent runner while retaining only ignored caches.

    Copied secret-bearing files are removed explicitly because ``git clean``
    correctly preserves ignored files. The run branch is deleted only after the
    checkout is detached, leaving commits protected by the caller's lifecycle
    checks until the final release step.
    """
    repo = Path(repo)
    path = Path(path)
    if not path.exists():
        return
    for entry in copied_files:
        candidate = path / entry
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)

    gitx.run(path, "reset", "--hard")
    gitx.run(path, "clean", "-fd")
    gitx.run(path, "switch", "--detach")
    if branch:
        gitx.run(repo, "branch", "-D", branch, check=False)


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


def protect_in_place_files(
    repo: Path | str,
    entries: list[str] | tuple[str, ...],
) -> list[str]:
    """Validate and register local environment files without copying them.

    In-place validation already has access to the checkout's ignored files. We
    still apply the same preflight and commit-content invariants as isolated
    modes so their contents can be redacted and can never enter a repair commit.
    """
    repo = Path(repo)
    protected: list[str] = []
    for entry in entries:
        source = repo / entry
        if not source.exists():
            continue
        if source.is_dir():
            raise CopyRefused(
                f"refusing to protect directory {entry!r}: [worktree] copy_files "
                "accepts files only. Use setup_command to prepare directories."
            )
        if not gitx.is_ignored(repo, entry):
            raise CopyRefused(
                f"refusing to use {entry!r} during in-place validation: git is not "
                "ignoring it, so a `git add -A` could commit and push it. "
                f"Add {entry!r} to .gitignore and commit that before running."
            )
        protected.append(entry)
    return protected


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
