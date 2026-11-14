"""Local run summaries and the user-level telemetry mirror.

A run's audit record (``run.json``, ``findings.json``, ``events.jsonl``) lives in
one clone. When a run ends, a compact, versioned
:class:`~agentic_preflight.run_summary.RunSummary` of code-derived facts is
written beside that record as ``summary.json``. Unless the user turns the mirror
off, the same summary is appended as one JSON line to a user-level
``runs.jsonl``, so runs from every repository on this machine can be read
together.

This module identifies the repository, reads stored records, writes
``summary.json`` and the mirror under its lock, records the end of a run, and
rebuilds summaries. :mod:`agentic_preflight.run_summary` derives the summary
itself.

Everything here is derived output:

* Nothing is sent anywhere. The mirror is a local file.
* ``summary.json`` is never read back as evidence.
* Writing a summary never fails the command that ended the run.
* A repository with no network remote cannot push, so it is a test fixture, not
  work that preflight gates. Its rows never reach the default telemetry directory,
  wherever it lives; only an explicit ``[telemetry] path`` receives them
  (:func:`exclusion_reason`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import filelock, fileperms, gitx
from .config import Config, ConfigError, load_config
from .machine import TERMINAL_STATES
from .models import Finding, RunDoc
from .run_summary import (
    RepoIdentity,
    RunSummary,
    as_str,
    effective_state,
    summarize,
    utc_now,
)
from .store import Store, atomic_write

SUMMARY_NAME = "summary.json"
MIRROR_NAME = "runs.jsonl"
_MIRROR_LOCK_NAME = "runs.jsonl.lock"
#: Why rows were kept out of the telemetry directory: the repository has no network
#: remote, and no explicit ``[telemetry] path`` opted it in.
NO_NETWORK_REMOTE = "no_network_remote"
#: A Windows drive, the first segment of a normalized local path such as ``C:/repos/x``.
_DRIVE = re.compile(r"[A-Za-z]:")
#: How an absolute or explicitly relative local path starts: ``/``, ``\``, ``.``, ``~``,
#: or a drive letter. Any other scheme-less value is a relative path to Git.
_LOCAL_PATH_START = re.compile(r"[/\\.~]|[A-Za-z]:")
#: Loopback hosts. A remote on this machine is a test server, not a hosted repository.
_LOOPBACK = re.compile(r"localhost|127(?:\.\d{1,3}){3}|::1")
_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
_GIT_TIMEOUT_SECONDS = 30
_ERROR_LIMIT = 500
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>.*)$")


# -- identity ---------------------------------------------------------------


def normalize_remote(url: str | None) -> str | None:
    """Return a remote URL as host and path, or a local path, with no ``.git``.

    The scheme, credentials, and port are removed, so SSH and HTTPS clones of one
    repository share an identity. ``C:/repo`` is a Windows path, not a host. A
    value without a scheme or an scp-style ``host:`` is a local path to Git; a
    relative one, such as ``fixtures/bare.git``, gains a ``./`` prefix so that
    :func:`has_network_remote` cannot mistake its first segment for a host.
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        path = parts.path
    elif (match := _SCP_REMOTE.match(url)) and len(match["host"]) > 1:
        host, path = match["host"].lower(), match["path"]
    else:
        host, path = "", url
        if not _LOCAL_PATH_START.match(path):
            path = f"./{path}"
    path = path.rstrip("/\\").removesuffix(".git").rstrip("/\\")
    if host:
        path = path.lstrip("/")
        return f"{host}/{path}" if path else host
    return path or None


def _remote_name(remote: str | None) -> str | None:
    if not remote or not re.search(r"[/\\]", remote):
        return None
    return re.split(r"[/\\]", remote)[-1] or None


