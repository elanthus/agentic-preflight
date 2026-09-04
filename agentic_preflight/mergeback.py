"""Cherry-picking isolated fix commits back onto the source branch.

Strict by design. Two invariants dominate this module:

**Never auto-resolve.** On any conflict, abort immediately, verify the branch is
exactly where it started, and hand back an explicit resolution path. No
``-X ours``, no ``-X theirs``, no rerere, no clever merge strategies, ever. A
tool that silently picks a side during a conflict has quietly made a code
decision nobody reviewed — which is the precise opposite of what this exists to
do.

In-place validation bypasses this module's cherry-pick operation because its
verified commits already live on the source branch.

**Tree-equivalence attestation.** The note is keyed on exact commit SHA, but
cherry-picking *changes* the SHA, so what was verified is not literally what
gets pushed. The reconciliation is to compare trees: if the branch tip's tree
equals the worktree branch tip's tree, the verified content is byte-identical
and green legitimately transfers. If not, green does not transfer and the run
must be re-verified. This single check is what makes SHA-keyed attestations
compatible with cherry-picked history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gitx

OperationInProgress = gitx.OperationInProgress


class MergebackConflict(Exception):
    """A cherry-pick conflicted. The branch has been restored."""

    def __init__(self, message: str, report: ConflictReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class ConflictReport:
    pre_sha: str
    conflicting_commit: str
    conflicting_files: list[str]
    fix_commits: list[str]
    worktree_path: str
    restored: bool
    resolution: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pre_sha": self.pre_sha,
            "conflicting_commit": self.conflicting_commit,
            "conflicting_files": self.conflicting_files,
            "fix_commits": self.fix_commits,
            "worktree_path": self.worktree_path,
            "branch_restored": self.restored,
            "resolution": self.resolution,
        }


@dataclass
class MergebackResult:
    pre_sha: str
    post_sha: str
    applied: list[str]
    local_tree_sha: str
    worktree_tree_sha: str

    @property
    def tree_equivalent(self) -> bool:
        return self.local_tree_sha == self.worktree_tree_sha

    def as_dict(self) -> dict:
        return {
            "pre_sha": self.pre_sha,
            "post_sha": self.post_sha,
            "applied": self.applied,
            "local_tree_sha": self.local_tree_sha,
            "worktree_tree_sha": self.worktree_tree_sha,
            "tree_equivalent": self.tree_equivalent,
            "green_transferred": self.tree_equivalent,
        }


def _conflicting_files(repo: Path | str) -> list[str]:
    """Paths git has marked as unmerged, read before the abort clears them."""
    output = gitx.out(repo, "diff", "--name-only", "--diff-filter=U")
    return [line for line in output.splitlines() if line.strip()]


def _abort_and_restore(
    repo: Path | str,
    pre_sha: str,
    pre_status: str,
    fix_commits: list[str],
) -> bool:
    """Abort the cherry-pick and confirm the branch is byte-for-byte restored.

    A failed start can expose somebody else's ``CHERRY_PICK_HEAD``. Ownership
    must be established before aborting so their sequencer is never adopted.
    The confirmation matters as much as the abort: reporting "restored" without
    checking would be a guess about the one thing the user most needs to trust.
    """
    cherry_pick_head = gitx.run(
        repo,
        "rev-parse",
        "--verify",
        "CHERRY_PICK_HEAD^{commit}",
        check=False,
    )
    resolved_fixes = {
        resolved.stdout.strip()
        for commit in fix_commits
        if (
            resolved := gitx.run(
                repo,
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
                check=False,
            )
        ).returncode
        == 0
    }
    if cherry_pick_head.returncode != 0 or cherry_pick_head.stdout.strip() not in resolved_fixes:
        return False

    aborted = gitx.run(repo, "cherry-pick", "--abort", check=False)
    if aborted.returncode != 0:
        return False
    # Never hard-reset here. Mergeback permits unrelated local changes, and a
    # reset would destroy exactly the work the scoped preflight preserved.
    return (
        gitx.rev_parse(repo, "HEAD") == pre_sha
        and gitx.out(repo, "status", "--porcelain=v1", "--untracked-files=all") == pre_status
    )


def cherry_pick_fixes(
    repo: Path | str,
    fix_commits: list[str],
    *,
    worktree_branch: str,
    worktree_path: str,
) -> MergebackResult:
    """Apply fix commits in order, aborting cleanly on the first conflict."""
    repo = Path(repo)
    operation = gitx.operation_in_progress(repo)
    if operation is not None:
        raise OperationInProgress(operation, repo)

    pre_sha = gitx.rev_parse(repo, "HEAD")
    pre_status = gitx.out(repo, "status", "--porcelain=v1", "--untracked-files=all")

    if fix_commits:
        # One sequencer operation matters: `--abort` then restores the start of
        # the whole stack, not merely the commit immediately before a conflict.
        result = gitx.run(repo, "cherry-pick", *fix_commits, check=False)
        if result.returncode != 0:
            conflicts = _conflicting_files(repo)
            head = gitx.run(repo, "rev-parse", "CHERRY_PICK_HEAD", check=False)
            conflicting = head.stdout.strip() if head.returncode == 0 else fix_commits[0]
            restored = _abort_and_restore(repo, pre_sha, pre_status, fix_commits)
            raise MergebackConflict(
                f"cherry-picking {conflicting[:8]} conflicted with the branch",
                ConflictReport(
                    pre_sha=pre_sha,
                    conflicting_commit=conflicting,
                    conflicting_files=conflicts,
                    fix_commits=list(fix_commits),
                    worktree_path=str(worktree_path),
                    restored=restored,
                    resolution=[
                        f"cd {worktree_path}",
                        f"# the fix commits are intact on {worktree_branch}",
                        f"cd {repo}",
                        f"git cherry-pick {' '.join(fix_commits)}",
                        "# resolve the conflict by hand, then:",
                        "git cherry-pick --continue",
                        "agentic-preflight mergeback   # attest the exact result or resume safely",
                    ],
                ),
            )

    post_sha = gitx.rev_parse(repo, "HEAD")
    return MergebackResult(
        pre_sha=pre_sha,
        post_sha=post_sha,
        applied=list(fix_commits),
        local_tree_sha=gitx.tree_sha(repo, "HEAD"),
        worktree_tree_sha=gitx.tree_sha(worktree_path, "HEAD"),
    )
