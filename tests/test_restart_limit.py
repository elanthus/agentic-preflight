"""The whole-run convergence guard: too many validation restarts need a person."""

import json
from pathlib import Path

import pytest

from agentic_preflight.config import Config
from agentic_preflight.envelope import ExitCode
from agentic_preflight.errors import MaxRestarts
from agentic_preflight.machine import Action, State
from agentic_preflight.runs._session import _apply
from tests.conftest import commit_all, make_run, write
from tests.driver import ScriptedAgent


def _findings(tmp_path):
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": []})
    )
    return str(path)


def _clear_review(agent, tmp_path):
    agent.run("context")
    env = agent.run("submit-findings", "--file", _findings(tmp_path))
    assert env["state"] == "DOCS_GREEN"


def _repair_red_lint(agent, body):
    """Fail lint, then commit a repair so the next lint run restarts validation."""
    agent.run(
        "stage", "run", "lint", "--command", "exit 1", "--record", expect=ExitCode.STAGE_FAILED
    )
    worktree = Path(agent.run("status")["data"]["worktree_path"])
    write(worktree, "src/app.py", body)
    commit_all(worktree, "repair lint")


@pytest.fixture
def agent(feature_repo, tmp_path):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[stage]\nmax_attempts = 10\nmax_restarts = 2\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _clear_review(agent, tmp_path)
    return agent


def test_default_limit_is_five_restarts():
    assert Config().stage.max_restarts == 5


def test_run_stops_for_a_person_when_the_restart_limit_is_reached(agent, tmp_path):
    _repair_red_lint(agent, "def greet(name):\n    return f'hello {name}'\n")
    first = agent.run("stage", "run", "lint", "--command", "true", "--record")
    assert first["data"]["validation_restarted"] is True
    assert agent.run("status")["data"]["validation_restarts"] == 1
    _clear_review(agent, tmp_path)

    _repair_red_lint(agent, "def greet(name):\n    return f'hi {name}'\n")
    stopped = agent.run(
        "stage", "run", "lint", "--command", "true", "--record", expect=ExitCode.NEEDS_HUMAN
    )
    assert stopped["error"]["code"] == "max_restarts"
    assert stopped["data"] == {
        "validation_restarts": 2,
        "max_restarts": 2,
        "needs_human": True,
    }
    assert "person" in stopped["next"]["instruction"]


def test_a_stopped_run_stays_stopped_but_remains_inspectable_and_abortable(agent, tmp_path):
    _repair_red_lint(agent, "def greet(name):\n    return f'hello {name}'\n")
    agent.run("stage", "run", "lint", "--command", "true", "--record")
    _clear_review(agent, tmp_path)
    _repair_red_lint(agent, "def greet(name):\n    return f'hi {name}'\n")
    agent.run("stage", "run", "lint", "--command", "true", "--record", expect=ExitCode.NEEDS_HUMAN)

    # The restart itself was recorded: stale green evidence must not survive the stop.
    status = agent.run("status")
    assert status["state"] == "REVIEW_AWAITING_FINDINGS"
    assert status["data"]["needs_human"] is True
    assert status["data"]["validation_restarts"] == 2
    assert status["next"]["command"] is None
    assert "person" in status["next"]["instruction"]

    # Inspection commands must not advertise the state's ordinary next move.
    events = agent.run("events")
    assert events["next"]["command"] is None
    assert events["data"]["needs_human"] is True
    assert "person" in events["next"]["instruction"]

    for command in (
        ("context",),
        ("submit-findings", "--file", _findings(tmp_path)),
    ):
        refused = agent.run(*command, expect=ExitCode.NEEDS_HUMAN)
        assert refused["error"]["code"] == "max_restarts"
        assert refused["data"]["needs_human"] is True

    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_reopening_a_review_that_is_already_open_is_not_a_restart():
    run = make_run()
    run.state = State.REVIEW_AWAITING_FINDINGS
    _apply(run, Action.INVALIDATE_REVIEW)
    assert run.validation_restarts == 0

    run.state = State.REVIEW_GREEN
    _apply(run, Action.INVALIDATE_REVIEW)
    assert run.validation_restarts == 1


def test_only_closing_actions_remain_legal_at_the_limit():
    run = make_run()
    run.state = State.REVIEW_AWAITING_FINDINGS
    run.validation_restarts = Config.model_validate(run.config_snapshot).stage.max_restarts
    with pytest.raises(MaxRestarts):
        _apply(run, Action.SUBMIT_CLEAN)
    _apply(run, Action.ABORT)
    assert run.state is State.ABORTED


def test_the_stop_outranks_the_stale_run_hint(agent, tmp_path, feature_repo):
    """Starting again resets the counter, so it must not be the advertised next move."""
    _repair_red_lint(agent, "def greet(name):\n    return f'hello {name}'\n")
    agent.run("stage", "run", "lint", "--command", "true", "--record")
    _clear_review(agent, tmp_path)
    _repair_red_lint(agent, "def greet(name):\n    return f'hi {name}'\n")
    agent.run("stage", "run", "lint", "--command", "true", "--record", expect=ExitCode.NEEDS_HUMAN)

    write(feature_repo, "src/other.py", "VALUE = 1\n")
    commit_all(feature_repo, "move the source branch")

    status = agent.run("status")
    assert status["data"]["stale"] is True
    assert status["data"]["needs_human"] is True
    assert status["next"]["command"] is None
    assert "person" in status["next"]["instruction"]

    refused = agent.run("context", expect=ExitCode.NEEDS_HUMAN)
    assert refused["error"]["code"] == "max_restarts"
