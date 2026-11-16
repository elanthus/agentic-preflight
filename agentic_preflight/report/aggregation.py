"""Filter summary rows and count them: every statistic of the usage report.

Everything here is pure. The rows come from :mod:`.reading`, and
:func:`build_report` hands the result to :mod:`.markdown` when a person asked
for Markdown.

* A null value is unknown, never zero. Each statistic leaves null values out and
  reports how many runs it left out.
* Every ratio names its denominator.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from .. import telemetry
from ..run_summary import SummaryRow, as_int, as_str, stage_order
from .markdown import render_markdown
from .reading import UNKNOWN, Clone, HookLogRead, belongs_to_clone, finish_order, tally

#: A summary row or a hook decision: both name their repository under ``repo``.
_Row = TypeVar("_Row", bound=Mapping[str, Any])

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


#: A response or abort recorded without the agent's reason.
UNSPECIFIED = "unspecified"


#: Finding statuses that record a response to the finding.
RESPONDED_STATUSES = ("fixed", "dismissed", "accepted")


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
    "An orphaned run can be a replaced attempt, not a failure.",
    "A null value is unknown, not zero. "
    "Each statistic leaves out null values and reports how many runs it left out.",
    "Finding response reasons and abort reasons are labels that the agent chose. "
    "They record the agent's judgment, not a verified cause. "
    "The perceived false-positive share is not a measured false-positive rate.",
    "Stage and state times are wall-clock seconds from recorded transitions, "
    "with one-second granularity. They include the agent's working time and any wait for "
    "the user. "
    "Reused and skipped stages, and runs without a complete transition history, have no time.",
    "`git push --no-verify` skips the pre-push hook, so a bypassed push leaves no hook decision.",
    "A push that the installed hook allowed because `agentic-preflight` was not on `PATH` "
    "is not logged. Neither is a decision whose log write failed or did not finish in time.",
)


#: Added to the limits only when at least one run in the report was synced.
HOSTED_LIMITS: tuple[str, ...] = (
    "A failed hosted check is not proof of an escaped defect. "
    "The check can be flaky, or fail for a reason unrelated to the change.",
    "Synced hosted outcomes count only the runs that `telemetry sync-hosted` fetched, "
    "as GitHub reported them at the fetch time. Runs that were not synced are not in them.",
)


SYNC_NOTE = (
    "Fetched from GitHub by `telemetry sync-hosted`. Only synced runs count. "
    "Runs with pending checks are left out of the failed share. "
    "A conclusion of `unknown` is a run whose commit GitHub did not know."
)


FALSE_POSITIVE_NOTE = (
    "The perceived false-positive share counts the findings that the agent dismissed "
    "or accepted as `false_positive`, over every finding with a recorded response. "
    "A dismissal or acceptance without a reason counts as `unspecified`. "
    "While any are unspecified, the share is a range."
)


TIMING_NOTE = (
    "Review time covers the review and docs states. Shell time covers the lint and test states. "
    "Only runs with a complete transition history are counted."
)


CATCH_NOTE = (
    "A catch is something the gate stopped before the push: a blocking review or docs "
    "finding that was fixed, or a lint or test stage that failed and later passed in the "
    "same run. It is not proof that hosted checks or a reviewer would have missed it."
)


TOOL_TIME_NOTE = (
    "Tool time is the wall time of the commands the tool ran on its own before the run "
    "ended: sync, setup, the review command, the contract planner, the shadow review of "
    "`review compare`, the baseline check, lint, test, and merge-back. "
    "The push is not timed. "
    "A run recorded before command timing has no tool time. "
    "A pull request sums the tool time of its synced `DONE` runs, and is left out "
    "when any of them has no tool time."
)


BLOCKING_NOTE = (
    "A summary counts blocking findings and finding statuses separately. "
    "The fixed count is exact only for runs where every finding or no finding is blocking. "
    "Otherwise it is a range."
)


HOOK_NOTE = (
    "Each row is one pre-push hook decision, not one push. "
    "Git can still fail an allowed push, and a blocked push is often retried. "
    "The state filter does not apply to hook decisions."
)


#: The reasons a hook decision is left out, in the order they are checked.
HOOK_FILTERS = ("scope", "repo", "since", "until", "time_unknown")


# -- filters ------------------------------------------------------------------


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
        # An already normalized ``host/path`` has no scheme, so it normalizes as a
        # relative path. Match it as given, too.
        candidates.add(normalized.removeprefix("./").casefold())
    repo = row.get("repo") or {}
    values = (repo.get("name"), repo.get("remote"))
    return any(isinstance(value, str) and value.casefold() in candidates for value in values)


def _excluded_by(row: SummaryRow, filters: Filters) -> str | None:
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
    rows: Iterable[SummaryRow], filters: Filters
) -> tuple[list[SummaryRow], dict[str, int]]:
    """Return the rows that pass, and how many each filter left out.

    Each row left out is counted once, under the first filter that excluded it.
    """
    left_out = dict.fromkeys(("state", "repo", "since", "until", "time_unknown"), 0)
    kept: list[SummaryRow] = []
    for row in rows:
        reason = _excluded_by(row, filters)
        if reason is None:
            kept.append(row)
        else:
            left_out[reason] += 1
    return kept, {**left_out, "total": sum(left_out.values())}


# -- statistics ---------------------------------------------------------------


def _reason_tally(values: Iterable[Any]) -> dict[str, int]:
    """Tally agent-chosen reasons. A missing reason is ``unspecified``, not ``unknown``."""
    counts = Counter(
        UNSPECIFIED if value is None else value if isinstance(value, str) and value else UNKNOWN
        for value in values
    )
    return dict(sorted(counts.items()))


def ratio(numerator: int, denominator: int, label: str) -> dict[str, Any]:
    """A ratio that names its denominator. ``value`` is null when nothing was counted."""
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "denominator_label": label,
    }


def _states(rows: Sequence[SummaryRow]) -> dict[str, int]:
    counts = Counter(as_str(row.get("state")) or UNKNOWN for row in rows)
    ordered = {state: counts.pop(state, 0) for state in REPORT_STATES}
    return {**ordered, **dict(sorted(counts.items()))}


def _known_findings(rows: Sequence[SummaryRow]) -> list[Mapping[str, Any]]:
    findings = (row.get("findings") or {} for row in rows)
    return [item for item in findings if as_int(item.get("total")) is not None]


def _add_counts(counter: Counter[str], counts: Any) -> None:
    if isinstance(counts, Mapping):
        for name, count in counts.items():
            if as_int(count) is not None:
                counter[str(name)] += count


def _blocking(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    known = [item for item in _known_findings(rows) if as_int(item.get("blocking")) is not None]
    total = low = high = 0
    for item in known:
        blocking, findings = item["blocking"], item["total"]
        fixed = as_int((item.get("by_status") or {}).get("fixed")) or 0
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


def _per_100_units(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    findings = units = observed = 0
    for row in rows:
        counts = row.get("findings") or {}
        total_units = as_int((row.get("review") or {}).get("total_units"))
        if as_int(counts.get("total")) is None or total_units is None:
            continue
        observed += 1
        findings += as_int((counts.get("by_stage") or {}).get("review")) or 0
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


def _findings(rows: Sequence[SummaryRow]) -> dict[str, Any]:
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


def _finding_responses(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Reasons for dismissed and accepted findings, and the perceived false-positive share.

    The share is ``false_positive`` responses over every finding with a recorded
    response (``fixed``, ``dismissed``, or ``accepted``). A dismissal or acceptance
    without a reason could be either, so ``max`` also counts the unspecified ones,
    as the blocking-fix range does. Only runs whose summary tallies reasons are
    counted; the rest are excluded runs.
    """
    findings = (row.get("findings") or {} for row in rows)
    known = [
        (by_reason, by_status)
        for item in findings
        if isinstance(by_reason := item.get("by_response_reason"), Mapping)
        and isinstance(by_status := item.get("by_status"), Mapping)
    ]
    reasons: Counter[str] = Counter()
    responses = 0
    for by_reason, by_status in known:
        _add_counts(reasons, by_reason)
        responses += sum(as_int(by_status.get(name)) or 0 for name in RESPONDED_STATUSES)
    false_positive, unspecified = reasons["false_positive"], reasons[UNSPECIFIED]
    upper = false_positive + unspecified
    return {
        "runs_observed": len(known),
        "excluded_runs": len(rows) - len(known),
        "by_reason": dict(sorted(reasons.items())),
        "responses": responses if known else None,
        "unspecified": ratio(unspecified, sum(reasons.values()), "dismissed or accepted findings"),
        "perceived_false_positive_share": {
            **ratio(false_positive, responses, "findings with a recorded response"),
            "max": round(upper / responses, 4) if responses else None,
            "numerator_max": upper,
            "exact": unspecified == 0,
        },
        "note": FALSE_POSITIVE_NOTE,
    }


