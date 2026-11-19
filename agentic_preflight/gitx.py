"""A thin, total wrapper over the ``git`` binary.

Deliberately not a git library: the tool's contract is defined in terms of what
git itself does, and shelling out keeps the semantics identical to git's. Query helpers do
not update refs, the index, or the worktree. Git's merge-tree plumbing can still
write unreachable tree objects, which normal object pruning may collect.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import IO, NoReturn
from urllib.parse import urlsplit, urlunsplit

from agentic_preflight.stages.command import resolve_on_path
from agentic_preflight.stages.shellstage import (
    kill_on_termination,
    kill_process_tree,
    process_group_kwargs,
    scratch_directory,
)

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

    def __init__(
        self, args: list[str], returncode: int, stderr: str, *, message: str | None = None
    ) -> None:
        super().__init__(message or f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


class NotOnBranch(GitError):
    """HEAD is a symbolic ref to something other than a branch.

    ``symbolic-ref`` itself succeeds here, so there is no failing exit status to report;
    the message names the ref instead. Handlers of ``GitError`` still catch it.
    """

    def __init__(self, ref: str) -> None:
        message = f"HEAD points at {ref}, which is not a branch"
        super().__init__(["symbolic-ref", "--quiet", "HEAD"], 0, "", message=message)
        self.ref = ref


class RemoteTimeout(GitError):
    """A Git command that contacts a remote did not finish within its timeout.

    It is a ``GitError``, so a handler of Git failures still catches it; the
    commands that report it name the remote and the operation instead.
    """

    def __init__(self, args: list[str], *, remote: str, timeout: float, stderr: str) -> None:
        # A remote name such as ``origin`` is kept as given; a URL loses its credentials.
        looks_like_url = "://" in remote or _SCP_REMOTE.fullmatch(remote) is not None
        self.remote = remote_identity(remote) if looks_like_url else remote
        self.operation = args[0] if args else "git"
        self.timeout = timeout
        bound = f"{timeout:g} seconds"
        if self.operation == "push":
            bound += ", a limit that includes the pre-push hook"
        message = (
            f"git {self.operation} with remote {self.remote!r} did not finish within "
            f"{bound}; check the credentials for that remote and the network"
        )
        super().__init__(args, _TIMEOUT_EXIT, stderr, message=message)


class GitVersionError(Exception):
    """The installed Git does not meet the supported minimum."""


class OperationInProgress(Exception):
    """A checkout already belongs to a user-started porcelain operation."""

    def __init__(self, operation: str, path: Path | str) -> None:
        self.operation = operation
        self.path = str(Path(path).resolve())
        super().__init__(f"git {operation} is already in progress in {self.path}")


#: The exit status that ``timeout(1)`` reports, recorded on a ``RemoteTimeout``.
_TIMEOUT_EXIT = 124
#: How long a timed-out remote command has to exit after ``SIGTERM``. Git removes
#: its lock files on that signal; ``SIGKILL`` would leave them behind.
_REMOTE_TERMINATE_GRACE_SECONDS = 3
#: Set for every command that contacts a remote. Git then fails instead of asking
#: for a username or password on the terminal. Credential helpers still answer.
REMOTE_ENV = {"GIT_TERMINAL_PROMPT": "0"}
#: The SSH command for a remote command when the user configured none. With
#: ``BatchMode`` ssh fails instead of asking for a host key, password, or key
#: passphrase. Keys from an SSH agent, and keys without a passphrase, still work.
BATCH_SSH_COMMAND = "ssh -o BatchMode=yes"


def run(
    cwd: Path | str,
    *args: str,
    check: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    remote: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command in a directory and return the completed process.

    Pass ``remote`` for a command that contacts that remote, such as ``fetch`` or
    ``push``. The command then cannot prompt: see ``_run_remote``.
    """
    if remote is not None:
        result = _run_remote(cwd, list(args), remote=remote, timeout=timeout, env=env)
    else:
        result = subprocess.run(
            [_git_executable(), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors=_DECODE_ERRORS,
            timeout=timeout,
            env=env,
        )
    if check and result.returncode != 0:
        raise GitError(list(args), result.returncode, result.stderr)
    return result


def _decode_output(handle: IO[bytes]) -> str:
    """Decode captured output exactly as a text-mode ``subprocess.run`` does."""
    handle.seek(0)
    text = handle.read().decode("utf-8", errors=_DECODE_ERRORS)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _stop_process_tree(process: subprocess.Popen) -> None:
    """Stop a remote command together with the helpers it started.

    The command runs in its own process group, so the signal also reaches the
    transport (``ssh`` or a remote helper). A graceful signal first (``SIGTERM``,
    or ``CTRL_BREAK_EVENT`` on Windows) lets Git remove its lock files; whatever is
    still running after the grace period is killed.
    """
    if process.poll() is None:
        signalled = True
        if sys.platform == "win32":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                signalled = False
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                signalled = False
        if signalled:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_REMOTE_TERMINATE_GRACE_SECONDS)
    # The group can outlive its leader, so this runs even after Git has exited.
    kill_process_tree(process)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_REMOTE_TERMINATE_GRACE_SECONDS)


