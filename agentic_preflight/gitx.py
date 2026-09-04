"""A thin, total wrapper over the ``git`` binary.

Deliberately not a git library: the tool's contract is defined in terms of what
git itself does, and shelling out keeps the semantics honest. Query helpers do
not update refs, the index, or the worktree. Git's merge-tree plumbing can still
write unreachable tree objects, which normal object pruning may collect.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

from agentic_preflight.stages.command import resolve_on_path

# `git merge-tree --write-tree` landed in Git 2.38.
_WRITE_TREE_MINIMUM = (2, 38)

# Git output is repository data and repositories contain latin-1, so decoding
# must never be able to crash the command that read it. ``backslashreplace``
# keeps the mojibake visible as ``\xe9`` instead of raising. Anything that
# feeds git output back *into* git stays in bytes — see ``commit_patch_id`` —
# because the escape text is not the bytes it stands for.
_DECODE_ERRORS = "backslashreplace"

# Resolved once per process. Handed a bare name, Windows' ``CreateProcess``
# searches the parent's current directory before PATH — for this tool, the
# repository under validation — so a repo-committed ``git.exe`` could become
# the git that validates the repository that ships it. POSIX ``execvp``
# searches PATH only; the absolute path pins Windows to the same rule.
_RESOLVED_GIT: str | None = None


def _git_executable() -> str:
    global _RESOLVED_GIT
    if _RESOLVED_GIT is None:
        resolved = resolve_on_path("git")
        if resolved is None:
            # Falling back to the bare name would hand ``CreateProcess`` its
            # current-directory search back — the exact hole this resolution
            # exists to close. A git missing from PATH fails loudly instead.
            raise FileNotFoundError("git was not found on PATH")
        _RESOLVED_GIT = resolved
    return _RESOLVED_GIT


class GitError(Exception):
    """A git invocation exited non-zero."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


