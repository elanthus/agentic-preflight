"""Read the summary rows and hook decisions that the usage report counts.

The rows come from the user-level mirror, or from one clone's run records
summarized in memory as ``telemetry rebuild`` would summarize them. A line or
record that cannot be used is counted and reported, never treated as absent.
Nothing here writes a file.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import hooklog, run_summary, telemetry
from ..config import Config
from ..run_summary import SummaryRow, as_str
from ..store import Store

#: The malformed mirror lines listed by number. All of them are counted.
MALFORMED_LINE_LIMIT = 20


UNKNOWN = "unknown"


def tally(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(value if isinstance(value, str) and value else UNKNOWN for value in values)
    return dict(sorted(counts.items()))


def finish_order(row: Mapping[str, Any]) -> tuple[str, str]:
    return (as_str(row.get("finished_at")) or "", row["run_id"])


# -- the mirror ---------------------------------------------------------------


@dataclass
class MirrorRead:
    """The rows read from the mirror, and every line that could not be used."""

    path: str
    exists: bool
    lines: int = 0
    rows: list[SummaryRow] = field(default_factory=list)
    malformed: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "lines": self.lines,
            "rows": len(self.rows),
            "malformed": len(self.malformed),
            "malformed_by_reason": tally(item["reason"] for item in self.malformed),
            "malformed_lines": self.malformed[:MALFORMED_LINE_LIMIT],
            "duplicates": self.duplicates,
        }


def _parse_line(raw: bytes) -> tuple[SummaryRow | None, str | None]:
    """Return a validated summary row, or the reason the line is not one."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "undecodable"
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(row, dict) or row.get("kind") != run_summary.SUMMARY_KIND:
        return None, "not_a_summary"
    if row.get("schema_version") != 1:
        return None, "unsupported_schema"
    try:
        summary = run_summary.RunSummary.model_validate(row)
    except ValidationError:
        return None, "invalid_summary"
    return run_summary.summary_row(summary), None


def _keep_newest(
    rows: Iterable[SummaryRow], key: Callable[[int, SummaryRow], Any]
) -> list[SummaryRow]:
    """One row per key: the newest ``written_at``, the later row on a tie, as rebuild does."""
    kept: dict[Any, SummaryRow] = {}
    for index, row in enumerate(rows):
        name = key(index, row)
        if name not in kept or row["written_at"] >= kept[name]["written_at"]:
            kept[name] = row
    return list(kept.values())


def _same_run(_: int, row: SummaryRow) -> tuple[Any, ...]:
    return (row["run_id"], row.get("created_at"), row["repo"].get("source_worktree_path"))


def _dedupe(rows: Sequence[SummaryRow]) -> tuple[list[SummaryRow], int]:
    """Keep one valid row per repository and run, then one per run.

    Both passes match ``telemetry rebuild``. The first uses the mirror key. The
    second joins one run that was mirrored under two keys, for example once
    without a root commit: the same run ID, start time, and source worktree.
    """
    by_key = _keep_newest(rows, lambda index, row: telemetry.row_key(row) or ("line", index))
    unique = sorted(_keep_newest(by_key, _same_run), key=finish_order)
    return unique, len(rows) - len(unique)


def parse_mirror(payload: bytes, path: Path | str) -> MirrorRead:
    """Read mirror bytes line by line. A bad line is counted, never skipped silently."""
    result = MirrorRead(path=str(path), exists=True)
    rows: list[SummaryRow] = []
    for number, raw in enumerate(payload.split(b"\n"), start=1):
        if not raw.strip():
            continue
        result.lines += 1
        row, reason = _parse_line(raw)
        if row is None:
            result.malformed.append({"line": number, "reason": reason})
        else:
            rows.append(row)
    result.rows, result.duplicates = _dedupe(rows)
    return result


def read_mirror(path: Path) -> MirrorRead:
    """Read the mirror at ``path``. A missing file has no rows; other read errors raise."""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return MirrorRead(path=str(path), exists=False)
    return parse_mirror(payload, path)


