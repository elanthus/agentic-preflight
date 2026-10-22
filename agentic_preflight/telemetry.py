"""Local run summaries and the user-level telemetry mirror.

A run's audit record (``run.json``, ``findings.json``, ``events.jsonl``) lives in
one clone. When a run ends, a compact, versioned :class:`RunSummary` of
code-derived facts is written beside that record as ``summary.json``. Unless the
user turns the mirror off, the same summary is appended as one JSON line to a
user-level ``runs.jsonl``, so runs from every repository on this machine can be
read together.

Everything here is derived output:

* Nothing is sent anywhere. The mirror is a local file.
* A summary records states, counts, and digests. It never copies the intent
  text, diffs, finding text, logs, or command output.
* No summary field feeds a fingerprint, an evidence digest, or an attestation,
  and ``summary.json`` is never read back as evidence.
* Absent evidence is ``None``, never zero. Older records lack many fields.
* Writing a summary never fails the command that ended the run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from . import filelock, gitx
from .config import Config, ConfigError, load_config
from .digests import sha256_text
from .machine import TERMINAL_STATES
from .models import Finding, RunDoc, Stage
from .store import RunReadError, Store, _atomic_write

SUMMARY_KIND = "agentic-preflight-run-summary"
SUMMARY_NAME = "summary.json"
MIRROR_NAME = "runs.jsonl"
_MIRROR_LOCK_NAME = "runs.jsonl.lock"

#: Events this module appends. They are left out of ``event_counts`` so that a
#: rebuilt summary counts the same events as the one written when the run ended.
TELEMETRY_EVENTS = frozenset({"summary_written", "summary_failed"})
#: Markers that repeat the terminal state. Some commands append them after the
#: terminal commit, so they are also left out of ``event_counts``.
TERMINAL_MARKER_EVENTS = frozenset({"finished", "aborted", "orphaned"})

_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
_STAGE_ORDER = [stage.value for stage in Stage]
_RETIRED_DELEGATED_STATES = ("TEST_DELEGATED", "PUBLICATION_READY")
_RETIRED_DELEGATION_REASON = "CI test delegation was removed; start a new local validation run"
_DEFAULT_BLOCKING = ("critical", "high")
_GIT_TIMEOUT_SECONDS = 30
_ERROR_LIMIT = 500
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>.*)$")


class RepoIdentity(BaseModel):
    """Which repository a run belongs to, independent of the clone it ran in."""

    model_config = ConfigDict(extra="forbid")

    #: Smallest root commit of ``HEAD``: the same in every clone of a repository.
    root_commit: str | None
    #: ``origin`` as host and path, without scheme, credentials, port, or ``.git``.
    remote: str | None
    #: Last path segment of ``remote``, else the clone's directory name.
    name: str | None
    source_worktree_path: str


class RunSummary(BaseModel):
    """Schema 1 of one finished run's local summary."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agentic-preflight-run-summary"] = "agentic-preflight-run-summary"
    schema_version: Literal[1] = 1
    written_at: str
    run_id: str
    state: str
    record_schema_version: int
    repo: RepoIdentity
    branch: str
    base_ref: str
    head_sha: str
    pushed_sha: str | None
    intent_sha256: str | None
    intent_source: str
    created_at: str
    finished_at: str
    duration_seconds: float | None
    producer: dict[str, Any] | None
    risk: dict[str, Any] | None
    review: dict[str, Any] | None
    stages: dict[str, dict[str, Any]]
    findings: dict[str, Any]
    fix_commits: int | None
    validation_restarts: int | None
    event_counts: dict[str, int] | None
    terminal: dict[str, Any]
    hosted: dict[str, Any]
    worktree_mode: str | None
    pr_mode: str | None
    config_digest: str


# -- identity ---------------------------------------------------------------


