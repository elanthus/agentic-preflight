"""Recovery for lint and test processes interrupted outside the harness."""

from __future__ import annotations

import json

from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import Action
from agentic_preflight.runs._session import _apply, open_session
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent


def _findings_file(tmp_path) -> str:
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": []}),
        encoding="utf-8",
    )
    return str(path)


def _docs_green(feature_repo, tmp_path, *, max_attempts: int = 3) -> ScriptedAgent:
    write(
        feature_repo,
        ".agentic-preflight.toml",
        (
            "[docs]\n"
            "enabled = false\n\n"
            "[commands]\n"
            "lint = 'true'\n"
            "test = 'true'\n\n"
            "[stage]\n"
            f"max_attempts = {max_attempts}\n"
        ),
    )
    commit_all(feature_repo, "configure stage commands")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    env = agent.run("submit-findings", "--file", _findings_file(tmp_path))
    assert env["state"] == "DOCS_GREEN"
    return agent


def _force_running(repo, action: Action) -> None:
    session = open_session(repo)
    run_id = session.active_run_id()
    assert run_id is not None
    with session.store.transaction(run_id) as run:
        _apply(run, action)


def test_interrupted_lint_is_recorded_before_retry(feature_repo, tmp_path):
    agent = _docs_green(feature_repo, tmp_path)
    _force_running(feature_repo, Action.RUN_LINT)

    env = agent.run("stage", "run", "lint")

    assert env["state"] == "LINT_GREEN"
    lint = agent.run("status")["data"]["stages"]["lint"]
    assert lint["status"] == "green"
    assert lint["attempts"] == 1
    events = agent.run("events")["data"]["events"]
    assert any(event["event"] == "lint_interrupted" and event["attempts"] == 1 for event in events)


def test_interrupted_test_is_recorded_before_retry(feature_repo, tmp_path):
    agent = _docs_green(feature_repo, tmp_path)
    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"
    _force_running(feature_repo, Action.RUN_TEST)

    env = agent.run("stage", "run", "test")

    assert env["state"] == "TEST_GREEN"
    test = agent.run("status")["data"]["stages"]["test"]
    assert test["status"] == "green"
    assert test["attempts"] == 1
    events = agent.run("events")["data"]["events"]
    assert any(event["event"] == "test_interrupted" and event["attempts"] == 1 for event in events)


def test_interrupted_stage_counts_toward_max_attempts(feature_repo, tmp_path):
    agent = _docs_green(feature_repo, tmp_path, max_attempts=1)
    _force_running(feature_repo, Action.RUN_LINT)

    env = agent.run("stage", "run", "lint", expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 1
    lint = agent.run("status")["data"]["stages"]["lint"]
    assert lint["status"] == "red"
    assert lint["attempts"] == 1
    assert lint["exit_code"] == 125
    assert lint["reason"] == "interrupted"


def test_interrupted_retry_clears_the_prior_attempt_s_artifacts(feature_repo, tmp_path):
    """A stale command/log/hash from the prior red attempt must not survive."""
    agent = _docs_green(feature_repo, tmp_path, max_attempts=2)
    agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record", expect=ExitCode.STAGE_FAILED
    )
    first = agent.run("status")["data"]["stages"]["lint"]
    assert first["command"]
    assert first["log_path"]
    assert first["output_sha256"]

    _force_running(feature_repo, Action.RETRY_LINT)
    env = agent.run("stage", "run", "lint", expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "max_attempts"
    lint = agent.run("status")["data"]["stages"]["lint"]
    assert lint["reason"] == "interrupted"
    assert lint["command"] is None
    assert lint["log_path"] is None
    assert lint["output_sha256"] is None