# -- one clone ----------------------------------------------------------------


@dataclass(frozen=True)
class Clone:
    """The clone the command runs in: its run store, identity, and worktree paths."""

    store: Store
    identity: run_summary.RepoIdentity
    worktree_paths: frozenset[str]
    #: The ``[telemetry]`` preference that ``telemetry rebuild`` would apply. ``None``
    #: counts every finished run as one the rebuild mirrors.
    config: Config | None = None

    def mirrors(self, summary: SummaryRow) -> bool:
        """Whether ``telemetry rebuild`` in this clone would mirror ``summary``.

        It would not when the clone, or the run's repository, has no network remote
        (:func:`agentic_preflight.telemetry.exclusion_reason`).
        """
        if self.config is None:
            return True
        clone = self.identity
        repo = summary.get("repo")
        repo = repo if isinstance(repo, Mapping) else {}
        source, remote = repo.get("source_worktree_path"), repo.get("remote")
        return not (
            telemetry.exclusion_reason(self.config, clone.source_worktree_path, clone.remote)
            or telemetry.exclusion_reason(
                self.config,
                source if isinstance(source, str) else None,
                remote if isinstance(remote, str) else None,
            )
        )


@dataclass
class CloneScan:
    """Summaries made in memory from a clone's run records, as rebuild makes them."""

    summaries: list[SummaryRow] = field(default_factory=list)
    skipped: int = 0
    unreadable_runs: list[dict[str, str]] = field(default_factory=list)


def summarize_clone(
    store: Store,
    identity: run_summary.RepoIdentity,
    run_ids: Iterable[str] | None = None,
    *,
    written_at: str | None = None,
) -> CloneScan:
    """Summarize terminal run records with the rebuild's code path. Nothing is written.

    A record that cannot be read or summarized is listed in ``unreadable_runs``,
    exactly as ``telemetry rebuild`` reports it, and never fails the scan.
    """
    scan = CloneScan()
    stamp = written_at or run_summary.utc_now()
    for run_id in store.list_runs() if run_ids is None else run_ids:
        try:
            summary = telemetry.summarize_stored_run_safely(store, run_id, identity, stamp)
        except telemetry.UnreadableRecord as exc:
            scan.unreadable_runs.append(
                {
                    "run_id": run_id,
                    "path": str(exc.path),
                    "reason": exc.reason,
                    "diagnostic": exc.diagnostic,
                }
            )
            continue
        if summary is None:
            scan.skipped += 1
        else:
            scan.summaries.append(run_summary.summary_row(summary))
    scan.summaries.sort(key=finish_order)
    return scan


def _same_path(path: Any, paths: Collection[str]) -> bool:
    if not isinstance(path, str) or not path:
        return False
    return path in paths or str(Path(path).resolve()) in paths


def belongs_to_clone(
    row: Mapping[str, Any], root_commit: str | None, worktree_paths: Collection[str]
) -> bool:
    """Match a row to a repository by root commit, else by its source worktree path."""
    repo = row.get("repo") or {}
    if root_commit and repo.get("root_commit"):
        return repo["root_commit"] == root_commit
    return _same_path(repo.get("source_worktree_path"), worktree_paths)


