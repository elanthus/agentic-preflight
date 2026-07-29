"""A thin, total wrapper over the ``git`` binary.

Deliberately not a git library: the tool's contract is defined in terms of what
git itself does, and shelling out keeps the semantics honest. Every helper is a
pure query except where the name says otherwise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """A git invocation exited non-zero."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


def run(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(list(args), result.returncode, result.stderr)
    return result


def out(cwd: Path | str, *args: str) -> str:
    return run(cwd, *args).stdout.strip()


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


# -- identity ---------------------------------------------------------------


def current_branch(cwd: Path | str) -> str:
    return out(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def rev_parse(cwd: Path | str, ref: str) -> str:
    return out(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")


def tree_sha(cwd: Path | str, ref: str = "HEAD") -> str:
    return out(cwd, "rev-parse", f"{ref}^{{tree}}")


def git_common_dir(cwd: Path | str) -> Path:
    """The *common* dir, not ``GIT_DIR``.

    These differ when the caller is already inside a worktree, and run state
    must live in one namespace per clone rather than one per worktree.
    """
    raw = out(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(raw)


def repo_root(cwd: Path | str) -> Path:
    return Path(out(cwd, "rev-parse", "--show-toplevel"))


# -- history ----------------------------------------------------------------


def merge_base(cwd: Path | str, a: str, b: str) -> str:
    return out(cwd, "merge-base", a, b)


def is_ancestor(cwd: Path | str, maybe_ancestor: str, descendant: str) -> bool:
    result = run(cwd, "merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False)
    return result.returncode == 0


def commit_exists(cwd: Path | str, sha: str) -> bool:
    result = run(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    return result.returncode == 0


def commit_files(cwd: Path | str, sha: str) -> list[str]:
    """Paths changed by a single commit."""
    return _lines(out(cwd, "diff-tree", "--no-commit-id", "--name-only", "-r", sha))


def commit_touches(cwd: Path | str, sha: str, path: str) -> bool:
    return path in commit_files(cwd, sha)


def commits_between(cwd: Path | str, base: str, head: str) -> list[str]:
    return _lines(out(cwd, "rev-list", "--reverse", f"{base}..{head}"))


def commit_subject(cwd: Path | str, sha: str) -> str:
    return out(cwd, "log", "-1", "--format=%s", sha)


def commit_patch_id(cwd: Path | str, sha: str) -> str | None:
    """Return git's stable patch identity for one commit.

    Patch IDs deliberately ignore commit metadata, so an ordinary cherry-pick
    compares equal to its source even though the commit SHA changes. Empty
    commits have no patch identity and return ``None``.
    """
    patch = run(
        cwd,
        "show",
        "--format=",
        "--no-ext-diff",
        "--binary",
        sha,
    ).stdout
    result = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=str(cwd),
        input=patch,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(["patch-id", "--stable"], result.returncode, result.stderr)
    fields = result.stdout.split()
    return fields[0] if fields else None


# -- diff -------------------------------------------------------------------


def changed_files(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    return _lines(out(cwd, "diff", "--name-only", f"{base}...{head}"))


def diff_text(cwd: Path | str, base: str, head: str = "HEAD") -> str:
    return run(cwd, "diff", "--no-color", f"{base}...{head}").stdout


def diff_text_for_path(cwd: Path | str, base: str, head: str, path: str) -> str:
    return run(cwd, "diff", "--no-color", f"{base}...{head}", "--", path).stdout


# -- working tree -----------------------------------------------------------


def is_clean(cwd: Path | str) -> bool:
    """Clean means *nothing* to report, untracked files included.

    Untracked matters here: the agent commits inside a worktree with ``git add
    -A``, so a stray file is not cosmetic, it is a candidate for being swept
    into a fix commit.
    """
    return out(cwd, "status", "--porcelain") == ""


def status_for_paths(cwd: Path | str, paths: list[str] | tuple[str, ...]) -> str:
    """Porcelain status limited to paths an operation may overwrite."""
    if not paths:
        return ""
    return run(
        cwd,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *sorted(set(paths)),
    ).stdout


def changed_paths_in_commits(cwd: Path | str, commits: list[str]) -> list[str]:
    """The stable union of paths touched across a commit stack."""
    return sorted({path for sha in commits for path in commit_files(cwd, sha)})


def is_ignored(cwd: Path | str, path: str) -> bool:
    result = run(cwd, "check-ignore", "-q", "--", path, check=False)
    return result.returncode == 0


# -- remotes ----------------------------------------------------------------


def remote_url(cwd: Path | str, remote: str = "origin") -> str | None:
    result = run(cwd, "remote", "get-url", remote, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def local_branch_exists(cwd: Path | str, branch: str) -> bool:
    return (
        run(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode
        == 0
    )


def delete_remote_branch(cwd: Path | str, branch: str, remote: str = "origin") -> bool:
    """Delete a remote branch, treating an already-absent ref as idempotent."""
    result = run(cwd, "push", remote, "--delete", branch, check=False)
    if result.returncode == 0:
        return True
    missing_markers = ("remote ref does not exist", "unable to delete")
    if any(marker in result.stderr.lower() for marker in missing_markers):
        return False
    raise GitError(["push", remote, "--delete", branch], result.returncode, result.stderr)


def list_worktrees(cwd: Path | str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` into records."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out(cwd, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records
