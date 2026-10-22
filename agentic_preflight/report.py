"""Aggregate local run summaries into a usage report.

The report reads :class:`~agentic_preflight.telemetry.RunSummary` rows, from the
user-level mirror or from one clone's run records, and counts them. Everything
here is pure apart from reading the mirror file and the run store, so the
aggregation can be tested without the CLI.

The report follows the summary's rules:

* Nothing is sent anywhere.
* A null value is unknown, never zero. Each statistic leaves null values out and
  reports how many runs it left out.
* An unreadable line or record is counted and reported, never treated as absent.
* Every ratio names its denominator.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import telemetry
from .models import Stage
from .store import Store

REPORT_STATES = ("DONE", "ABORTED", "ORPHANED")
FORMATS = ("json", "markdown")
#: Events that show rework or reuse. ``event_counts`` holds every other name too.
SELECTED_EVENTS = (
    "review_coverage_invalidated",
    "test_interrupted",
    "sync_conflict",
    "stage_reused",
)
#: A median or p90 of fewer observations than this is not computed.
MIN_QUANTILE_OBSERVATIONS = 5
#: The malformed mirror lines listed by number. All of them are counted.
MALFORMED_LINE_LIMIT = 20
UNKNOWN = "unknown"
_STAGE_ORDER = [stage.value for stage in Stage]
_FINDING_DIMENSIONS = ("by_stage", "by_severity", "by_status", "by_action")

LIMITS: tuple[str, ...] = (
    "The counts measure use and how findings were resolved. "
    "They do not measure reviewer recall or the defect escape rate.",
    "A run with no findings can be a clean change or a missed defect. "
    "The data cannot tell these apart.",
    "The mirror holds only runs that reached a terminal state after mirroring was enabled, "
    "or that `telemetry rebuild` added.",
    "Producer identity is partly self-reported by the agent. "
    "Runs without a client or a model are grouped as `unknown`.",
    "A duration is the wall-clock time from run creation to the terminal commit. "
    "It includes the agent's own working time.",
    "Hosted outcomes are recorded only when a run observed them. "
    "Current versions do not record them, so these counts do not count pull requests.",
    "An orphaned run can be a replaced attempt, not a failure.",
    "A null value is unknown, not zero. "
    "Each statistic leaves out null values and reports how many runs it left out.",
)

HOSTED_NOTE = (
    "Recorded only when a run observed a hosted outcome. Current versions record none. "
    "Do not use these counts as pull-request or CI counts."
)
BLOCKING_NOTE = (
    "A summary counts blocking findings and finding statuses separately. "
    "The fixed count is exact only for runs where every finding or no finding is blocking. "
    "Otherwise it is a range."
)


# -- the mirror ---------------------------------------------------------------


@dataclass
class MirrorRead:
    """The rows read from the mirror, and every line that could not be used."""

    path: str
    exists: bool
    lines: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    malformed: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "lines": self.lines,
            "rows": len(self.rows),
            "malformed": len(self.malformed),
            "malformed_by_reason": _tally(item["reason"] for item in self.malformed),
            "malformed_lines": self.malformed[:MALFORMED_LINE_LIMIT],
            "duplicates": self.duplicates,
        }


def _parse_line(raw: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Return a validated summary row, or the reason the line is not one."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "undecodable"
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(row, dict) or row.get("kind") != telemetry.SUMMARY_KIND:
        return None, "not_a_summary"
    if row.get("schema_version") != 1:
        return None, "unsupported_schema"
    try:
        summary = telemetry.RunSummary.model_validate(row)
    except ValidationError:
        return None, "invalid_summary"
    return summary.model_dump(mode="json"), None


def _finish_order(row: Mapping[str, Any]) -> tuple[str, str]:
    return (_str(row.get("finished_at")) or "", row["run_id"])


def _keep_newest(
    rows: Iterable[dict[str, Any]], key: Callable[[int, dict[str, Any]], Any]
) -> list[dict[str, Any]]:
    """One row per key: the newest ``written_at``, the later row on a tie, as rebuild does."""
    kept: dict[Any, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        name = key(index, row)
        if name not in kept or row["written_at"] >= kept[name]["written_at"]:
            kept[name] = row
    return list(kept.values())


def _same_run(_: int, row: Mapping[str, Any]) -> tuple[Any, ...]:
    repo = row.get("repo") or {}
    return (row["run_id"], row.get("created_at"), repo.get("source_worktree_path"))


def _dedupe(rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep one valid row per repository and run, then one per run.

    The first pass uses the mirror key that rebuild uses. The second pass joins
    one run that was mirrored under two keys, for example once without a root
    commit: the same run ID, start time, and source worktree.
    """
    by_key = _keep_newest(rows, lambda index, row: telemetry._row_key(row) or ("line", index))
    unique = sorted(_keep_newest(by_key, _same_run), key=_finish_order)
    return unique, len(rows) - len(unique)