def collect(
    mirror: MirrorRead, clone: Clone | None, *, all_repos: bool
) -> tuple[list[SummaryRow], dict[str, Any]]:
    """Select the rows to report and describe where they came from.

    With ``all_repos`` every mirror row is used. Otherwise the rows of ``clone``
    are used, and when the mirror has none of them, the clone's terminal run
    records are summarized in memory instead.
    """
    if not all_repos and clone is None:
        raise ValueError("a clone is required unless all_repos is set")
    source: dict[str, Any] = {
        "used": "mirror",
        "scope": "all" if all_repos else "clone",
        "mirror": mirror.describe(),
        "clone": None,
    }
    if clone is None:
        return list(mirror.rows), source
    identity = clone.identity
    mine = [
        row
        for row in mirror.rows
        if belongs_to_clone(row, identity.root_commit, clone.worktree_paths)
    ]
    run_ids = clone.store.list_runs()
    detail: dict[str, Any] = {
        **identity.model_dump(mode="json", exclude={"source_worktree_path"}),
        "mirror_rows": len(mine),
        "run_records": len(run_ids),
        "summarized": None,
        "skipped": None,
        "unmirrored_terminal_runs": None,
        "excluded_runs": None,
    }
    source["clone"] = detail
    if not all_repos and not mine:
        scan = summarize_clone(clone.store, identity, run_ids)
        source["used"] = "clone_records"
        detail.update(summarized=len(scan.summaries), skipped=scan.skipped)
        rows = scan.summaries
    else:
        mirrored = {row["run_id"] for row in mine}
        scan = summarize_clone(clone.store, identity, [r for r in run_ids if r not in mirrored])
        rows = list(mirror.rows) if all_repos else mine
    addable = sum(1 for summary in scan.summaries if clone.mirrors(summary))
    detail["excluded_runs"] = len(scan.summaries) - addable
    if source["used"] == "mirror":
        detail["unmirrored_terminal_runs"] = addable
    detail["unreadable"] = len(scan.unreadable_runs)
    detail["unreadable_runs"] = scan.unreadable_runs
    return rows, source


# -- the hook log -------------------------------------------------------------


@dataclass
class HookLogRead:
    """The decisions read from ``hook-decisions.jsonl``, and every line that could not be used."""

    path: str
    exists: bool
    error: str | None = None
    lines: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    malformed: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "error": self.error,
            "lines": self.lines,
            "rows": len(self.decisions),
            "malformed": len(self.malformed),
            "malformed_by_reason": tally(item["reason"] for item in self.malformed),
            "malformed_lines": self.malformed[:MALFORMED_LINE_LIMIT],
        }


def _parse_decision(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Return a validated hook decision, or the reason the line is not one."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "undecodable"
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(row, dict):
        return None, "not_a_decision"
    try:
        line = hooklog.HookDecisionLine.model_validate(row)
    except ValidationError:
        return None, "invalid_decision"
    return line.model_dump(mode="json"), None


def parse_hook_log(payload: bytes, path: Path | str) -> HookLogRead:
    """Read hook log bytes line by line. A bad line is counted, never skipped silently."""
    result = HookLogRead(path=str(path), exists=True)
    for number, raw in enumerate(payload.split(b"\n"), start=1):
        if not raw.strip():
            continue
        result.lines += 1
        row, reason = _parse_decision(raw)
        if row is None:
            result.malformed.append({"line": number, "reason": reason})
        else:
            result.decisions.append(row)
    return result


def read_hook_log(path: Path) -> HookLogRead:
    """Read the hook log after its rotated generation, oldest first.

    Both files are read under the writers' lock, so a rotation cannot fall
    between the two reads. A log directory that cannot hold the lock is read
    without it. A missing file has no decisions; another read error is recorded.
    """
    candidates = (path.with_name(hooklog.ROTATED_NAME), path)
    if not any(candidate.exists() for candidate in candidates):
        return HookLogRead(path=str(path), exists=False)
    payloads: list[bytes] = []
    with contextlib.ExitStack() as stack:
        with contextlib.suppress(OSError):
            stack.enter_context(hooklog.reading(path.parent))
        for candidate in candidates:
            try:
                payload = candidate.read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                return HookLogRead(
                    path=str(candidate), exists=True, error=telemetry.describe_error(exc)
                )
            payloads.append(payload if payload.endswith(b"\n") else payload + b"\n")
    if not payloads:
        return HookLogRead(path=str(path), exists=False)
    return parse_hook_log(b"".join(payloads), path)
