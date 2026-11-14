"""Aggregate local run summaries into a usage report.

The report reads :class:`~agentic_preflight.run_summary.RunSummary` rows, from the
user-level mirror or from one clone's run records, and counts them. It has three
parts:

* :mod:`.reading` parses the mirror and the hook log, and summarizes a clone's
  run records in memory. It is the only part that reads files.
* :mod:`.aggregation` filters the rows and computes every statistic.
* :mod:`.markdown` renders the report's data for a person.

The report follows the summary's rules:

* Nothing is sent anywhere.
* A null value is unknown, never zero. Each statistic leaves null values out and
  reports how many runs it left out.
* An unreadable line or record is counted and reported, never treated as absent.
* Every ratio names its denominator.
"""

from __future__ import annotations

from .aggregation import (
    BLOCKING_NOTE,
    CATCH_NOTE,
    FALSE_POSITIVE_NOTE,
    FORMATS,
    HOOK_FILTERS,
    HOOK_NOTE,
    HOSTED_LIMITS,
    LIMITS,
    MIN_QUANTILE_OBSERVATIONS,
    REPORT_STATES,
    RESPONDED_STATUSES,
    SELECTED_EVENTS,
    SYNC_NOTE,
    TIMING_NOTE,
    TOOL_TIME_NOTE,
    UNSPECIFIED,
    Filters,
    aggregate,
    apply_filters,
    build_report,
    by_producer,
    by_repo,
    hook_decisions,
    hook_decisions_by_repo,
    matches_repo,
    parse_timestamp,
    producer_value,
    ratio,
    repo_key,
)
from .markdown import render_markdown
from .reading import (
    MALFORMED_LINE_LIMIT,
    UNKNOWN,
    Clone,
    CloneScan,
    HookLogRead,
    MirrorRead,
    belongs_to_clone,
    collect,
    parse_hook_log,
    parse_mirror,
    read_hook_log,
    read_mirror,
    summarize_clone,
)

__all__ = [
    "BLOCKING_NOTE",
    "CATCH_NOTE",
    "FALSE_POSITIVE_NOTE",
    "FORMATS",
    "HOOK_FILTERS",
    "HOOK_NOTE",
    "HOSTED_LIMITS",
    "LIMITS",
    "MALFORMED_LINE_LIMIT",
    "MIN_QUANTILE_OBSERVATIONS",
    "REPORT_STATES",
    "RESPONDED_STATUSES",
    "SELECTED_EVENTS",
    "SYNC_NOTE",
    "TIMING_NOTE",
    "TOOL_TIME_NOTE",
    "UNKNOWN",
    "UNSPECIFIED",
    "Clone",
    "CloneScan",
    "Filters",
    "HookLogRead",
    "MirrorRead",
    "aggregate",
    "apply_filters",
    "belongs_to_clone",
    "build_report",
    "by_producer",
    "by_repo",
    "collect",
    "hook_decisions",
    "hook_decisions_by_repo",
    "matches_repo",
    "parse_hook_log",
    "parse_mirror",
    "parse_timestamp",
    "producer_value",
    "ratio",
    "read_hook_log",
    "read_mirror",
    "render_markdown",
    "repo_key",
    "summarize_clone",
]