def parse_mirror(payload: bytes, path: Path | str) -> MirrorRead:
    """Read mirror bytes line by line. A bad line is counted, never skipped silently."""
    result = MirrorRead(path=str(path), exists=True)
    rows: list[dict[str, Any]] = []
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


# -- one clone -----------------------------------------------------------------


@dataclass(frozen=True)
class Clone:
    """The clone the command runs in: its run store, identity, and worktree paths."""

    store: Store
    identity: telemetry.RepoIdentity
    worktree_paths: frozenset[str]


@dataclass
class CloneScan:
    """Summaries made in memory from a clone's run records, as rebuild makes them."""

    summaries: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    unreadable_runs: list[dict[str, str]] = field(default_factory=list)


def _summarize_one(
    store: Store, run_id: str, identity: telemetry.RepoIdentity, stamp: str
) -> telemetry.RunSummary | None:
    """Summarize one record. Any failure makes the record unreadable, never the report."""
    try:
        return telemetry._summarize_stored_run(store, run_id, identity, stamp)
    except telemetry._Unreadable:
        raise
    except Exception as exc:  # one malformed record must not fail the report
        # Name only the exception type: its message can quote record values.
        raise telemetry._Unreadable(
            store.run_dir(run_id),
            "invalid",
            f"Run record could not be summarized ({type(exc).__name__}).",
        ) from exc


def summarize_clone(
    store: Store,
    identity: telemetry.RepoIdentity,
    run_ids: Iterable[str] | None = None,
    *,
    written_at: str | None = None,
) -> CloneScan:
    """Summarize terminal run records with the rebuild's code path. Nothing is written."""
    scan = CloneScan()
    stamp = written_at or telemetry._now()
    for run_id in store.list_runs() if run_ids is None else run_ids:
        try:
            summary = _summarize_one(store, run_id, identity, stamp)
        except telemetry._Unreadable as exc:
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
            scan.summaries.append(summary.model_dump(mode="json"))
    scan.summaries.sort(key=_finish_order)
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        detail["unmirrored_terminal_runs"] = len(scan.summaries)
        rows = list(mirror.rows) if all_repos else mine
    detail["unreadable"] = len(scan.unreadable_runs)
    detail["unreadable_runs"] = scan.unreadable_runs
    return rows, source


# -- filters -------------------------------------------------------------------


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 date or time. A time without a zone is UTC."""
    parsed = datetime.fromisoformat(value.strip())
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Filters:
    since: datetime | None = None
    until: datetime | None = None
    repo: str | None = None
    states: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "repo": self.repo,
            "state": list(self.states) or None,
        }


def matches_repo(row: Mapping[str, Any], wanted: str) -> bool:
    """Match ``wanted`` to a row's repository name or remote, ignoring case."""
    candidates = {wanted.strip().casefold()}
    normalized = telemetry.normalize_remote(wanted)
    if normalized:
        candidates.add(normalized.casefold())
    repo = row.get("repo") or {}
    values = (repo.get("name"), repo.get("remote"))
    return any(isinstance(value, str) and value.casefold() in candidates for value in values)


