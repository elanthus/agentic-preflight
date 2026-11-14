"""An open auto-fix finding must get a disposition before the run leaves its stage.

`respond` is legal only in the review and docs states, and an attestation refuses an
open auto-fix finding. Before this gate a low-severity auto-fix finding left the review
stage green, the run reached `mergeback`, and `mergeback` failed with an internal error
from a state it could not leave.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentic_preflight import runs
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from tests.conftest import configure_base
from tests.driver import ScriptedAgent
from tests.driver import findings_json as review_findings_json

DOCS_ON = "[commands]\nlint = 'true'\ntest = 'true'\n"
DOCS_OFF = "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"


def _low_auto_fix(path: str = "src/app.py") -> dict:
    return {
        "path": path,
        "line": 1,
        "severity": "low",
        "action": "auto_fix",
        "title": "a small mechanical fix",
    }


def _docs_findings_json(tmp_path, items: list) -> str:
    path = tmp_path / "docs-findings.json"
    path.write_text(json.dumps({"findings": items}), encoding="utf-8")
    return str(path)


def _review_green_with_open_finding(feature_repo, tmp_path) -> ScriptedAgent:
    configure_base(feature_repo, DOCS_ON)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    env = agent.run("submit-findings", "--file", review_findings_json(tmp_path, [_low_auto_fix()]))
    assert env["state"] == "REVIEW_GREEN"
    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")
    return agent


def _notes(repo) -> str:
    result = subprocess.run(
        ["git", "notes", "--ref=refs/notes/agentic-preflight", "list"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_docs_stage_refuses_to_open_while_a_review_auto_fix_finding_is_open(feature_repo, tmp_path):
    agent = _review_green_with_open_finding(feature_repo, tmp_path)

    env = agent.run("context", "--section", "docs", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert [item["id"] for item in env["blocking"]] == ["F001"]
    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")
    assert agent.run("status")["state"] == "REVIEW_GREEN"


def test_an_accepted_review_finding_lets_the_docs_stage_open(feature_repo, tmp_path):
    agent = _review_green_with_open_finding(feature_repo, tmp_path)
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--reason",
        "accepted_risk",
        "--note",
        "cosmetic, not worth a change in this pull request",
    )

    env = agent.run("context", "--section", "docs")

    assert env["state"] == "DOCS_AWAITING_FINDINGS"


def test_lint_refuses_to_start_while_a_docs_auto_fix_finding_is_open(feature_repo, tmp_path):
    configure_base(feature_repo, DOCS_ON)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    env = agent.run(
        "submit-findings",
        "--file",
        _docs_findings_json(tmp_path, [_low_auto_fix("README.md")]),
    )
    assert env["state"] == "DOCS_GREEN"

    env = agent.run("stage", "run", "lint", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert [item["id"] for item in env["blocking"]] == ["F001"]
    assert env["stage"] == "docs"
    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")
    assert agent.run("status")["state"] == "DOCS_GREEN"


def test_mergeback_refuses_an_open_finding_without_an_internal_error(
    feature_repo, tmp_path, monkeypatch
):
    """A run that an earlier release advanced can still reach mergeback this way."""
    configure_base(feature_repo, DOCS_OFF)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"

    monkeypatch.chdir(feature_repo)
    session = runs.open_session()
    run_id = session.active_run_id()
    assert run_id is not None
    session.store.save_findings(
        run_id,
        [
            Finding(
                id="F001",
                stage=Stage.REVIEW,
                path="src/app.py",
                severity=Severity.LOW,
                action=FindingAction.AUTO_FIX,
                title="a small mechanical fix",
            )
        ],
    )

    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert [item["id"] for item in env["blocking"]] == ["F001"]
    assert env["next"]["command"] == "agentic-preflight abort"
    assert agent.run("status")["state"] == "TEST_GREEN"
    assert _notes(feature_repo) == ""


@pytest.mark.parametrize("severity", ["low", "medium"])
def test_a_clean_review_still_reaches_mergeback(feature_repo, tmp_path, severity):
    """A no-op finding needs no disposition, so the gate leaves it alone."""
    configure_base(feature_repo, DOCS_OFF)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    finding = {**_low_auto_fix(), "severity": severity, "action": "no_op"}
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, [finding]))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    env = agent.run("mergeback")

    assert env["state"] == "VERIFIED"