def _run_remote(
    cwd: Path | str,
    args: list[str],
    *,
    remote: str,
    timeout: float | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess:
    """Run a git command that contacts ``remote`` so that it cannot wait on a person.

    Stdin is empty and Git's own terminal prompt is off (``REMOTE_ENV``). When
    the user configured no SSH command, ``ssh`` runs in batch mode
    (``BATCH_SSH_COMMAND``), so it cannot prompt on any platform. The
    command runs in a new session, so on POSIX it has no controlling terminal and
    ``ssh`` cannot ask about a host key or a key passphrase. Credential helpers,
    askpass programs, and an SSH agent still work. Output goes to temporary files,
    so a helper that outlives Git cannot hold a pipe open. On timeout the whole
    process group is stopped and ``RemoteTimeout`` is raised.
    """
    child_env = {**(os.environ if env is None else env), **REMOTE_ENV}
    if not _user_ssh_command(cwd, child_env):
        child_env["GIT_SSH_COMMAND"] = BATCH_SSH_COMMAND
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
        # The command runs in its own session, so a SIGTERM or SIGHUP sent to this
        # process would not reach it; the guard stops it first. The guard is entered
        # before the command is started, so a signal that arrives while it starts
        # still stops it.
        kill_on_termination(stop=_stop_process_tree) as termination,
    ):
        process = subprocess.Popen(
            [_git_executable(), *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=child_env,
            **process_group_kwargs(),
        )
        termination.bind(process)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process_tree(process)
            raise RemoteTimeout(
                args,
                remote=remote,
                timeout=timeout or 0,
                stderr=_decode_output(stderr_file),
            ) from None
        except BaseException:
            # An interrupt must not leave the transport running behind the tool.
            _stop_process_tree(process)
            raise
        return subprocess.CompletedProcess(
            [_git_executable(), *args],
            returncode,
            _decode_output(stdout_file),
            _decode_output(stderr_file),
        )


def _user_ssh_command(cwd: Path | str, env: Mapping[str, str]) -> bool:
    """Whether the user chose an SSH command, which batch mode must not replace."""
    if env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH"):
        return True
    configured = subprocess.run(
        [_git_executable(), "config", "--get", "core.sshCommand"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=_DECODE_ERRORS,
        env=dict(env),
        stdin=subprocess.DEVNULL,
    )
    return configured.returncode == 0 and bool(configured.stdout.strip())


def _run_bytes(
    cwd: Path | str,
    *args: str,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> bytes:
    """Run a git command and return its raw stdout, without newline translation.

    ``input`` is written to stdin as raw bytes, also without newline translation.
    """
    result = subprocess.run(
        [_git_executable(), *args], cwd=str(cwd), capture_output=True, env=env, input=input
    )
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
    """Return the name of the checked-out branch, or ``"HEAD"`` when detached.

    ``rev-parse --abbrev-ref`` disambiguates a branch that shares its name with a
    tag to ``heads/<name>``, and a refspec built from that publishes to the wrong
    ref. ``symbolic-ref`` names the branch HEAD points at exactly.
    """
    result = run(cwd, "symbolic-ref", "--quiet", "HEAD", check=False)
    ref = result.stdout.strip()
    if result.returncode == 1 and not ref:
        return "HEAD"
    if result.returncode != 0:
        raise GitError(["symbolic-ref", "--quiet", "HEAD"], result.returncode, result.stderr)
    # Plumbing can point HEAD at any ref under refs/; only a branch has a name to publish.
    if not ref.startswith("refs/heads/"):
        raise NotOnBranch(ref)
    return ref.removeprefix("refs/heads/")


def current_branch_or_none(cwd: Path | str) -> str | None:
    """Return ``current_branch``, or ``None`` when HEAD points at a ref that is not a branch.

    For comparisons against a recorded branch or the detached ``"HEAD"``: ``None``
    matches neither, so a non-branch HEAD reads as a mismatch rather than a Git failure.
    """
    try:
        return current_branch(cwd)
    except NotOnBranch:
        return None


_FULL_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: Successful lookups of full object IDs inside :func:`object_cache`, keyed on the
#: repository path and the lookup. ``None`` outside the block, where nothing is cached.
_OBJECT_LOOKUPS: ContextVar[dict[tuple[str, str], object] | None] = ContextVar(
    "agentic_preflight_object_lookups", default=None
)


@contextmanager
def object_cache() -> Iterator[None]:
    """Memoize lookups of full object IDs until the block ends.

    What a full object ID peels to, and the paths that differ between two of them,
    cannot change while one verification runs, so the block runs each such
    lookup once. Symbolic names such as ``HEAD`` or a branch are never cached,
    and neither is a failure: a lookup that failed runs again and fails as it did.
    The cache lives only as long as the outermost block, so it never outlasts one
    verification and never leaks between callers. Nested blocks share it.

    An ID is full only in the repository's own object format: 40 hexadecimal digits
    with SHA-1, 64 with SHA-256. A peel names an object of that format, so a result
    is cached only when it is as long as the ID it came from. Otherwise the string
    may be an abbreviation or a ref name, and the lookup is not cached.
    """
    if _OBJECT_LOOKUPS.get() is not None:
        yield
        return
    token = _OBJECT_LOOKUPS.set({})
    try:
        yield
    finally:
        _OBJECT_LOOKUPS.reset(token)


def _peel(cwd: Path | str, ref: str, kind: str, args: Sequence[str]) -> str:
    lookups = _OBJECT_LOOKUPS.get()
    if lookups is None or _FULL_OBJECT_ID.fullmatch(ref) is None:
        return out(cwd, *args)
    slot = (os.fspath(cwd), f"{ref}^{{{kind}}}")
    cached = lookups.get(slot)
    if isinstance(cached, str):
        return cached
    answer = out(cwd, *args)
    if len(answer) == len(ref):
        lookups[slot] = answer
    return answer


def _known_full_id(lookups: dict[tuple[str, str], object], slot: str, oid: str) -> bool:
    """Whether a cached peel has shown ``oid`` to be a full ID in this repository."""
    return any(
        isinstance(lookups.get((slot, f"{oid}^{{{kind}}}")), str) for kind in ("commit", "tree")
    )


def prime_object_lookups(cwd: Path | str, object_ids: Iterable[str]) -> None:
    """Resolve the commit and tree of each full object ID in one Git process.

    Only inside :func:`object_cache`, and only for successes: an ID Git cannot
    peel is left out, so the later :func:`rev_parse` or :func:`tree_sha` for it
    runs its own command and raises exactly the error it always did. If the
    batch itself fails, nothing is cached.
    """
    lookups = _OBJECT_LOOKUPS.get()
    if lookups is None:
        return
    slot = os.fspath(cwd)
    wanted = [
        (oid, f"{oid}^{{{kind}}}")
        for oid in dict.fromkeys(object_ids)
        if _FULL_OBJECT_ID.fullmatch(oid) is not None
        for kind in ("commit", "tree")
    ]
    wanted = [(oid, name) for oid, name in wanted if (slot, name) not in lookups]
    if not wanted:
        return
    result = subprocess.run(
        [_git_executable(), "cat-file", "--batch-check=%(objectname)"],
        cwd=str(cwd),
        input=("\n".join(name for _, name in wanted) + "\n").encode("ascii"),
        capture_output=True,
    )
    if result.returncode:
        return
    answers = result.stdout.decode("ascii", errors="replace").splitlines()
    if len(answers) != len(wanted):
        return
    for (oid, name), answer in zip(wanted, answers, strict=True):
        # A name Git cannot peel prints "<name> missing", which is not an object ID,
        # and an ID in the other object format peels to an answer of another length.
        if _FULL_OBJECT_ID.fullmatch(answer) is not None and len(answer) == len(oid):
            lookups[slot, name] = answer


def rev_parse(cwd: Path | str, ref: str) -> str:
    """Resolve a ref to a commit SHA."""
    return _peel(cwd, ref, "commit", ("rev-parse", "--verify", f"{ref}^{{commit}}"))


def tree_sha(cwd: Path | str, ref: str = "HEAD") -> str:
    """Return the tree SHA of a ref."""
    return _peel(cwd, ref, "tree", ("rev-parse", f"{ref}^{{tree}}"))


_PATHSPEC_ENV = (
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


def literal_pathspec_env() -> dict[str, str]:
    """Git's environment for ``:(literal)`` pathspecs, free of the global pathspec switches.

    ``GIT_LITERAL_PATHSPECS`` would read ``:(literal)docs`` as a path with that
    name, and ``GIT_ICASE_PATHSPECS`` makes ``ls-tree`` refuse the pathspec.
    """
    return {name: value for name, value in os.environ.items() if name not in _PATHSPEC_ENV}


def _tree_records(
    cwd: Path | str,
    ref: str,
    *,
    scopes: Sequence[str] | None,
    recursive: bool,
    with_size: bool = False,
) -> Iterator[tuple[list[bytes], str]]:
    if scopes is not None and not scopes:
        return
    args = ["ls-tree", *(("-r",) if recursive else ()), "-z", "--full-tree"]
    if with_size:
        args.append("--long")
    args.append(ref)
    env = None
    if scopes is not None:
        args += ["--", *(f":(literal){scope}" for scope in scopes)]
        env = literal_pathspec_env()
    for record in _run_bytes(cwd, *args, env=env).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            yield metadata.split(), _decode_path(path)


def tree_files(
    cwd: Path | str,
    ref: str = "HEAD",
    *,
    scopes: Sequence[str] | None = None,
    recursive: bool = True,
) -> list[str]:
    """Return every blob path in a commit, in Git's order, excluding gitlinks.

    Scopes are literal repository paths and their descendants; an empty sequence
    lists nothing. ``recursive=False`` lists only the top level. Paths round-trip
    as filesystem names, including tabs and newlines.
    """
    return [
        path
        for metadata, path in _tree_records(cwd, ref, scopes=scopes, recursive=recursive)
        if metadata[1] == b"blob"
    ]


@dataclass(frozen=True)
class TreeBlob:
    object_id: str
    size: int


def tree_blobs(
    cwd: Path | str,
    ref: str = "HEAD",
    *,
    scopes: Sequence[str] | None = None,
    recursive: bool = True,
) -> dict[str, TreeBlob]:
    """Return blob identities and sizes from the tree, never from checkout files."""
    return {
        path: TreeBlob(metadata[2].decode("ascii"), int(metadata[3]))
        for metadata, path in _tree_records(
            cwd, ref, scopes=scopes, recursive=recursive, with_size=True
        )
        if metadata[1] == b"blob"
    }


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
    """Return the hook path Git will execute, including ``core.hooksPath``.

    Only the hooks directory is resolved. ``--path-format=absolute`` resolves
    symlinks, including a symlink at the hook itself, which would hand callers
    the link's target, even a target that does not exist, instead of the link.
    """
    # Strip only the line terminator: a configured hooks directory may end in a space.
    raw = run(cwd, "rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout
    return Path(raw.removesuffix("\n").removesuffix("\r")) / name


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


def merge_tree_result(
    cwd: Path | str, left: str, right: str, *, ignore_replacements: bool = False
) -> tuple[str, bool]:
    """Return Git's merge-result tree and whether the merge was clean.

    A conflicted merge still writes a tree, with conflict markers in the
    conflicted files, so every path the merge touches remains visible in it.
    ``ignore_replacements`` builds the merge from the commits themselves rather
    than from any ``refs/replace`` entry, as the review inventory requires.
    """
    args = [
        *(("--no-replace-objects",) if ignore_replacements else ()),
        "merge-tree",
        "--write-tree",
        left,
        right,
    ]
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
    that exists nowhere. The pinned options keep a submodule pointer change in
    the list, as the review inventory does, so that ``diff.ignoreSubmodules`` or
    a committed ``.gitmodules`` cannot hide it from the checks that use this list.
    """
    output = run(
        cwd,
        *_PINNED_DIFF_CONFIG,
        "diff-tree",
        "--ignore-submodules=none",
        "--no-commit-id",
        "--name-only",
        "-z",
        "-r",
        sha,
    ).stdout
    return [path for path in output.split("\0") if path]


def commit_touches(cwd: Path | str, sha: str, path: str) -> bool:
    """Return whether a commit changes a path."""
    return path in commit_files(cwd, sha)


def commits_between(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return commits in base..head, oldest first."""
    return _lines(out(cwd, "rev-list", "--reverse", f"{base}..{head}"))


def merge_commits_between(cwd: Path | str, base: str, head: str) -> list[str]:
    """Return the merge commits in base..head, oldest first."""
    return _lines(out(cwd, "rev-list", "--reverse", "--merges", f"{base}..{head}"))


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


def note_blobs(cwd: Path | str, notes_ref: str) -> dict[str, str]:
    """Map each annotated commit under a notes ref to its note blob.

    A successful list with no matching entry establishes absence; a failed
    `notes show` alone cannot distinguish a missing note from a read failure.
    """
    notes: dict[str, str] = {}
    for line in out(cwd, "notes", f"--ref={notes_ref}", "list").splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise GitError(["notes", "list"], 1, "unexpected notes listing")
        notes[fields[1]] = fields[0]
    return notes


def read_note(cwd: Path | str, notes_ref: str, sha: str) -> str | None:
    """Return the note on a commit under a notes ref, or None."""
    blob = note_blobs(cwd, notes_ref).get(rev_parse(cwd, sha))
    if blob is None:
        return None
    return run(cwd, "cat-file", "blob", blob).stdout.rstrip("\n")


def write_note(cwd: Path | str, notes_ref: str, sha: str, payload: str) -> None:
    """Write or replace the note on a commit under a notes ref.

    The payload goes on stdin: as a ``-m`` argument a large note exceeded the
    operating system's argument limit. ``-F`` and ``-m`` apply the same cleanup,
    so Git stores the same blob either way. Attestation payloads are ASCII JSON;
    UTF-8 matches the argument bytes Git received on Windows and in a UTF-8
    locale. A binary pipe keeps Windows from translating newlines.
    """
    _run_bytes(
        cwd, "notes", f"--ref={notes_ref}", "add", "-f", "-F", "-", sha, input=payload.encode()
    )


def write_blob(cwd: Path | str, data: bytes) -> str:
    """Store ``data`` as a blob, byte for byte, and return its object ID.

    ``--stdin`` without ``--path`` applies no filters, and a binary pipe keeps
    Windows from translating newlines.
    """
    return _run_bytes(cwd, "hash-object", "-w", "--stdin", input=data).decode("ascii").strip()


def write_tree(cwd: Path | str, entries: Sequence[tuple[str, str, str, str]]) -> str:
    """Store one tree level from ``(mode, type, object ID, name)`` entries."""
    payload = b"".join(
        f"{mode} {kind} {oid}\t{name}".encode() + b"\0" for mode, kind, oid, name in entries
    )
    return _run_bytes(cwd, "mktree", "-z", input=payload).decode("ascii").strip()


def commit_tree(
    cwd: Path | str,
    tree: str,
    message: str,
    *,
    name: str,
    email: str,
    date: str,
) -> str:
    """Create a parentless, unsigned commit of ``tree`` with a fixed identity and date.

    The author and committer come from the arguments, never from the user's Git
    configuration or environment, and ``--no-gpg-sign`` overrides
    ``commit.gpgSign``, so the commit carries nothing about who made it.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": date,
    }
    return (
        _run_bytes(
            cwd, "commit-tree", "--no-gpg-sign", tree, "-F", "-", env=env, input=message.encode()
        )
        .decode("ascii")
        .strip()
    )


def tree_entries(cwd: Path | str, commit: str) -> list[tuple[str, str, str, str]]:
    """Every entry of a commit's tree, recursively, as ``(mode, type, object ID, path)``."""
    raw = _run_bytes(cwd, "ls-tree", "-r", "-z", "--full-tree", f"{commit}^{{commit}}")
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        header, _, path = item.partition(b"\t")
        mode, kind, oid = header.decode("ascii").split()
        entries.append((mode, kind, oid, _decode_path(path)))
    return entries


def commit_parents(cwd: Path | str, commit: str) -> list[str]:
    """The parents of a commit, in order."""
    return out(cwd, "rev-list", "--parents", "-n", "1", f"{commit}^{{commit}}").split()[1:]


def read_blob(cwd: Path | str, oid: str) -> bytes:
    """The raw bytes of one blob."""
    return _run_bytes(cwd, "cat-file", "blob", oid)


#: Temporary refs that notes synchronization and publication create and delete.
#: An interrupted process can leave one behind; see ``sweep_notes_staging_refs``.
_FETCHED_NOTES_PREFIX = "refs/agentic-preflight/fetched-notes-"
_PUBLISH_NOTES_PREFIX = "refs/notes/agentic-preflight-publish-"


def fetch_notes(
    cwd: Path | str, remote: str, notes_ref: str, *, timeout: float | None = None
) -> str | None:
    """Reconcile remote notes without discarding locally-created attestations.

    Return the remote notes tip that was reconciled, or ``None`` when the remote
    has no notes ref yet.
    """
    fetched_ref = f"{_FETCHED_NOTES_PREFIX}{os.getpid()}"
    result = run(
        cwd,
        "fetch",
        remote,
        f"+{notes_ref}:{fetched_ref}",
        check=False,
        remote=remote,
        timeout=timeout,
    )
    if result.returncode != 0:
        if "couldn't find remote ref" in result.stderr.lower():
            return None
        raise GitError(
            ["fetch", remote, f"+{notes_ref}:{fetched_ref}"], result.returncode, result.stderr
        )

    try:
        fetched = rev_parse(cwd, fetched_ref)
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
        return fetched
    finally:
        run(cwd, "update-ref", "-d", fetched_ref, check=False)


def remote_ref_tip(
    cwd: Path | str, remote: str, ref: str, *, timeout: float | None = None
) -> str | None:
    """Return the object a remote ref names now, or ``None`` when it does not exist."""
    listing = run(cwd, "ls-remote", remote, ref, remote=remote, timeout=timeout).stdout
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
    return None


def sweep_notes_staging_refs(cwd: Path | str) -> list[str]:
    """Delete temporary notes refs that an interrupted fetch or publication left.

    Call it only while holding the clone's ``notes`` resource lock: every
    command that creates these refs holds that lock, so none is in use.
    """
    listing = out(
        cwd,
        "for-each-ref",
        "--format=%(refname)",
        f"{_FETCHED_NOTES_PREFIX}*",
        f"{_PUBLISH_NOTES_PREFIX}*",
    )
    stale = [
        ref
        for ref in listing.splitlines()
        if ref.startswith((_FETCHED_NOTES_PREFIX, _PUBLISH_NOTES_PREFIX))
    ]
    for ref in stale:
        run(cwd, "update-ref", "-d", ref)
    return stale


@contextmanager
def notes_publication(
    cwd: Path | str, notes_ref: str, base: str | None, commits: Iterable[str]
) -> Iterator[tuple[str | None, list[str]]]:
    """Stage a notes commit that adds only the given commits' local notes to ``base``.

    ``base`` is the remote notes tip, or ``None`` when the remote has no notes
    ref. The commit is built in a temporary notes ref, so notes on other local
    commits never enter it. Yields that ref, or ``None`` when it would be empty,
    and the commits whose note it adds to or changes on ``base``, oldest first.
    The ref is deleted on exit.
    """
    staging = f"{_PUBLISH_NOTES_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    local = note_blobs(cwd, notes_ref)
    try:
        published: dict[str, str] = {}
        if base is not None:
            run(cwd, "update-ref", staging, base, "")
            published = note_blobs(cwd, staging)
        changed = [sha for sha in commits if sha in local and published.get(sha) != local[sha]]
        for sha in changed:
            run(cwd, "notes", f"--ref={staging}", "add", "-f", "-C", local[sha], sha)
        exists = run(cwd, "rev-parse", "--verify", "--quiet", staging, check=False)
        yield (staging if exists.returncode == 0 else None), changed
    finally:
        run(cwd, "update-ref", "-d", staging, check=False)


def fold_published_notes(cwd: Path | str, notes_ref: str, published_ref: str) -> bool:
    """Merge a published notes commit into the local notes ref, keeping local notes.

    Afterwards the local ref descends from what the remote holds, so the next
    ``fetch_notes`` fast-forwards or keeps local history instead of attempting a
    three-way merge that a locally rewritten note would fail. Best effort: on
    failure the local ref is left as it was and ``False`` is returned.
    """
    merged = run(
        cwd, "notes", f"--ref={notes_ref}", "merge", "-q", "-s", "ours", published_ref, check=False
    )
    if merged.returncode != 0:
        run(cwd, "notes", f"--ref={notes_ref}", "merge", "--abort", check=False)
        return False
    return True


# -- diff -------------------------------------------------------------------


# Git enables rename detection by default (``diff.renames``). It is a similarity
# heuristic over whichever changes a single diff can see, so restricting a diff
# with a pathspec can pair a deletion with an unrelated addition and hide the
# deleted path behind a rename record. Every diff here is consumed as data keyed
# by path, so renames are disabled and each path reports as A, D, M or T.
_NO_RENAMES = "--no-renames"

# Review evidence is a hash of patch text, and a verifier on another machine must
# rebuild that text from the same commits. Git shapes it from ``core.*`` and
# ``diff.*`` configuration, so the settings that change a byte or drop a path are
# fixed here instead of being read from whoever runs the command. Each value is
# Git's own default, with two exceptions that are switched off: replacement
# objects and the user-level attributes file. A clone with neither, with no such
# configuration, and with no attributes that change a patch, produces the same
# text as before apart from the ``index`` line, which now carries full object IDs.
# ``/dev/null`` is the spelling Git
# documents for "no file" and resolves itself on Windows.
#
# Attributes, and ``diff.default.xfuncname`` and ``diff.default.funcname``, which
# shape hunk headers and have no reset value, are handled by ``diff_text_by_path``
# below.
#
# Replacement objects (``refs/replace``) exist in one clone only and would show
# content that the commits do not hold, so they are ignored. Settings with no
# command-line option follow. ``diff.default.*`` configures the driver that every
# path without a ``diff`` attribute uses; ``auto`` restores its binary detection.
# ``core.bigFileThreshold`` turns a larger file's patch into a binary marker.
# ``core.precomposeUnicode``, which macOS sets at ``git init``, rewrites a
# decomposed pathspec so that it no longer matches the path in the tree. The
# attributes file is the user-level one.
_PINNED_DIFF_CONFIG = (
    "--no-replace-objects",
    "-c",
    "core.quotePath=true",
    "-c",
    "diff.suppressBlankEmpty=false",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.bigFileThreshold=512m",
    "-c",
    "core.precomposeUnicode=false",
    "-c",
    "diff.default.binary=auto",
)
# The abbreviated form of 0.8.1 and earlier, with Git's default length.
_LEGACY_ABBREV_CONFIG = ("-c", "core.abbrev=auto")
# Which paths a diff lists, and in which order.
_PINNED_INVENTORY_OPTIONS = (
    "--no-ext-diff",
    "--no-textconv",
    _NO_RENAMES,
    "--ignore-submodules=none",
    "--no-relative",
    "-O/dev/null",
)
# The text of each patch.
_PINNED_PATCH_OPTIONS = (
    *_PINNED_INVENTORY_OPTIONS,
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--unified=3",
    "--inter-hunk-context=0",
    "--submodule=short",
)

# Attributes also shape patch text. ``-diff`` (or ``binary``) turns a text file's
# patch into "Binary files ... differ", which hides its content from review, and
# ``diff=<driver>`` lets ``diff.<driver>.xfuncname`` rewrite every hunk header.
# Git reads them from ``.gitattributes`` files, which for a diff between two
# commits means the files in the directory the command runs in, not in either
# commit; from the index; from ``attr.tree`` (Git 2.42) or ``GIT_ATTR_SOURCE``
# (Git 2.40); from the user and system files; and from ``$GIT_DIR/info/attributes``.
# None of these is reproducible by a verifier on another machine.
#
# Review patches therefore run in an environment that has no attributes: the
# command runs in an empty directory that is also the work tree, with an empty
# index, the system file disabled, ``GIT_ATTR_SOURCE`` removed, and ``attr.tree``
# set to a value that names no tree, which Git reads as an empty tree. Every one
# of these works from Git 2.38, the supported minimum; ``--attr-source`` would
# need Git 2.40 and still read ``$GIT_DIR/info/attributes``.
#
# Git always reads ``$GIT_DIR/info/attributes``, so two more rules cover it.
# Patches are built with ``--text`` and the tool decides which files are binary
# with Git's own heuristic, so ``-diff`` there has no effect. A driver there
# with a hunk-header pattern, or ``diff`` set on a binary file, is refused with
# ``ReviewDiffError``.
_ATTRIBUTE_FREE_CONFIG = ("-c", "attr.tree=")
# Git's ``buffer_is_binary`` looks for a NUL byte in this many leading bytes.
_BINARY_PREFIX_BYTES = 8000
# Git's default ``core.bigFileThreshold``, which ``_PINNED_DIFF_CONFIG`` fixes.
# Git treats a larger blob as binary without reading it, so the tool does too.
_BIG_FILE_THRESHOLD = 512 * 1024 * 1024
# Path variables that a command run from another directory must see as absolute.
_REPOSITORY_PATH_VARIABLES = ("GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
_NO_BLOB_MODES = ("000000", "160000")
# Git's built-in diff drivers that set a hunk-header pattern, as of Git 2.54.
# A driver Git adds later is not refused; its headers then differ on this
# machine only, so a verifier elsewhere rejects the note instead of accepting it.
_BUILTIN_HUNK_HEADER_DRIVERS = frozenset(
    {
        "ada", "bash", "bibtex", "cpp", "csharp", "css", "dts", "elixir", "fortran",
        "fountain", "golang", "html", "ini", "java", "kotlin", "markdown", "matlab",
        "objc", "pascal", "perl", "php", "python", "r", "ruby", "rust", "scheme", "tex",
    }
)  # fmt: skip


class ReviewDiffError(ValueError):
    """Git attributes would hide or reshape part of a review patch."""


def changed_files(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    """Return display paths changed between the merge base and head."""
    return [display_path(path) for path in changed_files_raw(cwd, base, head)]


def changed_files_raw(cwd: Path | str, base: str, head: str = "HEAD") -> list[str]:
    """Return changed paths in raw form, suitable only for handing back to Git."""
    lookups = _OBJECT_LOOKUPS.get()
    slot = os.fspath(cwd)
    # Cached only between commits already peeled as full IDs in this repository.
    if (
        lookups is None
        or not _known_full_id(lookups, slot, base)
        or not _known_full_id(lookups, slot, head)
    ):
        return _changed_files_raw(cwd, base, head)
    key = (slot, f"changed:{base}...{head}")
    cached = lookups.get(key)
    if not isinstance(cached, tuple):
        cached = tuple(_changed_files_raw(cwd, base, head))
        lookups[key] = cached
    return list(cached)


def _changed_files_raw(cwd: Path | str, base: str, head: str) -> list[str]:
    output = _run_bytes(
        cwd,
        *_PINNED_DIFF_CONFIG,
        "diff",
        *_PINNED_INVENTORY_OPTIONS,
        "--name-only",
        "-z",
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
    tree, _clean = merge_tree_result(cwd, base, head, ignore_replacements=True)
    output = _run_bytes(
        cwd,
        *_PINNED_DIFF_CONFIG,
        "diff",
        *_PINNED_INVENTORY_OPTIONS,
        "--name-only",
        "-z",
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
        *_PINNED_DIFF_CONFIG,
        "log",
        "--no-merges",
        "--no-show-signature",
        "--root",
        "--format=",
        "--name-only",
        "-z",
        *_PINNED_INVENTORY_OPTIONS,
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


@dataclass(frozen=True)
class _RawEntry:
    """One record of ``git diff --raw``: a path, its status, and both sides."""

    path: str
    status: str
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str

    def blocks(self) -> tuple[tuple[tuple[str, ...], bool], ...]:
        """The blobs behind each patch block Git prints, and whether content changed.

        A type change prints a deletion and then an addition. A submodule side
        and a missing side have no blob.
        """
        old = () if self.old_mode in _NO_BLOB_MODES else (self.old_oid,)
        new = () if self.new_mode in _NO_BLOB_MODES else (self.new_oid,)
        if self.status == "T":
            return ((old, True), (new, True))
        return (((*old, *new), self.old_oid != self.new_oid),)


def _raw_entries(raw: bytes) -> list[_RawEntry]:
    fields = [_decode_path(field) for field in raw.split(b"\0")]
    entries: list[_RawEntry] = []
    position = 0
    while position < len(fields):
        metadata = fields[position]
        position += 1
        parts = metadata[1:].split(" ")
        if not metadata.startswith(":") or len(parts) != 5:
            raise ValueError("git diff returned malformed raw metadata")
        old_mode, new_mode, old_oid, new_oid, status = parts
        if status.startswith(("R", "C")):
            raise ValueError("git diff reported a rename or copy although renames are disabled")
        if position >= len(fields):
            raise ValueError("git diff raw metadata omitted a path")
        entries.append(_RawEntry(fields[position], status, old_mode, new_mode, old_oid, new_oid))
        position += 1

    return entries


def _raw_patch_entries(raw: bytes) -> list[tuple[str, str]]:
    return [(entry.path, entry.status) for entry in _raw_entries(raw)]


def _parse_raw_patch_output(data: bytes) -> dict[str, list[str]]:
    """Map patch blocks to paths using Git's NUL-delimited raw prelude.

    Git represents a file-type change as one raw record but emits the content
    change as a deletion patch followed by an addition patch. Both blocks map to
    the path, in order, so their concatenation is byte-for-byte a per-path diff.
    """
    try:
        raw, patch_data = data.split(b"\0\0", 1)
    except ValueError as exc:
        raise ValueError("git diff did not separate its raw inventory from patch output") from exc

    entries = _raw_patch_entries(raw)

    patches = _split_patches(patch_data)
    per_file: dict[str, list[str]] = {}
    patch_position = 0
    for path, status in entries:
        patch_count = 2 if status == "T" else 1
        patch_end = patch_position + patch_count
        if patch_end > len(patches):
            raise ValueError(f"git diff omitted patch blocks for raw entry {path!r} ({status})")
        per_file.setdefault(path, []).extend(patches[patch_position:patch_end])
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
    entries: list[tuple[str, str]],
    chunks: Iterable[bytes],
    requested: set[str],
    stop_after: int | None = None,
) -> tuple[dict[str, list[bytearray]], int, int, str | None]:
    """Collect the requested patch blocks, stopping early once ``stop_after`` is passed.

    The running count is a lower bound on the decoded size of the collected text:
    decoding keeps every valid byte, widens an invalid one, and only folds a CR,
    so the raw byte count less the CR count can never exceed it. Once that bound
    passes ``stop_after`` the read stops and the path being read is returned last.
    """
    owners = [path for path, status in entries for _ in range(2 if status == "T" else 1)]
    buffers: dict[str, list[bytearray]] = {}
    target: bytearray | None = None
    target_path: str | None = None
    count = 0
    measured = 0
    for header, chunk in _patch_stream_chunks(chunks):
        if header:
            path = owners[count] if count < len(owners) else None
            target = None
            target_path = None
            if path in requested:
                target = bytearray()
                target_path = path
                buffers.setdefault(path, []).append(target)
            count += 1
        if target is not None:
            target.extend(chunk)
            if stop_after is not None:
                measured += len(chunk) - chunk.count(b"\r")
                if measured > stop_after:
                    return buffers, count, len(owners), target_path
    return buffers, count, len(owners), None


def _finish_streamed_diff(
    process: subprocess.Popen[bytes], args: tuple[str, ...], errors: IO[bytes]
) -> None:
    returncode = process.wait()
    if returncode:
        errors.seek(0)
        raise GitError(list(args), returncode, errors.read().decode("utf-8", errors=_DECODE_ERRORS))


def _streamed_diff_text_by_path(
    cwd: Path | str,
    args: tuple[str, ...],
    requested: set[str],
    env: Mapping[str, str] | None = None,
    stop_after: int | None = None,
) -> tuple[dict[str, list[str]], tuple[str, int] | None]:
    """Read requested patch blocks from one streamed ``git diff``.

    With ``stop_after``, the read ends once a lower bound on the decoded text
    passes it. The second value then names the path being read with a lower
    bound on its decoded size, the first holds only the paths read in full, and
    Git is stopped without reading the rest of its output.
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [_git_executable(), *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=errors,
            env=env,
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
            buffers, count, expected, stopped = _select_streamed_patches(
                entries, _diff_chunks(stream, initial), requested, stop_after
            )
            if stopped is not None:
                # Git may already have failed. Its error, not an early stop, is the result.
                if process.poll() not in (None, 0):
                    _finish_streamed_diff(process, args, errors)
                partial = buffers.pop(stopped)
                size = sum(len(block) - block.count(b"\r") for block in partial)
                return _decode_blocks(buffers), (stopped, size)
            _finish_streamed_diff(process, args, errors)
            if count != expected:
                raise ValueError(
                    f"git diff returned {expected} expected patch blocks but {count} patches"
                )
            return _decode_blocks(buffers), None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            stream.close()


def _decode_blocks(buffers: Mapping[str, list[bytearray]]) -> dict[str, list[str]]:
    return {
        path: [_decode_patch(bytes(block)) for block in blocks] for path, blocks in buffers.items()
    }


def _pathspec_batches(paths: Sequence[str]) -> Iterator[tuple[list[str], tuple[str, ...], bool]]:
    """Yield each batch of raw paths, its pathspec arguments, and whether it is unfiltered.

    Windows arguments cannot carry surrogateescaped Git names. For those names,
    a command runs once over the whole diff and keeps only the requested paths.
    """
    unfiltered = _WINDOWS_PATH_ARGUMENTS and any(
        "\udc80" <= char <= "\udcff" for path in paths for char in path
    )
    if unfiltered:
        yield list(paths), (), True
        return
    for batch in _path_batches(paths):
        yield batch, tuple(f":(literal){path}" for path in batch), False


def _require_every_path(batch: Sequence[str], found: Mapping[str, object]) -> None:
    missing = [path for path in batch if path not in found]
    if missing:
        raise ValueError("git diff omitted requested changed paths: " + ", ".join(missing))


def _patches(
    cwd: Path | str,
    base: str,
    head: str,
    paths: Sequence[str],
    *,
    config: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Return each path's patch blocks using one Git process per bounded batch."""
    per_file: dict[str, list[str]] = {}
    for batch, pathspecs, unfiltered in _pathspec_batches(paths):
        args = (
            *_PINNED_DIFF_CONFIG,
            *config,
            "diff",
            *_PINNED_PATCH_OPTIONS,
            *options,
            "--raw",
            "-z",
            "--patch",
            f"{base}...{head}",
            "--",
            *pathspecs,
        )
        if unfiltered:
            parsed, _ = _streamed_diff_text_by_path(cwd, args, set(batch), env)
        else:
            parsed = _parse_raw_patch_output(_run_bytes(cwd, *args, env=env))
        _require_every_path(batch, parsed)
        per_file.update((path, parsed[path]) for path in batch)
    return per_file


def _raw_inventory(
    cwd: Path | str,
    base: str,
    head: str,
    paths: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> dict[str, _RawEntry]:
    """Return the modes and full object IDs on both sides of each requested path."""
    found: dict[str, _RawEntry] = {}
    for batch, pathspecs, _unfiltered in _pathspec_batches(paths):
        output = _run_bytes(
            cwd,
            *_PINNED_DIFF_CONFIG,
            "diff",
            *_PINNED_INVENTORY_OPTIONS,
            "--raw",
            "-z",
            "--no-abbrev",
            f"{base}...{head}",
            "--",
            *pathspecs,
            env=env,
        )
        requested = set(batch)
        entries = _raw_entries(output.removesuffix(b"\0")) if output else []
        found.update((entry.path, entry) for entry in entries if entry.path in requested)
        _require_every_path(batch, found)
    return found


def _read_exactly(stream: IO[bytes], size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("git cat-file returned an incomplete object")
    return data


def _unreadable_blob(
    process: subprocess.Popen[bytes],
    args: tuple[str, ...],
    errors: IO[bytes],
    oid: str,
    reply: list[bytes],
) -> NoReturn:
    """Raise Git's own error for a blob ``cat-file`` could not read.

    In a partial clone, a blob that cannot be fetched from the promisor remote is
    reported missing, and Git's stderr says why. That is a Git failure, not a bug.
    """
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    returncode = process.wait()
    errors.seek(0)
    stderr = errors.read().decode("utf-8", errors=_DECODE_ERRORS)
    detail = stderr.strip() or b" ".join(reply).decode("utf-8", errors=_DECODE_ERRORS)
    raise GitError(
        list(args),
        returncode or 1,
        stderr,
        message=f"git {' '.join(args)} could not read blob {oid}: {detail or 'no reply'}",
    )


def _binary_blobs(
    cwd: Path | str, object_ids: Iterable[str], env: Mapping[str, str] | None = None
) -> set[str]:
    """Return the blobs that Git's own heuristic, without attributes, calls binary.

    A blob is binary when it is larger than Git's default big-file threshold or
    has a NUL byte in its first 8,000 bytes. Only that prefix is kept in memory.
    """
    wanted = sorted(set(object_ids))
    if not wanted:
        return set()
    binary: set[str] = set()
    # Like the diff, read the blobs the commits hold, not a clone-local replacement.
    args = ("--no-replace-objects", "cat-file", "--batch-command")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [_git_executable(), *args],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            env=env,
        )
        requests, replies = process.stdin, process.stdout
        if requests is None or replies is None:
            process.kill()
            process.wait()
            raise RuntimeError("git cat-file streams are unavailable")
        try:
            for oid in wanted:
                requests.write(f"info {oid}\n".encode("ascii"))
                requests.flush()
                fields = replies.readline().split()
                if len(fields) != 3 or fields[0].decode("ascii") != oid or fields[1] != b"blob":
                    _unreadable_blob(process, args, errors, oid, fields)
                size = int(fields[2])
                if size > _BIG_FILE_THRESHOLD:
                    binary.add(oid)
                    continue
                requests.write(f"contents {oid}\n".encode("ascii"))
                requests.flush()
                header = replies.readline().split()
                if header != fields:
                    _unreadable_blob(process, args, errors, oid, header)
                prefix = _read_exactly(replies, min(size, _BINARY_PREFIX_BYTES))
                if b"\0" in prefix:
                    binary.add(oid)
                remaining = size - len(prefix)
                while remaining:
                    remaining -= len(_read_exactly(replies, min(remaining, _DIFF_STREAM_BYTES)))
                if _read_exactly(replies, 1) != b"\n":
                    raise ValueError("git cat-file returned an incomplete object")
            requests.close()
            _finish_streamed_diff(process, args, errors)
        finally:
            if not requests.closed:
                requests.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            replies.close()
    return binary


def _patch_environment() -> dict[str, str]:
    """Copy the environment without ``GIT_DIFF_OPTS``, which overrides ``--unified``."""
    return {name: value for name, value in os.environ.items() if name != "GIT_DIFF_OPTS"}


_C_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("f"): 12,
    ord("n"): 10,
    ord("r"): 13,
    ord("t"): 9,
    ord("v"): 11,
    ord("\\"): ord("\\"),
    ord('"'): ord('"'),
}


def _unquote_c_style(value: bytes, start: int) -> tuple[bytes, int] | None:
    """Git's ``unquote_c_style``: the path quoted at ``start`` and the index after it.

    Returns ``None`` for broken quoting, which Git then reads as an unquoted path.
    """
    out = bytearray()
    index = start + 1
    while index < len(value):
        byte = value[index]
        index += 1
        if byte == ord('"'):
            return bytes(out), index
        if byte != ord("\\"):
            out.append(byte)
            continue
        if index >= len(value):
            return None
        escape = value[index]
        index += 1
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
        elif ord("0") <= escape <= ord("3"):
            digits = value[index : index + 2]
            if len(digits) < 2 or not all(ord("0") <= digit <= ord("7") for digit in digits):
                return None
            out.append((escape - 48) << 6 | (digits[0] - 48) << 3 | (digits[1] - 48))
            index += 2
        else:
            return None
    return None


def _alternate_entries(value: str) -> list[str]:
    """Split ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` as Git's ``parse_alt_odb_entry`` does.

    An entry that starts with ``"`` is C-quoted and may contain the separator, one
    that starts with ``#`` is a comment, and empty entries are skipped.
    """
    raw = os.fsencode(value)
    separator = os.fsencode(os.pathsep)[0]
    entries: list[str] = []
    index = 0
    while index < len(raw):
        quoted = _unquote_c_style(raw, index) if raw[index] == ord('"') else None
        if quoted is not None:
            entry, index = quoted
            index += 1 if index < len(raw) else 0
        else:
            end = raw.find(separator, index)
            end = len(raw) if end < 0 else end
            entry = b"" if raw[index] == ord("#") else raw[index:end]
            index = end + 1
        if entry:
            entries.append(os.fsdecode(entry))
    return entries


def _quote_alternate(path: str) -> str:
    """Write an alternate so Git reads it back as exactly ``path``."""
    raw = os.fsencode(path)
    plain = (
        raw[:1] not in (b'"', b"#")
        and os.fsencode(os.pathsep) not in raw
        and all(byte >= 0x20 and byte != 0x7F for byte in raw)
    )
    if plain:
        return path
    escaped = bytearray(b'"')
    for byte in raw:
        if byte in (ord('"'), ord("\\")):
            escaped += bytes((ord("\\"), byte))
        elif byte < 0x20 or byte == 0x7F:
            escaped += f"\\{byte:03o}".encode()
        else:
            escaped.append(byte)
    escaped.append(ord('"'))
    return os.fsdecode(bytes(escaped))


def _absolute_repository_variables(cwd: Path | str) -> dict[str, str]:
    """Copy the patch environment with repository paths resolved against ``cwd``.

    Git resolves relative alternates, quoted or not, against its working directory,
    which for a review patch is an empty temporary one.
    """
    env = _patch_environment()
    base = Path(cwd).resolve()
    for name in _REPOSITORY_PATH_VARIABLES:
        if env.get(name):
            env[name] = str(base / env[name])
    alternates = env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
    if alternates:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(
            _quote_alternate(str(base / entry)) for entry in _alternate_entries(alternates)
        )
    return env


@contextmanager
def _attribute_free(cwd: Path | str) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield a directory and environment in which Git sees no attributes but its info file.

    Commands must also pass ``_ATTRIBUTE_FREE_CONFIG``. See the comment above it.
    """
    repository = git_dir(cwd)
    env = _absolute_repository_variables(cwd)
    env.pop("GIT_ATTR_SOURCE", None)
    with scratch_directory("agentic-preflight-diff-") as empty:
        env.update(
            GIT_DIR=str(repository),
            GIT_WORK_TREE=empty,
            GIT_INDEX_FILE=str(Path(empty) / "index"),
            GIT_ATTR_NOSYSTEM="1",
        )
        yield empty, env


#: The values ``git check-attr`` prints for an attribute's states. A driver with one
#: of these names prints the same value, so the output cannot tell the two apart.
_ATTRIBUTE_STATES = frozenset({"unspecified", "unset", "set"})


def _diff_driver_config(cwd: Path | str, env: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """Return the drivers that set a hunk-header pattern, and those whose ``binary``
    value is false, which makes Git print a binary file's content as text, or is one
    Git would reject (see :func:`_forces_text`).

    One query reads both. As in Git, the last value of a key wins. ``default`` is
    left out of the second set: the pinned options set its ``binary`` to ``auto``.
    """
    result = subprocess.run(
        [_git_executable(), "config", "-z", "--get-regexp", r"^diff\..+\.(x?funcname|binary)$"],
        cwd=str(cwd),
        capture_output=True,
        env=env,
    )
    if result.returncode not in (0, 1):
        raise GitError(
            ["config", "--get-regexp"],
            result.returncode,
            result.stderr.decode("utf-8", errors=_DECODE_ERRORS),
        )
    headers: set[str] = set()
    binary: dict[str, bytes | None] = {}
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"\n")
        driver, _, variable = _decode_path(key)[len("diff.") :].rpartition(".")
        if variable == "binary":
            binary[driver] = value if separator else None  # the last value wins
        else:
            headers.add(driver)
    text_forcing = {
        driver for driver, value in binary.items() if driver != "default" and _forces_text(value)
    }
    return headers, text_forcing


def _forces_text(value: bytes | None) -> bool:
    """Whether a ``diff.<driver>.binary`` value may make Git print binary content as text.

    Git reads the setting as a tristate: ``auto`` keeps its own detection, a true
    boolean (or a key with no ``=``) marks the file binary, and a false one, which
    includes an empty value and any integer equal to zero, prints it as text. Git
    parses integers with ``strtoimax`` in base 0 (``0x`` hex, a leading ``0`` octal)
    and an optional ``k``, ``m`` or ``g`` suffix. The zero test reads the digits
    instead of converting them, so a zero of any length counts. Any value Git would
    reject also counts: Git's own diff then fails, so refusing it costs nothing.
    """
    if value is None:
        return False
    text = value.decode("utf-8", errors=_DECODE_ERRORS).strip().lower()
    if text in ("auto", "true", "yes", "on"):
        return False
    if text in ("false", "no", "off", ""):
        return True
    number = text[:-1] if text[-1:] in ("k", "m", "g") else text
    digits = number[1:] if number[:1] in ("+", "-") else number
    if digits.startswith("0x"):
        body, allowed = digits[2:], set("0123456789abcdef")
    elif digits.startswith("0"):
        body, allowed = digits[1:], set("01234567")
    elif digits:
        body, allowed = digits, set("0123456789")
    else:
        return True
    return not set(body) <= allowed or set(body) <= {"0"}


def _refuse_local_diff_attributes(
    cwd: Path | str, env: Mapping[str, str], paths: Sequence[str], binary_paths: set[str]
) -> None:
    """Refuse a ``diff`` attribute in ``$GIT_DIR/info/attributes`` that would change a patch.

    In the attribute-free environment that file is the only source left. ``-diff``
    there is harmless, because text patches are built with ``--text``, and so is a
    driver that only sets ``textconv`` or ``command``, which the pinned options
    disable. A driver with a hunk-header pattern, built in or configured, would
    rewrite hunk headers, and ``diff`` set on a binary file, or a driver with
    ``binary`` set to false, would print its content. Refusing these before any
    patch is read lets a bounded read refuse them wherever it stops.

    Every path without a ``diff`` attribute uses Git's ``default`` driver, so a
    pattern configured for it would rewrite every hunk header. Git has no value
    that restores the built-in rule, so that configuration is refused too. So is a
    pattern, or ``binary`` set to false, for a driver named ``set``, ``unset`` or
    ``unspecified``: ``check-attr`` prints ``diff=unset`` exactly as it prints
    ``-diff``, yet Git applies the driver, so the attribute cannot be told from the
    harmless state.
    """
    if not paths:
        return
    configured, text_forcing = _diff_driver_config(cwd, env)
    if "default" in configured:
        raise ReviewDiffError(
            "Git configuration sets diff.default.xfuncname or diff.default.funcname, which "
            "rewrites the hunk headers of the review patch on this machine only. "
            "Remove that setting."
        )
    colliding = sorted(configured & _ATTRIBUTE_STATES)
    if colliding:
        raise ReviewDiffError(
            f"Git configuration sets diff.{colliding[0]}.xfuncname or "
            f"diff.{colliding[0]}.funcname. Git cannot tell a driver named "
            f"{colliding[0]!r} from the attribute state of that name, and the driver "
            "would rewrite the hunk headers of the review patch on this machine only. "
            "Remove that setting."
        )
    text_forcing_states = text_forcing & _ATTRIBUTE_STATES
    if text_forcing_states:
        name = sorted(text_forcing_states)[0]
        raise ReviewDiffError(
            f"Git configuration sets diff.{name}.binary to false. Git cannot tell a "
            f"driver named {name!r} from the attribute state of that name, and the driver "
            "would print binary content into the review patch on this machine only. "
            "Remove that setting."
        )
    output = _run_bytes(
        cwd,
        *_PINNED_DIFF_CONFIG,
        *_ATTRIBUTE_FREE_CONFIG,
        "check-attr",
        "-z",
        "--stdin",
        "diff",
        env=env,
        input=b"".join(path.encode("utf-8", errors=_PATH_ERRORS) + b"\0" for path in paths),
    )
    fields = [_decode_path(field) for field in output.split(b"\0")[:-1]]
    if len(fields) % 3:
        raise ValueError("git check-attr returned malformed output")
    drivers = _BUILTIN_HUNK_HEADER_DRIVERS | configured
    entries = list(zip(fields[0::3], fields[1::3], fields[2::3], strict=True))
    for path, _name, value in entries:
        shown = display_path(path)
        if path in binary_paths and value in text_forcing:
            raise ReviewDiffError(
                f"$GIT_DIR/info/attributes sets diff={value} on binary file {shown}, and "
                f"diff.{value}.binary = false would print its content into the review patch. "
                "Remove the attribute from that file."
            )
        if value == "set" and path in binary_paths:
            raise ReviewDiffError(
                f"$GIT_DIR/info/attributes sets the diff attribute on binary file {shown}, "
                "which would print its content into the review patch. "
                "Remove the attribute from that file."
            )
        if value in _ATTRIBUTE_STATES:
            continue
        if value in drivers:
            raise ReviewDiffError(
                f"$GIT_DIR/info/attributes sets diff={value} on {shown}. That driver rewrites "
                "hunk headers on this machine only. Remove the attribute from that file."
            )


def _entry_blobs(entries: Iterable[_RawEntry]) -> set[str]:
    return {oid for entry in entries for blobs, _changed in entry.blocks() for oid in blobs}


def _patch_lines(patch: str) -> list[str]:
    # Split on LF only: a lone CR or form feed in file content is not a Git line break.
    lines = [line + "\n" for line in patch.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


def _has_binary_marker(block: str) -> bool:
    """Whether a patch block is Git's "Binary files ... differ" form.

    The marker is the last line of a block that has no hunks. Lines split on LF
    only, so file content can put a marker-like line in a block only inside a
    hunk, and a block with a hunk is not binary.
    """
    lines = _patch_lines(block)
    return (
        bool(lines)
        and lines[-1].startswith("Binary files ")
        and lines[-1].rstrip("\n").endswith(" differ")
        and not any(line.startswith("@@ ") for line in lines)
    )


def _check_binary_markers(
    path: str, blocks: list[str], entry: _RawEntry, binary: set[str], *, strict: bool
) -> None:
    """Refuse a patch whose binary markers disagree with the blobs behind it.

    A "Binary files ... differ" block for content that is not binary hides that
    content from review. With ``strict``, binary content printed as text is
    refused too, because a verifier without the same attributes prints a marker.
    """
    expected = entry.blocks()
    if len(blocks) != len(expected):
        raise ValueError(f"git diff returned {len(blocks)} patch blocks for {path!r}")
    shown = display_path(path)
    for block, (blobs, changed) in zip(blocks, expected, strict=True):
        marked = _has_binary_marker(block)
        is_binary = any(oid in binary for oid in blobs)
        if marked and not is_binary:
            if strict:
                raise ReviewDiffError(
                    f"Git reported {shown} as binary, but its content is text, so the review "
                    "would not see it. $GIT_DIR/info/attributes is the only attribute source "
                    "left; remove the entry there that applies to this file."
                )
            raise ReviewDiffError(
                f"The earlier review text shows {shown} as binary, but its content is "
                "text, so that review never saw it. Validate the commit again to "
                "replace the note."
            )
        if strict and changed and is_binary and not marked:
            raise ReviewDiffError(
                f"Git printed binary file {shown} as text. A diff driver that "
                "$GIT_DIR/info/attributes names for it, such as one with "
                "diff.<driver>.binary = false, prints its content on this machine only. "
                "Remove that entry."
            )


def diff_text_by_path(
    cwd: Path | str,
    base: str,
    head: str,
    paths: Sequence[str],
    *,
    legacy: bool = False,
    refuse_hidden: bool = True,
) -> dict[str, str]:
    """Return complete per-file patches using one Git process per bounded batch.

    ``paths`` and the returned keys are raw paths from :func:`changed_files_raw`.

    The text depends only on the two commits. The pinned options above keep
    ``core.*`` and ``diff.*`` configuration out of it, ``GIT_DIFF_OPTS`` is removed
    from Git's environment, and Git runs without attributes. Text files are diffed with ``--text``; a file is binary when
    either side is binary by Git's own heuristic, and its patch is Git's
    "Binary files ... differ" marker. A ``diff`` attribute in
    ``$GIT_DIR/info/attributes``, which Git always reads, raises
    :class:`ReviewDiffError` when it would still change the text.

    ``legacy=True`` reproduces the patches of 0.8.1 and earlier releases, whose
    ``index`` lines abbreviate object IDs to Git's default length, which grows with
    the clone, and whose text follows the checkout's attributes. It exists to verify notes
    those releases wrote. It raises :class:`ReviewDiffError` when such text marks
    a text file as binary, so a note cannot rely on an attribute to hide content.
    ``refuse_hidden=False`` skips that check, for a caller that first needs to know
    whether the earlier text reproduces a digest at all.
    """
    if not paths:
        return {}
    if legacy:
        return _legacy_diff_text_by_path(cwd, base, head, paths, refuse_hidden=refuse_hidden)
    with _attribute_free(cwd) as (root, env):
        entries = _raw_inventory(root, base, head, paths, env)
        binary = _binary_blobs(root, _entry_blobs(entries.values()), env)
        binary_paths = {path for path in paths if _entry_blobs([entries[path]]) & binary}
        _refuse_local_diff_attributes(root, env, paths, binary_paths)
        per_file = _patches(
            root,
            base,
            head,
            [path for path in paths if path not in binary_paths],
            config=_ATTRIBUTE_FREE_CONFIG,
            options=("--full-index", "--text"),
            env=env,
        )
        per_file |= _patches(
            root,
            base,
            head,
            [path for path in paths if path in binary_paths],
            config=_ATTRIBUTE_FREE_CONFIG,
            options=("--full-index",),
            env=env,
        )
    for path in paths:
        _check_binary_markers(path, per_file[path], entries[path], binary, strict=True)
    return {path: "".join(per_file[path]) for path in paths}


def _legacy_diff_text_by_path(
    cwd: Path | str, base: str, head: str, paths: Sequence[str], *, refuse_hidden: bool
) -> dict[str, str]:
    per_file = _patches(
        cwd, base, head, paths, config=_LEGACY_ABBREV_CONFIG, env=_patch_environment()
    )
    marked = [path for path in paths if any(map(_has_binary_marker, per_file[path]))]
    if refuse_hidden and marked:
        entries = _raw_inventory(cwd, base, head, marked)
        binary = _binary_blobs(cwd, _entry_blobs(entries[path] for path in marked))
        for path in marked:
            _check_binary_markers(path, per_file[path], entries[path], binary, strict=False)
    return {path: "".join(per_file[path]) for path in paths}


@dataclass(frozen=True)
class BoundedPatches:
    """Per-file patches read until their decoded size passed a bound.

    When ``complete`` is true, ``patches`` equals what :func:`diff_text_by_path`
    returns for the same paths. Otherwise it holds only the paths read in full,
    and ``partial``, when set, names the path being read when the read stopped
    with a lower bound on the UTF-8 size of its decoded patch.
    """

    patches: dict[str, str]
    complete: bool
    partial: tuple[str, int] | None = None


def _bounded_patches(
    cwd: Path | str,
    base: str,
    head: str,
    paths: Sequence[str],
    *,
    options: tuple[str, ...],
    env: Mapping[str, str],
    stop_after: int,
    measured: int,
) -> tuple[dict[str, list[str]], tuple[str, int] | None, int, bool]:
    """Stream :func:`_patches` with the attribute-free options until past ``stop_after``.

    Returns the blocks of the paths read in full, the partially read path with a
    lower bound on its size, the running measure, and whether every path was read.
    """
    per_file: dict[str, list[str]] = {}
    for batch, pathspecs, _unfiltered in _pathspec_batches(paths):
        if measured > stop_after:
            return per_file, None, measured, False
        args = (
            *_PINNED_DIFF_CONFIG,
            *_ATTRIBUTE_FREE_CONFIG,
            "diff",
            *_PINNED_PATCH_OPTIONS,
            *options,
            "--raw",
            "-z",
            "--patch",
            f"{base}...{head}",
            "--",
            *pathspecs,
        )
        parsed, partial = _streamed_diff_text_by_path(
            cwd, args, set(batch), env, stop_after=stop_after - measured
        )
        if partial is not None:
            per_file.update(parsed)
            return per_file, partial, measured + partial[1], False
        _require_every_path(batch, parsed)
        per_file.update((path, parsed[path]) for path in batch)
        measured += sum(len("".join(parsed[path]).encode()) for path in batch)
    return per_file, None, measured, True


def diff_text_by_path_bounded(
    cwd: Path | str, base: str, head: str, paths: Sequence[str], *, stop_after_bytes: int
) -> BoundedPatches:
    """Read the patches of :func:`diff_text_by_path`, stopping once they pass a size.

    The size is the UTF-8 length of the decoded patch text. The read uses the same
    attribute-free environment, binary decisions, refusals, and options as
    :func:`diff_text_by_path`, and Git is stopped as soon as a lower bound on the
    text read passes ``stop_after_bytes``, so an oversized diff costs a bounded
    read of patch text. Deciding which files are binary still reads every changed
    blob, keeping only its first 8,000 bytes. Only the current patch format is
    read; the earlier form is never bounded. As in :func:`diff_text_by_path`, the
    unfiltered Windows read still streams the patches of paths that were not
    requested; only requested text counts.
    """
    if not paths:
        return BoundedPatches({}, complete=True)
    with _attribute_free(cwd) as (root, env):
        entries = _raw_inventory(root, base, head, paths, env)
        binary = _binary_blobs(root, _entry_blobs(entries.values()), env)
        binary_paths = {path for path in paths if _entry_blobs([entries[path]]) & binary}
        _refuse_local_diff_attributes(root, env, paths, binary_paths)
        per_file: dict[str, list[str]] = {}
        measured = 0
        groups = (
            ([path for path in paths if path not in binary_paths], ("--full-index", "--text")),
            ([path for path in paths if path in binary_paths], ("--full-index",)),
        )
        for group, options in groups:
            if not group:
                continue
            read, partial, measured, complete = _bounded_patches(
                root,
                base,
                head,
                group,
                options=options,
                env=env,
                stop_after=stop_after_bytes,
                measured=measured,
            )
            per_file |= read
            if not complete:
                for path in per_file:
                    _check_binary_markers(path, per_file[path], entries[path], binary, strict=True)
                return BoundedPatches(
                    {path: "".join(per_file[path]) for path in paths if path in per_file},
                    complete=False,
                    partial=partial,
                )
    for path in paths:
        _check_binary_markers(path, per_file[path], entries[path], binary, strict=True)
    return BoundedPatches({path: "".join(per_file[path]) for path in paths}, complete=True)


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
    """Parse ``git worktree list --porcelain -z`` into records.

    Paths are printed raw, so fields are split on NUL and read as bytes: a path
    may contain a newline or carriage return. Values are decoded like other
    paths, so they can be handed back to Git or the filesystem.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in _run_bytes(cwd, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field.partition(b" ")
        current[_decode_path(key)] = _decode_path(value)
    if current:
        records.append(current)
    return records