def normalize_remote(url: str | None) -> str | None:
    """Return a remote URL as host and path, or a local path, with no ``.git``.

    The scheme, credentials, and port are removed, so SSH and HTTPS clones of one
    repository share an identity. ``C:/repo`` is a Windows path, not a host.
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
    path = path.rstrip("/\\").removesuffix(".git").rstrip("/\\")
    if host:
        path = path.lstrip("/")
        return f"{host}/{path}" if path else host
    return path or None


def _remote_name(remote: str | None) -> str | None:
    if not remote or not re.search(r"[/\\]", remote):
        return None
    return re.split(r"[/\\]", remote)[-1] or None


def _git_output(cwd: Path, *args: str) -> str | None:
    try:
        result = gitx.run(cwd, *args, check=False, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _root_commit(repo_root: Path) -> str | None:
    listing = _git_output(repo_root, "rev-list", "--max-parents=0", "HEAD")
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
    remote = normalize_remote(_git_output(root, "remote", "get-url", "origin"))
    return RepoIdentity(
        root_commit=_root_commit(root),
        remote=remote,
        name=_remote_name(remote) or _directory_name(root),
        source_worktree_path=str(source_worktree_path or root),
    )


# -- summary ----------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _length(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _as_record(run: RunDoc | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(run, BaseModel):
        return run.model_dump(mode="json")
    return dict(run)


def _producer(run: RunDoc | Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any] | None:
    producer = getattr(run, "producer", None) if isinstance(run, BaseModel) else None
    if producer is None:
        producer = record.get("producer")
    if isinstance(producer, BaseModel):
        return producer.model_dump(mode="json")
    return _dict(producer)


def _effective_state(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return the state and orphan reason the tool reads, as ``RunDoc`` would load them."""
    state = _str(record.get("state"))
    retired = state in _RETIRED_DELEGATED_STATES or (
        record.get("test_delegation") is not None and state not in ("PUSHED", *_TERMINAL)
    )
    if retired:
        return "ORPHANED", _RETIRED_DELEGATION_REASON
    return state, _str(record.get("orphaned_reason"))


