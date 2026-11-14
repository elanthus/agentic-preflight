"""Outcome fields in run summaries and the report (issue 365, step 1).

What the gate caught before the push, and how much wall time the tool's own
commands took. Both are derived from records the tool already writes, so these
tests drive :func:`telemetry.summarize` and :func:`report.aggregate` with
synthetic records, and one real run checks that the commands are timed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_preflight import report, telemetry, timing
from tests.conftest import git
from tests.driver import ScriptedAgent, green_run
from tests.test_report import OTHER_REPO, _markdown, _stage, make_row
from tests.test_telemetry import _identity, _schema_2_record, _store_record

pytestmark = pytest.mark.usefixtures("telemetry_opt_in")


def _at(clock: str) -> str:
    return f"2026-10-21T10:{clock}+00:00"


def _transition(source: str | None, target: str, clock: str, **extra: Any) -> dict[str, Any]:
    return {"event": "state_transition", "from": source, "to": target, "at": _at(clock), **extra}


def _chain(*states: tuple[str, str]) -> list[dict[str, Any]]:
    """Connected transitions from creation through ``states``, each with its clock."""
    events = [_transition(None, "CREATED", "00:00")]
    previous = "CREATED"
    for target, clock in states:
        events.append(_transition(previous, target, clock))
        previous = target
    return events


def _command(kind: str, seconds: float | None) -> list[dict[str, Any]]:
    finished: dict[str, Any] = {"event": timing.COMMAND_FINISHED_EVENT, "command": kind}
    if seconds is not None:
        finished["seconds"] = seconds
    return [{"event": timing.COMMAND_STARTED_EVENT, "command": kind}, finished]


CREATED = {"event": "run_created", timing.COMMAND_TIMING_MARKER: timing.COMMAND_TIMING_VERSION}

#: A run whose lint failed, was repaired, and passed. 330 seconds from start to end.
LINT_REPAIRED = _chain(
    ("SYNC_GREEN", "00:30"),
    ("REVIEW_AWAITING_FINDINGS", "00:30"),
    ("REVIEW_GREEN", "01:00"),
    ("DOCS_GREEN", "01:00"),
    ("LINT_RUNNING", "01:10"),
    ("LINT_RED", "01:20"),
    ("LINT_RUNNING", "02:00"),
    ("LINT_GREEN", "02:10"),
    ("TEST_RUNNING", "02:10"),
    ("TEST_GREEN", "04:10"),
    ("ABORTED", "05:30"),
)


def _summary(record: dict[str, Any], findings: Any, events: Any) -> dict[str, Any]:
    return telemetry.summarize(record, findings, events, _identity()).model_dump(mode="json")


def _aborted_record() -> dict[str, Any]:
    record = _schema_2_record()
    record["stages"] = {
        "review": {"status": "green", "attempts": 0},
        "docs": {"status": "green", "attempts": 0},
        "lint": {"status": "green", "attempts": 1},
        "test": {"status": "green", "attempts": 0},
    }
    record["evidence"] = {}
    return record


# -- caught before push: findings ----------------------------------------------------


def _finding(fid: str, stage: str, severity: Any, status: str) -> dict[str, Any]:
    return {
        "id": fid,
        "stage": stage,
        "severity": severity,
        "status": status,
        "action": "auto_fix",
        "path": "src/app.py",
        "title": "t",
        "detail": "SECRET FINDING DETAIL",
    }


def test_blocking_findings_fixed_are_counted_per_stage():
    # Review blocks critical; docs blocks high and medium (the schema-2 snapshot).
    findings = [
        _finding("F001", "review", "critical", "fixed"),
        _finding("F002", "review", "critical", "dismissed"),
        _finding("F003", "review", "high", "fixed"),  # fixed, but not blocking for review
        _finding("F004", "docs", "medium", "fixed"),
        _finding("F005", "docs", "high", "fixed"),
        _finding("F006", "docs", "low", "fixed"),
    ]

    row = _summary(_aborted_record(), findings, LINT_REPAIRED)

    assert row["findings"]["review_blocking_fixed"] == 1
    assert row["findings"]["docs_blocking_fixed"] == 2


def test_no_blocking_fix_is_a_measured_zero():
    row = _summary(_aborted_record(), [], LINT_REPAIRED)

    assert row["findings"]["review_blocking_fixed"] == 0
    assert row["findings"]["docs_blocking_fixed"] == 0


@pytest.mark.parametrize(
    ("findings", "snapshot"),
    [
        pytest.param(None, None, id="no-findings-file"),
        pytest.param({"not": "a list"}, None, id="malformed-findings-file"),
        pytest.param([], {"review": {"blocking_severities": "critical"}}, id="unknown-severities"),
    ],
)
def test_blocking_fixes_are_null_when_unknown(findings, snapshot):
    record = _aborted_record()
    if snapshot is not None:
        record["config_snapshot"] = snapshot

    row = _summary(record, findings, LINT_REPAIRED)

    assert row["findings"]["review_blocking_fixed"] is None
    assert row["findings"]["docs_blocking_fixed"] is None


# -- caught before push: shell stages ---------------------------------------------------


def test_a_stage_that_failed_and_later_passed_is_red_then_green():
    row = _summary(_aborted_record(), [], LINT_REPAIRED)

    assert row["stages"]["lint"]["red_then_green"] is True
    assert row["stages"]["test"]["red_then_green"] is False
    # Review and docs are judged stages: their catches are findings, not reds.
    assert row["stages"]["review"]["red_then_green"] is None
    assert row["stages"]["docs"]["red_then_green"] is None


def test_a_red_that_never_turned_green_is_not_a_catch():
    events = [*LINT_REPAIRED[:7], _transition("LINT_RED", "ABORTED", "02:00")]

    row = _summary(_aborted_record(), [], events)

    assert row["stages"]["lint"]["red_then_green"] is False


def test_an_interrupted_command_is_not_a_catch():
    # The recovery commits LINT_RED and appends `lint_interrupted` right after it.
    events = [*LINT_REPAIRED[:7], {"event": "lint_interrupted"}, *LINT_REPAIRED[7:]]

    row = _summary(_aborted_record(), [], events)

    assert row["stages"]["lint"]["red_then_green"] is False


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(None, id="no-event-log"),
        pytest.param([{"event": "lint_finished"}], id="no-transitions"),
        # A gap in the chain could hide a red, so nothing is claimed.
        pytest.param([*LINT_REPAIRED[:4], *LINT_REPAIRED[5:]], id="broken-chain"),
    ],
)
def test_red_then_green_is_null_without_a_complete_transition_chain(events):
    row = _summary(_aborted_record(), [], events)

    assert row["stages"]["lint"]["red_then_green"] is None
    assert row["stages"]["test"]["red_then_green"] is None


# -- cost: tool seconds ------------------------------------------------------------------


def _timed_events() -> list[dict[str, Any]]:
    return [
        LINT_REPAIRED[0],
        CREATED,
        *_command("sync", 2.5),
        *LINT_REPAIRED[1:6],
        *_command("lint", 9.0),
        LINT_REPAIRED[6],
        *_command("lint", 8.25),
        *LINT_REPAIRED[7:10],
        *_command("test", 110.0),
        *LINT_REPAIRED[10:],
    ]


def test_tool_seconds_sum_the_timed_commands_and_the_agent_has_the_rest():
    row = _summary(_aborted_record(), [], _timed_events())

    assert row["timing"]["complete"] is True
    assert row["timing"]["tool_seconds"] == 129.75
    # 330 seconds from creation to the terminal state.
    assert row["timing"]["agent_seconds"] == 200.25


def test_agent_seconds_need_a_complete_chain():
    events = [event for event in _timed_events() if event.get("to") != "LINT_GREEN"]

    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["complete"] is False
    assert row["timing"]["tool_seconds"] == 129.75
    assert row["timing"]["agent_seconds"] is None


def test_agent_seconds_never_go_below_zero_from_whole_second_times():
    events = [
        _transition(None, "CREATED", "00:00"),
        CREATED,
        *_command("sync", 0.6),
        _transition("CREATED", "ABORTED", "00:00"),
    ]

    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["tool_seconds"] == 0.6
    assert row["timing"]["agent_seconds"] == 0.0


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(
            [event for event in _timed_events() if event is not CREATED],
            id="recorded-before-command-timing",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", 1.0)[:1], _timed_events()[-1]],
            id="a-command-that-never-finished",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", None), _timed_events()[-1]],
            id="a-finish-without-seconds",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", -1.0), _timed_events()[-1]],
            id="negative-seconds",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", float("nan")), _timed_events()[-1]],
            id="nan-seconds",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", float("inf")), _timed_events()[-1]],
            id="infinite-seconds",
        ),
        pytest.param(
            [*_timed_events()[:-1], *_command("mergeback", 10**400), _timed_events()[-1]],
            id="integer-too-large-for-a-float",
        ),
    ],
)
def test_tool_and_agent_seconds_are_null_when_any_command_time_is_unknown(events):
    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["tool_seconds"] is None
    assert row["timing"]["agent_seconds"] is None


# -- privacy -----------------------------------------------------------------------------


def _new_values(row: dict[str, Any]) -> list[Any]:
    stages = row["stages"].values()
    return [
        row["findings"]["review_blocking_fixed"],
        row["findings"]["docs_blocking_fixed"],
        *(stage["red_then_green"] for stage in stages),
        row["timing"]["tool_seconds"],
        row["timing"]["agent_seconds"],
    ]


def test_new_fields_hold_only_counts_flags_and_seconds(tmp_path):
    findings = [_finding("F001", "review", "critical", "fixed")]
    summary = telemetry.summarize(_aborted_record(), findings, _timed_events(), _identity())
    mirror_row = json.loads(telemetry._row(summary))

    for row in (summary.model_dump(mode="json"), mirror_row):
        values = _new_values(row)
        assert values, "the new fields are present"
        for value in values:
            assert value is None or isinstance(value, bool | int | float), value
        text = json.dumps(values)
        assert "SECRET FINDING DETAIL" not in text
        assert "/" not in text
        assert "http" not in text


# -- report ------------------------------------------------------------------------------


def _caught_row(
    run_id: str,
    *,
    state: str = "DONE",
    review: int | None = 0,
    docs: int | None = 0,
    lint: bool | None = False,
    test: bool | None = False,
    tool: float | None = None,
    pr: int | None = None,
    repo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = {
        "total": 0,
        "by_stage": {},
        "by_severity": {},
        "by_status": {},
        "by_action": {},
        "blocking": 0,
        "review_blocking_fixed": review,
        "docs_blocking_fixed": docs,
    }
    stages = {
        "review": {**_stage(), "red_then_green": None},
        "lint": {**_stage(), "red_then_green": lint},
        "test": {**_stage(), "red_then_green": test},
    }
    run_timing = {
        "complete": True,
        "states": {},
        "review_wall_seconds": 0.0,
        "shell_wall_seconds": 0.0,
        "tool_seconds": tool,
        "agent_seconds": None,
    }
    hosted = {"fetched_at": None}
    if pr is not None:
        hosted = {
            "pr_number": pr,
            "pr_state": "closed",
            "merged": True,
            "checks_conclusion": "success",
            "fetched_at": "2026-10-02T00:00:00+00:00",
        }
    extra = {"repo": repo} if repo else {}
    return make_row(
        run_id,
        state=state,
        findings=findings,
        stages=stages,
        timing=run_timing,
        hosted=hosted,
        **extra,
    )


def test_report_counts_done_runs_with_any_catch_and_each_source():
    rows = [
        _caught_row("r_01", review=2),
        _caught_row("r_02", lint=True, test=True),
        _caught_row("r_03"),
        _caught_row("r_04", docs=1, test=None),  # a known catch despite an unknown source
        _caught_row("r_05", review=None),  # no known catch and an unknown source
        _caught_row("r_06", state="ABORTED", review=5),  # not a DONE run
    ]

    caught = report.aggregate(rows)["caught"]

    assert caught["runs_with_any_catch"] == {
        "value": 0.75,
        "numerator": 3,
        "denominator": 4,
        "denominator_label": "DONE runs with every catch source known or a known catch",
    }
    assert caught["done_runs"] == 5
    assert caught["excluded_runs"] == 1
    sources = caught["by_source"]
    assert sources["review_blocking_fixed"]["runs_with_any"]["numerator"] == 1
    assert sources["review_blocking_fixed"]["runs_with_any"]["denominator"] == 4
    assert sources["review_blocking_fixed"]["total"] == 2
    assert sources["review_blocking_fixed"]["excluded_runs"] == 1
    assert sources["docs_blocking_fixed"]["total"] == 1
    assert sources["lint_red_then_green"]["runs_with_any"]["numerator"] == 1
    assert sources["lint_red_then_green"]["runs_with_any"]["denominator"] == 5
    assert sources["lint_red_then_green"]["total"] is None
    assert sources["test_red_then_green"]["excluded_runs"] == 1
    assert caught["note"] == report.CATCH_NOTE


def test_report_catch_values_are_null_without_done_runs():
    caught = report.aggregate([_caught_row("r_01", state="ABORTED", review=1)])["caught"]

    assert caught["runs_with_any_catch"]["value"] is None
    assert caught["by_source"]["review_blocking_fixed"]["total"] is None


def test_older_rows_without_the_fields_are_excluded_not_zero():
    old = make_row("r_old")

    caught = report.aggregate([old])["caught"]
    cost = report.aggregate([old])["tool_time"]

    assert caught["excluded_runs"] == 1
    assert caught["runs_with_any_catch"]["denominator"] == 0
    assert cost["per_run"]["observations"] == 0
    assert cost["per_run"]["excluded_runs"] == 1


def test_report_gives_median_and_p90_tool_seconds_per_done_run():
    rows = [_caught_row(f"r_{index:02d}", tool=float(index * 10)) for index in range(1, 7)]
    rows.append(_caught_row("r_07", tool=None))
    rows.append(_caught_row("r_08", state="ABORTED", tool=999.0))

    cost = report.aggregate(rows)["tool_time"]

    assert cost["per_run"]["observations"] == 6
    assert cost["per_run"]["excluded_runs"] == 1
    assert cost["per_run"]["median"] == 35.0
    assert cost["per_run"]["p90"] == 55.0
    assert cost["per_run_label"] == "DONE runs"


def test_report_sums_tool_seconds_per_synced_pull_request():
    rows = [
        _caught_row("r_01", tool=10.0, pr=1),
        _caught_row("r_02", tool=15.0, pr=1),
        _caught_row("r_03", tool=20.0, pr=2),
        _caught_row("r_04", tool=None, pr=3),  # one run of the PR has no time
        _caught_row("r_05", tool=30.0, pr=3),
        _caught_row("r_06", tool=40.0, pr=1, repo=OTHER_REPO),  # PR 1 of another repository
        _caught_row("r_07", tool=50.0, pr=4),
        _caught_row("r_08", tool=60.0, pr=5),
        _caught_row("r_09", tool=70.0),  # not synced
    ]

    per_pr = report.aggregate(rows)["tool_time"]["per_pull_request"]

    assert per_pr["pull_requests"] == 6
    assert per_pr["observations"] == 5
    assert per_pr["excluded_pull_requests"] == 1
    assert per_pr["median"] == 40.0
    assert per_pr["runs_without_pull_request"] == 1


def test_markdown_shows_the_caught_and_tool_time_sections():
    rows = [_caught_row(f"r_{index:02d}", review=index % 2, tool=10.0) for index in range(6)]

    text = _markdown(rows)

    assert "### Caught before push" in text
    assert "### Tool time" in text
    assert report.CATCH_NOTE in text


# -- the commands are timed in a real run -------------------------------------------------


def _run_events(repo: Path, run_id: str) -> list[dict[str, Any]]:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
    path = common / "agentic-preflight" / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_a_real_run_times_each_command_the_tool_runs(feature_repo, bare_remote, tmp_path):
    agent = green_run(feature_repo, tmp_path)
    token = agent.run("gate")["data"]["token"]
    run_id = agent.run("push", "--confirm", token)["run_id"]
    agent.run("finish")

    events = _run_events(feature_repo, run_id)
    created = next(event for event in events if event["event"] == "run_created")
    assert created[timing.COMMAND_TIMING_MARKER] == timing.COMMAND_TIMING_VERSION
    started = [e["command"] for e in events if e["event"] == timing.COMMAND_STARTED_EVENT]
    finished = [e for e in events if e["event"] == timing.COMMAND_FINISHED_EVENT]
    assert {"sync", "lint", "test", "mergeback"} <= set(started)
    assert sorted(started) == sorted(event["command"] for event in finished)
    assert all(isinstance(event["seconds"], float) for event in finished)
    assert set(finished[0]) == {"at", "event", "command", "seconds"}

    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
    summary = json.loads(
        (common / "agentic-preflight" / "runs" / run_id / "summary.json").read_text("utf-8")
    )
    assert summary["timing"]["complete"] is True
    assert summary["timing"]["tool_seconds"] == pytest.approx(
        sum(event["seconds"] for event in finished), abs=0.01
    )
    assert summary["timing"]["agent_seconds"] is not None
    assert summary["stages"]["lint"]["red_then_green"] is False
    assert summary["findings"]["review_blocking_fixed"] == 0


# -- rebuild of earlier runs --------------------------------------------------------------


def test_rebuild_adds_the_new_fields_to_an_earlier_run(tmp_repo):
    record = _aborted_record()
    findings = [_finding("F001", "review", "critical", "fixed")]
    # An earlier version wrote the same transitions and findings, but timed nothing.
    _store_record(tmp_repo, record, findings=findings, events=LINT_REPAIRED)

    result = ScriptedAgent(tmp_repo).run("telemetry", "rebuild")

    assert result["data"]["summarized"] == 1
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=tmp_repo))
    path = common / "agentic-preflight" / "runs" / record["run_id"] / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    assert summary["findings"]["review_blocking_fixed"] == 1
    assert summary["stages"]["lint"]["red_then_green"] is True
    # Command times were never recorded for this run, so they stay unknown.
    assert summary["timing"]["complete"] is True
    assert summary["timing"]["tool_seconds"] is None
    assert summary["timing"]["agent_seconds"] is None


def test_a_command_after_the_terminal_state_does_not_count():
    # `review compare` can run on a DONE run and append timed events after the end.
    events = [*_timed_events(), *_command("review_compare", 60.0)]

    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["tool_seconds"] == 129.75
    assert row["timing"]["agent_seconds"] == 200.25


def test_a_command_still_running_when_the_run_ended_makes_tool_time_unknown():
    started, finished = _command("lint", 5.0)
    events = [*_timed_events()[:-1], started, _timed_events()[-1], finished]

    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["tool_seconds"] is None


# -- the timing context manager ----------------------------------------------------------


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail = fail

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        if self.fail:
            raise OSError("event log is not writable")
        self.events.append(event)


def test_the_finish_event_is_written_when_the_command_raises():
    store = _Store()

    with pytest.raises(RuntimeError), timing.timed_command(store, "r_1", "lint"):  # type: ignore[arg-type]
        raise RuntimeError("the command failed")

    assert [event["event"] for event in store.events] == [
        timing.COMMAND_STARTED_EVENT,
        timing.COMMAND_FINISHED_EVENT,
    ]
    assert store.events[1]["command"] == "lint"
    assert store.events[1]["seconds"] >= 0


def test_an_unwritable_event_log_never_affects_the_command():
    store = _Store(fail=True)
    ran = []

    with timing.timed_command(store, "r_1", "test"):  # type: ignore[arg-type]
        ran.append(True)

    assert ran == [True]


def test_an_unknown_command_name_runs_the_command_untimed():
    store = _Store()
    ran = []

    with timing.timed_command(store, "r_1", "a-future-stage"):  # type: ignore[arg-type]
        ran.append(True)

    assert ran == [True]
    assert store.events == []


def test_malformed_event_values_never_break_the_summary():
    events = [
        *_timed_events()[:-1],
        {"event": "state_transition", "from": None, "to": ["not", "a", "state"]},
        {"event": timing.COMMAND_STARTED_EVENT, "command": ["x"]},
        {"event": timing.COMMAND_FINISHED_EVENT, "command": ["x"], "seconds": 1.0},
        _timed_events()[-1],
    ]

    row = _summary(_aborted_record(), [], events)

    assert row["timing"]["tool_seconds"] == 130.75


def test_tool_time_treats_a_hosted_value_that_is_not_an_object_as_unsynced():
    row = {**_caught_row("r_01", tool=10.0), "hosted": "not an object"}

    per_pr = report._tool_time([row])["per_pull_request"]

    assert per_pr["pull_requests"] == 0
    assert per_pr["runs_without_pull_request"] == 1