def git_output(cwd: Path, *args: str) -> str | None:
    """Git's stripped output in ``cwd``, or ``None`` when Git fails or prints nothing."""
    try:
        result = gitx.run(cwd, *args, check=False, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _root_commit(repo_root: Path) -> str | None:
    listing = git_output(repo_root, "rev-list", "--max-parents=0", "HEAD")
    roots = [line.strip() for line in (listing or "").splitlines()]
    roots = [line for line in roots if _SHA.fullmatch(line)]
    return min(roots) if roots else None


def _directory_name(repo_root: Path) -> str | None:
    try:
        common = gitx.git_common_dir(repo_root)
    except (gitx.GitError, OSError, subprocess.SubprocessError):
        return repo_root.name or None
    name = common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
    return name or None


def repo_identity(
    repo_root: Path | str, source_worktree_path: Path | str | None = None
) -> RepoIdentity:
    """Identify the repository at ``repo_root``; unknown parts are ``None``."""
    root = Path(repo_root)
    remote = normalize_remote(git_output(root, "remote", "get-url", "origin"))
    return RepoIdentity(
        root_commit=_root_commit(root),
        remote=remote,
        name=_remote_name(remote) or _directory_name(root),
        source_worktree_path=str(source_worktree_path or root),
    )


# -- hosted outcomes ----------------------------------------------------------


#: Hosted outcomes that ``telemetry sync-hosted`` joined onto a pushed run.
HOSTED_NAME = "hosted.json"


def read_hosted(store: Store, run_id: str) -> dict[str, Any] | None:
    """The run's ``hosted.json``, or ``None`` when it has none.

    The file is derived from GitHub, not evidence. A file that cannot be read,
    or that is not a JSON object, counts as not synced, and the next
    ``sync-hosted`` writes it again.
    """
    try:
        value = json.loads((store.run_dir(run_id) / HOSTED_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# -- storage ----------------------------------------------------------------


def telemetry_dir(config: Config | None = None) -> Path:
    """Return the user-level telemetry directory.

    ``[telemetry] path`` wins. Otherwise ``$XDG_DATA_HOME/agentic-preflight/telemetry``
    when ``XDG_DATA_HOME`` is an absolute path (relative values are invalid per
    the XDG specification and are ignored), else
    ``~/.local/share/agentic-preflight/telemetry``.
    """
    configured = config.telemetry.path if config is not None else None
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base / "agentic-preflight" / "telemetry"


def mirror_path(config: Config | None = None) -> Path:
    return telemetry_dir(config) / MIRROR_NAME


def has_network_remote(remote: str | None) -> bool:
    """Whether a :func:`normalize_remote` value names a repository on another host.

    True for ``host/path``. False when there is no remote, for a local path (one that
    starts with ``/``, ``\\``, ``.``, ``~``, or a drive letter, as a bare repository
    on this machine does), for a loopback host, and for a value without a path.
    """
    if not remote:
        return False
    head, separator, rest = remote.partition("/")
    if not separator or not rest or not head or "\\" in head:
        return False
    if head in {".", ".."} or head.startswith("~") or _DRIVE.fullmatch(head):
        return False
    return not _LOOPBACK.fullmatch(head.lower())


def exclusion_reason(
    config: Config | None, repo_path: Path | str | None, remote: str | None
) -> str | None:
    """Why rows of the repository at ``repo_path`` stay out of the telemetry directory.

    Returns :data:`NO_NETWORK_REMOTE` when ``remote`` (the normalized ``origin``) is
    not a network remote (:func:`has_network_remote`). A preflight run gates a push,
    so a repository that cannot push is a test fixture: fixtures have no remote, or
    a bare repository on this machine, wherever a harness puts them. Real work
    pushes to a host and keeps its rows, even from a scratch clone under the
    temporary directory. ``repo_path`` is the worktree the rule is judged for; it
    is accepted for callers and diagnostics, and does not change the answer. An
    explicit ``[telemetry] path`` is an opt-in and receives every row. The default
    location, which is the user's own record across repositories, is what the rule
    protects. ``None`` otherwise.
    """
    del repo_path  # the rule no longer depends on where the repository lives
    if config is not None and config.telemetry.path:
        return None
    if has_network_remote(remote):
        return None
    return NO_NETWORK_REMOTE


def describe_error(exc: BaseException) -> str:
    """The exception's type and message, cut to a bounded length for an envelope or log."""
    message = f"{type(exc).__name__}: {exc}"
    return message[:_ERROR_LIMIT]


def _row(summary: RunSummary) -> str:
    return json.dumps(summary.model_dump(mode="json"), sort_keys=True)


def _append_row(target: Path, summary: RunSummary) -> None:
    """Append one line under the mirror lock, ending any interrupted line first."""
    fileperms.private_directory(target.parent)
    with (
        filelock.exclusive(target.parent / _MIRROR_LOCK_NAME),
        open(target, "ab+") as handle,
    ):
        handle.seek(0, os.SEEK_END)
        prefix = b""
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = b"\n"
        handle.write(prefix + _row(summary).encode("utf-8") + b"\n")
        handle.flush()


def write_summary(
    run_dir: Path | str,
    summary: RunSummary,
    config: Config | None,
    *,
    config_error: str | None = None,
) -> dict[str, Any]:
    """Store ``summary.json`` and, when enabled, append the mirror row. Never raises.

    ``config`` supplies ``[telemetry]``. ``None`` means the configuration could
    not be read, so the user's mirror preference is unknown and nothing is
    mirrored. A run of a repository without a network remote is not mirrored
    either (:func:`exclusion_reason`), and ``reason`` names the rule. The returned
    status names what was written and any error.
    """
    status: dict[str, Any] = {
        "run_id": summary.run_id,
        "summary_path": None,
        "mirrored": False,
        "mirror_path": None,
        "reason": None,
        "error": None,
    }
    errors: list[str] = []
    try:
        path = Path(run_dir) / SUMMARY_NAME
        atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        status["summary_path"] = str(path)
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
        errors.append(f"summary: {describe_error(exc)}")
    if config is None:
        errors.append(f"mirror skipped: {config_error or 'configuration unavailable'}")
    elif config.telemetry.mirror:
        try:
            status["reason"] = exclusion_reason(
                config, summary.repo.source_worktree_path, summary.repo.remote
            )
            if status["reason"] is None:
                target = mirror_path(config)
                status["mirror_path"] = str(target)
                _append_row(target, summary)
                status["mirrored"] = True
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
            errors.append(f"mirror: {describe_error(exc)}")
    status["error"] = "; ".join(errors) or None
    return status


def live_config(repo_root: Path | str) -> tuple[Config | None, str | None]:
    """Read the current ``[telemetry]`` preference.

    Run snapshots never carry telemetry settings, so the files are read again.
    """
    try:
        return load_config(repo_root), None
    except (ConfigError, OSError) as exc:
        return None, describe_error(exc)


# -- the terminal hook --------------------------------------------------------

#: Status of each summary written in this process, until an envelope takes it.
_REPORTS: dict[str, dict[str, Any]] = {}


def take_report(run_id: str) -> dict[str, Any] | None:
    """Return and forget the summary status recorded for ``run_id`` in this process."""
    return _REPORTS.pop(run_id, None)


def _load_findings(store: Store, run_id: str) -> list[Finding] | None:
    if not store.findings_path(run_id).exists():
        return None
    try:
        return store.load_findings(run_id)
    except (OSError, ValueError):
        return None


def _load_events(store: Store, run_id: str) -> list[dict] | None:
    # The store skips lines that are not JSON objects, such as a torn append.
    try:
        return store.load_events(run_id)
    except OSError:
        return None


def _summarize_run_end(store: Store, repo_root: Path, run: RunDoc) -> dict[str, Any]:
    summary = summarize(
        run,
        _load_findings(store, run.run_id),
        _load_events(store, run.run_id),
        repo_identity(repo_root, run.source_worktree_path),
    )
    config, config_error = live_config(repo_root)
    return write_summary(store.run_dir(run.run_id), summary, config, config_error=config_error)


def record_run_end(store: Store, repo_root: Path | str, run: RunDoc) -> dict[str, Any]:
    """Summarize a run whose terminal state was just committed. Never raises.

    The outcome is appended to the run's event log as ``summary_written`` or
    ``summary_failed`` and kept for the command's envelope (:func:`take_report`).
    """
    try:
        status = _summarize_run_end(store, Path(repo_root), run)
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
        status = {
            "run_id": run.run_id,
            "summary_path": None,
            "mirrored": False,
            "mirror_path": None,
            "reason": None,
            "error": describe_error(exc),
        }
    event = "summary_written" if status["summary_path"] else "summary_failed"
    try:
        store.append_event(
            run.run_id,
            {"event": event, **{key: value for key, value in status.items() if key != "run_id"}},
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
        status["error"] = "; ".join(
            filter(None, [status["error"], f"event: {describe_error(exc)}"])
        )
    _REPORTS[run.run_id] = status
    return status


# -- rebuild ------------------------------------------------------------------


class UnreadableRecord(Exception):
    """A stored run record that cannot be read or summarized.

    ``reason`` is ``unreadable`` or ``invalid``. ``diagnostic`` names the problem
    without quoting record values.
    """

    def __init__(self, path: Path, reason: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.path = path
        self.reason = reason
        self.diagnostic = diagnostic


def _read_json(path: Path, label: str) -> Any:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UnreadableRecord(path, "unreadable", f"Cannot read {label}.") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UnreadableRecord(path, "invalid", f"{label} is not valid JSON.") from exc


def read_record(store: Store, run_id: str) -> dict[str, Any]:
    """The raw ``run.json`` object of any schema version. Raises :class:`UnreadableRecord`."""
    record = _read_json(store.run_path(run_id), "Run record")
    if not isinstance(record, dict):
        raise UnreadableRecord(
            store.run_path(run_id), "invalid", "Run record is not a JSON object."
        )
    return record


def _read_findings(store: Store, run_id: str) -> Any:
    """The parsed findings file, or ``None`` when there is none.

    A file that is not valid JSON makes the record unreadable. Valid JSON that is
    not a list of objects gives ``None`` finding counts in the summary.
    """
    path = store.findings_path(run_id)
    if not path.exists():
        return None
    return _read_json(path, "Findings file")


def _read_events(store: Store, run_id: str) -> list[dict[str, Any]]:
    # The store skips lines that are not JSON objects, such as a torn append.
    try:
        return store.load_events(run_id)
    except OSError as exc:
        raise UnreadableRecord(
            store.events_path(run_id), "unreadable", "Cannot read events."
        ) from exc


def row_key(row: Any) -> tuple[str, str] | None:
    """The first mirror deduplication key: repository identity and run id."""
    if not isinstance(row, dict) or not isinstance(row.get("run_id"), str):
        return None
    repo = row.get("repo")
    if not isinstance(repo, dict):
        return None
    identity = repo.get("root_commit") or repo.get("source_worktree_path")
    return (identity, row["run_id"]) if isinstance(identity, str) and identity else None


def _run_key(row: Any) -> tuple[str, str, str] | None:
    """The second deduplication key, which ``report`` also joins on.

    One run mirrored once with a root commit and once without, for example when
    Git was unavailable at one write, has two :func:`row_key` values. Its run
    ID, start time, and source worktree are still the same.
    """
    if not isinstance(row, dict):
        return None
    repo = row.get("repo")
    run_id = as_str(row.get("run_id"))
    created_at = as_str(row.get("created_at"))
    source = as_str(repo.get("source_worktree_path")) if isinstance(repo, dict) else None
    if not (run_id and created_at and source):
        return None
    return (run_id, created_at, source)


def _root_of(row: Mapping[str, Any]) -> Any:
    repo = row.get("repo")
    return repo.get("root_commit") if isinstance(repo, dict) else None


def _newest(entries: Iterable[tuple[int, dict[str, Any], str]], key: Any) -> list:
    """One entry per key: the newest ``written_at``, the later entry on a tie."""
    kept: dict[Any, tuple[int, dict[str, Any], str]] = {}
    for entry in sorted(entries, key=lambda item: item[0]):
        name = key(entry)
        written = as_str(entry[1].get("written_at")) or ""
        if name not in kept or written >= (as_str(kept[name][1].get("written_at")) or ""):
            kept[name] = entry
    return list(kept.values())


def _merge_mirror(
    lines: list[str], fresh: list[RunSummary], root_commit: str | None
) -> tuple[list[str], int, int]:
    """Replace this repository's rows, keeping the newest per run.

    A row is this repository's when it has this root commit or the key of a
    fresh row. Another row also joins when its :func:`_run_key` matches one of
    those rows and it has no root commit, or this clone has none to compare.
    Rows are deduplicated by :func:`row_key`, then by :func:`_run_key`, as
    ``report`` does. Rows of other repositories, and lines that are not summary
    rows, keep their exact text. A row of this repository whose run could not be
    summarized again stays, so an unreadable record is never treated as absent.
    """
    fresh_rows = [(summary.model_dump(mode="json"), _row(summary)) for summary in fresh]
    parsed: list[tuple[int, dict[str, Any] | None, str]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = None
        parsed.append((index, row if row_key(row) is not None else None, line))
    offset = len(lines)
    candidates = [(offset + index, row, line) for index, (row, line) in enumerate(fresh_rows)]
    fresh_keys = {row_key(row) for _, row, _ in candidates}

    def claimed(row: dict[str, Any]) -> bool:
        return row_key(row) in fresh_keys or (
            root_commit is not None and _root_of(row) == root_commit
        )

    mine = [(i, row, line) for i, row, line in parsed if row is not None and claimed(row)]
    runs = {_run_key(row) for _, row, _ in [*mine, *candidates]} - {None}
    # A matching run key joins a row unless its root commit names another repository.
    joined = [
        (i, row, line)
        for i, row, line in parsed
        if row is not None
        and not claimed(row)
        and (_root_of(row) is None or root_commit is None)
        and _run_key(row) in runs
    ]
    taken = {i for i, _, _ in [*mine, *joined]}
    preserved = [line for i, _, line in parsed if i not in taken]
    entries = _newest([*mine, *joined, *candidates], lambda item: row_key(item[1]))
    entries = _newest(entries, lambda item: _run_key(item[1]) or ("row", row_key(item[1])))
    kept = sorted(
        entries,
        key=lambda item: (as_str(item[1].get("finished_at")) or "", item[1]["run_id"]),
    )
    return [*preserved, *(line for _, _, line in kept)], len(kept), len(preserved)


def _rewrite_mirror(
    target: Path, fresh: list[RunSummary], root_commit: str | None
) -> tuple[int, int]:
    fileperms.private_directory(target.parent)
    with filelock.exclusive(target.parent / _MIRROR_LOCK_NAME):
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        # Split on newlines only, so preserved rows keep their exact text.
        lines, mine, preserved = _merge_mirror(text.split("\n"), fresh, root_commit)
        atomic_write(target, "".join(f"{line}\n" for line in lines))
    return mine, preserved


def replace_mirror_rows(
    config: Config,
    fresh: list[RunSummary],
    identity: RepoIdentity,
    *,
    rewrite_without_rows: bool = True,
) -> dict[str, Any]:
    """Replace the mirror rows of the clone ``identity`` names; the envelope's ``mirror``.

    Nothing is written when the mirror is off, or when the clone itself has no
    network remote (:func:`exclusion_reason`): ``reason`` then names the rule and
    the mirror is not touched. Otherwise a summary of a repository without a
    network remote, judged by its own ``repo.remote``, is left out, and
    ``excluded_runs`` counts it. ``rewrite_without_rows=False`` leaves the mirror alone when no summary
    remains to write.
    """
    mirror: dict[str, Any] = {
        "enabled": config.telemetry.mirror,
        "path": None,
        "rows": None,
        "preserved_rows": None,
        "excluded_runs": None,
        "reason": None,
        "error": None,
    }
    if not config.telemetry.mirror:
        return mirror
    kept = [
        summary
        for summary in fresh
        if exclusion_reason(config, summary.repo.source_worktree_path, summary.repo.remote) is None
    ]
    mirror["excluded_runs"] = len(fresh) - len(kept)
    if reason := exclusion_reason(config, identity.source_worktree_path, identity.remote):
        mirror["reason"] = reason
        return mirror
    if not kept and not rewrite_without_rows:
        return mirror
    target = mirror_path(config)
    mirror["path"] = str(target)
    try:
        mirror["rows"], mirror["preserved_rows"] = _rewrite_mirror(
            target, kept, identity.root_commit
        )
    except (OSError, UnicodeDecodeError) as exc:
        # An unreadable mirror is left exactly as it was.
        mirror["error"] = describe_error(exc)
    return mirror


def _summarize_stored_run(
    store: Store, run_id: str, identity: RepoIdentity, written_at: str
) -> RunSummary | None:
    """Summarize one stored record, or return ``None`` when it is not terminal."""
    record = read_record(store, run_id)
    state, _ = effective_state(record)
    if state not in _TERMINAL:
        return None
    findings = _read_findings(store, run_id)
    events = _read_events(store, run_id)
    source = as_str(record.get("source_worktree_path")) or identity.source_worktree_path
    try:
        return summarize(
            record,
            findings,
            events,
            identity.model_copy(update={"source_worktree_path": source}),
            written_at=written_at,
            hosted=read_hosted(store, run_id),
        )
    except ValidationError as exc:
        raise UnreadableRecord(
            store.run_path(run_id), "invalid", "Run record lacks fields that a summary requires."
        ) from exc


def summarize_stored_run_safely(
    store: Store, run_id: str, identity: RepoIdentity, written_at: str
) -> RunSummary | None:
    """:func:`_summarize_stored_run`, with any other failure reported as unreadable."""
    try:
        return _summarize_stored_run(store, run_id, identity, written_at)
    except UnreadableRecord:
        raise
    except Exception as exc:  # one malformed record must not fail the clone
        # Name only the exception type: its message can quote record values.
        raise UnreadableRecord(
            store.run_dir(run_id),
            "invalid",
            f"Run record could not be summarized ({type(exc).__name__}).",
        ) from exc


def rebuild(store: Store, repo_root: Path | str, config: Config) -> dict[str, Any]:
    """Regenerate every terminal run's summary in this clone and its mirror rows.

    Returns the counts and paths for the command envelope. Records that cannot
    be read are reported and keep any summary and mirror row they already had.
    A clone without a network remote writes its summaries but no mirror rows
    (:func:`replace_mirror_rows`).
    """
    identity = repo_identity(repo_root)
    written_at = utc_now()
    fresh: list[RunSummary] = []
    skipped = 0
    unreadable: list[dict[str, str]] = []
    write_failures: list[dict[str, str]] = []
    for run_id in store.list_runs():
        try:
            summary = summarize_stored_run_safely(store, run_id, identity, written_at)
        except UnreadableRecord as exc:
            unreadable.append(
                {
                    "run_id": run_id,
                    "path": str(exc.path),
                    "reason": exc.reason,
                    "diagnostic": exc.diagnostic,
                }
            )
            continue
        if summary is None:
            skipped += 1
            continue
        fresh.append(summary)
        try:
            path = store.run_dir(run_id) / SUMMARY_NAME
            atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            write_failures.append({"run_id": run_id, "error": describe_error(exc)})

    return {
        "repo": identity.model_dump(mode="json", exclude={"source_worktree_path"}),
        "summarized": len(fresh),
        "skipped": skipped,
        "unreadable": len(unreadable),
        "unreadable_runs": unreadable,
        "write_failures": write_failures,
        "mirror": replace_mirror_rows(config, fresh, identity),
    }