def _tally(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(value if isinstance(value, str) else "unknown" for value in values)
    return dict(sorted(counts.items()))


def _blocking_severities(snapshot: Mapping[str, Any], section: str) -> frozenset[str] | None:
    """The run's configured blocking severities for one findings stage.

    A missing key held the model default when the run started. A value that is
    present but malformed is unknown.
    """
    body = snapshot.get(section)
    if not isinstance(body, dict) or "blocking_severities" not in body:
        return frozenset(_DEFAULT_BLOCKING)
    value = body["blocking_severities"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


def _findings_summary(
    findings: Sequence[Finding | Mapping[str, Any]] | None, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    keys = ("total", "by_stage", "by_severity", "by_status", "by_action", "blocking")
    if findings is None:
        return dict.fromkeys(keys)
    items = [
        item.model_dump(mode="json") if isinstance(item, BaseModel) else dict(item)
        for item in findings
    ]
    review = _blocking_severities(snapshot, "review")
    docs = _blocking_severities(snapshot, "docs")
    blocking: int | None = None
    if review is not None and docs is not None:
        blocking = sum(
            1
            for item in items
            if item.get("severity") in (docs if item.get("stage") == "docs" else review)
        )
    return {
        "total": len(items),
        "by_stage": _tally(item.get("stage") for item in items),
        "by_severity": _tally(item.get("severity") for item in items),
        "by_status": _tally(item.get("status") for item in items),
        "by_action": _tally(item.get("action") for item in items),
        "blocking": blocking,
    }


def _stage_order(name: str) -> tuple[int, str]:
    return (_STAGE_ORDER.index(name) if name in _STAGE_ORDER else len(_STAGE_ORDER), name)


def _stages(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    stages = _dict(record.get("stages")) or {}
    evidence = _dict(record.get("evidence"))
    summary: dict[str, dict[str, Any]] = {}
    for name in sorted(stages, key=_stage_order):
        stage = _dict(stages[name]) or {}
        status = _str(stage.get("status"))
        reused: bool | None = None
        if evidence is not None:
            item = _dict(evidence.get(name))
            reused = item is not None and item.get("derivation_reason") is not None
        summary[name] = {
            "status": status,
            "attempts": _int(stage.get("attempts")),
            "finished_at": _str(stage.get("finished_at")),
            "reused": reused,
            # Skip reasons are code-assigned. Failure reasons can quote command
            # or reviewer output, which a summary never copies.
            "reason": _str(stage.get("reason")) if status == "skipped" else None,
        }
    return summary


def _risk(record: Mapping[str, Any]) -> dict[str, Any] | None:
    risk = _dict(record.get("risk"))
    if risk is None:
        return None
    return {
        "level": _str(risk.get("level")),
        "verdict": _str(risk.get("verdict")),
        "requires_human_review": _bool(risk.get("requires_human_review")),
    }


def _review(record: Mapping[str, Any]) -> dict[str, Any] | None:
    coverage = _dict(record.get("review_coverage"))
    stage = _dict((_dict(record.get("stages")) or {}).get("review"))
    if coverage is None and stage is None:
        return None
    coverage = coverage or {}
    return {
        "total_units": _int(coverage.get("total_units")),
        "cited_count": _length(coverage.get("cited_units")),
        "clean_count": _length(coverage.get("clean_units")),
        "excluded_count": _length(coverage.get("excluded_files")),
        "executor": _str((stage or {}).get("executor")),
    }


def _event_counts(events: Sequence[Mapping[str, Any]] | None) -> dict[str, int] | None:
    if events is None:
        return None
    names = (event.get("event") for event in events if isinstance(event, Mapping))
    return _tally(
        name
        for name in names
        if isinstance(name, str)
        and name not in TELEMETRY_EVENTS
        and name not in TERMINAL_MARKER_EVENTS
    )


def _finished_at(record: Mapping[str, Any], events: Sequence[Mapping[str, Any]] | None) -> Any:
    """The time of the terminal commit.

    A later write, such as ``gc`` recording a released worktree, moves
    ``updated_at``. The terminal transition event keeps the original time, and
    its ``seq`` shows whether ``updated_at`` still belongs to that commit.
    """
    updated = _str(record.get("updated_at"))
    for event in events or ():
        if event.get("event") == "state_transition" and event.get("to") in _TERMINAL:
            if updated and event.get("seq") is not None and event.get("seq") == record.get("seq"):
                return updated
            return _str(event.get("at")) or updated
    return updated


def _duration(start: str | None, end: str | None) -> float | None:
    try:
        delta = datetime.fromisoformat(end or "") - datetime.fromisoformat(start or "")
    except (TypeError, ValueError):
        return None
    seconds = delta.total_seconds()
    return seconds if seconds >= 0 else None


def _terminal(
    state: str | None, reason: str | None, events: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    forced = None
    if state == "ABORTED":
        for event in reversed(events or ()):
            if event.get("event") == "aborted":
                forced = _bool(event.get("forced"))
                break
    return {"aborted_forced": forced, "orphaned_reason": reason}


def _hosted(
    record: Mapping[str, Any], events: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    outcome = None
    pr_event = False
    for event in events or ():
        name = event.get("event")
        if name == "ci_checked":
            outcome = _str(event.get("outcome"))
        elif name in ("pr_opened", "pr_updated") and event.get("url"):
            pr_event = True
    recorded: bool | None = bool(record.get("pr_url")) or pr_event
    if events is None and not recorded:
        recorded = None
    return {"last_ci_outcome": outcome, "pr_url_recorded": recorded}


def summarize(
    run: RunDoc | Mapping[str, Any],
    findings: Sequence[Finding | Mapping[str, Any]] | None,
    events: Sequence[Mapping[str, Any]] | None,
    repo: RepoIdentity,
    *,
    written_at: str | None = None,
) -> RunSummary:
    """Summarize one run record from code-derived facts.

    ``run`` is a loaded run document or the raw ``run.json`` object of any
    schema version. ``findings`` and ``events`` are ``None`` when they are not
    available, and the fields derived from them are then ``None`` too.
    Raises :class:`pydantic.ValidationError` when the record lacks a field that
    every summary requires.
    """
    record = _as_record(run)
    state, orphaned_reason = _effective_state(record)
    snapshot = _dict(record.get("config_snapshot")) or {}
    intent = record.get("intent")
    created_at = _str(record.get("created_at"))
    finished_at = _finished_at(record, events)
    return RunSummary.model_validate(
        {
            "written_at": written_at or _now(),
            "run_id": record.get("run_id"),
            "state": state,
            "record_schema_version": _int(record.get("schema_version")),
            "repo": repo,
            "branch": record.get("branch"),
            "base_ref": record.get("base_ref"),
            "head_sha": record.get("head_sha"),
            "pushed_sha": _str(record.get("pushed_sha")),
            "intent_sha256": sha256_text(intent) if isinstance(intent, str) and intent else None,
            "intent_source": record.get("intent_source"),
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_seconds": _duration(created_at, _str(finished_at)),
            "producer": _producer(run, record),
            "risk": _risk(record),
            "review": _review(record),
            "stages": _stages(record),
            "findings": _findings_summary(findings, snapshot),
            "fix_commits": _length(record.get("fix_commits")),
            "validation_restarts": _int(record.get("validation_restarts")),
            "event_counts": _event_counts(events),
            "terminal": _terminal(state, orphaned_reason, events),
            "hosted": _hosted(record, events),
            "worktree_mode": _str((_dict(snapshot.get("worktree")) or {}).get("mode")),
            "pr_mode": _str((_dict(snapshot.get("pr")) or {}).get("mode")),
            "config_digest": record.get("config_digest"),
        }
    )


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


def _describe(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message[:_ERROR_LIMIT]


def _row(summary: RunSummary) -> str:
    return json.dumps(summary.model_dump(mode="json"), sort_keys=True)


def _append_row(target: Path, summary: RunSummary) -> None:
    """Append one line under the mirror lock, ending any interrupted line first."""
    target.parent.mkdir(parents=True, exist_ok=True)
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
    mirrored. The returned status names what was written and any error.
    """
    status: dict[str, Any] = {
        "run_id": summary.run_id,
        "summary_path": None,
        "mirrored": False,
        "mirror_path": None,
        "error": None,
    }
    errors: list[str] = []
    try:
        path = Path(run_dir) / SUMMARY_NAME
        _atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        status["summary_path"] = str(path)
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
        errors.append(f"summary: {_describe(exc)}")
    if config is None:
        errors.append(f"mirror skipped: {config_error or 'configuration unavailable'}")
    elif config.telemetry.mirror:
        try:
            target = mirror_path(config)
            status["mirror_path"] = str(target)
            _append_row(target, summary)
            status["mirrored"] = True
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
            errors.append(f"mirror: {_describe(exc)}")
    status["error"] = "; ".join(errors) or None
    return status


def live_config(repo_root: Path | str) -> tuple[Config | None, str | None]:
    """Read the current ``[telemetry]`` preference.

    Run snapshots never carry telemetry settings, so the files are read again.
    """
    try:
        return load_config(repo_root), None
    except (ConfigError, OSError) as exc:
        return None, _describe(exc)


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
    try:
        return store.load_events(run_id)
    except (OSError, RunReadError):
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
            "error": _describe(exc),
        }
    event = "summary_written" if status["summary_path"] else "summary_failed"
    try:
        store.append_event(
            run.run_id,
            {"event": event, **{key: value for key, value in status.items() if key != "run_id"}},
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the caller
        status["error"] = "; ".join(filter(None, [status["error"], f"event: {_describe(exc)}"]))
    _REPORTS[run.run_id] = status
    return status


# -- rebuild ------------------------------------------------------------------


class _Unreadable(Exception):
    def __init__(self, path: Path, reason: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.path = path
        self.reason = reason
        self.diagnostic = diagnostic


def _read_json(path: Path, label: str) -> Any:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Unreadable(path, "unreadable", f"Cannot read {label}.") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise _Unreadable(path, "invalid", f"{label} is not valid JSON.") from exc


def _read_record(store: Store, run_id: str) -> dict[str, Any]:
    record = _read_json(store.run_path(run_id), "Run record")
    if not isinstance(record, dict):
        raise _Unreadable(store.run_path(run_id), "invalid", "Run record is not a JSON object.")
    return record


def _read_findings(store: Store, run_id: str) -> list[dict[str, Any]] | None:
    path = store.findings_path(run_id)
    if not path.exists():
        return None
    findings = _read_json(path, "Findings file")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise _Unreadable(path, "invalid", "Findings file is not a list of objects.")
    return findings


def _read_events(store: Store, run_id: str) -> list[dict[str, Any]]:
    try:
        return store.load_events(run_id)
    except RunReadError as exc:
        raise _Unreadable(exc.path, exc.reason, str(exc)) from exc
    except OSError as exc:
        raise _Unreadable(store.events_path(run_id), "unreadable", "Cannot read events.") from exc


def _row_key(row: Any) -> tuple[str, str] | None:
    """The mirror deduplication key: repository identity and run id."""
    if not isinstance(row, dict) or not isinstance(row.get("run_id"), str):
        return None
    repo = row.get("repo")
    if not isinstance(repo, dict):
        return None
    identity = repo.get("root_commit") or repo.get("source_worktree_path")
    return (identity, row["run_id"]) if isinstance(identity, str) and identity else None


def _merge_mirror(
    lines: list[str], fresh: list[RunSummary], root_commit: str | None
) -> tuple[list[str], int, int]:
    """Replace this repository's rows, keeping the newest per key.

    Rows of other repositories, and lines that are not summary rows, keep their
    exact text. A row of this repository whose run could not be summarized again
    stays, so an unreadable record is never treated as absent.
    """
    fresh_rows = [(summary.model_dump(mode="json"), _row(summary)) for summary in fresh]
    fresh_keys = {_row_key(row) for row, _ in fresh_rows}
    preserved: list[str] = []
    ours: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}

    def keep_newest(key: tuple[str, str], row: dict[str, Any], line: str) -> None:
        written = _str(row.get("written_at")) or ""
        if key not in ours or written >= ours[key][0]:
            ours[key] = (written, row, line)

    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            preserved.append(line)
            continue
        key = _row_key(row)
        if key is None or (
            key not in fresh_keys and (root_commit is None or key[0] != root_commit)
        ):
            preserved.append(line)
            continue
        keep_newest(key, row, line)
    for row, line in fresh_rows:
        key = _row_key(row)
        if key is not None:
            keep_newest(key, row, line)
    mine = sorted(
        ours.values(),
        key=lambda item: (_str(item[1].get("finished_at")) or "", item[1]["run_id"]),
    )
    return [*preserved, *(line for _, _, line in mine)], len(mine), len(preserved)


def _rewrite_mirror(
    target: Path, fresh: list[RunSummary], root_commit: str | None
) -> tuple[int, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    with filelock.exclusive(target.parent / _MIRROR_LOCK_NAME):
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        # Split on newlines only, so preserved rows keep their exact text.
        lines, mine, preserved = _merge_mirror(text.split("\n"), fresh, root_commit)
        _atomic_write(target, "".join(f"{line}\n" for line in lines))
    return mine, preserved


def _summarize_stored_run(
    store: Store, run_id: str, identity: RepoIdentity, written_at: str
) -> RunSummary | None:
    """Summarize one stored record, or return ``None`` when it is not terminal."""
    record = _read_record(store, run_id)
    state, _ = _effective_state(record)
    if state not in _TERMINAL:
        return None
    findings = _read_findings(store, run_id)
    events = _read_events(store, run_id)
    source = _str(record.get("source_worktree_path")) or identity.source_worktree_path
    try:
        return summarize(
            record,
            findings,
            events,
            identity.model_copy(update={"source_worktree_path": source}),
            written_at=written_at,
        )
    except ValidationError as exc:
        raise _Unreadable(
            store.run_path(run_id), "invalid", "Run record lacks fields that a summary requires."
        ) from exc


def rebuild(store: Store, repo_root: Path | str, config: Config) -> dict[str, Any]:
    """Regenerate every terminal run's summary in this clone and its mirror rows.

    Returns the counts and paths for the command envelope. Records that cannot
    be read are reported and keep any summary and mirror row they already had.
    """
    identity = repo_identity(repo_root)
    written_at = _now()
    fresh: list[RunSummary] = []
    skipped = 0
    unreadable: list[dict[str, str]] = []
    write_failures: list[dict[str, str]] = []
    for run_id in store.list_runs():
        try:
            summary = _summarize_stored_run(store, run_id, identity, written_at)
        except _Unreadable as exc:
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
            _atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            write_failures.append({"run_id": run_id, "error": _describe(exc)})

    mirror: dict[str, Any] = {
        "enabled": config.telemetry.mirror,
        "path": None,
        "rows": None,
        "preserved_rows": None,
        "error": None,
    }
    if config.telemetry.mirror:
        target = mirror_path(config)
        mirror["path"] = str(target)
        try:
            mirror["rows"], mirror["preserved_rows"] = _rewrite_mirror(
                target, fresh, identity.root_commit
            )
        except (OSError, UnicodeDecodeError) as exc:
            # An unreadable mirror is left exactly as it was.
            mirror["error"] = _describe(exc)
    return {
        "repo": identity.model_dump(mode="json", exclude={"source_worktree_path"}),
        "summarized": len(fresh),
        "skipped": skipped,
        "unreadable": len(unreadable),
        "unreadable_runs": unreadable,
        "write_failures": write_failures,
        "mirror": mirror,
    }