def run(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [_git_executable(), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=_DECODE_ERRORS,
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


def git_dir(cwd: Path | str) -> Path:
    """The private Git directory for this specific worktree."""
    raw = out(cwd, "rev-parse", "--path-format=absolute", "--git-dir")
    return Path(raw)


def hook_path(cwd: Path | str, name: str) -> Path:
    """Return the hook path Git will execute, including ``core.hooksPath``."""
    raw = out(cwd, "rev-parse", "--path-format=absolute", "--git-path", f"hooks/{name}")
    return Path(raw)


def repo_root(cwd: Path | str) -> Path:
    return Path(out(cwd, "rev-parse", "--show-toplevel"))


# -- history ----------------------------------------------------------------


def merge_base(cwd: Path | str, a: str, b: str) -> str:
    return out(cwd, "merge-base", a, b)


def is_ancestor(cwd: Path | str, maybe_ancestor: str, descendant: str) -> bool:
    result = run(cwd, "merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False)
    return result.returncode == 0


def version(cwd: Path | str = ".") -> tuple[int, int] | None:
    """``(major, minor)`` of the git binary, or ``None`` if it cannot be read.

    Deliberately not cached. It is read at most twice per run, so a
    process-wide cache would trade an unmeasurable saving for global state
    that leaks between tests.
    """
    result = run(cwd, "--version", check=False)
    if result.returncode != 0:
        return None
    # "git version 2.46.2.windows.1" and "git version 2.39.3 (Apple Git-146)"
    # are both real; take the first two numeric components and ignore the rest.
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _merge_tree_via_index(cwd: Path | str, left: str, right: str) -> str | None:
    """The pre-2.38 three-way merge, performed in a throwaway index.

    Intentionally more conservative than ``--write-tree``: it can reject a
    merge that would have been textually clean. What it cannot do is
    manufacture a false equivalence, which is the direction that matters when
    the answer decides whether a green attestation may be reused.
    """
    base = merge_base(cwd, left, right)
    with tempfile.TemporaryDirectory(prefix="agentic-preflight-merge-") as temp_dir:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp_dir) / "index")}
        merged = subprocess.run(
            [_git_executable(), "read-tree", "-m", base, left, right],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=_DECODE_ERRORS,
        )
        if merged.returncode != 0:
            return None
        written = subprocess.run(
            [_git_executable(), "write-tree"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=_DECODE_ERRORS,
        )
        if written.returncode != 0:
            return None
        value = written.stdout.strip()
        return value if len(value) == 40 else None


def merge_tree(cwd: Path | str, left: str, right: str) -> str | None:
    """Return Git's clean merge-result tree, or ``None`` for a conflict.

    ``merge-tree --write-tree`` uses the commits' ancestry as well as their
    snapshots. That distinction is essential when deciding whether an existing
    attestation remains valid against a newly synchronized base. Git may add
    unreachable tree objects, but this does not update refs, the index, or files.

    Which interface to use is decided by the git *version*, not by recognising
    an error message. Git 2.30-2.37 predates ``--write-tree`` and reports the
    flag as ``fatal: unknown rev --write-tree`` — on stderr, while exiting
    zero, so there is no failure to detect. Message matching also breaks under
    any locale where git speaks a translated string.
    """
    if _supports_write_tree(cwd):
        result = run(cwd, "merge-tree", "--write-tree", left, right, check=False)
        if result.returncode == 1:
            return None
        if result.returncode == 0:
            first_line = result.stdout.splitlines()[0] if result.stdout else ""
            if len(first_line) == 40:
                return first_line
            # Exit zero without a tree means the flag was not understood after
            # all, so the version probe was wrong. Fall through rather than
            # report "no clean merge" for a merge that may well be clean.
        elif not _rejected_the_flag(result):
            raise GitError(
                ["merge-tree", "--write-tree", left, right],
                result.returncode,
                result.stderr,
            )

    return _merge_tree_via_index(cwd, left, right)


def _supports_write_tree(cwd: Path | str) -> bool:
    detected = version(cwd)
    # An unreadable version is treated as modern: the newer interface is tried
    # first and falls back on its own, which is the order this used to take.
    return detected is None or detected >= _WRITE_TREE_MINIMUM


def _rejected_the_flag(result: subprocess.CompletedProcess) -> bool:
    """Whether a failure is git refusing ``--write-tree`` rather than a real error.

    A second line of defence behind the version check, for a build that reports
    the flag some other way. Kept as a *secondary* signal because these strings
    are translated under a non-English locale, which is exactly why matching on
    them alone was not enough.
    """
    stderr = result.stderr.lower()
    return (
        result.returncode == 129
        or "unknown option" in stderr
        or "not a valid object name --write-tree" in stderr
        or "unknown rev --write-tree" in stderr
    )


def commit_exists(cwd: Path | str, sha: str) -> bool:
    result = run(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    return result.returncode == 0


def commit_files(cwd: Path | str, sha: str) -> list[str]:
    """Paths changed by a single commit.

    NUL-delimited for the same reason as ``changed_files``: under default
    ``core.quotePath`` a non-ASCII path comes back C-quoted, naming a file
    that exists nowhere.
    """
    output = run(cwd, "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", sha).stdout
    return [path for path in output.split("\0") if path]


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

    The patch travels between the two git processes as raw bytes. Decoding it
    first would fold the raw byte ``0xE9`` and the literal escape text
    ``\\xe9`` into one string, giving two byte-distinct changes the same
    identity — and identity is the whole point of this function.
    """
    show_args = ["show", "--format=", "--no-ext-diff", "--binary", sha]
    patch = subprocess.run(
        [_git_executable(), *show_args],
        cwd=str(cwd),
        capture_output=True,
    )
    if patch.returncode != 0:
        raise GitError(
            show_args, patch.returncode, patch.stderr.decode("utf-8", errors=_DECODE_ERRORS)
        )
    result = subprocess.run(
        [_git_executable(), "patch-id", "--stable"],
        cwd=str(cwd),
        input=patch.stdout,
        capture_output=True,
    )
    if result.returncode != 0:
        raise GitError(
            ["patch-id", "--stable"],
            result.returncode,
            result.stderr.decode("utf-8", errors=_DECODE_ERRORS),
        )
    fields = result.stdout.decode("utf-8", errors=_DECODE_ERRORS).split()
    return fields[0] if fields else None


# -- attestations -----------------------------------------------------------


def read_note(cwd: Path | str, notes_ref: str, sha: str) -> str | None:
    result = run(cwd, "notes", f"--ref={notes_ref}", "show", sha, check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise GitError(
            ["notes", f"--ref={notes_ref}", "show", sha], result.returncode, result.stderr
        )
    return result.stdout.rstrip("\n")


def write_note(cwd: Path | str, notes_ref: str, sha: str, payload: str) -> None:
    run(cwd, "notes", f"--ref={notes_ref}", "add", "-f", "-m", payload, sha)


def fetch_notes(cwd: Path | str, remote: str, notes_ref: str) -> bool:
    """Reconcile remote notes without discarding locally-created attestations."""
    fetched_ref = f"refs/agentic-preflight/fetched-notes-{os.getpid()}"
    result = run(cwd, "fetch", remote, f"+{notes_ref}:{fetched_ref}", check=False)
    if result.returncode != 0:
        if "couldn't find remote ref" in result.stderr.lower():
            return False
        raise GitError(
            ["fetch", remote, f"+{notes_ref}:{fetched_ref}"], result.returncode, result.stderr
        )

    try:
        local = run(cwd, "rev-parse", "--verify", notes_ref, check=False)
        if local.returncode != 0:
            run(cwd, "update-ref", notes_ref, fetched_ref)
        elif is_ancestor(cwd, fetched_ref, notes_ref):
            # Local notes are already a strict superset of the remote history.
            pass
        elif is_ancestor(cwd, notes_ref, fetched_ref):
            run(cwd, "update-ref", notes_ref, fetched_ref)
        else:
            merged = run(
                cwd,
                "notes",
                f"--ref={notes_ref}",
                "merge",
                "-s",
                "manual",
                fetched_ref,
                check=False,
            )
            if merged.returncode != 0:
                run(cwd, "notes", f"--ref={notes_ref}", "merge", "--abort", check=False)
                raise GitError(
                    ["notes", f"--ref={notes_ref}", "merge", "-s", "manual", fetched_ref],
                    merged.returncode,
                    merged.stderr,
                )
        return True
    finally:
        run(cwd, "update-ref", "-d", fetched_ref, check=False)


# -- diff -------------------------------------------------------------------


def changed_files(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    output = run(cwd, "diff", "--name-only", "-z", f"{base}...{head}").stdout
    return [path for path in output.split("\0") if path]


def diff_text(cwd: Path | str, base: str, head: str = "HEAD") -> str:
    return run(cwd, "diff", "--no-color", f"{base}...{head}").stdout


def diff_text_for_path(cwd: Path | str, base: str, head: str, path: str) -> str:
    return run(
        cwd,
        "diff",
        "--no-color",
        f"{base}...{head}",
        "--",
        f":(literal){path}",
    ).stdout


_DIFF_PATH_BATCH_FILES = 256
_DIFF_PATH_BATCH_BYTES = 24_000


def _path_batches(paths: Sequence[str]) -> Iterator[list[str]]:
    """Keep diff path arguments below conservative count and command-size bounds."""
    batch: list[str] = []
    batch_bytes = 0
    for path in paths:
        path_bytes = len(path.encode(errors="surrogateescape")) + len(":(literal)") + 1
        if batch and (
            len(batch) >= _DIFF_PATH_BATCH_FILES
            or batch_bytes + path_bytes > _DIFF_PATH_BATCH_BYTES
        ):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(path)
        batch_bytes += path_bytes
    if batch:
        yield batch


def _split_patches(text: str) -> list[str]:
    """Split ordinary Git patch output at file headers.

    A hunk line always carries a context/addition/deletion prefix, so an exact
    line-start ``diff --git`` marker cannot be confused with file contents.
    """
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    return [
        "".join(lines[start : starts[position + 1] if position + 1 < len(starts) else None])
        for position, start in enumerate(starts)
    ]


def _parse_raw_patch_output(text: str) -> dict[str, str]:
    """Map patches to destination paths using Git's NUL-delimited raw prelude.

    Git represents a file-type change as one raw record but emits the content
    change as a deletion patch followed by an addition patch. Keep both blocks
    together so the result remains byte-for-byte equivalent to a per-path diff.
    """
    try:
        raw, patch_text = text.split("\0\0", 1)
    except ValueError as exc:
        raise ValueError("git diff did not separate its raw inventory from patch output") from exc

    fields = raw.split("\0")
    entries: list[tuple[str, str]] = []
    position = 0
    while position < len(fields):
        metadata = fields[position]
        position += 1
        if not metadata.startswith(":"):
            raise ValueError("git diff returned malformed raw metadata")
        status = metadata.rsplit(" ", 1)[-1]
        path_count = 2 if status.startswith(("R", "C")) else 1
        if position + path_count > len(fields):
            raise ValueError("git diff raw metadata omitted a path")
        entries.append((fields[position + path_count - 1], status))
        position += path_count

    patches = _split_patches(patch_text)
    per_file: dict[str, str] = {}
    patch_position = 0
    for path, status in entries:
        patch_count = 2 if status == "T" else 1
        patch_end = patch_position + patch_count
        if patch_end > len(patches):
            raise ValueError(f"git diff omitted patch blocks for raw entry {path!r} ({status})")
        per_file[path] = per_file.get(path, "") + "".join(patches[patch_position:patch_end])
        patch_position = patch_end

    if patch_position != len(patches):
        raise ValueError(
            f"git diff returned {len(entries)} raw entries with "
            f"{patch_position} expected patch blocks but {len(patches)} patches"
        )
    return per_file


def diff_text_by_path(
    cwd: Path | str, base: str, head: str, paths: Sequence[str]
) -> dict[str, str]:
    """Return complete per-file patches using one Git process per bounded batch."""
    per_file: dict[str, str] = {}
    for batch in _path_batches(paths):
        output = run(
            cwd,
            "diff",
            "--raw",
            "-z",
            "--patch",
            "--no-color",
            f"{base}...{head}",
            "--",
            *(f":(literal){path}" for path in batch),
        ).stdout
        parsed = _parse_raw_patch_output(output)
        missing = [path for path in batch if path not in parsed]
        if missing:
            raise ValueError("git diff omitted requested changed paths: " + ", ".join(missing))
        per_file.update((path, parsed[path]) for path in batch)
    return per_file


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
