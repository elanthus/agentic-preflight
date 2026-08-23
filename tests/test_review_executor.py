"""Configurable and attestable review command execution."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import Action
from agentic_preflight.runs._session import _apply, open_session
from tests.conftest import commit_all, write
from tests.driver import ScriptedAgent

REVIEWER = """\
import json
import pathlib
import sys
import time

mode = sys.argv[1]
raw = sys.stdin.read()
if len(sys.argv) > 2:
    pathlib.Path(sys.argv[2]).write_text(raw)
data = json.loads(raw)
manifest = data["review_coverage"]

if mode == "nonzero":
    print("reviewer failed", file=sys.stderr)
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(2)
if mode == "malformed":
    print("not json")
    raise SystemExit(0)
if mode == "stale":
    manifest = {**manifest, "manifest": "0" * 64}

findings = []
if mode == "finding":
    unit = data["review_coverage"]["units"][0]
    findings.append({
        "path": unit["path"],
        "unit": unit["id"],
        "severity": "high",
        "action": "auto_fix",
        "title": "Independent reviewer finding",
    })

json.dump({
    "coverage": {"manifest": manifest["manifest"], "examined": "all"},
    "findings": findings,
}, sys.stdout)
"""


def configure_reviewer(
    repo: Path,
    *,
    mode: str = "valid",
    capture: Path | None = None,
    extra_review: str = "",
    stage: str = "",
) -> None:
    write(repo, "reviewer.py", REVIEWER)
    command = shlex.join(
        [sys.executable, "reviewer.py", mode, *([str(capture)] if capture is not None else [])]
    )
    body = (
        "[review]\n"
        "executor = 'command'\n"
        f"command = {command!r}\n"
        f"{extra_review}"
        "\n[docs]\nenabled = false\n"
        f"{stage}"
    )
    write(repo, ".agentic-preflight.toml", body)
    commit_all(repo, f"configure {mode} independent reviewer")


def test_command_receives_the_exact_context_bundle_and_records_evidence(feature_repo, tmp_path):
    capture = tmp_path / "review-input.json"
    configure_reviewer(feature_repo, capture=capture)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")

    env = agent.run("review", "run")

    assert json.loads(capture.read_text()) == context["data"]
    assert env["state"] == "DOCS_GREEN"
    assert env["data"]["executor"] == "command"
    review = agent.run("status")["data"]["stages"]["review"]
    assert review["status"] == "green"
    assert review["executor"] == "command"
    assert review["exit_code"] == 0
    assert len(review["output_sha256"]) == 64


def test_command_can_submit_blocking_findings(feature_repo):
    configure_reviewer(feature_repo, mode="finding")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    env = agent.run("review", "run")

    assert env["state"] == "REVIEW_BLOCKED"
    assert env["blocking"][0]["title"] == "Independent reviewer finding"


@pytest.mark.parametrize(
    ("mode", "expected_exit"),
    [
        ("malformed", 0),
        ("stale", 0),
        ("nonzero", 7),
        ("timeout", 124),
    ],
)
def test_command_failures_are_retryable_and_attested_as_red(feature_repo, mode, expected_exit):
    stage = "\n[stage]\ntimeout_seconds = 1\n" if mode == "timeout" else ""
    configure_reviewer(feature_repo, mode=mode, stage=stage)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    env = agent.run("review", "run", expect=ExitCode.STAGE_FAILED)

    assert env["state"] == "REVIEW_COMMAND_RED"
    record = agent.run("status")["data"]["stages"]["review"]
    assert record["attempts"] == 1
    assert record["exit_code"] == expected_exit
    assert agent.run("logs", "--stage", "review")["data"]["output"]
    assert agent.run("review", "run", expect=ExitCode.STAGE_FAILED)["state"] == (
        "REVIEW_COMMAND_RED"
    )


def test_unreadable_copied_file_blocks_review_command_before_execution(feature_repo):
    (feature_repo / ".env").write_bytes(b"SECRET=\xff\n")
    configure_reviewer(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")

    env = agent.run("review", "run", expect=ExitCode.STAGE_FAILED)

    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert env["data"]["copied_file"].endswith("/.env")
    assert "redaction is unavailable" in env["error"]["message"]
    assert "review" not in agent.run("status")["data"]["stages"]


def test_max_attempts_survive_a_new_process(feature_repo):
    configure_reviewer(
        feature_repo,
        mode="nonzero",
        stage="\n[stage]\nmax_attempts = 2\n",
    )
    ScriptedAgent(feature_repo).run("start")
    ScriptedAgent(feature_repo).run("review", "run", expect=ExitCode.STAGE_FAILED)
    ScriptedAgent(feature_repo).run("review", "run", expect=ExitCode.STAGE_FAILED)

    env = ScriptedAgent(feature_repo).run("review", "run", expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "max_attempts"
    assert env["data"]["attempts"] == 2


def test_interrupted_command_is_recorded_before_a_new_process_retries(feature_repo):
    configure_reviewer(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    session = open_session(feature_repo)
    run_id = session.store.get_current()
    assert run_id is not None
    with session.store.transaction(run_id) as run:
        _apply(run, Action.RUN_REVIEW_COMMAND)

    env = ScriptedAgent(feature_repo).run("review", "run")

    assert env["state"] == "DOCS_GREEN"
    review = ScriptedAgent(feature_repo).run("status")["data"]["stages"]["review"]
    assert review["status"] == "green"
    assert review["attempts"] == 1
    assert review["executor"] == "command"


def test_risk_policy_requires_command_and_refuses_direct_submission(feature_repo, tmp_path):
    configure_reviewer(
        feature_repo,
        extra_review="require_command_for = ['high']\n",
    )
    with open(feature_repo / ".agentic-preflight.toml", "a") as handle:
        handle.write("\n[policy]\nhigh_risk_paths = ['src/**']\n")
    commit_all(feature_repo, "require independent review for high risk changes")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    context = agent.run("context")
    submission = tmp_path / "findings.json"
    submission.write_text(
        json.dumps(
            {
                "coverage": {
                    "manifest": context["data"]["review_coverage"]["manifest"],
                    "examined": "all",
                },
                "findings": [],
            }
        )
    )

    refused = agent.run("submit-findings", "--file", str(submission), expect=ExitCode.PRECONDITION)
    assert refused["data"]["mode"] == "needs_command"
    assert refused["next"]["command"] == "agentic-preflight review run"
    assert agent.run("review", "run")["state"] == "DOCS_GREEN"


def test_committed_repairs_clear_review_evidence_and_require_a_fresh_command(feature_repo):
    configure_reviewer(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("review", "run")
    agent.run(
        "stage",
        "run",
        "lint",
        "--command",
        "false",
        "--record",
        expect=ExitCode.STAGE_FAILED,
    )
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return f'hello {name}'\n")
    commit_all(feature_repo, "repair lint feedback")

    restarted = agent.run("stage", "run", "lint", "--command", "true", "--record")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    review = agent.run("status")["data"]["stages"]["review"]
    assert review["status"] == "pending"
    assert review["executor"] is None
    assert agent.run("context")["next"]["command"] == "agentic-preflight review run"
