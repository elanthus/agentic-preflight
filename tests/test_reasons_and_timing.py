"""Structured reasons on finding responses and aborts, and wall-clock stage timing.

Reasons are agent-chosen labels from closed vocabularies, kept for telemetry.
Stage start times and state timing are code-derived. Neither may reach stage
evidence, fingerprints, or the attestation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from agentic_preflight import runs, telemetry
from agentic_preflight.envelope import ExitCode
from agentic_preflight.errors import InvalidResponse
from agentic_preflight.models import ABORT_REASONS, RESPONSE_REASONS, Finding, StageRecord
from agentic_preflight.runs import _session
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent, findings_json, green_run
from tests.test_telemetry import _findings, _identity, _schema_2_record

BLOCKING = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "loud flag is never used",
    }
]


def _run_dir(repo: Path, run_id: str) -> Path:
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
    return common / "agentic-preflight" / "runs" / run_id


def _events(repo: Path, run_id: str, name: str) -> list[dict]:
    path = _run_dir(repo, run_id) / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [event for event in events if event["event"] == name]


def _summary(repo: Path, run_id: str) -> dict:
    return json.loads((_run_dir(repo, run_id) / "summary.json").read_text(encoding="utf-8"))


@pytest.fixture
def blocked(agent, tmp_path):
    """A run parked in REVIEW_BLOCKED with F001 outstanding."""
    env = agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    return agent, env["run_id"]


# -- respond --reason --------------------------------------------------------------


def test_the_vocabularies_are_the_contracted_closed_sets():
    assert RESPONSE_REASONS == (
        "false_positive",
        "out_of_scope",
        "duplicate",
        "already_fixed",
        "accepted_risk",
        "deferred",
        "other",
    )
    assert ABORT_REASONS == (
        "user_cancelled",
        "superseded",
        "wrong_base",
        "environment_failure",
        "tool_error",
        "no_longer_needed",
        "other",
    )


def test_a_dismissal_records_its_reason_on_the_finding_and_the_event(blocked, feature_repo):
    agent, run_id = blocked

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "the flag is read by the caller in another module",
        "--reason",
        "false_positive",
    )

    assert env["data"]["finding"]["response_reason"] == "false_positive"
    stored = json.loads((_run_dir(feature_repo, run_id) / "findings.json").read_text())
    assert stored[0]["response_reason"] == "false_positive"
    (resolved,) = _events(feature_repo, run_id, "finding_resolved")
    assert (resolved["action"], resolved["reason"]) == ("dismissed", "false_positive")


def test_an_acceptance_without_a_reason_is_still_valid_and_stores_no_key(blocked, feature_repo):
    agent, run_id = blocked

    env = agent.run(
        "respond", "--id", "F001", "--action", "accepted", "--note", "worth it, not now"
    )

    assert env["data"]["finding"]["status"] == "accepted"
    assert "response_reason" not in env["data"]["finding"]
    stored = json.loads((_run_dir(feature_repo, run_id) / "findings.json").read_text())
    # Written exactly as earlier versions wrote it, so they can still read it.
    assert "response_reason" not in stored[0]
    (resolved,) = _events(feature_repo, run_id, "finding_resolved")
    assert resolved["reason"] is None


def test_a_reason_is_refused_for_a_fix_at_the_cli(blocked):
    agent, _ = blocked

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        "--commit",
        "deadbeef",
        "--reason",
        "already_fixed",
        expect=ExitCode.USAGE_ERROR,
    )

    assert env["error"]["code"] == "usage_error"
    assert "--reason" in env["error"]["message"]


def test_a_reason_outside_the_vocabulary_is_a_usage_error(blocked):
    agent, _ = blocked

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "x",
        "--reason",
        "not_a_bug",
        expect=ExitCode.USAGE_ERROR,
    )

    assert env["error"]["code"] == "usage_error"


def test_the_python_api_refuses_a_reason_for_a_fix(blocked, feature_repo, monkeypatch):
    monkeypatch.chdir(feature_repo)
    session = runs.open_session()

    with pytest.raises(InvalidResponse, match="--reason"):
        runs.respond(
            session, finding_id="F001", action="fixed", commit="deadbeef", reason="already_fixed"
        )
    with pytest.raises(InvalidResponse, match="--reason must be one of"):
        runs.respond(
            session,
            finding_id="F001",
            action="dismissed",
            note="x",
            reason="not_a_bug",  # type: ignore[arg-type]
        )
    finding = next(
        f for f in session.store.load_findings(session.active_run_id()) if f.id == "F001"
    )
    assert finding.status.value == "open"


# -- abort --reason ------------------------------------------------------------------


def test_abort_records_its_reason_and_the_state_it_left(blocked, feature_repo):
    agent, run_id = blocked

    env = agent.run("abort", "--reason", "superseded")

    assert env["state"] == "ABORTED"
    (marker,) = _events(feature_repo, run_id, "aborted")
    assert marker["reason"] == "superseded"
    assert marker["aborted_from_state"] == "REVIEW_BLOCKED"
    assert marker["forced"] is False
    (transition,) = [
        event
        for event in _events(feature_repo, run_id, "state_transition")
        if event["to"] == "ABORTED"
    ]
    assert transition["from"] == marker["aborted_from_state"]
    assert transition["seq"] == marker["seq"]
    terminal = _summary(feature_repo, run_id)["terminal"]
    assert terminal == {
        "aborted_forced": False,
        "abort_reason": "superseded",
        "aborted_from_state": "REVIEW_BLOCKED",
        "orphaned_reason": None,
    }


def test_abort_without_a_reason_records_null_and_still_the_state(agent, feature_repo):
    run_id = agent.run("start")["run_id"]

    agent.run("abort", "--force")

    (marker,) = _events(feature_repo, run_id, "aborted")
    assert marker["reason"] is None
    assert marker["aborted_from_state"] == "REVIEW_AWAITING_FINDINGS"
    terminal = _summary(feature_repo, run_id)["terminal"]
    assert terminal["abort_reason"] is None
    assert terminal["aborted_from_state"] == "REVIEW_AWAITING_FINDINGS"
    assert terminal["aborted_forced"] is True


def test_abort_refuses_a_reason_outside_the_vocabulary(agent):
    agent.run("start")

    env = agent.run("abort", "--reason", "bored", expect=ExitCode.USAGE_ERROR)

    assert env["error"]["code"] == "usage_error"
    assert agent.run("status")["data"]["has_run"] is True


# -- stage start times -----------------------------------------------------------


@pytest.fixture
def ticking_clock(monkeypatch):
    """Give each transition its own second, so a reset is visible."""
    origin = datetime.now(UTC).replace(microsecond=0)
    ticks = count()
    monkeypatch.setattr(
        _session,
        "_now",
        lambda: (origin + timedelta(seconds=next(ticks))).isoformat(timespec="seconds"),
    )


def _stages(agent: ScriptedAgent) -> dict:
    return agent.run("status")["data"]["stages"]


def test_review_starts_at_begin_review_and_restarts_with_the_review(
    ticking_clock, blocked, feature_repo
):
    agent, run_id = blocked
    begun = _stages(agent)["review"]
    worktree = agent.run("status")["data"]["worktree_path"]
    (begin,) = [
        event
        for event in _events(feature_repo, run_id, "state_transition")
        if event.get("action") == "BEGIN_REVIEW"
    ]
    assert begun["started_at"] == begin["at"]
    assert begun["finished_at"] is not None

    Path(worktree, "src/app.py").write_text(
        "def greet(name, loud=False):\n    return 'HI' if loud else 'hi'\n", encoding="utf-8"
    )
    git("commit", "-am", "fix the flag", cwd=Path(worktree))
    sha = git("rev-parse", "HEAD", cwd=Path(worktree))
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    assert agent.run("verify")["state"] == "REVIEW_AWAITING_FINDINGS"

    restarted = _stages(agent)["review"]
    (invalidate,) = [
        event
        for event in _events(feature_repo, run_id, "state_transition")
        if event.get("action") == "INVALIDATE_REVIEW"
    ]
    assert restarted["started_at"] == invalidate["at"]
    assert restarted["started_at"] > begun["started_at"]
    # The discarded review result goes with its finish time.
    assert restarted.get("finished_at") is None


def test_each_stage_starts_at_its_own_begin_transition(ticking_clock, feature_repo, tmp_path):
    agent = green_run(feature_repo, tmp_path)
    run_id = agent.run("status")["run_id"]
    stages = _stages(agent)
    starts = {
        event["action"]: event["at"]
        for event in _events(feature_repo, run_id, "state_transition")
        if event.get("action") in {"BEGIN_REVIEW", "RUN_LINT", "RUN_TEST"}
    }

    assert stages["review"]["started_at"] == starts["BEGIN_REVIEW"]
    assert stages["lint"]["started_at"] == starts["RUN_LINT"]
    assert stages["test"]["started_at"] == starts["RUN_TEST"]
    # Docs are disabled here, so that stage was skipped and never began.
    assert "started_at" not in stages["docs"]


def test_a_full_run_summarizes_stage_durations_and_state_timing(feature_repo, tmp_path):
    agent = green_run(feature_repo, tmp_path)
    run_id = agent.run("status")["run_id"]

    agent.run("abort", "--reason", "no_longer_needed")

    summary = _summary(feature_repo, run_id)
    stages = summary["stages"]
    for name in ("review", "lint", "test"):
        assert stages[name]["started_at"] is not None
        assert stages[name]["duration_seconds"] >= 0
    assert stages["docs"]["status"] == "skipped"
    assert stages["docs"]["duration_seconds"] is None
    timing = summary["timing"]
    assert timing["complete"] is True
    assert "ABORTED" not in timing["states"]
    assert timing["states"]["REVIEW_AWAITING_FINDINGS"]["visits"] == 1
    assert timing["review_wall_seconds"] >= 0
    assert timing["shell_wall_seconds"] >= 0
    assert summary["terminal"]["abort_reason"] == "no_longer_needed"
    assert summary["terminal"]["aborted_from_state"] == "VERIFIED"


def test_stage_records_without_a_start_serialize_as_before():
    record = StageRecord(status="green", finished_at="2026-10-21T10:00:00+00:00")

    assert "started_at" not in record.model_dump(mode="json")
    started = record.model_copy(update={"started_at": "2026-10-21T09:59:00+00:00"})
    assert started.model_dump(mode="json")["started_at"] == "2026-10-21T09:59:00+00:00"


# -- evidence and attestation stay unchanged ------------------------------------------


def test_response_reasons_never_reach_stage_evidence_or_the_note(
    feature_repo, bare_remote, tmp_path
):
    from agentic_preflight.models import OriginalExecution

    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start")["run_id"]
    agent.run("context")
    minor = [{**BLOCKING[0], "severity": "medium", "title": "loud is unused"}]
    submitted = agent.run("submit-findings", "--file", findings_json(tmp_path, minor))
    assert submitted["state"] == "REVIEW_GREEN"
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "the caller reads it",
        "--reason",
        "false_positive",
    )
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    run = json.loads((_run_dir(feature_repo, run_id) / "run.json").read_text())
    origin = run["evidence"]["review"]["origin"]
    assert origin["findings"][0]["status"] == "dismissed"
    assert origin["findings"][0]["response_note"] == "the caller reads it"
    assert "response_reason" not in origin["findings"][0]
    stored = json.loads((_run_dir(feature_repo, run_id) / "findings.json").read_text())
    assert stored[0]["response_reason"] == "false_positive"

    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    note = git("notes", "--ref=agentic-preflight", "show", head, cwd=feature_repo)
    assert "the caller reads it" in note
    assert "response_reason" not in note
    assert agent.run("verify", head)["ok"] is True

    reasoned = {**origin["findings"][0], "response_reason": "false_positive"}
    with pytest.raises(ValueError, match="response reasons"):
        OriginalExecution.model_validate({**origin, "findings": [reasoned]})


def test_older_findings_keep_their_evidence_digest():
    finding = Finding(
        id="F001", stage="review", path="a.py", severity="low", action="no_op", title="t"
    )

    assert "response_reason" not in finding.model_dump(mode="json")
    assert Finding.model_validate(finding.model_dump(mode="json")) == finding


# -- the summary ------------------------------------------------------------------


def test_reasons_are_tallied_for_dismissed_and_accepted_findings_only():
    findings = _findings()
    findings[2]["response_reason"] = "accepted_risk"
    findings.append({**findings[3], "id": "F005", "response_reason": "false_positive"})

    counts = telemetry.summarize(_schema_2_record(), findings, [], _identity()).findings

    # F001 is fixed and F002 open: neither carries a reason, and neither is tallied.
    assert counts["by_response_reason"] == {
        "accepted_risk": 1,
        "false_positive": 1,
        "unspecified": 1,
    }


def test_an_abort_marker_with_a_reason_and_state_is_summarized():
    events = [
        {"event": "state_transition", "from": "LINT_RED", "to": "ABORTED", "seq": 8},
        {
            "event": "aborted",
            "forced": False,
            "reason": "environment_failure",
            "aborted_from_state": "LINT_RED",
            "seq": 8,
        },
    ]

    terminal = telemetry.summarize(_schema_2_record(), None, events, _identity()).terminal

    assert terminal == {
        "aborted_forced": False,
        "abort_reason": "environment_failure",
        "aborted_from_state": "LINT_RED",
        "orphaned_reason": None,
    }


def test_an_abort_without_a_marker_state_takes_it_from_the_transition():
    # `start` aborts the run it was creating without an `aborted` marker.
    events = [{"event": "state_transition", "from": "SYNC_GREEN", "to": "ABORTED", "seq": 4}]

    terminal = telemetry.summarize(_schema_2_record(), None, events, _identity()).terminal

    assert terminal["abort_reason"] is None
    assert terminal["aborted_forced"] is None
    assert terminal["aborted_from_state"] == "SYNC_GREEN"


def test_reasons_and_abort_state_stay_null_for_a_finished_run():
    record = {**_schema_2_record(), "state": "DONE"}
    events = [{"event": "state_transition", "from": "LINT_RED", "to": "ABORTED"}]

    terminal = telemetry.summarize(record, None, events, _identity()).terminal

    assert terminal["abort_reason"] is None
    assert terminal["aborted_from_state"] is None


def test_a_stage_duration_needs_both_ends_and_a_stage_that_ran_in_this_run():
    record = _schema_2_record()
    record["stages"] = {
        "review": {
            "status": "green",
            "started_at": "2026-10-11T19:50:00+00:00",
            "finished_at": "2026-10-11T19:52:30+00:00",
        },
        "docs": {"status": "green", "started_at": "2026-10-11T19:53:00+00:00"},
        "lint": {
            "status": "red",
            "started_at": "2026-10-11T19:58:00+00:00",
            "finished_at": "2026-10-11T19:57:28+00:00",
        },
        "test": {
            "status": "skipped",
            "started_at": "2026-10-11T19:58:00+00:00",
            "finished_at": "2026-10-11T19:58:00+00:00",
        },
    }
    record["evidence"] = {}

    stages = telemetry.summarize(record, None, None, _identity()).stages

    assert stages["review"]["started_at"] == "2026-10-11T19:50:00+00:00"
    assert stages["review"]["duration_seconds"] == 150.0
    # No finish yet.
    assert stages["docs"]["duration_seconds"] is None
    # A start after the last finish: the stage began again and never finished.
    assert stages["lint"]["duration_seconds"] is None
    assert stages["test"]["duration_seconds"] is None

    record["evidence"] = {"review": {"version": 1, "derivation_reason": "equivalent_inputs"}}
    reused = telemetry.summarize(record, None, None, _identity()).stages["review"]
    assert reused["reused"] is True
    assert reused["duration_seconds"] is None


def test_a_summary_with_timing_validates_and_one_without_it_still_does():
    events = [
        {
            "event": "state_transition",
            "from": None,
            "to": "CREATED",
            "at": "2026-10-11T19:50:00+00:00",
        },
        {
            "event": "state_transition",
            "from": "CREATED",
            "to": "ABORTED",
            "at": "2026-10-11T19:50:07+00:00",
        },
    ]
    summary = telemetry.summarize(_schema_2_record(), None, events, _identity())
    row = summary.model_dump(mode="json")

    assert row["timing"] == {
        "complete": True,
        "states": {"CREATED": {"visits": 1, "seconds": 7.0}},
        "review_wall_seconds": 0.0,
        "shell_wall_seconds": 0.0,
    }
    del row["timing"]
    assert telemetry.RunSummary.model_validate(row).timing is None
