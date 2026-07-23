"""Fresh-base synchronization for the isolated validation worktree."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitx


class SyncConflict(Exception):
    """A rebase conflicted and was aborted back to its original head."""

    def __init__(
        self,
        *,
        base_ref: str,
        base_sha: str,
        head_before: str,
        conflicting_files: list[str],
    ) -> None:
        super().__init__(f"rebasing onto {base_ref} conflicted")
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_before = head_before
        self.conflicting_files = conflicting_files


@dataclass(frozen=True)
class SyncResult:
    remote: str | None
    base_ref: str
    base_sha: str
    head_before: str
    head_after: str
    rebased: bool

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "remote": self.remote,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "rebased": self.rebased,
        }


def _origin_base_ref(base_ref: str) -> tuple[str, str]:
    branch = base_ref.removeprefix("refs/remotes/origin/").removeprefix("origin/")
    return branch, f"refs/remotes/origin/{branch}"


def rebase_onto(worktree_path: Path | str, base_ref: str) -> tuple[str, str]:
    """Rebase one clean worktree, aborting and reporting any conflict."""
    before = gitx.rev_parse(worktree_path, "HEAD")
    base_sha = gitx.rev_parse(worktree_path, base_ref)
    if gitx.is_ancestor(worktree_path, base_sha, "HEAD"):
        return before, before

    result = gitx.run(worktree_path, "rebase", base_sha, check=False)
    if result.returncode == 0:
        return before, gitx.rev_parse(worktree_path, "HEAD")

    conflicts = [
        line.strip()
        for line in gitx.run(
            worktree_path,
            "diff",
            "--name-only",
            "--diff-filter=U",
            check=False,
        ).stdout.splitlines()
        if line.strip()
    ]
    gitx.run(worktree_path, "rebase", "--abort", check=False)
    raise SyncConflict(
        base_ref=base_ref,
        base_sha=base_sha,
        head_before=before,
        conflicting_files=conflicts,
    )


def synchronize(
    repo: Path | str,
    worktree_path: Path | str,
    *,
    base_ref: str,
) -> SyncResult:
    """Fetch the authoritative base when possible, then rebase the worktree."""
    remote: str | None = None
    target_ref = base_ref
    if gitx.remote_url(repo, "origin"):
        branch, target_ref = _origin_base_ref(base_ref)
        gitx.run(
            repo,
            "fetch",
            "--prune",
            "origin",
            f"+refs/heads/{branch}:{target_ref}",
        )
        remote = "origin"

    base_sha = gitx.rev_parse(repo, target_ref)
    before, after = rebase_onto(worktree_path, base_sha)
    return SyncResult(
        remote=remote,
        base_ref=target_ref,
        base_sha=base_sha,
        head_before=before,
        head_after=after,
        rebased=before != after,
    )
