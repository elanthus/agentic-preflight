"""A thin, total wrapper over the ``git`` binary.

Deliberately not a git library: the tool's contract is defined in terms of what
git itself does, and shelling out keeps the semantics identical to git's. Query helpers do
not update refs, the index, or the worktree. Git's merge-tree plumbing can still
write unreachable tree objects, which normal object pruning may collect.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from functools import cache
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit, urlunsplit

from agentic_preflight.stages.command import resolve_on_path

MINIMUM_GIT_VERSION = (2, 38)

# Git output is repository data and repositories contain latin-1, so decoding
# must never be able to crash the command that read it. ``backslashreplace``
# keeps the mojibake visible as ``\xe9`` instead of raising. Anything that
# feeds git output back *into* git stays in bytes (see ``commit_patch_id``)
# because the escape text is not the bytes it stands for.
_DECODE_ERRORS = "backslashreplace"

# Paths are the exception: a path read from git is handed straight back to git
# as a pathspec, so it must round-trip to the same bytes. ``surrogateescape``
# does, and ``subprocess`` encodes arguments with the same handler on POSIX.
_PATH_ERRORS = "surrogateescape"
_WINDOWS_PATH_ARGUMENTS = os.name == "nt"

# Resolved once per process. Handed a bare name, Windows' ``CreateProcess``
# searches the parent's current directory before PATH (for this tool, the
# repository under validation) so a repo-committed ``git.exe`` could become
# the git that validates the repository that ships it. POSIX ``execvp``
# searches PATH only; the absolute path pins Windows to the same rule.
_RESOLVED_GIT: str | None = None


def remote_identity(url: str) -> str:
    """Remove URL credentials, query, and fragment (including scp-style usernames)."""
    if "://" in url:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    return url.split("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]


_SCHEME_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+")
_SCP_REMOTE = re.compile(r"[^\s'\"@/]+@[A-Za-z0-9.-]+:[^\s'\"]*")


def redact_remote_credentials(text: str) -> str:
    """Replace every URL or scp-style remote in ``text`` with its credential-free form."""
    text = _SCHEME_URL.sub(lambda match: remote_identity(match.group(0)), text)
    return _SCP_REMOTE.sub(lambda match: remote_identity(match.group(0)), text)


def _git_executable() -> str:
    global _RESOLVED_GIT
    if _RESOLVED_GIT is None:
        resolved = resolve_on_path("git")
        if resolved is None:
            # Falling back to the bare name would hand ``CreateProcess`` its
            # current-directory search back, the exact hole this resolution
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


class GitVersionError(Exception):
    """The installed Git does not meet the supported minimum."""


class OperationInProgress(Exception):
    """A checkout already belongs to a user-started porcelain operation."""

    def __init__(self, operation: str, path: Path | str) -> None:
        self.operation = operation
        self.path = str(Path(path).resolve())
        super().__init__(f"git {operation} is already in progress in {self.path}")


def run(
    cwd: Path | str, *args: str, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run a git command in a directory and return the completed process."""
    result = subprocess.run(
        [_git_executable(), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=_DECODE_ERRORS,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise GitError(list(args), result.returncode, result.stderr)
    return result


def _run_bytes(cwd: Path | str, *args: str) -> bytes:
    """Run a git command and return its raw stdout, without newline translation."""
    result = subprocess.run([_git_executable(), *args], cwd=str(cwd), capture_output=True)
    if result.returncode != 0:
        raise GitError(
            list(args), result.returncode, result.stderr.decode("utf-8", errors=_DECODE_ERRORS)
        )
    return result.stdout


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", errors=_PATH_ERRORS)


def display_path(path: str) -> str:
    """Convert a raw (surrogateescape) path to the text stored, hashed, and shown.

    The result equals the historical ``backslashreplace`` decoding, so it is safe
    for JSON and UTF-8 encoding. Only the raw form may be handed back to Git.
    """
    return path.encode("utf-8", errors=_PATH_ERRORS).decode("utf-8", errors=_DECODE_ERRORS)


def _decode_patch(raw: bytes) -> str:
    """Decode one patch exactly as text-mode ``run`` did, universal newlines included.

    Evidence digests were computed over that text, so CRLF and lone CR still read
    as LF here; only the split point moved to the raw bytes.
    """
    text = raw.decode("utf-8", errors=_DECODE_ERRORS)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def out(cwd: Path | str, *args: str) -> str:
    """Run a git command and return its stripped stdout."""
    return run(cwd, *args).stdout.strip()


def read_blobs(cwd: Path | str, object_ids: Sequence[str]) -> list[bytes]:
    """Read size-vetted immutable blobs in one process, preserving their raw bytes."""
    if not object_ids:
        return []
    if any(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None for oid in object_ids):
        raise ValueError("blob identities must be full Git object IDs")
    result = subprocess.run(
        [_git_executable(), "cat-file", "--batch"],
        cwd=str(cwd),
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        capture_output=True,
    )
    if result.returncode:
        raise GitError(
            ["cat-file", "--batch"],
            result.returncode,
            result.stderr.decode("utf-8", errors=_DECODE_ERRORS),
        )
    blobs = []
    offset = 0
    for oid in object_ids:
        end = result.stdout.index(b"\n", offset)
        actual_oid, kind, size = result.stdout[offset:end].split()
        if actual_oid.decode("ascii") != oid or kind != b"blob":
            raise ValueError("Git returned an unexpected blob identity or type")
        offset = end + 1
        end = offset + int(size)
        if result.stdout[end : end + 1] != b"\n":
            raise ValueError("Git returned an incomplete blob")
        blobs.append(result.stdout[offset:end])
        offset = end + 1
    if offset != len(result.stdout):
        raise ValueError("Git returned unexpected trailing blob data")
    return blobs


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


# -- identity ---------------------------------------------------------------


def current_branch(cwd: Path | str) -> str:
    """Return the name of the checked-out branch."""
    return out(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def rev_parse(cwd: Path | str, ref: str) -> str:
    """Resolve a ref to a commit SHA."""
    return out(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")


def tree_sha(cwd: Path | str, ref: str = "HEAD") -> str:
    """Return the tree SHA of a ref."""
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


def git_path(cwd: Path | str, name: str) -> Path:
    """Resolve a per-worktree Git state path without assuming ``.git`` is a directory."""
    raw = out(cwd, "rev-parse", "--git-path", name)
    path = Path(raw)
    return path if path.is_absolute() else Path(cwd) / path


def operation_in_progress(cwd: Path | str) -> str | None:
    """Report porcelain state that a new operation must not adopt or abort."""
    for operation, names in (
        ("rebase", ("rebase-merge", "rebase-apply")),
        ("cherry-pick", ("CHERRY_PICK_HEAD",)),
        ("merge", ("MERGE_HEAD",)),
    ):
        if any(git_path(cwd, name).exists() for name in names):
            return operation
    return None


def repo_root(cwd: Path | str) -> Path:
    """Return the top-level directory of the working tree."""
    return Path(out(cwd, "rev-parse", "--show-toplevel"))


# -- history ----------------------------------------------------------------


def merge_base(cwd: Path | str, a: str, b: str) -> str:
    """Return the merge base of two refs."""
    return out(cwd, "merge-base", a, b)


def is_ancestor(cwd: Path | str, maybe_ancestor: str, descendant: str) -> bool:
    """Return whether one commit is an ancestor of another."""
    result = run(cwd, "merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False)
    return result.returncode == 0


def version() -> tuple[int, int] | None:
    """``(major, minor)`` of the git binary, or ``None`` if it cannot be read.

    Deliberately not cached. It is read at most twice per run, so a
    process-wide cache would trade an unmeasurable saving for global state
    that leaks between tests.
    """
    result = run(".", "--version", check=False)
    if result.returncode != 0:
        return None
    # "git version 2.46.2.windows.1" and "git version 2.39.3 (Apple Git-146)"
    # are both real; take the first two numeric components and ignore the rest.
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


@cache
def require_minimum_version() -> None:
    """Fail once at CLI startup unless Git 2.38 or newer is available."""
    detected = version()
    required = ".".join(str(part) for part in MINIMUM_GIT_VERSION)
    if detected is None:
        raise GitVersionError(f"Git {required} or newer is required; version could not be read")
    if detected < MINIMUM_GIT_VERSION:
        actual = ".".join(str(part) for part in detected)
        raise GitVersionError(f"Git {required} or newer is required; found Git {actual}")


def merge_tree(cwd: Path | str, left: str, right: str) -> str | None:
    """Return Git's clean merge-result tree, or ``None`` for a conflict.

    ``merge-tree --write-tree`` uses the commits' ancestry as well as their
    snapshots. That distinction is essential when deciding whether an existing
    attestation remains valid against a newly synchronized base. Git may add
    unreachable tree objects, but this does not update refs, the index, or files.
    """
    tree, clean = merge_tree_result(cwd, left, right)
    return tree if clean else None


def merge_tree_result(cwd: Path | str, left: str, right: str) -> tuple[str, bool]:
    """Return Git's merge-result tree and whether the merge was clean.

    A conflicted merge still writes a tree, with conflict markers in the
    conflicted files, so every path the merge touches remains visible in it.
    """
    args = ["merge-tree", "--write-tree", left, right]
    result = run(cwd, *args, check=False)
    if result.returncode in (0, 1):
        return result.stdout.splitlines()[0], result.returncode == 0
    raise GitError(args, result.returncode, result.stderr)


def commit_exists(cwd: Path | str, sha: str) -> bool:
    """Return whether a commit object exists."""
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
    """Return whether a commit changes a path."""
    return path in commit_files(cwd, sha)


def commits_between(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return commits in base..head, oldest first."""
    return _lines(out(cwd, "rev-list", "--reverse", f"{base}..{head}"))


def commit_subject(cwd: Path | str, sha: str) -> str:
    """Return a commit's subject line."""
    return out(cwd, "log", "-1", "--format=%s", sha)


def commit_patch_id(cwd: Path | str, sha: str) -> str | None:
    """Return git's stable patch identity for one commit.

    Patch IDs deliberately ignore commit metadata, so an ordinary cherry-pick
    compares equal to its source even though the commit SHA changes. Empty
    commits have no patch identity and return ``None``.

    The patch travels between the two git processes as raw bytes. Decoding it
    first would fold the raw byte ``0xE9`` and the literal escape text
    ``\\xe9`` into one string, giving two byte-distinct changes the same
    identity, and identity is the whole point of this function.
    """
    return _stable_patch_id(
        cwd, ["show", "--format=", "--no-ext-diff", "--no-textconv", "--binary", sha]
    )


def range_patch_id(cwd: Path | str, base: str, head: str) -> str | None:
    """Return git's stable patch identity for a branch's whole change against ``base``.

    The patch is ``merge-base(base, head)..head``, the same range ``changed_files``
    reports, so a merge from the base or a clean rebase keeps the identity while
    any change to the contribution alters it. Context lines are part of the
    identity, so a base change next to the contribution also alters it. Git
    ignores whitespace and line numbers when hashing; callers deciding content
    equality must compare trees as well. An empty change returns ``None``.
    """
    return _stable_patch_id(
        cwd,
        [
            "diff",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--binary",
            _NO_RENAMES,
            merge_base(cwd, base, head),
            head,
        ],
    )


def _stable_patch_id(cwd: Path | str, patch_args: list[str]) -> str | None:
    """Pipe one Git patch, as raw bytes, into ``git patch-id --stable``."""
    patch = subprocess.run(
        [_git_executable(), *patch_args],
        cwd=str(cwd),
        capture_output=True,
    )
    if patch.returncode != 0:
        raise GitError(
            patch_args, patch.returncode, patch.stderr.decode("utf-8", errors=_DECODE_ERRORS)
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


def patch_ids(cwd: Path | str, base: str, head: str) -> dict[str, str | None]:
    """Return ``commit_patch_id`` for every commit in ``base..head`` in one pipeline.

    One ``git log`` prints each commit's patch exactly as ``git show`` would
    (``--cc`` gives merges the same combined diff), headed by ``commit <sha>``
    so ``git patch-id`` labels each identity with its commit. Bytes stay raw
    between the processes for the reason ``commit_patch_id`` gives. Commits
    with no patch, such as empty commits, map to ``None``.
    """
    log_args = [
        "log",
        "--format=commit %H",
        "--cc",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        f"{base}..{head}",
    ]
    patches = subprocess.run(
        [_git_executable(), *log_args],
        cwd=str(cwd),
        capture_output=True,
    )
    if patches.returncode != 0:
        raise GitError(
            log_args, patches.returncode, patches.stderr.decode("utf-8", errors=_DECODE_ERRORS)
        )
    # Patch lines carry a one-character prefix and base85 lines contain no
    # spaces, so only the format header starts with "commit ".
    ids: dict[str, str | None] = {
        line[len(b"commit ") :].decode("ascii"): None
        for line in patches.stdout.splitlines()
        if line.startswith(b"commit ")
    }
    if not ids:
        return ids
    result = subprocess.run(
        [_git_executable(), "patch-id", "--stable"],
        cwd=str(cwd),
        input=patches.stdout,
        capture_output=True,
    )
    if result.returncode != 0:
        raise GitError(
            ["patch-id", "--stable"],
            result.returncode,
            result.stderr.decode("utf-8", errors=_DECODE_ERRORS),
        )
    for line in result.stdout.decode("utf-8", errors=_DECODE_ERRORS).splitlines():
        patch_id, commit = line.split()
        ids[commit] = patch_id
    return ids


# -- attestations -----------------------------------------------------------


def read_note(cwd: Path | str, notes_ref: str, sha: str) -> str | None:
    # A successful list with no matching entry establishes absence; a failed
    # `notes show` alone cannot distinguish a missing note from a read failure.
    """Return the note on a commit under a notes ref, or None."""
    resolved = rev_parse(cwd, sha)
    listing = out(cwd, "notes", f"--ref={notes_ref}", "list")
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise GitError(["notes", "list"], 1, "unexpected notes listing")
        if fields[1] == resolved:
            return run(cwd, "cat-file", "blob", fields[0]).stdout.rstrip("\n")
    return None


def write_note(cwd: Path | str, notes_ref: str, sha: str, payload: str) -> None:
    """Write or replace the note on a commit under a notes ref."""
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


# Git enables rename detection by default (``diff.renames``). It is a similarity
# heuristic over whichever changes a single diff can see, so restricting a diff
# with a pathspec can pair a deletion with an unrelated addition and hide the
# deleted path behind a rename record. Every diff here is consumed as data keyed
# by path, so renames are disabled and each path reports as A, D, M or T.
_NO_RENAMES = "--no-renames"


def changed_files(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    """Return display paths changed between the merge base and head."""
    return [display_path(path) for path in changed_files_raw(cwd, base, head)]


def changed_files_raw(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    """Return changed paths in raw form, suitable only for handing back to Git."""
    output = _run_bytes(
        cwd,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "-z",
        _NO_RENAMES,
        f"{base}...{head}",
    )
    return [_decode_path(path) for path in output.split(b"\0") if path]


def merge_result_files(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return paths that differ between ``base`` and Git's merge of ``head`` into it.

    ``changed_files`` diffs ``head`` against one merge base. With criss-cross
    history there are several, and Git picks one, so that diff can omit a path the
    real merge changes: a head that merges a side branch already in ``base`` with
    ``-s ours`` reverts it without showing the revert. The merge-tree result
    accounts for every merge base. Conflicted paths are reported too.
    """
    tree, _clean = merge_tree_result(cwd, base, head)
    output = _run_bytes(
        cwd,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        "-z",
        _NO_RENAMES,
        base,
        tree,
    )
    return [display_path(_decode_path(path)) for path in output.split(b"\0") if path]


def non_merge_commit_files(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return the sorted union of paths touched by each non-merge commit in base..head.

    A rebase merge replays exactly these commits and drops merge commits, so a path
    none of them touches keeps the base's content. A merge whose resolution cancels
    an earlier commit hides that commit from both tree comparisons, but not from
    this list. One ``log`` call covers the whole range; ``--root`` lists a root
    commit's files whatever ``log.showRoot`` says.
    """
    output = run(
        cwd,
        "log",
        "--no-merges",
        "--no-show-signature",
        "--root",
        "--format=",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        _NO_RENAMES,
        f"{base}..{head}",
        "--",
    ).stdout
    return sorted({path for path in output.split("\0") if path})


def classified_files(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return the paths a risk or change-scope decision must classify.

    This is the union of ``changed_files`` and ``merge_result_files``, so criss-cross
    history cannot hide a change from the classification. ``base`` must be the base
    tip, not a merge base. Within a run the two lists are equal, because
    synchronization makes the recorded base tip an ancestor of the validated head.
    """
    return sorted({*changed_files(cwd, base, head), *merge_result_files(cwd, base, head)})


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


def _split_patches(data: bytes) -> list[str]:
    """Split ordinary Git patch output at file headers.

    A hunk line always carries a context/addition/deletion prefix, so an exact
    line-start ``diff --git`` marker cannot be confused with file contents.
    Splitting happens on the raw bytes at LF only: a lone CR inside file
    content is not a line break to Git and must not start a phantom patch.
    """
    lines = data.split(b"\n")
    lines = [line + b"\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    starts = [index for index, line in enumerate(lines) if line.startswith(b"diff --git ")]
    ends = [*starts[1:], len(lines)]
    return [
        _decode_patch(b"".join(lines[start:end])) for start, end in zip(starts, ends, strict=True)
    ]


def _raw_patch_entries(raw: bytes) -> list[tuple[str, str]]:
    fields = [_decode_path(field) for field in raw.split(b"\0")]
    entries: list[tuple[str, str]] = []
    position = 0
    while position < len(fields):
        metadata = fields[position]
        position += 1
        if not metadata.startswith(":"):
            raise ValueError("git diff returned malformed raw metadata")
        status = metadata.rsplit(" ", 1)[-1]
        if status.startswith(("R", "C")):
            raise ValueError("git diff reported a rename or copy although renames are disabled")
        if position >= len(fields):
            raise ValueError("git diff raw metadata omitted a path")
        entries.append((fields[position], status))
        position += 1

    return entries


def _parse_raw_patch_output(data: bytes) -> dict[str, str]:
    """Map patches to paths using Git's NUL-delimited raw prelude.

    Git represents a file-type change as one raw record but emits the content
    change as a deletion patch followed by an addition patch. Keep both blocks
    together so the result remains byte-for-byte equivalent to a per-path diff.
    """
    try:
        raw, patch_data = data.split(b"\0\0", 1)
    except ValueError as exc:
        raise ValueError("git diff did not separate its raw inventory from patch output") from exc

    entries = _raw_patch_entries(raw)

    patches = _split_patches(patch_data)
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


_DIFF_STREAM_BYTES = 64 * 1024
_PATCH_HEADER = b"diff --git "


def _read_raw_inventory(stream: IO[bytes]) -> tuple[bytes | None, bytes]:
    inventory = bytearray()
    while True:
        start = max(0, len(inventory) - 1)
        chunk = stream.read(_DIFF_STREAM_BYTES)
        if not chunk:
            return None, b""
        inventory.extend(chunk)
        boundary = inventory.find(b"\0\0", start)
        if boundary >= 0:
            return bytes(inventory[:boundary]), bytes(inventory[boundary + 2 :])


def _diff_chunks(stream: IO[bytes], initial: bytes) -> Iterator[bytes]:
    if initial:
        yield initial
    while chunk := stream.read(_DIFF_STREAM_BYTES):
        yield chunk


def _patch_stream_chunks(chunks: Iterable[bytes]) -> Iterator[tuple[bool, bytes]]:
    """Recognize LF-delimited patch headers without buffering whole lines."""
    prefix = b""
    line_start = True
    for chunk in chunks:
        position = 0
        while position < len(chunk):
            end = chunk.find(b"\n", position)
            end = len(chunk) if end < 0 else end + 1
            piece = chunk[position:end]
            position = end
            ends_line = piece.endswith(b"\n")
            if line_start:
                take = min(len(_PATCH_HEADER) - len(prefix), len(piece))
                prefix += piece[:take]
                piece = piece[take:]
                if len(prefix) < len(_PATCH_HEADER) and not ends_line:
                    continue
                yield prefix.startswith(_PATCH_HEADER), prefix
                prefix = b""
            if piece:
                yield False, piece
            line_start = ends_line
    if prefix:
        yield False, prefix


def _select_streamed_patches(
    entries: list[tuple[str, str]], chunks: Iterable[bytes], requested: set[str]
) -> tuple[dict[str, bytearray], int, int]:
    owners = [path for path, status in entries for _ in range(2 if status == "T" else 1)]
    buffers: dict[str, bytearray] = {}
    target: bytearray | None = None
    count = 0
    for header, chunk in _patch_stream_chunks(chunks):
        if header:
            path = owners[count] if count < len(owners) else None
            target = buffers.setdefault(path, bytearray()) if path in requested else None
            count += 1
        if target is not None:
            target.extend(chunk)
    return buffers, count, len(owners)


def _finish_streamed_diff(
    process: subprocess.Popen[bytes], args: tuple[str, ...], errors: IO[bytes]
) -> None:
    returncode = process.wait()
    if returncode:
        errors.seek(0)
        raise GitError(list(args), returncode, errors.read().decode("utf-8", errors=_DECODE_ERRORS))


def _streamed_diff_text_by_path(
    cwd: Path | str, args: tuple[str, ...], requested: set[str]
) -> dict[str, str]:
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [_git_executable(), *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=errors
        )
        stream = process.stdout
        if stream is None:
            process.kill()
            process.wait()
            raise RuntimeError("git diff stdout stream is unavailable")
        try:
            raw, initial = _read_raw_inventory(stream)
            if raw is None:
                _finish_streamed_diff(process, args, errors)
                raise ValueError("git diff did not separate its raw inventory from patch output")
            entries = _raw_patch_entries(raw)
            buffers, count, expected = _select_streamed_patches(
                entries, _diff_chunks(stream, initial), requested
            )
            _finish_streamed_diff(process, args, errors)
            if count != expected:
                raise ValueError(
                    f"git diff returned {expected} expected patch blocks but {count} patches"
                )
            return {path: _decode_patch(bytes(data)) for path, data in buffers.items()}
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            stream.close()


def diff_text_by_path(
    cwd: Path | str, base: str, head: str, paths: Sequence[str]
) -> dict[str, str]:
    """Return complete per-file patches using one Git process per bounded batch.

    ``paths`` and the returned keys are raw paths from :func:`changed_files_raw`.
    Windows arguments cannot carry surrogateescaped Git names. For those names,
    stream the full diff once and retain only requested raw inventory entries.
    """
    unfiltered = _WINDOWS_PATH_ARGUMENTS and any(
        "\udc80" <= char <= "\udcff" for path in paths for char in path
    )
    batches = [list(paths)] if unfiltered else _path_batches(paths)
    per_file: dict[str, str] = {}
    for batch in batches:
        args = (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--raw",
            "-z",
            "--patch",
            "--no-color",
            _NO_RENAMES,
            f"{base}...{head}",
            "--",
            *(f":(literal){path}" for path in batch if not unfiltered),
        )
        if unfiltered:
            parsed = _parse_raw_patch_output(_run_bytes(cwd, *args))
        else:
            parsed = _parse_raw_patch_output(_run_bytes(cwd, *args))
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
    return (
        out(
            cwd,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
        == ""
    )


def status_for_paths(cwd: Path | str, paths: list[str] | tuple[str, ...]) -> str:
    """Porcelain status limited to paths an operation may overwrite."""
    if not paths:
        return ""
    return run(
        cwd,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
        "--",
        *sorted(set(paths)),
    ).stdout


def changed_paths_in_commits(cwd: Path | str, commits: list[str]) -> list[str]:
    """The stable union of paths touched across a commit stack."""
    return sorted({path for sha in commits for path in commit_files(cwd, sha)})


def is_ignored(cwd: Path | str, path: str) -> bool:
    """Return whether git ignores a path."""
    result = run(cwd, "check-ignore", "-q", "--", path, check=False)
    return result.returncode == 0


# -- remotes ----------------------------------------------------------------


def remote_url(cwd: Path | str, remote: str = "origin") -> str | None:
    """Return a remote's URL, or None when it is not configured."""
    result = run(cwd, "remote", "get-url", remote, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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
