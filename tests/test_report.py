"""The usage report over local run summaries: ``agentic-preflight report``.

The aggregation is tested on synthetic summary rows. The command is tested on
real run stores, with the mirror in a per-test data directory.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentic_preflight import report, telemetry
from agentic_preflight.envelope import ExitCode
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent
from tests.test_telemetry import _findings, _schema_1_record, _schema_2_record, _store_record

ROOT_A = "a" * 40
ROOT_B = "b" * 40

FINDINGS_NULL = dict.fromkeys(
    ("total", "by_stage", "by_severity", "by_status", "by_action", "by_response_reason", "blocking")
)


def _findings_counts(
    *, review: int = 0, docs: int = 0, blocking: int = 0, fixed: int = 0
) -> dict[str, Any]:
    total = review + docs
    by_stage = {name: count for name, count in (("docs", docs), ("review", review)) if count}
    statuses = {name: count for name, count in (("fixed", fixed), ("open", total - fixed)) if count}
    return {
        "total": total,
        "by_stage": by_stage,
        "by_severity": {"high": blocking, "low": total - blocking} if total else {},
        "by_status": statuses,
        "by_action": {"auto_fix": total} if total else {},
        "blocking": blocking,
    }


def _stage(status: str = "green", attempts: int | None = 0, reused: bool | None = False) -> dict:
    return {
        "status": status,
        "attempts": attempts,
        "finished_at": None,
        "reused": reused,
        "reason": None,
    }


def make_row(run_id: str, **overrides: Any) -> dict[str, Any]:
    """A valid schema-1 summary row. Every override replaces one top-level field."""
    repo = overrides.pop("repo", None) or {
        "root_commit": ROOT_A,
        "remote": "github.com/owner/widget",
        "name": "widget",
        "source_worktree_path": "/repos/widget",
    }
    row: dict[str, Any] = {
        "written_at": "2026-10-01T00:00:00+00:00",
        "run_id": run_id,
        "state": "DONE",
        "record_schema_version": 2,
        "repo": repo,
        "branch": "feature/x",
        "base_ref": "main",
        "head_sha": "1" * 40,
        "pushed_sha": None,
        "intent_sha256": None,
        "intent_source": "user",
        "created_at": "2026-10-01T00:00:00+00:00",
        "finished_at": "2026-10-01T00:10:00+00:00",
        "duration_seconds": 600.0,
        "producer": None,
        "risk": None,
        "review": {
            "total_units": 10,
            "cited_count": 0,
            "clean_count": 10,
            "excluded_count": 0,
            "executor": "in_harness",
        },
        "stages": {"review": _stage(), "lint": _stage()},
        "findings": _findings_counts(),
        "fix_commits": 0,
        "validation_restarts": 0,
        "event_counts": {"state_transition": 4},
        "terminal": {"aborted_forced": None, "orphaned_reason": None},
        "hosted": {"last_ci_outcome": None, "pr_url_recorded": False},
        "worktree_mode": "in_place",
        "pr_mode": "auto",
        "config_digest": "f" * 64,
    }
    row.update(overrides)
    return telemetry.RunSummary.model_validate(row).model_dump(mode="json")


def _producer(client: str | None, model: str | None = None) -> dict[str, Any]:
    return {
        "tool_version": "9.9.9",
        "client": client,
        "client_source": "flag" if client else None,
        "reported_model": model,
        "reported_model_source": "flag" if model else None,
    }


OTHER_REPO = {
    "root_commit": ROOT_B,
    "remote": "gitlab.example/team/gadget",
    "name": "gadget",
    "source_worktree_path": "/repos/gadget",
}


def _mixed_rows() -> list[dict[str, Any]]:
    """Two repositories, three clients, one run with no producer, many null counts."""
    return [
        make_row(
            "r_0000000001",
            producer=_producer("claude-code", "claude-opus-5-5"),
            findings=_findings_counts(review=2, blocking=2, fixed=1),
            fix_commits=1,
            duration_seconds=100.0,
            event_counts={"review_coverage_invalidated": 2, "stage_reused": 1},
            stages={"review": _stage(reused=True), "lint": _stage("green", attempts=2)},
        ),
        make_row(
            "r_0000000002",
            state="ABORTED",
            producer=_producer("codex"),
            findings=_findings_counts(review=1, docs=2, blocking=1, fixed=2),
            validation_restarts=2,
            duration_seconds=200.0,
            stages={"review": _stage(), "lint": _stage("red", attempts=1)},
        ),
        make_row(
            "r_0000000003",
            state="ORPHANED",
            repo=OTHER_REPO,
            terminal={"aborted_forced": None, "orphaned_reason": "replaced by a new start"},
            findings=FINDINGS_NULL,
            fix_commits=None,
            validation_restarts=None,
            event_counts=None,
            duration_seconds=None,
            review=None,
            hosted={"last_ci_outcome": "merged", "pr_url_recorded": True},
            stages={"test": _stage("skipped", attempts=None, reused=None)},
        ),
        make_row(
            "r_0000000004",
            repo=OTHER_REPO,
            producer=_producer("claude-code"),
            duration_seconds=400.0,
            hosted={"last_ci_outcome": None, "pr_url_recorded": None},
        ),
    ]


# -- aggregation ----------------------------------------------------------------


def test_states_and_completion_name_their_denominator():
    metrics = report.aggregate(_mixed_rows())

    assert metrics["runs"] == 4
    assert metrics["states"] == {"DONE": 2, "ABORTED": 1, "ORPHANED": 1}
    assert metrics["completion_rate"] == {
        "value": 0.5,
        "numerator": 2,
        "denominator": 4,
        "denominator_label": "runs",
    }


def test_null_counts_are_left_out_and_counted_as_excluded():
    metrics = report.aggregate(_mixed_rows())

    findings = metrics["findings"]
    assert (findings["runs_observed"], findings["excluded_runs"]) == (3, 1)
    assert findings["total"] == 5
    assert findings["by_stage"] == {"docs": 2, "review": 3}
    assert findings["by_status"] == {"fixed": 3, "open": 2}
    assert metrics["fix_commits"]["excluded_runs"] == 1
    assert metrics["fix_commits"]["runs_with_any"] == report.ratio(
        1, 3, "runs with a fix-commit count"
    )
    assert metrics["validation_restarts"]["runs_with_any"]["numerator"] == 1
    assert metrics["validation_restarts"]["excluded_runs"] == 1
    assert metrics["events"]["excluded_runs"] == 1
    assert metrics["events"]["runs_observed"] == 3
    assert metrics["duration_seconds"]["excluded_runs"] == 1


def test_blocking_fixed_share_is_a_range_unless_the_run_pins_it():
    blocking = report.aggregate(_mixed_rows())["findings"]["blocking"]

    # Run 1: every finding blocks, so its one fix is a blocking fix. Run 2: two
    # fixes among three findings with one blocking, so zero or one blocking fix.
    assert blocking["total"] == 3
    assert blocking["fixed"] == {"min": 1, "max": 2, "exact": False}
    assert blocking["fixed_share"]["denominator"] == 3
    assert blocking["fixed_share"]["denominator_label"] == "blocking findings"
    assert blocking["fixed_share"]["min"] == round(1 / 3, 4)
    assert blocking["fixed_share"]["max"] == round(2 / 3, 4)
    assert blocking["excluded_runs"] == 1

    exact = report.aggregate(_mixed_rows()[:1])["findings"]["blocking"]
    assert exact["fixed"] == {"min": 1, "max": 1, "exact": True}


def test_review_findings_per_100_units_uses_only_runs_with_both_counts():
    per_units = report.aggregate(_mixed_rows())["findings"]["review_findings_per_100_units"]

    assert per_units["numerator"] == 3
    assert per_units["denominator"] == 30
    assert per_units["value"] == 10.0
    assert per_units["denominator_label"] == "review units"
    assert per_units["excluded_runs"] == 1


def test_stage_attempts_and_reuse_are_counted_per_stage():
    stages = report.aggregate(_mixed_rows())["stages"]

    assert list(stages) == ["review", "lint", "test"]
    assert stages["lint"]["attempts_above_one"]["numerator"] == 1
    assert stages["lint"]["attempts_at_least_one"]["numerator"] == 2
    assert stages["lint"]["attempts_above_one"]["denominator"] == 3
    assert stages["lint"]["statuses"] == {"green": 2, "red": 1}
    assert stages["review"]["reused"] == report.ratio(
        1, 3, "runs with a reuse value for this stage"
    )
    assert stages["test"]["attempts_excluded_runs"] == 1
    assert stages["test"]["reused_excluded_runs"] == 1
    assert stages["test"]["reused"]["value"] is None


def test_fewer_than_five_durations_leave_the_median_null_with_a_note():
    duration = report.aggregate(_mixed_rows())["duration_seconds"]

    assert duration["observations"] == 3
    assert duration["median"] is None
    assert duration["p90"] is None
    assert duration["note"].startswith("Fewer than 5 observations")


def test_five_durations_give_a_median_and_an_interpolated_p90():
    rows = [
        make_row(f"r_000000000{index}", duration_seconds=float(seconds))
        for index, seconds in enumerate((50, 10, 40, 20, 30), start=1)
    ]

    duration = report.aggregate(rows)["duration_seconds"]

    assert duration["median"] == 30.0
    assert duration["p90"] == 46.0
    assert duration["note"] is None


def test_hosted_outcomes_are_recorded_counts_with_the_caveat():
    hosted = report.aggregate(_mixed_rows())["hosted"]

    assert hosted["outcomes"] == {"merged": 1}
    assert hosted["outcome_recorded"] == report.ratio(1, 4, "runs")
    assert hosted["pr_url_recorded"]["numerator"] == 1
    assert hosted["pr_url_recorded"]["denominator"] == 3
    assert hosted["pr_url_excluded_runs"] == 1
    assert "pull-request" in hosted["note"]


def test_orphaned_reasons_and_selected_events_are_counted():
    metrics = report.aggregate(_mixed_rows())

    assert metrics["orphaned_reasons"] == {"replaced by a new start": 1}
    counts = metrics["events"]["counts"]
    assert list(counts) == list(report.SELECTED_EVENTS)
    assert counts["review_coverage_invalidated"]["total"] == 2
    assert counts["review_coverage_invalidated"]["runs"] == report.ratio(
        1, 3, "runs with event counts"
    )
    assert counts["sync_conflict"]["total"] == 0


def test_groups_by_repository_client_and_model_with_null_as_unknown():
    rows = _mixed_rows()

    repos = report.by_repo(rows)
    clients = report.by_producer(rows, "client")
    models = report.by_producer(rows, "reported_model")

    assert [(group["name"], group["metrics"]["runs"]) for group in repos] == [
        ("gadget", 2),
        ("widget", 2),
    ]
    assert repos[1]["key"] == ROOT_A
    assert repos[0]["remote"] == "gitlab.example/team/gadget"
    assert {group["client"]: group["metrics"]["runs"] for group in clients} == {
        "claude-code": 2,
        "codex": 1,
        "unknown": 1,
    }
    assert clients[0]["client"] == "claude-code"
    assert {group["reported_model"]: group["metrics"]["runs"] for group in models} == {
        "claude-opus-5-5": 1,
        "unknown": 3,
    }


def test_an_empty_report_has_null_ratios_not_zero():
    metrics = report.aggregate([])

    assert metrics["runs"] == 0
    assert metrics["completion_rate"]["value"] is None
    assert metrics["findings"]["blocking"]["fixed_share"]["min"] is None
    assert metrics["findings"]["review_findings_per_100_units"]["value"] is None
    assert metrics["stages"] == {}
    assert metrics["finding_responses"]["responses"] is None
    assert metrics["finding_responses"]["perceived_false_positive_share"]["value"] is None
    assert metrics["time_in_stages"]["total_seconds"] is None
    assert metrics["time_in_stages"]["review_share"]["value"] is None
    assert metrics["abort_reasons"] == {"runs": 0, "by_reason": {}, "by_state": {}}


# -- reasons and timing -----------------------------------------------------------


def _responded(statuses: dict[str, int], reasons: dict[str, int]) -> dict[str, Any]:
    total = sum(statuses.values())
    return {
        "total": total,
        "by_stage": {"review": total},
        "by_severity": {"low": total},
        "by_status": statuses,
        "by_action": {"no_op": total},
        "by_response_reason": reasons,
        "blocking": 0,
    }


def _timed_stage(status: str, duration: float | None, reused: bool | None = False) -> dict:
    return {
        **_stage(status, reused=reused),
        "started_at": None,
        "duration_seconds": duration,
    }


def _timing(review: float, shell: float, other: float, complete: bool = True) -> dict:
    return {
        "complete": complete,
        "states": {
            "SYNC_RUNNING": {"visits": 1, "seconds": other},
            "REVIEW_AWAITING_FINDINGS": {"visits": 1, "seconds": review},
            "TEST_RUNNING": {"visits": 1, "seconds": shell},
        },
        "review_wall_seconds": review if complete else None,
        "shell_wall_seconds": shell if complete else None,
    }


def _reason_rows() -> list[dict[str, Any]]:
    return [
        make_row(
            "r_0000000001",
            findings=_responded(
                {"dismissed": 2, "accepted": 1, "fixed": 1, "open": 1},
                {"false_positive": 1, "accepted_risk": 1, "unspecified": 1},
            ),
            timing=_timing(60.0, 120.0, 20.0),
        ),
        make_row(
            "r_0000000002",
            repo=OTHER_REPO,
            findings=_responded({"dismissed": 2, "fixed": 2}, {"false_positive": 2}),
            timing=_timing(30.0, 0.0, 10.0),
        ),
        make_row(
            "r_0000000003",
            state="ABORTED",
            terminal={
                "aborted_forced": False,
                "abort_reason": "superseded",
                "aborted_from_state": "LINT_RED",
                "orphaned_reason": None,
            },
            timing=_timing(5.0, 5.0, 5.0, complete=False),
        ),
        # A summary from before reasons and timing existed.
        make_row(
            "r_0000000004",
            state="ABORTED",
            terminal={"aborted_forced": True, "orphaned_reason": None},
        ),
    ]


def test_finding_responses_give_reasons_and_a_perceived_false_positive_share():
    responses = report.aggregate(_reason_rows())["finding_responses"]

    # Rows three and four have summaries from before reason counts.
    assert (responses["runs_observed"], responses["excluded_runs"]) == (2, 2)
    assert responses["by_reason"] == {"accepted_risk": 1, "false_positive": 3, "unspecified": 1}
    assert responses["responses"] == 8
    share = responses["perceived_false_positive_share"]
    assert share["denominator_label"] == "findings with a recorded response"
    assert (share["numerator"], share["denominator"], share["value"]) == (3, 8, 0.375)
    # The unspecified dismissal could be a false positive too.
    assert (share["numerator_max"], share["max"], share["exact"]) == (4, 0.5, False)
    assert responses["unspecified"] == report.ratio(1, 5, "dismissed or accepted findings")


def test_finding_responses_are_counted_for_each_repository():
    widget, gadget = sorted(report.by_repo(_reason_rows()), key=lambda group: group["name"])[::-1]

    gadget_share = gadget["metrics"]["finding_responses"]["perceived_false_positive_share"]
    assert gadget["name"] == "gadget"
    assert (gadget_share["value"], gadget_share["exact"]) == (0.5, True)
    assert widget["metrics"]["finding_responses"]["by_reason"] == {
        "accepted_risk": 1,
        "false_positive": 1,
        "unspecified": 1,
    }


def test_summaries_without_reason_counts_leave_the_share_unknown():
    responses = report.aggregate(_mixed_rows())["finding_responses"]

    assert responses["runs_observed"] == 0
    assert responses["responses"] is None
    assert responses["perceived_false_positive_share"]["value"] is None
    assert responses["unspecified"]["value"] is None


def test_aborts_are_counted_by_reason_and_by_the_state_they_left():
    aborts = report.aggregate(_reason_rows())["abort_reasons"]

    assert aborts == {
        "runs": 2,
        "by_reason": {"superseded": 1, "unspecified": 1},
        "by_state": {"LINT_RED": 1, "unknown": 1},
    }


def test_stage_durations_have_a_median_and_p90_only_with_enough_observations():
    durations = (10.0, 50.0, 20.0, 40.0, 30.0)
    rows = [
        make_row(
            f"r_000000000{index}",
            stages={
                "review": _timed_stage("green", seconds),
                "lint": _timed_stage("green", None if index > 2 else 4.0),
            },
        )
        for index, seconds in enumerate(durations, start=1)
    ]
    rows.append(make_row("r_0000000006", stages={"review": _timed_stage("green", None, True)}))

    stages = report.aggregate(rows)["stages"]

    review = stages["review"]["duration_seconds"]
    assert (review["observations"], review["excluded_runs"]) == (5, 1)
    assert (review["median"], review["p90"]) == (30.0, 46.0)
    lint = stages["lint"]["duration_seconds"]
    assert (lint["observations"], lint["median"], lint["p90"]) == (2, None, None)
    assert lint["note"].startswith("Fewer than 5 observations")
    # Older summaries have no stage durations at all.
    old = report.aggregate(_mixed_rows())["stages"]["lint"]["duration_seconds"]
    assert (old["observations"], old["excluded_runs"]) == (0, 3)


def test_time_in_stages_uses_only_runs_with_a_complete_transition_history():
    split = report.aggregate(_reason_rows())["time_in_stages"]

    assert (split["runs_observed"], split["excluded_runs"]) == (2, 2)
    assert split["total_seconds"] == 240.0
    label = "seconds in runs with a complete transition history"
    assert split["review_share"] == {
        "value": 0.375,
        "numerator": 90.0,
        "denominator": 240.0,
        "denominator_label": label,
    }
    assert split["shell_share"]["value"] == 0.5


def test_markdown_renders_reasons_aborts_and_stage_times():
    text = _markdown(_reason_rows())

    responses = text.split("### Finding responses\n\n", 1)[1].split("### Stages", 1)[0]
    assert (
        "| Perceived false-positive share | 37.5% to 50.0% | 3 to 4 "
        "| findings with a recorded response: 8 | 2 |"
    ) in responses
    assert "| Dismissed or accepted without a reason | 20.0% | 1 |" in responses
    assert "| false_positive | 3 |" in responses
    assert "| gadget (gitlab.example/team/gadget) | false_positive | 2 |" in responses
    assert "| gadget (gitlab.example/team/gadget) | 50.0% | 2 |" in responses
    aborts = text.split("### Abort reasons\n\n", 1)[1].split("### By repository", 1)[0]
    assert "| superseded | 1 |" in aborts
    assert "| LINT_RED | 1 |" in aborts
    duration = text.split("### Duration\n\n", 1)[1].split("### Selected events", 1)[0]
    assert "| Review and docs states | 37.5% | 1m 30s |" in duration
    assert "| Lint and test states | 50.0% | 2m 00s |" in duration
    assert "| widget (github.com/owner/widget) | 1 | 30.0% | 60.0% |" in duration
    stages = text.split("### Stages\n\n", 1)[1].split("### Duration", 1)[0]
    assert "| Stage | Duration observations | Excluded runs | Median | p90 |" in stages


def test_markdown_leaves_out_abort_reasons_without_aborted_runs():
    text = _markdown([make_row("r_0000000001")])

    assert "### Abort reasons" not in text
    assert "### Finding responses" in text


# -- filters --------------------------------------------------------------------


def _times(*finished: str | None) -> list[dict[str, Any]]:
    return [
        make_row(f"r_00000000{index:02d}", finished_at=value or "unknown")
        for index, value in enumerate(finished)
    ]


def test_since_is_inclusive_and_until_is_exclusive():
    rows = _times(
        "2026-09-30T23:59:59+00:00",
        "2026-10-01T00:00:00+00:00",
        "2026-10-15T12:00:00+00:00",
        "2026-11-01T00:00:00+00:00",
        None,
    )
    filters = report.Filters(
        since=report.parse_timestamp("2026-10-01"), until=report.parse_timestamp("2026-11-01")
    )

    kept, left_out = report.apply_filters(rows, filters)

    assert [row["run_id"] for row in kept] == ["r_0000000001", "r_0000000002"]
    assert left_out == {
        "state": 0,
        "repo": 0,
        "since": 1,
        "until": 1,
        "time_unknown": 1,
        "total": 3,
    }


def test_a_time_without_a_zone_is_utc():
    assert report.parse_timestamp("2026-10-01T05:00") == datetime(2026, 10, 1, 5, tzinfo=UTC)
    assert report.parse_timestamp("2026-10-01T05:00Z") == datetime(2026, 10, 1, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="Invalid isoformat"):
        report.parse_timestamp("last week")


@pytest.mark.parametrize(
    "wanted",
    [
        "gadget",
        "GADGET",
        "gitlab.example/team/gadget",
        "https://gitlab.example/team/gadget.git",
        "git@gitlab.example:team/gadget.git",
    ],
)
def test_repo_matches_a_name_or_remote_in_any_form(wanted):
    kept, left_out = report.apply_filters(_mixed_rows(), report.Filters(repo=wanted))

    assert [row["run_id"] for row in kept] == ["r_0000000003", "r_0000000004"]
    assert left_out["repo"] == 2


def test_state_keeps_only_the_named_terminal_states():
    kept, left_out = report.apply_filters(
        _mixed_rows(), report.Filters(states=("ABORTED", "ORPHANED"))
    )

    assert [row["state"] for row in kept] == ["ABORTED", "ORPHANED"]
    assert left_out["state"] == 2
    assert left_out["total"] == 2


# -- the mirror -----------------------------------------------------------------


def _line(row: dict[str, Any]) -> bytes:
    return json.dumps(row, sort_keys=True).encode("utf-8")


def test_malformed_mirror_lines_are_counted_by_reason_not_dropped_silently(tmp_path):
    good = make_row("r_0000000001")
    future = {**make_row("r_0000000002"), "schema_version": 2}
    extra = {**make_row("r_0000000003"), "unexpected": True}
    lines = [
        _line(good),
        b"",
        b'{"truncated": ',
        b'{"run_id": "r_0000000009", "repo": {"root_commit": "x"}}',
        _line(future),
        _line(extra),
        b"\xff\xfe not text",
        b"   ",
    ]

    mirror = report.parse_mirror(b"\n".join(lines) + b"\n", tmp_path / "runs.jsonl")

    assert [row["run_id"] for row in mirror.rows] == ["r_0000000001"]
    described = mirror.describe()
    assert described["lines"] == 6
    assert described["malformed"] == 5
    assert described["malformed_by_reason"] == {
        "invalid_json": 1,
        "invalid_summary": 1,
        "not_a_summary": 1,
        "undecodable": 1,
        "unsupported_schema": 1,
    }
    assert described["malformed_lines"][0] == {"line": 3, "reason": "invalid_json"}
    # Line numbers name the file's own lines, blank lines included.
    assert [item["line"] for item in described["malformed_lines"]] == [3, 4, 5, 6, 7]


def test_duplicate_rows_keep_the_newest_written_at_like_rebuild(tmp_path):
    old = make_row("r_0000000001", written_at="2026-10-01T00:00:00+00:00", fix_commits=1)
    new = make_row("r_0000000001", written_at="2026-10-02T00:00:00+00:00", fix_commits=3)
    other_repo = make_row("r_0000000001", repo=OTHER_REPO)
    payload = b"\n".join(_line(row) for row in (new, old, other_repo))

    mirror = report.parse_mirror(payload, tmp_path / "runs.jsonl")

    assert mirror.duplicates == 1
    assert sorted((row["repo"]["name"], row["fix_commits"]) for row in mirror.rows) == [
        ("gadget", 0),
        ("widget", 3),
    ]


def test_a_missing_mirror_is_absent_and_an_unreadable_one_raises(tmp_path):
    missing = report.read_mirror(tmp_path / "runs.jsonl")
    assert missing.exists is False
    assert missing.rows == []

    with pytest.raises((IsADirectoryError, PermissionError)):
        report.read_mirror(tmp_path)


# -- sources ------------------------------------------------------------------


def _state_root(repo: Path) -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)
    return Path(common) / "agentic-preflight"


def _clone(repo: Path) -> report.Clone:
    return report.Clone(
        store=Store(_state_root(repo)),
        identity=telemetry.repo_identity(repo),
        worktree_paths=frozenset({str(repo)}),
    )


def _write_mirror(rows: list[dict[str, Any]], *extra: bytes) -> Path:
    path = telemetry.mirror_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + b"\n" for line in [*(_line(row) for row in rows), *extra]))
    return path


def test_the_clone_reads_its_own_mirror_rows_and_counts_unmirrored_runs(tmp_repo):
    root = git("rev-list", "--max-parents=0", "HEAD", cwd=tmp_repo)
    _store_record(tmp_repo, _schema_1_record())
    _store_record(tmp_repo, _schema_2_record())
    ours = make_row("r_0000000001", repo={**OTHER_REPO, "root_commit": root, "name": "repo"})
    path = _write_mirror([ours, make_row("r_0000000005")])

    rows, source = report.collect(report.read_mirror(path), _clone(tmp_repo), all_repos=False)

    assert [row["run_id"] for row in rows] == ["r_0000000001"]
    assert source["used"] == "mirror"
    assert source["scope"] == "clone"
    clone = source["clone"]
    assert clone["root_commit"] == root
    assert clone["mirror_rows"] == 1
    assert clone["run_records"] == 2
    # r_0000000002 finished but is not in the mirror yet.
    assert clone["unmirrored_terminal_runs"] == 1
    assert clone["unreadable"] == 0


def test_a_row_without_a_root_commit_matches_by_its_worktree_path(tmp_repo):
    path_only = {"root_commit": None, "remote": None, "name": "repo"}
    here = make_row("r_0000000001", repo={**path_only, "source_worktree_path": str(tmp_repo)})
    elsewhere = make_row("r_0000000002", repo={**path_only, "source_worktree_path": "/else"})
    path = _write_mirror([here, elsewhere])

    rows, source = report.collect(report.read_mirror(path), _clone(tmp_repo), all_repos=False)

    assert [row["run_id"] for row in rows] == ["r_0000000001"]
    assert source["used"] == "mirror"


def test_with_no_mirror_rows_for_the_clone_its_records_are_summarized_in_memory(tmp_repo):
    _store_record(tmp_repo, _schema_1_record())
    _store_record(tmp_repo, _schema_2_record(), findings=_findings())
    active = {**_schema_2_record(), "run_id": "r_0000000003", "state": "REVIEW_GREEN"}
    _store_record(tmp_repo, active)
    broken = _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000004"})
    (broken / "run.json").write_text('{"truncated": ', encoding="utf-8")
    path = _write_mirror([make_row("r_0000000009", repo=OTHER_REPO)])
    before = path.read_bytes()

    rows, source = report.collect(report.read_mirror(path), _clone(tmp_repo), all_repos=False)

    assert source["used"] == "clone_records"
    assert [row["run_id"] for row in rows] == ["r_0000000001", "r_0000000002"]
    assert rows[1]["findings"]["total"] == 4
    clone = source["clone"]
    assert (clone["summarized"], clone["skipped"], clone["unreadable"]) == (2, 1, 1)
    assert clone["unreadable_runs"][0]["run_id"] == "r_0000000004"
    assert clone["unmirrored_terminal_runs"] is None
    # Nothing is written: no summary.json and no mirror change.
    assert not list(_state_root(tmp_repo).glob("runs/*/summary.json"))
    assert path.read_bytes() == before


def test_all_repos_reads_every_mirror_row_and_still_checks_this_clone(tmp_repo):
    _store_record(tmp_repo, _schema_2_record())
    path = _write_mirror([make_row("r_0000000001"), make_row("r_0000000002", repo=OTHER_REPO)])

    rows, source = report.collect(report.read_mirror(path), _clone(tmp_repo), all_repos=True)
    elsewhere, outside = report.collect(report.read_mirror(path), None, all_repos=True)

    assert len(rows) == 2
    assert source["used"] == "mirror"
    assert source["scope"] == "all"
    assert source["clone"]["mirror_rows"] == 0
    assert source["clone"]["unmirrored_terminal_runs"] == 1
    assert len(elsewhere) == 2
    assert outside["clone"] is None


# -- Markdown ---------------------------------------------------------------------


def _markdown(rows: list[dict[str, Any]]) -> str:
    data = report.build_report(
        rows,
        source={
            "used": "mirror",
            "scope": "all",
            "mirror": report.MirrorRead(path="/data/runs.jsonl", exists=True).describe(),
            "clone": None,
        },
        filters=report.Filters(states=("DONE",)),
        filtered_out=report.apply_filters([], report.Filters())[1],
        output_format="markdown",
    )
    return data["markdown"]


def test_markdown_has_the_sections_in_order_and_ends_with_the_limits():
    text = _markdown(_mixed_rows())

    headings = re.findall(r"^#{2,3} (.+)$", text, re.MULTILINE)
    assert headings == [
        "Agentic Preflight report",
        "Source",
        "Runs",
        "Hosted outcomes recorded",
        "Hosted outcomes synced",
        "Findings",
        "Finding responses",
        "Stages",
        "Duration",
        "Selected events",
        "Orphaned reasons",
        "Abort reasons",
        "By repository",
        "By client",
        "By reported model",
        "Limits",
    ]
    limits = text.split("### Limits\n\n", 1)[1]
    assert limits == "".join(f"- {limit}\n" for limit in report.LIMITS)
    assert "| Filters | state DONE |" in text
    assert "| claude-code | 2 |" in text
    assert "| widget (github.com/owner/widget) | 2 |" in text
    assert "not computed: too few observations" in text


def test_markdown_puts_every_number_in_a_table():
    text = _markdown(_mixed_rows())

    prose = [line for line in text.splitlines() if line and not line.startswith(("|", "#", "- "))]
    assert prose, "the notes are prose"
    assert not [line for line in prose if re.search(r"\d", line)]
    for limit in report.LIMITS:
        assert not re.search(r"\d", limit)


def test_markdown_tables_are_well_formed_and_escape_pipes():
    rows = [
        make_row(
            "r_0000000001",
            state="ORPHANED",
            terminal={"aborted_forced": None, "orphaned_reason": "a | b"},
        )
    ]

    text = _markdown(rows)

    assert "| a \\| b | 1 |" in text
    for block in re.split(r"\n\n+", text):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("|"):
            continue
        widths = {len(re.findall(r"(?<!\\)\|", line)) for line in lines}
        assert len(widths) == 1, block


def test_json_reports_omit_markdown_and_rows_unless_asked():
    source = {
        "used": "mirror",
        "scope": "all",
        "mirror": report.MirrorRead(path="/p", exists=False).describe(),
        "clone": None,
    }
    common = {"source": source, "filters": report.Filters(), "filtered_out": {"total": 0}}

    plain = report.build_report(_mixed_rows(), **common)
    with_rows = report.build_report(_mixed_rows(), include_rows=True, **common)

    assert plain["markdown"] is None
    assert plain["rows"] is None
    assert plain["limits"] == list(report.LIMITS)
    assert [row["run_id"] for row in with_rows["rows"]] == [row["run_id"] for row in _mixed_rows()]


# -- the command ----------------------------------------------------------------


def test_report_in_a_clone_without_mirror_rows_falls_back_and_points_at_rebuild(tmp_repo):
    _store_record(tmp_repo, _schema_1_record())
    _store_record(tmp_repo, _schema_2_record(), findings=_findings())

    result = ScriptedAgent(tmp_repo).run("report")

    data = result["data"]
    assert result["ok"] is True
    assert data["format"] == "json"
    assert data["source"]["used"] == "clone_records"
    assert data["source"]["mirror"] == {
        **data["source"]["mirror"],
        "path": str(telemetry.mirror_path()),
        "exists": False,
        "enabled": True,
    }
    assert data["rows_reported"] == 2
    assert data["overall"]["states"] == {"DONE": 1, "ABORTED": 1, "ORPHANED": 0}
    assert data["limits"] == list(report.LIMITS)
    assert data["markdown"] is None
    assert data["rows"] is None
    assert result["next"]["command"] == "agentic-preflight telemetry rebuild"
    assert not telemetry.mirror_path().exists()


def test_report_after_rebuild_reads_the_mirror(tmp_repo):
    _store_record(tmp_repo, _schema_2_record())
    agent = ScriptedAgent(tmp_repo)
    agent.run("telemetry", "rebuild")

    result = agent.run("report", "--rows")

    assert result["data"]["source"]["used"] == "mirror"
    assert result["data"]["source"]["clone"]["unmirrored_terminal_runs"] == 0
    assert [row["run_id"] for row in result["data"]["rows"]] == ["r_0000000002"]
    assert result["next"] is None


def test_report_filters_and_renders_markdown_for_every_repository(tmp_repo):
    _write_mirror(
        _mixed_rows(), b'{"truncated": ', _line(make_row("r_0000000099", state="ABORTED"))
    )

    result = ScriptedAgent(tmp_repo).run(
        "report",
        "--all-repos",
        "--state",
        "done",
        "--since",
        "2026-10-01",
        "--until",
        "2026-10-02T00:00:00+00:00",
        "--format",
        "markdown",
    )

    data = result["data"]
    assert data["rows_reported"] == 2
    assert data["filtered_out"]["state"] == 3
    assert data["filters"]["state"] == ["DONE"]
    assert data["filters"]["since"] == "2026-10-01T00:00:00+00:00"
    assert data["source"]["mirror"]["malformed"] == 1
    assert data["markdown"].startswith("## Agentic Preflight report\n")
    assert data["markdown"].endswith(f"- {report.LIMITS[-1]}\n")
    assert "Some mirror lines are not readable summaries" in result["next"]["instruction"]


def test_report_repo_reads_every_mirror_row(tmp_repo):
    _write_mirror(_mixed_rows())

    result = ScriptedAgent(tmp_repo).run("report", "--repo", "gadget")

    assert result["data"]["source"]["scope"] == "all"
    assert result["data"]["rows_reported"] == 2
    assert result["data"]["filtered_out"]["repo"] == 2


def test_report_outside_a_clone_needs_all_repos(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    _write_mirror(_mixed_rows())

    refused = ScriptedAgent(outside).run("report", expect=ExitCode.USAGE)
    result = ScriptedAgent(outside).run("report", "--all-repos")

    assert refused["error"]["code"] == "not_a_repository"
    assert refused["next"]["command"] == "agentic-preflight report --all-repos"
    assert result["data"]["rows_reported"] == 4
    assert result["data"]["source"]["clone"] is None


def test_report_rejects_a_time_that_is_not_iso_8601(tmp_repo):
    refused = ScriptedAgent(tmp_repo).run(
        "report", "--since", "last week", expect=ExitCode.USAGE_ERROR
    )

    assert refused["error"]["code"] == "usage_error"
    assert "ISO 8601" in refused["error"]["message"]


def test_report_fails_visibly_when_the_mirror_cannot_be_read(tmp_repo):
    telemetry.mirror_path().mkdir(parents=True)

    failed = ScriptedAgent(tmp_repo).run("report", expect=ExitCode.USAGE)

    assert failed["error"]["code"] == "telemetry_mirror_unreadable"
    assert failed["data"]["path"] == str(telemetry.mirror_path())


def test_a_clone_with_no_finished_runs_reports_zero_and_suggests_nothing(tmp_repo):
    result = ScriptedAgent(tmp_repo).run("report", "--format", "markdown")

    assert result["data"]["source"]["used"] == "clone_records"
    assert result["data"]["source"]["clone"]["summarized"] == 0
    assert result["data"]["rows_reported"] == 0
    assert result["data"]["overall"]["completion_rate"]["value"] is None
    # A rebuild would add nothing, so following `next` must not loop.
    assert result["next"] is None
    assert result["data"]["markdown"].endswith(f"- {report.LIMITS[-1]}\n")


def test_with_the_mirror_off_the_report_does_not_suggest_a_rebuild(tmp_repo):
    write(tmp_repo, ".agentic-preflight.toml", "[telemetry]\nmirror = false\n")
    commit_all(tmp_repo, "keep summaries local")
    _store_record(tmp_repo, _schema_2_record())

    result = ScriptedAgent(tmp_repo).run("report")

    assert result["data"]["source"]["used"] == "clone_records"
    assert result["data"]["source"]["mirror"]["enabled"] is False
    assert result["next"]["command"] is None
    assert "The mirror is off" in result["next"]["instruction"]


def test_a_record_that_cannot_be_summarized_is_unreadable_not_fatal(tmp_repo):
    broken_findings = _store_record(tmp_repo, _schema_1_record())
    (broken_findings / "findings.json").write_text("[{", encoding="utf-8")
    missing_branch = _schema_2_record()
    del missing_branch["branch"]
    _store_record(tmp_repo, missing_branch)
    _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000003"})

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["rows_reported"] == 1
    assert clone["unreadable"] == 2
    by_run = {item["run_id"]: item for item in clone["unreadable_runs"]}
    assert by_run["r_0000000001"]["reason"] == "invalid"
    assert by_run["r_0000000001"]["diagnostic"] == "Findings file is not valid JSON."
    assert by_run["r_0000000002"]["reason"] == "invalid"
    assert by_run["r_0000000002"]["diagnostic"] == (
        "Run record lacks fields that a summary requires."
    )
    assert "data.source.clone.unreadable_runs" in result["next"]["instruction"]


def test_an_unexpected_summary_error_is_unreadable_and_names_only_its_type(tmp_repo, monkeypatch):
    _store_record(tmp_repo, _schema_2_record())

    def broken(*args, **kwargs):
        raise RuntimeError("SECRET RECORD VALUE")

    monkeypatch.setattr(telemetry, "summarize", broken)
    scan = report.summarize_clone(Store(_state_root(tmp_repo)), telemetry.repo_identity(tmp_repo))

    assert scan.summaries == []
    (item,) = scan.unreadable_runs
    assert item["reason"] == "invalid"
    assert item["diagnostic"] == "Run record could not be summarized (RuntimeError)."
    assert "SECRET" not in json.dumps(item)


def test_tolerated_corrupt_shapes_are_summarized_and_counted_as_rows(tmp_repo):
    # A non-object event line is skipped, and a non-text severity is `unknown`.
    _store_record(tmp_repo, _schema_1_record(), events=[[1, 2], {"event": "pushed"}])
    bad_finding = [{**_findings()[0], "severity": ["high"]}]
    _store_record(tmp_repo, _schema_2_record(), findings=bad_finding)

    result = ScriptedAgent(tmp_repo).run("report", "--rows")

    data = result["data"]
    assert data["source"]["clone"]["unreadable"] == 0
    assert data["rows_reported"] == 2
    by_run = {row["run_id"]: row for row in data["rows"]}
    assert by_run["r_0000000001"]["event_counts"] == {"pushed": 1}
    assert data["overall"]["findings"]["by_severity"] == {"unknown": 1}
    assert data["overall"]["findings"]["blocking"]["total"] == 0


def test_unknown_totals_are_null_and_render_as_not_available():
    rows = [
        make_row(
            f"r_000000000{index}",
            findings=FINDINGS_NULL,
            fix_commits=None,
            validation_restarts=None,
            event_counts=None,
        )
        for index in (1, 2)
    ]

    metrics = report.aggregate(rows)
    text = _markdown(rows)

    assert metrics["findings"]["total"] is None
    assert metrics["findings"]["blocking"]["total"] is None
    assert metrics["findings"]["blocking"]["fixed"] is None
    assert metrics["fix_commits"]["total"] is None
    assert metrics["validation_restarts"]["total"] is None
    assert metrics["events"]["counts"]["stage_reused"]["total"] is None
    assert "| Findings | n/a | runs with a findings file: 0 | 2 |" in text
    assert "| Blocking findings fixed | n/a |" in text
    assert "| widget (github.com/owner/widget) | 2 | 2 | 0 | 0 | 100.0% | n/a |" in text


def test_one_run_mirrored_under_two_identity_keys_counts_once(tmp_path):
    path_only = {
        "root_commit": None,
        "remote": "github.com/owner/widget",
        "name": "widget",
        "source_worktree_path": "/repos/widget",
    }
    early = make_row("r_0000000001", repo=path_only, written_at="2026-10-01T00:00:00+00:00")
    later = make_row("r_0000000001", written_at="2026-10-02T00:00:00+00:00")
    payload = b"\n".join(_line(row) for row in (later, early))

    mirror = report.parse_mirror(payload, tmp_path / "runs.jsonl")

    assert mirror.duplicates == 1
    (row,) = mirror.rows
    assert row["repo"]["root_commit"] == ROOT_A


def test_a_written_at_tie_keeps_the_later_line_like_rebuild(tmp_path):
    first = make_row("r_0000000001", fix_commits=1)
    second = make_row("r_0000000001", fix_commits=2)

    mirror = report.parse_mirror(_line(first) + b"\n" + _line(second), tmp_path / "runs.jsonl")

    assert [row["fix_commits"] for row in mirror.rows] == [2]


def test_markdown_cells_collapse_newlines():
    rows = [
        make_row(
            "r_0000000001",
            state="ORPHANED",
            terminal={"aborted_forced": None, "orphaned_reason": "line one\nline two"},
        )
    ]

    assert "| line one line two | 1 |" in _markdown(rows)


def test_markdown_formats_computed_durations():
    rows = [
        make_row(f"r_000000000{index}", duration_seconds=float(seconds))
        for index, seconds in enumerate((45, 600, 605, 3600, 7500), start=1)
    ]

    text = _markdown(rows)

    assert "| Median | 10m 05s |" in text
    assert "| p90 | 1h 39m |" in text
    assert "| widget (github.com/owner/widget) | 5 | 5 | 0 | 0 | 100.0% | 0 | 10m 05s |" in text
    assert report._duration(45.4) == "45s"


def test_report_points_at_rebuild_for_unmirrored_runs_and_names_unreadable_records(tmp_repo):
    root = git("rev-list", "--max-parents=0", "HEAD", cwd=tmp_repo)
    _store_record(tmp_repo, _schema_1_record())
    _store_record(tmp_repo, _schema_2_record())
    broken = _store_record(tmp_repo, {**_schema_2_record(), "run_id": "r_0000000004"})
    (broken / "run.json").write_text('{"truncated": ', encoding="utf-8")
    _write_mirror([make_row("r_0000000001", repo={**OTHER_REPO, "root_commit": root})])

    result = ScriptedAgent(tmp_repo).run("report")

    clone = result["data"]["source"]["clone"]
    assert result["data"]["source"]["used"] == "mirror"
    assert (clone["unmirrored_terminal_runs"], clone["unreadable"]) == (1, 1)
    assert clone["unreadable_runs"][0]["run_id"] == "r_0000000004"
    assert result["next"]["command"] == "agentic-preflight telemetry rebuild"
    instruction = result["next"]["instruction"]
    assert instruction.startswith("Some finished runs in this clone are not in the mirror.")
    assert "data.source.clone.unreadable_runs" in instruction