def _excluded_by(row: Mapping[str, Any], filters: Filters) -> str | None:
    if filters.states and row.get("state") not in filters.states:
        return "state"
    if filters.repo is not None and not matches_repo(row, filters.repo):
        return "repo"
    if filters.since is None and filters.until is None:
        return None
    finished = _timestamp(row.get("finished_at"))
    if finished is None:
        return "time_unknown"
    if filters.since is not None and finished < filters.since:
        return "since"
    if filters.until is not None and finished >= filters.until:
        return "until"
    return None


def apply_filters(
    rows: Iterable[dict[str, Any]], filters: Filters
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return the rows that pass, and how many each filter left out.

    Each row left out is counted once, under the first filter that excluded it.
    """
    left_out = dict.fromkeys(("state", "repo", "since", "until", "time_unknown"), 0)
    kept: list[dict[str, Any]] = []
    for row in rows:
        reason = _excluded_by(row, filters)
        if reason is None:
            kept.append(row)
        else:
            left_out[reason] += 1
    return kept, {**left_out, "total": sum(left_out.values())}


# -- aggregation ---------------------------------------------------------------


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _tally(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(value if isinstance(value, str) and value else UNKNOWN for value in values)
    return dict(sorted(counts.items()))


def ratio(numerator: int, denominator: int, label: str) -> dict[str, Any]:
    """A ratio that names its denominator. ``value`` is null when nothing was counted."""
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "denominator_label": label,
    }


def _states(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(_str(row.get("state")) or UNKNOWN for row in rows)
    ordered = {state: counts.pop(state, 0) for state in REPORT_STATES}
    return {**ordered, **dict(sorted(counts.items()))}


def _known_findings(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    findings = (row.get("findings") or {} for row in rows)
    return [item for item in findings if _int(item.get("total")) is not None]


def _add_counts(counter: Counter[str], counts: Any) -> None:
    if isinstance(counts, Mapping):
        for name, count in counts.items():
            if _int(count) is not None:
                counter[str(name)] += count


def _blocking(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = [item for item in _known_findings(rows) if _int(item.get("blocking")) is not None]
    total = low = high = 0
    for item in known:
        blocking, findings = item["blocking"], item["total"]
        fixed = _int((item.get("by_status") or {}).get("fixed")) or 0
        total += blocking
        # The fixed findings fill the non-blocking ones first for the lower bound.
        low += max(0, fixed - max(0, findings - blocking))
        high += min(fixed, blocking)
    return {
        "total": total if known else None,
        "runs_observed": len(known),
        "excluded_runs": len(rows) - len(known),
        "fixed": {"min": low, "max": high, "exact": low == high} if known else None,
        "fixed_share": {
            "min": round(low / total, 4) if total else None,
            "max": round(high / total, 4) if total else None,
            "numerator_min": low,
            "numerator_max": high,
            "denominator": total,
            "denominator_label": "blocking findings",
        },
        "note": BLOCKING_NOTE,
    }


def _per_100_units(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    findings = units = observed = 0
    for row in rows:
        counts = row.get("findings") or {}
        total_units = _int((row.get("review") or {}).get("total_units"))
        if _int(counts.get("total")) is None or total_units is None:
            continue
        observed += 1
        findings += _int((counts.get("by_stage") or {}).get("review")) or 0
        units += total_units
    return {
        "value": round(100 * findings / units, 2) if units else None,
        "numerator": findings,
        "numerator_label": "review-stage findings",
        "denominator": units,
        "denominator_label": "review units",
        "runs_observed": observed,
        "excluded_runs": len(rows) - observed,
    }


def _findings(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = _known_findings(rows)
    tallies: dict[str, Counter[str]] = {name: Counter() for name in _FINDING_DIMENSIONS}
    for item in known:
        for name, counter in tallies.items():
            _add_counts(counter, item.get(name))
    return {
        "runs_observed": len(known),
        "excluded_runs": len(rows) - len(known),
        "total": sum(item["total"] for item in known) if known else None,
        **{name: dict(sorted(counter.items())) for name, counter in tallies.items()},
        "blocking": _blocking(rows),
        "review_findings_per_100_units": _per_100_units(rows),
    }


def _any_positive(rows: Sequence[Mapping[str, Any]], key: str, label: str) -> dict[str, Any]:
    known = [value for value in (_int(row.get(key)) for row in rows) if value is not None]
    return {
        "runs_with_any": ratio(sum(1 for value in known if value > 0), len(known), label),
        "total": sum(known) if known else None,
        "excluded_runs": len(rows) - len(known),
    }


def _stage_key(name: str) -> tuple[int, str]:
    return (_STAGE_ORDER.index(name) if name in _STAGE_ORDER else len(_STAGE_ORDER), name)


def _stage(present: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = [
        value for value in (_int(item.get("attempts")) for item in present) if value is not None
    ]
    reused = [item["reused"] for item in present if isinstance(item.get("reused"), bool)]
    attempts_label = "runs with an attempt count for this stage"
    return {
        "runs": len(present),
        "statuses": _tally(item.get("status") for item in present),
        "attempts_at_least_one": ratio(
            sum(1 for a in attempts if a >= 1), len(attempts), attempts_label
        ),
        "attempts_above_one": ratio(
            sum(1 for a in attempts if a > 1), len(attempts), attempts_label
        ),
        "attempts_excluded_runs": len(present) - len(attempts),
        "reused": ratio(sum(reused), len(reused), "runs with a reuse value for this stage"),
        "reused_excluded_runs": len(present) - len(reused),
    }


def _stages(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    stages = [row.get("stages") or {} for row in rows]
    names = sorted({name for item in stages for name in item}, key=_stage_key)
    return {
        name: _stage([item[name] for item in stages if isinstance(item.get(name), Mapping)])
        for name in names
    }


def _quantile_values(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    values = (row.get("duration_seconds") for row in rows)
    return sorted(
        float(value)
        for value in values
        if isinstance(value, int | float) and not isinstance(value, bool)
    )


def _durations(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = _quantile_values(rows)
    enough = len(values) >= MIN_QUANTILE_OBSERVATIONS
    return {
        "observations": len(values),
        "excluded_runs": len(rows) - len(values),
        "minimum_observations": MIN_QUANTILE_OBSERVATIONS,
        "median": round(statistics.median(values), 1) if enough else None,
        # Linear interpolation between the closest ranks, inclusive of both ends.
        "p90": (
            round(statistics.quantiles(values, n=10, method="inclusive")[8], 1) if enough else None
        ),
        "note": (
            None
            if enough
            else f"Fewer than {MIN_QUANTILE_OBSERVATIONS} observations. "
            "The median and p90 are not computed."
        ),
    }


def _hosted(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hosted = [row.get("hosted") or {} for row in rows]
    outcomes = [item["last_ci_outcome"] for item in hosted if _str(item.get("last_ci_outcome"))]
    urls = [
        item["pr_url_recorded"] for item in hosted if isinstance(item.get("pr_url_recorded"), bool)
    ]
    return {
        "outcome_recorded": ratio(len(outcomes), len(rows), "runs"),
        "outcomes": _tally(outcomes),
        "pr_url_recorded": ratio(sum(urls), len(urls), "runs with a pull-request URL value"),
        "pr_url_excluded_runs": len(rows) - len(urls),
        "note": HOSTED_NOTE,
    }


def _orphaned_reasons(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return _tally(
        (row.get("terminal") or {}).get("orphaned_reason")
        for row in rows
        if row.get("state") == "ORPHANED"
    )


def _events(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known = [row["event_counts"] for row in rows if isinstance(row.get("event_counts"), Mapping)]
    counts: dict[str, Any] = {}
    for name in SELECTED_EVENTS:
        per_run = [_int(item.get(name)) or 0 for item in known]
        counts[name] = {
            "total": sum(per_run) if known else None,
            "runs": ratio(
                sum(1 for count in per_run if count), len(known), "runs with event counts"
            ),
        }
    return {"runs_observed": len(known), "excluded_runs": len(rows) - len(known), "counts": counts}


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every statistic for one group of summary rows."""
    states = _states(rows)
    return {
        "runs": len(rows),
        "states": states,
        "completion_rate": ratio(states["DONE"], len(rows), "runs"),
        "findings": _findings(rows),
        "fix_commits": _any_positive(rows, "fix_commits", "runs with a fix-commit count"),
        "validation_restarts": _any_positive(
            rows, "validation_restarts", "runs with a validation-restart count"
        ),
        "stages": _stages(rows),
        "duration_seconds": _durations(rows),
        "hosted": _hosted(rows),
        "orphaned_reasons": _orphaned_reasons(rows),
        "events": _events(rows),
    }


def _groups(
    rows: Iterable[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], str]
) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return groups


