"""Fresh-base synchronization for an in-place or isolated validation checkout."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitx
from .attestation import NOTES_REF

OperationInProgress = gitx.OperationInProgress


class SyncConflict(Exception):
    """A rebase or merge conflicted and was aborted back to its original head."""

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
    merged: bool = False

    def as_dict(self) -> dict[str, str | bool | None]:
        return asdict(self)


def _origin_base_ref(base_ref: str) -> tuple[str, str]:
    branch = base_ref.removeprefix("refs/remotes/origin/").removeprefix("origin/")
    return branch, f"refs/remotes/origin/{branch}"


def _unmerged_paths(worktree_path: Path | str) -> list[str]:
    return [
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


def _landed_tree(worktree_path: Path | str, base_sha: str, head: str) -> str | None:
    """Return the tree a clean merge of ``head`` into the base would land, if any."""
    try:
        return gitx.merge_tree(worktree_path, base_sha, head)
    except gitx.GitError:
        # Unrelated histories have no merge result to preserve.
        return None


def _merge_base_in(
    worktree_path: Path | str, *, base_ref: str, base_sha: str, before: str, landed: str
) -> str:
    """Merge the base into the restored head, or restore it and report a conflict."""
    result = gitx.run(worktree_path, "merge", "--no-ff", "--no-edit", base_sha, check=False)
    if result.returncode == 0 and gitx.tree_sha(worktree_path, "HEAD") == landed:
        return gitx.rev_parse(worktree_path, "HEAD")
    conflicts = _unmerged_paths(worktree_path)
    gitx.run(worktree_path, "merge", "--abort", check=False)
    gitx.run(worktree_path, "reset", "--hard", before)
    raise SyncConflict(
        base_ref=base_ref,
        base_sha=base_sha,
        head_before=before,
        conflicting_files=conflicts,
    )


def sync_onto(worktree_path: Path | str, base_ref: str) -> tuple[str, str, bool]:
    """Bring one clean worktree onto the base, aborting only a conflict this call started.

    Returns the head before and after, and whether the base was merged in. A rebase is
    preferred, but it replays commits: it drops merge commits and skips commits the
    base already has. When that changes what merging the branch would land, such as
    an ``-s ours`` merge that reverts a branch already in the base, the head is
    restored and the base is merged in instead, so validation sees what would land.
    """
    operation = gitx.operation_in_progress(worktree_path)
    if operation is not None:
        raise OperationInProgress(operation, worktree_path)

    before = gitx.rev_parse(worktree_path, "HEAD")
    base_sha = gitx.rev_parse(worktree_path, base_ref)
    if gitx.is_ancestor(worktree_path, base_sha, "HEAD"):
        return before, before, False

    landed = _landed_tree(worktree_path, base_sha, before)
    result = gitx.run(worktree_path, "rebase", base_sha, check=False)
    if result.returncode == 0:
        after = gitx.rev_parse(worktree_path, "HEAD")
        # A conflicted merge has no landed tree; the rebase is then the resolution.
        if landed is None or gitx.tree_sha(worktree_path, after) == landed:
            return before, after, False
        # The worktree was clean before synchronizing, so this restores it exactly.
        gitx.run(worktree_path, "reset", "--hard", before)
        merged = _merge_base_in(
            worktree_path, base_ref=base_ref, base_sha=base_sha, before=before, landed=landed
        )
        return before, merged, True

    conflicts = _unmerged_paths(worktree_path)
    rebase_head_path = gitx.git_path(worktree_path, "REBASE_HEAD")
    rebase_head = gitx.run(
        worktree_path,
        "rev-parse",
        "--verify",
        "REBASE_HEAD^{commit}",
        check=False,
    )
    started_here = (
        rebase_head_path.exists()
        and rebase_head.returncode == 0
        and gitx.is_ancestor(worktree_path, rebase_head.stdout.strip(), before)
    )
    if started_here:
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
    """Fetch the authoritative base when possible, then bring the worktree onto it."""
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
        # Keep the shared notes history current before this run eventually adds
        # its own attestation. Locally-ahead and disjoint histories are retained;
        # a same-commit note conflict remains a loud Git error.
        gitx.fetch_notes(repo, "origin", NOTES_REF)
        remote = "origin"

    base_sha = gitx.rev_parse(repo, target_ref)
    before, after, merged = sync_onto(worktree_path, base_sha)
    return SyncResult(
        remote=remote,
        base_ref=target_ref,
        base_sha=base_sha,
        head_before=before,
        head_after=after,
        rebased=before != after and not merged,
        merged=merged,
    )