def _any_positive(rows: Sequence[SummaryRow], key: str, label: str) -> dict[str, Any]:
    known = [value for value in (as_int(row.get(key)) for row in rows) if value is not None]
    return {
        "runs_with_any": ratio(sum(1 for value in known if value > 0), len(known), label),
        "total": sum(known) if known else None,
        "excluded_runs": len(rows) - len(known),
    }


def _numbers(values: Iterable[Any]) -> list[float]:
    """The numeric values, sorted. Null and non-numeric values are left out."""
    return sorted(
        float(value)
        for value in values
        if isinstance(value, int | float) and not isinstance(value, bool)
    )


def _quantiles(values: Sequence[float], population: int) -> dict[str, Any]:
    """Median and p90 of ``values``, from ``population`` runs that could have had one."""
    enough = len(values) >= MIN_QUANTILE_OBSERVATIONS
    return {
        "observations": len(values),
        "excluded_runs": population - len(values),
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


def _stage(present: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = [
        value for value in (as_int(item.get("attempts")) for item in present) if value is not None
    ]
    reused = [item["reused"] for item in present if isinstance(item.get("reused"), bool)]
    attempts_label = "runs with an attempt count for this stage"
    return {
        "runs": len(present),
        "statuses": tally(item.get("status") for item in present),
        "attempts_at_least_one": ratio(
            sum(1 for a in attempts if a >= 1), len(attempts), attempts_label
        ),
        "attempts_above_one": ratio(
            sum(1 for a in attempts if a > 1), len(attempts), attempts_label
        ),
        "attempts_excluded_runs": len(present) - len(attempts),
        "reused": ratio(sum(reused), len(reused), "runs with a reuse value for this stage"),
        "reused_excluded_runs": len(present) - len(reused),
        # Reused and skipped stages, and records from before stage start times, have none.
        "duration_seconds": _quantiles(
            _numbers(item.get("duration_seconds") for item in present), len(present)
        ),
    }


def _stages(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    stages = [row.get("stages") or {} for row in rows]
    names = sorted({name for item in stages for name in item}, key=stage_order)
    return {
        name: _stage([item[name] for item in stages if isinstance(item.get(name), Mapping)])
        for name in names
    }


def _durations(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    return _quantiles(_numbers(row.get("duration_seconds") for row in rows), len(rows))


def _run_seconds(timing: Mapping[str, Any]) -> float | None:
    """Seconds in every listed state of one timing, or ``None`` when any is unknown."""
    states = timing.get("states")
    if not isinstance(states, Mapping):
        return None
    seconds = [
        entry.get("seconds") if isinstance(entry, Mapping) else None for entry in states.values()
    ]
    values = _numbers(seconds)
    return sum(values) if len(values) == len(seconds) else None


def _seconds_share(numerator: float, denominator: float, label: str) -> dict[str, Any]:
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "denominator_label": label,
    }


def _time_split(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Share of run time in the review and docs states versus the lint and test states."""
    observed: list[tuple[float, float, float]] = []
    for row in rows:
        timing = row.get("timing")
        if not isinstance(timing, Mapping) or timing.get("complete") is not True:
            continue
        review = _numbers([timing.get("review_wall_seconds")])
        shell = _numbers([timing.get("shell_wall_seconds")])
        total = _run_seconds(timing)
        if review and shell and total is not None:
            observed.append((review[0], shell[0], total))
    review_seconds = sum(item[0] for item in observed)
    shell_seconds = sum(item[1] for item in observed)
    total_seconds = sum(item[2] for item in observed)
    label = "seconds in runs with a complete transition history"
    return {
        "runs_observed": len(observed),
        "excluded_runs": len(rows) - len(observed),
        "total_seconds": total_seconds if observed else None,
        "review_share": _seconds_share(review_seconds, total_seconds, label),
        "shell_share": _seconds_share(shell_seconds, total_seconds, label),
        "note": TIMING_NOTE,
    }


#: The catch sources of a summary: where the value is, and whether it is a count.
_CATCH_SOURCES: tuple[tuple[str, str, str, bool], ...] = (
    ("review_blocking_fixed", "findings", "review_blocking_fixed", True),
    ("docs_blocking_fixed", "findings", "docs_blocking_fixed", True),
    ("lint_red_then_green", "lint", "red_then_green", False),
    ("test_red_then_green", "test", "red_then_green", False),
)


def _catch_value(row: SummaryRow, section: str, key: str, count: bool) -> int | bool | None:
    """One catch source of one run, or ``None`` when the summary does not know it."""
    holder: Mapping[str, Any] | None
    if section == "findings":
        holder = row.get("findings")
    else:
        stages = row.get("stages")
        holder = stages.get(section) if isinstance(stages, Mapping) else None
    value = holder.get(key) if isinstance(holder, Mapping) else None
    if count:
        return as_int(value)
    return value if isinstance(value, bool) else None


def _caught(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """``DONE`` runs where the gate stopped something before the push, by source.

    A run with any known catch counts as caught. A run with no known catch counts
    as not caught only when every source is known; otherwise it is excluded.
    """
    done = [row for row in rows if row.get("state") == "DONE"]
    values = [
        {
            name: _catch_value(row, section, key, count)
            for name, section, key, count in _CATCH_SOURCES
        }
        for row in done
    ]
    with_catch = sum(1 for item in values if any(item.values()))
    known = sum(1 for item in values if any(item.values()) or None not in item.values())
    by_source: dict[str, Any] = {}
    for name, _, _, count in _CATCH_SOURCES:
        observed = [value for item in values if (value := item[name]) is not None]
        label = f"DONE runs with a known {name.replace('_', ' ')} value"
        by_source[name] = {
            "runs_with_any": ratio(sum(1 for value in observed if value), len(observed), label),
            "total": sum([int(value) for value in observed]) if count and observed else None,
            "excluded_runs": len(done) - len(observed),
        }
    return {
        "done_runs": len(done),
        "runs_with_any_catch": ratio(
            with_catch, known, "DONE runs with every catch source known or a known catch"
        ),
        "excluded_runs": len(done) - known,
        "by_source": by_source,
        "note": CATCH_NOTE,
    }


def _tool_seconds(row: SummaryRow) -> float | None:
    timing = row.get("timing")
    values = _numbers([timing.get("tool_seconds")]) if isinstance(timing, Mapping) else []
    return values[0] if values else None


def _tool_time(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Median and p90 tool seconds per ``DONE`` run, and per synced pull request.

    A pull request can span several runs, so its time is the sum over its synced
    ``DONE`` runs, grouped by repository and ``hosted.pr_number``. Only outcomes
    that ``telemetry sync-hosted`` already joined are used.
    """
    done = [row for row in rows if row.get("state") == "DONE"]
    per_run = [value for value in (_tool_seconds(row) for row in done) if value is not None]
    pull_requests: dict[tuple[str, int], list[float | None]] = {}
    unsynced = 0
    for row in done:
        hosted = row.get("hosted")
        number = as_int(hosted.get("pr_number")) if isinstance(hosted, Mapping) else None
        if number is None:
            unsynced += 1
            continue
        pull_requests.setdefault((repo_key(row), number), []).append(_tool_seconds(row))
    sums = sorted(
        sum(value for value in times if value is not None)
        for times in pull_requests.values()
        if None not in times
    )
    per_pr = _quantiles(sums, len(pull_requests))
    excluded_pull_requests = per_pr.pop("excluded_runs")
    return {
        "per_run": _quantiles(sorted(per_run), len(done)),
        "per_run_label": "DONE runs",
        "total_seconds": round(sum(per_run), 3) if per_run else None,
        "per_pull_request": {
            **per_pr,
            "pull_requests": len(pull_requests),
            "excluded_pull_requests": excluded_pull_requests,
            "runs_without_pull_request": unsynced,
            "label": "synced pull requests of DONE runs",
        },
        "note": TOOL_TIME_NOTE,
    }


def _hosted(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Hosted outcomes that ``telemetry sync-hosted`` joined."""
    return {"synced": _hosted_synced(rows)}


def _hosted_synced(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Outcomes that ``telemetry sync-hosted`` joined. Every ratio counts synced runs only.

    A run whose commit GitHub did not know has no conclusion, and a run whose
    checks were pending has no settled one. Each is left out of the shares that
    need that value, and counted as excluded.
    """
    synced = [
        (row, row.get("hosted") or {})
        for row in rows
        if as_str((row.get("hosted") or {}).get("fetched_at"))
    ]
    known = [(row, item) for row, item in synced if as_str(item.get("checks_conclusion"))]
    settled = [(row, item) for row, item in known if item["checks_conclusion"] != "pending"]
    with_pr = [item for _, item in known if as_int(item.get("pr_number")) is not None]
    green_failed = sum(
        1
        for row, item in settled
        if row.get("state") == "DONE" and item["checks_conclusion"] == "failure"
    )
    return {
        "runs": ratio(len(synced), len(rows), "runs"),
        "green_checks_failed": ratio(green_failed, len(settled), "synced runs with settled checks"),
        "checks_excluded_runs": len(synced) - len(settled),
        "pull_request_found": ratio(
            len(with_pr), len(known), "synced runs whose commit GitHub knew"
        ),
        "commit_unknown_runs": len(synced) - len(known),
        "merged": ratio(
            sum(1 for item in with_pr if item.get("merged") is True),
            len(with_pr),
            "synced runs with a pull request",
        ),
        "pr_states": tally(item.get("pr_state") for item in with_pr),
        "checks_conclusions": tally(item.get("checks_conclusion") for _, item in synced),
        "note": SYNC_NOTE,
    }


def _orphaned_reasons(rows: Sequence[SummaryRow]) -> dict[str, int]:
    return tally(
        (row.get("terminal") or {}).get("orphaned_reason")
        for row in rows
        if row.get("state") == "ORPHANED"
    )


def _abort_reasons(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Aborted runs by the agent's reason and by the state the run was in."""
    aborted = [row.get("terminal") or {} for row in rows if row.get("state") == "ABORTED"]
    return {
        "runs": len(aborted),
        "by_reason": _reason_tally(item.get("abort_reason") for item in aborted),
        "by_state": tally(item.get("aborted_from_state") for item in aborted),
    }


def _events(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    known = [item for row in rows if isinstance(item := row.get("event_counts"), Mapping)]
    counts: dict[str, Any] = {}
    for name in SELECTED_EVENTS:
        per_run = [as_int(item.get(name)) or 0 for item in known]
        counts[name] = {
            "total": sum(per_run) if known else None,
            "runs": ratio(
                sum(1 for count in per_run if count), len(known), "runs with event counts"
            ),
        }
    return {"runs_observed": len(known), "excluded_runs": len(rows) - len(known), "counts": counts}


def aggregate(rows: Sequence[SummaryRow]) -> dict[str, Any]:
    """Every statistic for one group of summary rows."""
    states = _states(rows)
    return {
        "runs": len(rows),
        "states": states,
        "completion_rate": ratio(states["DONE"], len(rows), "runs"),
        "findings": _findings(rows),
        "finding_responses": _finding_responses(rows),
        "fix_commits": _any_positive(rows, "fix_commits", "runs with a fix-commit count"),
        "validation_restarts": _any_positive(
            rows, "validation_restarts", "runs with a validation-restart count"
        ),
        "stages": _stages(rows),
        "duration_seconds": _durations(rows),
        "time_in_stages": _time_split(rows),
        "caught": _caught(rows),
        "tool_time": _tool_time(rows),
        "hosted": _hosted(rows),
        "orphaned_reasons": _orphaned_reasons(rows),
        "abort_reasons": _abort_reasons(rows),
        "events": _events(rows),
    }


def _groups(rows: Iterable[_Row], key: Callable[[_Row], str]) -> dict[str, list[_Row]]:
    groups: dict[str, list[_Row]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return groups


def _by_size(item: Mapping[str, Any]) -> tuple[int, str]:
    return (-item["metrics"]["runs"], str(item.get("label") or ""))


def repo_key(row: Mapping[str, Any]) -> str:
    """The repository part of the mirror key: the root commit, else the worktree path."""
    repo = row.get("repo") or {}
    return as_str(repo.get("root_commit")) or f"path:{repo.get('source_worktree_path')}"


def by_repo(rows: Sequence[SummaryRow]) -> list[dict[str, Any]]:
    """One group per repository, named by its most recently finished run."""
    result = []
    for key, members in _groups(rows, repo_key).items():
        # Public callers may pass unvalidated rows, so a missing repository is tolerated.
        latest: Mapping[str, Any] = max(members, key=finish_order)
        repo = latest.get("repo") or {}
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


def producer_value(row: SummaryRow, name: str) -> str:
    """One producer field, or ``unknown`` when the run did not record it."""
    producer = row.get("producer")
    value = producer.get(name) if isinstance(producer, Mapping) else None
    return value if isinstance(value, str) and value else UNKNOWN


def by_producer(rows: Sequence[SummaryRow], name: str) -> list[dict[str, Any]]:
    groups = _groups(rows, lambda row: producer_value(row, name))
    result = [
        {"label": value, name: value, "metrics": aggregate(members)}
        for value, members in groups.items()
    ]
    return sorted(result, key=_by_size)


def build_report(
    rows: Sequence[SummaryRow],
    *,
    source: dict[str, Any],
    filters: Filters,
    filtered_out: dict[str, int],
    output_format: str = "json",
    include_rows: bool = False,
    hook_decisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The report ``data``: source, filters, aggregates, limits, and optional rows.

    ``hook_decisions`` is the :func:`hook_decisions` section, or ``None`` to leave it out.
    """
    overall = aggregate(rows)
    data: dict[str, Any] = {
        "format": output_format,
        "source": source,
        "filters": filters.describe(),
        "filtered_out": filtered_out,
        "rows_reported": len(rows),
        "overall": overall,
        "by_repo": by_repo(rows),
        "by_client": by_producer(rows, "client"),
        "by_model": by_producer(rows, "reported_model"),
        "hook_decisions": hook_decisions,
        "limits": [
            *LIMITS,
            *(HOSTED_LIMITS if overall["hosted"]["synced"]["runs"]["numerator"] else ()),
        ],
        "markdown": None,
        "rows": list(rows) if include_rows else None,
    }
    if output_format == "markdown":
        data["markdown"] = render_markdown(data)
    return data


# -- hook decisions -----------------------------------------------------------


def _hook_excluded_by(row: Mapping[str, Any], clone: Clone | None, filters: Filters) -> str | None:
    if clone is not None and not belongs_to_clone(
        row, clone.identity.root_commit, clone.worktree_paths
    ):
        return "scope"
    if filters.repo is not None and not matches_repo(row, filters.repo):
        return "repo"
    if filters.since is None and filters.until is None:
        return None
    at = _timestamp(row.get("at"))
    if at is None:
        return "time_unknown"
    if filters.since is not None and at < filters.since:
        return "since"
    if filters.until is not None and at >= filters.until:
        return "until"
    return None


def _hook_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    allowed = sum(1 for row in rows if row.get("allowed") is True)
    blocked = len(rows) - allowed
    return {
        "decisions": len(rows),
        "allowed": allowed,
        "blocked": blocked,
        "blocked_share": ratio(blocked, len(rows), "hook decisions"),
        "allowed_by_reason": tally(row.get("reason") for row in rows if row.get("allowed")),
        "blocked_by_reason": tally(row.get("reason") for row in rows if not row.get("allowed")),
    }


def _hook_order(row: Mapping[str, Any]) -> str:
    return as_str(row.get("at")) or ""


def hook_decisions_by_repo(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One group per repository, named by its latest decision."""
    result = []
    for key, members in _groups(rows, repo_key).items():
        repo = max(members, key=_hook_order).get("repo") or {}
        result.append(
            {
                "key": key,
                "label": repo.get("name") or repo.get("remote") or key,
                "name": repo.get("name"),
                "remote": repo.get("remote"),
                "root_commit": repo.get("root_commit"),
                "metrics": _hook_counts(members),
            }
        )
    return sorted(result, key=lambda item: (-item["metrics"]["decisions"], str(item["label"])))


def hook_decisions(
    log: HookLogRead,
    clone: Clone | None,
    *,
    all_repos: bool,
    filters: Filters,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """The "Hook decisions" section: allowed and blocked counts, overall and per repository.

    The scope, ``--repo``, ``--since``, and ``--until`` select decisions as they
    select runs, with the decision's ``at`` time in place of ``finished_at``.
    ``--state`` does not apply. When the log cannot be read, the counts are null.
    """
    scoped = None if all_repos else clone
    left_out = dict.fromkeys(HOOK_FILTERS, 0)
    kept: list[dict[str, Any]] = []
    for row in log.decisions:
        reason = _hook_excluded_by(row, scoped, filters)
        if reason is None:
            kept.append(row)
        else:
            left_out[reason] += 1
    readable = log.error is None
    return {
        "source": {**log.describe(), "enabled": enabled},
        "scope": "clone" if scoped is not None else "all",
        "filtered_out": {**left_out, "total": sum(left_out.values())},
        "overall": _hook_counts(kept) if readable else None,
        "by_repo": hook_decisions_by_repo(kept) if readable else [],
        "note": HOOK_NOTE,
    }
