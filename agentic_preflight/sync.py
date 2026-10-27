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
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"synchronizing with {base_ref} conflicted")
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_before = head_before
        self.conflicting_files = conflicting_files


class SyncObstructed(SyncConflict):
    """Synchronizing would overwrite untracked local files, so nothing was changed."""

    def __init__(self, *, base_ref: str, base_sha: str, head_before: str, paths: list[str]):
        super().__init__(
            base_ref=base_ref,
            base_sha=base_sha,
            head_before=head_before,
            conflicting_files=paths,
            message=(
                f"synchronizing with {base_ref} would overwrite untracked or ignored "
                f"local files: {', '.join(paths)}"
            ),
        )


class SyncRebaseFailed(SyncConflict):
    """The rebase failed for a reason other than a conflict, such as a hook or signer.

    It subclasses :class:`SyncConflict` so existing handlers still stop the run, but it
    reports no conflicting files and carries Git's own error output.
    """

    def __init__(self, *, base_ref: str, base_sha: str, head_before: str, stderr: str):
        detail = stderr.strip() or "git reported no error output"
        super().__init__(
            base_ref=base_ref,
            base_sha=base_sha,
            head_before=head_before,
            conflicting_files=[],
            message=f"rebasing onto {base_ref} failed without a conflict: {detail}",
        )
        self.stderr = stderr


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


def _merge_trees(
    worktree_path: Path | str, base_sha: str, head: str
) -> tuple[str | None, str | None]:
    """Return the clean landed tree and Git's merge-result tree for ``head`` into the base.

    The landed tree is ``None`` when the merge conflicts or the histories are unrelated,
    so there is no merge result to preserve. A conflicted merge still has a result tree,
    with markers, that shows every path the merge would write; unrelated histories have
    neither. Any other Git failure propagates before the worktree is touched, rather
    than silently disabling the comparison.
    """
    args = ["merge-base", base_sha, head]
    common = gitx.run(worktree_path, *args, check=False)
    if common.returncode == 1:
        return None, None
    if common.returncode != 0:
        raise gitx.GitError(args, common.returncode, common.stderr)
    tree, clean = gitx.merge_tree_result(worktree_path, base_sha, head)
    return (tree if clean else None), tree


def _obstructed_paths(worktree_path: Path | str, before: str, trees: list[str]) -> list[str]:
    """Return untracked or ignored local files at paths that any of ``trees`` adds.

    Git treats ignored files as expendable, so a rebase or merge can overwrite them: a
    rebase first checks out the base, and a directory rename can move a branch file onto
    an ignored local file.
    """
    added: list[str] = []
    for tree in trees:
        output = gitx.run(
            worktree_path,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=A",
            before,
            tree,
        ).stdout
        added.extend(path for path in output.split("\0") if path and path not in added)
    root = Path(worktree_path)
    candidates = [path for path in added if (root / path).exists() or (root / path).is_symlink()]
    if not candidates:
        return []
    # On a case-insensitive filesystem a tracked file can answer for an added path that
    # differs only in case, so let Git decide which candidates are untracked or ignored.
    untracked = [
        path
        for path in gitx.run(
            worktree_path, "--literal-pathspecs", "ls-files", "-z", "--others", "--", *candidates
        ).stdout.split("\0")
        if path
    ]
    return [
        path
        for path in candidates
        if any(found == path or found.startswith(f"{path}/") for found in untracked)
    ]


def _merge_base_in(
    worktree_path: Path | str, *, base_ref: str, base_sha: str, before: str, landed: str
) -> str:
    """Merge the base into the restored head, or restore it and report a conflict."""
    # The options override any configured merge behavior: always commit a real merge,
    # never overwrite ignored local files, and skip commit hooks, because the rebase this
    # replaces runs none and the merge adds no user content.
    result = gitx.run(
        worktree_path,
        "merge",
        "--no-ff",
        "--commit",
        "--no-squash",
        "--no-edit",
        "--no-verify",
        "--no-overwrite-ignore",
        base_sha,
        check=False,
    )
    if (
        result.returncode == 0
        and gitx.operation_in_progress(worktree_path) is None
        and gitx.is_ancestor(worktree_path, base_sha, "HEAD")
        and gitx.tree_sha(worktree_path, "HEAD") == landed
    ):
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

    landed, merged = _merge_trees(worktree_path, base_sha, before)
    # Check every path the base or the merge result adds, before anything changes.
    obstructed = _obstructed_paths(worktree_path, before, [base_sha, *([merged] if merged else [])])
    if obstructed:
        raise SyncObstructed(
            base_ref=base_ref, base_sha=base_sha, head_before=before, paths=obstructed
        )
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
    if not conflicts:
        # Entry refused any operation in progress, so a rebase left now is this call's.
        # A rebase that stops without a conflict may not write REBASE_HEAD.
        if gitx.operation_in_progress(worktree_path) == "rebase":
            gitx.run(worktree_path, "rebase", "--abort", check=False)
        if gitx.rev_parse(worktree_path, "HEAD") != before:
            gitx.run(worktree_path, "reset", "--hard", before)
        raise SyncRebaseFailed(
            base_ref=base_ref,
            base_sha=base_sha,
            head_before=before,
            stderr=result.stderr,
        )
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
