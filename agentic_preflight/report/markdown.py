"""Render the usage report's data as Markdown for a person.

Numbers appear in tables only. A null value reads ``n/a``, never ``0``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..run_summary import STAGE_ORDER


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
    return ["### Runs", "", *states, *rate_table]


_CATCH_LABELS = {
    "review_blocking_fixed": "Blocking review finding fixed",
    "docs_blocking_fixed": "Blocking docs finding fixed",
    "lint_red_then_green": "Lint failed, then passed",
    "test_red_then_green": "Test failed, then passed",
}


def _caught_section(metrics: Mapping[str, Any]) -> list[str]:
    caught = metrics["caught"]
    rows = [
        _rate_row("Runs with any catch", caught["runs_with_any_catch"], caught["excluded_runs"]),
        *(
            _rate_row(
                label,
                caught["by_source"][name]["runs_with_any"],
                caught["by_source"][name]["excluded_runs"],
            )
            for name, label in _CATCH_LABELS.items()
        ),
    ]
    totals = [
        [_CATCH_LABELS[name], caught["by_source"][name]["total"]]
        for name in ("review_blocking_fixed", "docs_blocking_fixed")
    ]
    return [
        "### Caught before push",
        "",
        caught["note"],
        "",
        *_table(["Measure", "Rate", "Count", "Denominator", "Excluded runs"], rows),
        *_table(["Findings", "Total"], totals),
    ]


def _tool_time_section(metrics: Mapping[str, Any]) -> list[str]:
    cost = metrics["tool_time"]
    per_run, per_pr = cost["per_run"], cost["per_pull_request"]
    rows = [
        [
            f"Per run ({cost['per_run_label']})",
            per_run["observations"],
            per_run["excluded_runs"],
            _quantile_cell(per_run, "median"),
            _quantile_cell(per_run, "p90"),
        ],
        [
            f"Per pull request ({per_pr['label']})",
            per_pr["observations"],
            per_pr["excluded_pull_requests"],
            _quantile_cell(per_pr, "median"),
            _quantile_cell(per_pr, "p90"),
        ],
    ]
    return [
        "### Tool time",
        "",
        cost["note"],
        "",
        *_table(["Group", "Observations", "Excluded", "Median", "p90"], rows),
    ]


def _hosted_sync_section(
    metrics: Mapping[str, Any], repositories: Sequence[Mapping[str, Any]]
) -> list[str]:
    synced = metrics["hosted"]["synced"]
    if not synced["runs"]["numerator"]:
        # Nothing was synced, so every share would be n/a. The data keeps the zeros.
        return []
    rows = [
        _rate_row("Runs synced", synced["runs"], None),
        _rate_row(
            "Preflight green, hosted checks failed",
            synced["green_checks_failed"],
            synced["checks_excluded_runs"],
        ),
        _rate_row(
            "Pull request found", synced["pull_request_found"], synced["commit_unknown_runs"]
        ),
        _rate_row("Pull request merged", synced["merged"], None),
    ]
    per_repo = []
    for group in repositories:
        item = group["metrics"]["hosted"]["synced"]
        if item["runs"]["numerator"]:
            per_repo.append(
                [
                    group["label"],
                    item["runs"]["numerator"],
                    item["green_checks_failed"]["numerator"],
                    _percent(item["green_checks_failed"]["value"]),
                    _fraction(item["merged"]),
                    _percent(item["merged"]["value"]),
                ]
            )
    repo_headers = [
        "Repository",
        "Synced runs",
        "Green, hosted checks failed",
        "Share of synced runs with settled checks",
        "Merged of synced runs with a pull request",
        "Merged share",
    ]
    return [
        "### Hosted outcomes synced",
        "",
        synced["note"],
        "",
        *_table(["Measure", "Rate", "Count", "Denominator", "Excluded runs"], rows),
        *(
            _table(["Hosted check conclusion", "Synced runs"], synced["checks_conclusions"].items())
            if synced["checks_conclusions"]
            else []
        ),
        *(_table(repo_headers, per_repo) if per_repo else []),
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
    durations = [
        [
            name,
            stage["duration_seconds"]["observations"],
            stage["duration_seconds"]["excluded_runs"],
            _quantile_cell(stage["duration_seconds"], "median"),
            _quantile_cell(stage["duration_seconds"], "p90"),
        ]
        for name, stage in metrics["stages"].items()
    ]
    duration_headers = ["Stage", "Duration observations", "Excluded runs", "Median", "p90"]
    return [
        "### Stages",
        "",
        *_table(headers, rows),
        *_table(duration_headers, durations),
    ]


def _quantile_cell(quantiles: Mapping[str, Any], key: str) -> str:
    if quantiles["note"] is not None:
        return "not computed: too few observations"
    return _duration(quantiles[key])


def _share_row(name: str, share: Mapping[str, Any], excluded: int) -> list[Any]:
    known = share["denominator"] or None
    return [
        name,
        _percent(share["value"]),
        _duration(share["numerator"]) if known else "n/a",
        f"{share['denominator_label']}: {_duration(share['denominator']) if known else 'n/a'}",
        excluded,
    ]


def _stage_median(metrics: Mapping[str, Any], stage: str) -> str:
    item = metrics["stages"].get(stage)
    return _duration(item["duration_seconds"]["median"]) if item else "n/a"


def _duration_section(
    metrics: Mapping[str, Any], repositories: Sequence[Mapping[str, Any]] = ()
) -> list[str]:
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
    split = metrics["time_in_stages"]
    shares = [
        _share_row("Review and docs states", split["review_share"], split["excluded_runs"]),
        _share_row("Lint and test states", split["shell_share"], split["excluded_runs"]),
    ]
    per_repo = [
        [
            group["label"],
            group["metrics"]["time_in_stages"]["runs_observed"],
            _percent(group["metrics"]["time_in_stages"]["review_share"]["value"]),
            _percent(group["metrics"]["time_in_stages"]["shell_share"]["value"]),
            *(_stage_median(group["metrics"], stage) for stage in STAGE_ORDER),
        ]
        for group in repositories
    ]
    per_repo_headers = [
        "Repository",
        "Runs with complete timing",
        "Review and docs share",
        "Lint and test share",
        *(f"Median {stage}" for stage in STAGE_ORDER),
    ]
    return [
        "### Duration",
        "",
        *_table(["Measure", "Value"], rows),
        split["note"],
        "",
        *_table(["Time in", "Share", "Time", "Denominator", "Excluded runs"], shares),
        *(_table(per_repo_headers, per_repo) if per_repo else []),
    ]


def _false_positive_row(name: str, share: Mapping[str, Any], excluded: int) -> list[Any]:
    """A share that is a range while some responses have no reason."""
    if share["exact"] or share["value"] is None:
        rate, count = _percent(share["value"]), share["numerator"]
    else:
        rate = f"{_percent(share['value'])} to {_percent(share['max'])}"
        count = f"{share['numerator']} to {share['numerator_max']}"
    return [name, rate, count, _of(share["denominator"], share["denominator_label"]), excluded]


def _finding_responses_section(
    metrics: Mapping[str, Any], repositories: Sequence[Mapping[str, Any]] = ()
) -> list[str]:
    responses = metrics["finding_responses"]
    measures = [
        _false_positive_row(
            "Perceived false-positive share",
            responses["perceived_false_positive_share"],
            responses["excluded_runs"],
        ),
        _rate_row(
            "Dismissed or accepted without a reason",
            responses["unspecified"],
            responses["excluded_runs"],
        ),
    ]
    reasons = list(responses["by_reason"].items())
    per_repo = [
        [group["label"], reason, count]
        for group in repositories
        for reason, count in group["metrics"]["finding_responses"]["by_reason"].items()
    ]
    shares = [
        _false_positive_row(
            group["label"],
            group["metrics"]["finding_responses"]["perceived_false_positive_share"],
            group["metrics"]["finding_responses"]["excluded_runs"],
        )
        for group in repositories
    ]
    return [
        "### Finding responses",
        "",
        responses["note"],
        "",
        *_table(["Measure", "Rate", "Count", "Denominator", "Excluded runs"], measures),
        *(_table(["Reason", "Findings"], reasons) if reasons else []),
        *(_table(["Repository", "Reason", "Findings"], per_repo) if per_repo else []),
        *(
            _table(
                [
                    "Repository",
                    "Perceived false-positive share",
                    "Count",
                    "Denominator",
                    "Excluded runs",
                ],
                shares,
            )
            if shares
            else []
        ),
    ]


def _abort_reasons_section(metrics: Mapping[str, Any]) -> list[str]:
    aborts = metrics["abort_reasons"]
    if not aborts["runs"]:
        return []
    return [
        "### Abort reasons",
        "",
        *_table(["Reason", "Runs"], aborts["by_reason"].items()),
        *_table(["State when aborted", "Runs"], aborts["by_state"].items()),
    ]


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


def _yes_no(value: Any) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def _hook_section(data: Mapping[str, Any]) -> list[str]:
    hooks = data.get("hook_decisions")
    if hooks is None:
        return []
    source = hooks["source"]
    overall = hooks["overall"]
    share = overall["blocked_share"] if overall else None
    rows: list[list[Any]] = [
        ["Hook log path", f"`{source['path']}`"],
        ["Hook log enabled", _yes_no(source["enabled"])],
        ["Hook log file exists", _yes_no(source["exists"])],
        ["Hook log readable", _yes_no(source["error"] is None)],
        ["Hook log lines read", source["lines"]],
        ["Malformed hook log lines", source["malformed"]],
        ["Decisions left out by scope and filters", hooks["filtered_out"]["total"]],
        ["Decisions in this report", overall["decisions"] if overall else None],
        ["Allowed", overall["allowed"] if overall else None],
        ["Blocked", overall["blocked"] if overall else None],
        [
            "Blocked share",
            f"{_percent(share['value'])} ({_fraction(share)} {share['denominator_label']})"
            if share
            else None,
        ],
    ]
    reasons = [
        [outcome, reason, count]
        for outcome, key in (("allowed", "allowed_by_reason"), ("blocked", "blocked_by_reason"))
        for reason, count in (overall[key] if overall else {}).items()
    ]
    repositories = [
        [
            f"{group['label']} ({group['remote']})" if group["remote"] else group["label"],
            group["metrics"]["decisions"],
            group["metrics"]["allowed"],
            group["metrics"]["blocked"],
            _percent(group["metrics"]["blocked_share"]["value"]),
        ]
        for group in hooks["by_repo"]
    ]
    return [
        "### Hook decisions",
        "",
        hooks["note"],
        "",
        *_table(["Measure", "Value"], rows),
        *(_table(["Decision", "Reason", "Decisions"], reasons) if reasons else []),
        *(
            _table(["Repository", "Decisions", "Allowed", "Blocked", "Blocked share"], repositories)
            if repositories
            else []
        ),
    ]


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
        *_caught_section(overall),
        *_hosted_sync_section(overall, repositories),
        *_findings_section(overall),
        *_finding_responses_section(overall, repositories),
        *_stages_section(overall),
        *_duration_section(overall, repositories),
        *_tool_time_section(overall),
        *_events_section(overall),
        *_abort_reasons_section(overall),
        *_group_section("By repository", "Repository", repositories),
        *_group_section("By client", "Client", data["by_client"]),
        *_group_section("By reported model", "Reported model", data["by_model"]),
        *_hook_section(data),
        "### Limits",
        "",
        *(f"- {limit}" for limit in data["limits"]),
    ]
    return "\n".join(lines) + "\n"
