"""Golden outputs for run summaries, mirror rows, and the usage report.

These pin the exact bytes of a ``summary.json``, a mirror row, the report's JSON
``data``, and the Markdown report for fixed inputs, so that a refactor of
``telemetry`` or ``report`` cannot change them silently. A deliberate change
regenerates the files with ``AP_UPDATE_GOLDENS=1 uv run pytest -n 0
tests/test_report_golden.py`` and commits the diff with the change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agentic_preflight import report, run_summary, telemetry
from tests.test_outcome_telemetry import _aborted_record, _caught_row, _finding, _timed_events
from tests.test_report import OTHER_REPO, _mixed_rows, make_row
from tests.test_telemetry import _findings, _identity, _schema_1_record, _schema_2_record

GOLDEN = Path(__file__).parent / "golden"
WRITTEN_AT = "2026-11-01T00:00:00+00:00"
HOOK_REPO = {
    "root_commit": "a" * 40,
    "remote": "github.com/owner/widget",
    "name": "widget",
    "source_worktree_path": "/repos/widget",
}


def _check(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("AP_UPDATE_GOLDENS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual, f"{name} differs from its golden file"


def _summaries() -> list[telemetry.RunSummary]:
    synced = _schema_2_record()
    synced["run_id"] = "r_0000000009"
    synced["pushed_sha"] = synced["head_sha"]
    hosted = {
        "sha": synced["pushed_sha"],
        "fetched_at": "2026-11-01T00:00:00+00:00",
        "pr": {"number": 7, "state": "closed", "merged_at": "2026-11-01T00:00:00+00:00"},
        "checks": {"conclusion": "failure"},
    }
    findings = [
        _finding("F-1", "review", "high", "fixed"),
        _finding("F-2", "docs", "critical", "fixed"),
        _finding("F-3", "review", "low", "dismissed"),
    ]
    return [
        telemetry.summarize(_schema_1_record(), None, None, _identity(), written_at=WRITTEN_AT),
        telemetry.summarize(
            _schema_2_record(), _findings(), [], _identity(), written_at=WRITTEN_AT
        ),
        telemetry.summarize(
            _aborted_record(), findings, _timed_events(), _identity(), written_at=WRITTEN_AT
        ),
        telemetry.summarize(
            synced, _findings(), [], _identity(None), written_at=WRITTEN_AT, hosted=hosted
        ),
    ]


def _hook_log() -> bytes:
    def line(at: str, allowed: bool, reason: str, repo: dict[str, Any]) -> str:
        update = {
            "local_ref": "refs/heads/x",
            "remote_ref": "refs/heads/x",
            "sha": "1234567",
            "force": False,
        }
        item = {
            "at": at,
            "repo": repo,
            "remote": "origin",
            "allowed": allowed,
            "reason": reason,
            "updates": [update],
        }
        return json.dumps(item, sort_keys=True)

    lines = [
        line("2026-10-01T09:00:00+00:00", False, "missing_attestation", HOOK_REPO),
        line("2026-10-01T09:05:00+00:00", True, "attested", HOOK_REPO),
        line("2026-10-02T09:00:00+00:00", True, "attested", OTHER_REPO),
        "not json",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _report(output_format: str) -> dict[str, Any]:
    mirror_lines = [telemetry._row(summary) for summary in _summaries()]
    mirror_lines += [json.dumps(row, sort_keys=True) for row in _mixed_rows()]
    mirror_lines += [
        json.dumps(_caught_row("r_0000000020", lint=True, tool=12.5, pr=3), sort_keys=True),
        json.dumps(_caught_row("r_0000000021", review=None, tool=None, pr=3), sort_keys=True),
        json.dumps(make_row("r_0000000022", state="ORPHANED", repo=OTHER_REPO), sort_keys=True),
        "{not json",
        json.dumps({"kind": "other"}),
    ]
    mirror = report.parse_mirror("\n".join(mirror_lines).encode("utf-8"), "/data/runs.jsonl")
    rows, source = report.collect(mirror, None, all_repos=True)
    filters = report.Filters(since=report.parse_timestamp("2026-01-01"))
    kept, filtered_out = report.apply_filters(rows, filters)
    hooks = report.hook_decisions(
        report.parse_hook_log(_hook_log(), "/data/hook-decisions.jsonl"),
        None,
        all_repos=True,
        filters=filters,
        enabled=True,
    )
    return report.build_report(
        kept,
        source=source,
        filters=filters,
        filtered_out=filtered_out,
        output_format=output_format,
        include_rows=True,
        hook_decisions=hooks,
    )


def test_summary_files_and_mirror_rows_match_their_goldens(tmp_path):
    rows = []
    for index, summary in enumerate(_summaries()):
        run_dir = tmp_path / str(index)
        run_dir.mkdir()
        status = telemetry.write_summary(run_dir, summary, None)
        assert status["summary_path"] is not None
        _check(f"summary-{index}.json", (run_dir / telemetry.SUMMARY_NAME).read_text("utf-8"))
        rows.append(telemetry._row(summary))
    _check("mirror.jsonl", "".join(f"{row}\n" for row in rows))


@pytest.mark.parametrize("output_format", ["json", "markdown"])
def test_report_data_matches_its_golden(output_format):
    data = _report(output_format)

    if output_format == "markdown":
        _check("report.md", data["markdown"])
        # Apart from the rendering, the data is the JSON report's.
        data = {**data, "format": "json", "markdown": None}
    _check("report.json", json.dumps(data, indent=1) + "\n")


def test_typed_rows_name_every_key_a_summary_writes():
    """``SummaryRow`` and its parts list exactly the keys that ``summarize`` writes."""
    assert set(run_summary.SummaryRow.__annotations__) == set(run_summary.RunSummary.model_fields)
    assert set(run_summary.RepoRow.__annotations__) == set(run_summary.RepoIdentity.model_fields)
    for summary in _summaries():
        row = run_summary.summary_row(summary)
        assert set(row["findings"]) == set(run_summary.FindingCounts.__annotations__)
        assert set(row["terminal"]) == set(run_summary.TerminalRow.__annotations__)
        assert set(row["hosted"]) == set(run_summary.HostedRow.__annotations__)
        for stage in row["stages"].values():
            assert set(stage) == set(run_summary.StageRow.__annotations__)
        if row["review"] is not None:
            assert set(row["review"]) == set(run_summary.ReviewRow.__annotations__)
        if row["risk"] is not None:
            assert set(row["risk"]) == set(run_summary.RiskRow.__annotations__)
        if row["timing"] is not None:
            assert set(row["timing"]) == set(run_summary.TimingRow.__annotations__)


@pytest.mark.parametrize("repo", [None, "missing"])
def test_a_row_without_a_repository_is_grouped_by_its_key(repo):
    """The report's public functions tolerate unvalidated rows, as before the split."""
    row = make_row("r_0000000030")
    if repo == "missing":
        del row["repo"]
    else:
        row["repo"] = repo

    groups = report.by_repo([row])

    assert [(group["key"], group["label"]) for group in groups] == [("path:None", "path:None")]