def _by_size(item: Mapping[str, Any]) -> tuple[int, str]:
    return (-item["metrics"]["runs"], str(item.get("label") or ""))


def repo_key(row: Mapping[str, Any]) -> str:
    """The repository part of the mirror key: the root commit, else the worktree path."""
    repo = row.get("repo") or {}
    return _str(repo.get("root_commit")) or f"path:{repo.get('source_worktree_path')}"


def by_repo(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One group per repository, named by its most recently finished run."""
    result = []
    for key, members in _groups(rows, repo_key).items():
        repo = max(members, key=_finish_order).get("repo") or {}
        result.append(
            {
                "key": key,
                "label": repo.get("name") or repo.get("remote") or key,
                "name": repo.get("name"),
                "remote": repo.get("remote"),
                "root_commit": repo.get("root_commit"),
                "metrics": aggregate(members),
            }
        )
    return sorted(result, key=_by_size)


def producer_value(row: Mapping[str, Any], name: str) -> str:
    """One producer field, or ``unknown`` when the run did not record it."""
    producer = row.get("producer")
    value = producer.get(name) if isinstance(producer, Mapping) else None
    return value if isinstance(value, str) and value else UNKNOWN


def by_producer(rows: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    groups = _groups(rows, lambda row: producer_value(row, name))
    result = [
        {"label": value, name: value, "metrics": aggregate(members)}
        for value, members in groups.items()
    ]
    return sorted(result, key=_by_size)


def build_report(
    rows: Sequence[dict[str, Any]],
    *,
    source: dict[str, Any],
    filters: Filters,
    filtered_out: dict[str, int],
    output_format: str = "json",
    include_rows: bool = False,
) -> dict[str, Any]:
    """The report ``data``: source, filters, aggregates, limits, and optional rows."""
    data: dict[str, Any] = {
        "format": output_format,
        "source": source,
        "filters": filters.describe(),
        "filtered_out": filtered_out,
        "rows_reported": len(rows),
        "overall": aggregate(rows),
        "by_repo": by_repo(rows),
        "by_client": by_producer(rows, "client"),
        "by_model": by_producer(rows, "reported_model"),
        "limits": list(LIMITS),
        "markdown": None,
        "rows": list(rows) if include_rows else None,
    }
    if output_format == "markdown":
        data["markdown"] = render_markdown(data)
    return data


# -- Markdown ------------------------------------------------------------------


def _cell(value: Any) -> str:
    """One table cell. A null value is unknown, so it reads ``n/a``, never ``0``."""
    text = "n/a" if value is None else str(value)
    return " ".join(text.split()).replace("|", "\\|")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(_cell(item) for item in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(item) for item in row) + " |" for row in rows)
    return [*lines, ""]


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "not computed"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _of(count: int, label: str) -> str:
    """A denominator cell: what was counted, then how many."""
    return f"{label}: {count}"


def _fraction(item: Mapping[str, Any]) -> str:
    return f"{item['numerator']} of {item['denominator']}"


def _rate_row(name: str, item: Mapping[str, Any], excluded: int | None) -> list[Any]:
    return [
        name,
        _percent(item["value"]),
        item["numerator"],
        _of(item["denominator"], item["denominator_label"]),
        "n/a" if excluded is None else excluded,
    ]


def _source_section(data: Mapping[str, Any]) -> list[str]:
    source = data["source"]
    mirror = source["mirror"]
    clone = source["clone"] or {}
    used = {
        "mirror": "Mirror rows",
        "clone_records": "This clone's run records, summarized in memory",
    }[source["used"]]
    scope = "All repositories" if source["scope"] == "all" else "This repository"
    filters = [
        f"{name} {', '.join(value) if isinstance(value, list) else value}"
        for name, value in data["filters"].items()
        if value
    ]
    rows: list[list[Any]] = [
        ["Source", used],
        ["Scope", scope],
        ["Mirror path", f"`{mirror['path']}`"],
        ["Mirror file exists", "yes" if mirror["exists"] else "no"],
        ["Mirror lines read", mirror["lines"]],
        ["Malformed mirror lines", mirror["malformed"]],
        ["Older duplicate mirror rows left out", mirror["duplicates"]],
    ]
    if clone:
        rows.extend(
            [
                ["Unreadable run records in this clone", clone["unreadable"]],
                [
                    "Finished runs in this clone missing from the mirror",
                    "n/a"
                    if clone["unmirrored_terminal_runs"] is None
                    else clone["unmirrored_terminal_runs"],
                ],
            ]
        )
    rows.extend(
        [
            ["Filters", "; ".join(filters) or "none"],
            ["Rows left out by filters", data["filtered_out"]["total"]],
            ["Runs in this report", data["rows_reported"]],
        ]
    )
    return ["### Source", "", *_table(["Item", "Value"], rows)]


def _runs_section(metrics: Mapping[str, Any]) -> list[str]:
    states = _table(["State", "Runs"], metrics["states"].items())
    hosted = metrics["hosted"]
    rates = [
        _rate_row("Completed (`DONE`)", metrics["completion_rate"], None),
        _rate_row(
            "Runs with a fix commit",
            metrics["fix_commits"]["runs_with_any"],
            metrics["fix_commits"]["excluded_runs"],
        ),
        _rate_row(
            "Runs with a validation restart",
            metrics["validation_restarts"]["runs_with_any"],
            metrics["validation_restarts"]["excluded_runs"],
        ),
    ]
    rate_table = _table(["Measure", "Rate", "Count", "Denominator", "Excluded runs"], rates)
    hosted_rows = [
        _rate_row("Hosted outcome recorded", hosted["outcome_recorded"], None),
        _rate_row(
            "Pull-request URL recorded", hosted["pr_url_recorded"], hosted["pr_url_excluded_runs"]
        ),
    ]
    return [
        "### Runs",
        "",
        *states,
        *rate_table,
        "### Hosted outcomes recorded",
        "",
        hosted["note"],
        "",
        *_table(["Measure", "Rate", "Count", "Denominator", "Excluded runs"], hosted_rows),
        *(
            _table(["Hosted outcome", "Runs"], hosted["outcomes"].items())
            if hosted["outcomes"]
            else []
        ),
    ]


def _findings_section(metrics: Mapping[str, Any]) -> list[str]:
    findings = metrics["findings"]
    blocking = findings["blocking"]
    share = blocking["fixed_share"]
    fixed = blocking["fixed"]
    per_units = findings["review_findings_per_100_units"]
    if fixed is None:
        fixed_count = fixed_share = None
    elif fixed["exact"]:
        fixed_count, fixed_share = fixed["min"], _percent(share["min"])
    else:
        fixed_count = f"{fixed['min']} to {fixed['max']}"
        fixed_share = f"{_percent(share['min'])} to {_percent(share['max'])}"
    rows = [
        [
            "Findings",
            findings["total"],
            _of(findings["runs_observed"], "runs with a findings file"),
            findings["excluded_runs"],
        ],
        [
            "Blocking findings",
            blocking["total"],
            _of(blocking["runs_observed"], "runs with a blocking count"),
            blocking["excluded_runs"],
        ],
        [
            "Blocking findings fixed",
            fixed_count,
            _of(share["denominator"], share["denominator_label"]),
            blocking["excluded_runs"],
        ],
        [
            "Share of blocking findings fixed",
            fixed_share,
            _of(share["denominator"], share["denominator_label"]),
            blocking["excluded_runs"],
        ],
        [
            "Review findings per 100 review units",
            "n/a" if per_units["value"] is None else per_units["value"],
            _of(per_units["denominator"], per_units["denominator_label"]),
            per_units["excluded_runs"],
        ],
    ]
    labels = {
        "by_stage": "Stage",
        "by_severity": "Severity",
        "by_status": "Status",
        "by_action": "Action",
    }
    breakdown = [
        [label, value, count]
        for name, label in labels.items()
        for value, count in findings[name].items()
    ]
    return [
        "### Findings",
        "",
        *_table(["Measure", "Value", "Denominator", "Excluded runs"], rows),
        blocking["note"],
        "",
        *(_table(["Dimension", "Value", "Findings"], breakdown) if breakdown else []),
    ]


def _stages_section(metrics: Mapping[str, Any]) -> list[str]:
    rows = [
        [
            name,
            stage["runs"],
            ", ".join(f"{status} {count}" for status, count in stage["statuses"].items()),
            _fraction(stage["attempts_at_least_one"]),
            _fraction(stage["attempts_above_one"]),
            _fraction(stage["reused"]),
            f"{stage['attempts_excluded_runs']} / {stage['reused_excluded_runs']}",
        ]
        for name, stage in metrics["stages"].items()
    ]
    if not rows:
        return []
    headers = [
        "Stage",
        "Runs",
        "Statuses",
        "Failed attempts: one or more",
        "Failed attempts: more than one",
        "Reused",
        "Excluded runs (attempts / reuse)",
    ]
    return ["### Stages", "", *_table(headers, rows)]


def _duration_section(metrics: Mapping[str, Any]) -> list[str]:
    duration = metrics["duration_seconds"]
    enough = duration["note"] is None
    rows = [
        ["Observations", duration["observations"]],
        ["Excluded runs", duration["excluded_runs"]],
        ["Minimum observations", duration["minimum_observations"]],
        [
            "Median",
            _duration(duration["median"]) if enough else "not computed: too few observations",
        ],
        ["p90", _duration(duration["p90"]) if enough else "not computed: too few observations"],
    ]
    return ["### Duration", "", *_table(["Measure", "Value"], rows)]


def _events_section(metrics: Mapping[str, Any]) -> list[str]:
    events = metrics["events"]
    rows = [
        [
            f"`{name}`",
            item["total"],
            item["runs"]["numerator"],
            _of(item["runs"]["denominator"], item["runs"]["denominator_label"]),
        ]
        for name, item in events["counts"].items()
    ]
    orphaned = metrics["orphaned_reasons"]
    return [
        "### Selected events",
        "",
        *_table(["Event", "Events", "Runs with the event", "Denominator"], rows),
        *(
            ["### Orphaned reasons", "", *_table(["Reason", "Runs"], orphaned.items())]
            if orphaned
            else []
        ),
    ]


def _group_section(title: str, header: str, groups: Sequence[Mapping[str, Any]]) -> list[str]:
    if not groups:
        return []
    rows = []
    for group in groups:
        metrics = group["metrics"]
        states = metrics["states"]
        rows.append(
            [
                group["label"],
                metrics["runs"],
                states["DONE"],
                states["ABORTED"],
                states["ORPHANED"],
                _percent(metrics["completion_rate"]["value"]),
                metrics["findings"]["total"],
                _duration(metrics["duration_seconds"]["median"]),
            ]
        )
    headers = [
        header,
        "Runs",
        "DONE",
        "ABORTED",
        "ORPHANED",
        "Completed",
        "Findings",
        "Median duration",
    ]
    return [f"### {title}", "", *_table(headers, rows)]


def render_markdown(data: Mapping[str, Any]) -> str:
    """A compact report for a person. Numbers appear in tables only."""
    overall = data["overall"]
    repositories = [
        {
            **group,
            "label": f"{group['label']} ({group['remote']})" if group["remote"] else group["label"],
        }
        for group in data["by_repo"]
    ]
    lines = [
        "## Agentic Preflight report",
        "",
        *_source_section(data),
        *_runs_section(overall),
        *_findings_section(overall),
        *_stages_section(overall),
        *_duration_section(overall),
        *_events_section(overall),
        *_group_section("By repository", "Repository", repositories),
        *_group_section("By client", "Client", data["by_client"]),
        *_group_section("By reported model", "Reported model", data["by_model"]),
        "### Limits",
        "",
        *(f"- {limit}" for limit in data["limits"]),
    ]
    return "\n".join(lines) + "\n"
