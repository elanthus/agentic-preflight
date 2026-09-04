"""Strict-mode stage restart regressions."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import State
from agentic_preflight.runs import stages
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path: Path) -> str:
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps(
            {
                "coverage": {"manifest": "$context", "examined": "all"},
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_strict_mode_ignores_a_stale_lint_head_after_review_restarts(
    feature_repo: Path, tmp_path: Path
) -> None:
    write(
        feature_repo,
        ".agentic-preflight.toml",
        r"""[worktree]
mode = "strict"

[commands]
lint = "python -c 'from pathlib import Path; raise SystemExit(not Path(\"lint-fixed\").exists())'"
test = "true"

[docs]
enabled = false
""",
    )
    commit_all(feature_repo, "configure strict validation")
    agent = ScriptedAgent(feature_repo)

    started = agent.run("start")
    worktree_path = Path(started["data"]["worktree_path"])
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path))

    failed = agent.run("stage", "run", "lint", expect=ExitCode.STAGE_FAILED)
    assert failed["state"] == "LINT_RED"

    write(worktree_path, "lint-fixed", "fixed\n")
    git("add", "-A", cwd=worktree_path)
    git("commit", "-m", "repair lint failure", cwd=worktree_path)
    restarted = agent.run("stage", "run", "lint")
    assert restarted["data"]["validation_restarted"] is True

    write(worktree_path, "extra.txt", "second repair\n")
    git("add", "-A", cwd=worktree_path)
    git("commit", "-m", "add second repair", cwd=worktree_path)
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path))

    linted = agent.run("stage", "run", "lint")
    assert linted["state"] == "LINT_GREEN"
    tested = agent.run("stage", "run", "test")
    assert tested["state"] == "TEST_GREEN"


def test_stage_ready_states_are_derived_from_legal_machine_actions() -> None:
    assert set(stages._STAGE_READY_STATES["lint"]) == {
        State.DOCS_GREEN,
        State.LINT_RED,
        State.LINT_RUNNING,
    }
    assert set(stages._STAGE_READY_STATES["test"]) == {
        State.LINT_GREEN,
        State.TEST_RED,
        State.TEST_RUNNING,
    }
