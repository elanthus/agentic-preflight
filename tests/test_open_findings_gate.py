"""An open auto-fix finding must get a disposition before the run leaves its stage.

`respond` is legal only in the review and docs states, and an attestation refuses an
open auto-fix finding. Before this gate a low-severity auto-fix finding left the review
stage green, the run reached `mergeback`, and `mergeback` failed with an internal error
from a state it could not leave.
"""

from __future__ import annotations

import importlib
import json
import subprocess

import pytest

from agentic_preflight import runs
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent
from tests.driver import findings_json as review_findings_json

# `agentic_preflight.runs` re-exports a `mergeback` function under the module's name.
mergeback_module = importlib.import_module("agentic_preflight.runs.mergeback")

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


def _inject(feature_repo, monkeypatch, *findings: Finding) -> None:
    """Add findings to the active run, as a run that an earlier release advanced holds."""
    with monkeypatch.context() as patch:
        patch.chdir(feature_repo)
        session = runs.open_session()
        run_id = session.active_run_id()
        assert run_id is not None
        session.store.save_findings(run_id, [*session.store.load_findings(run_id), *findings])


def _open(fid: str, *, action=FindingAction.AUTO_FIX, severity=Severity.LOW) -> Finding:
    return Finding(
        id=fid,
        stage=Stage.REVIEW,
        path="src/app.py",
        severity=severity,
        action=action,
        title="left open by an earlier release",
    )


def test_status_points_to_respond_after_a_stage_guard_refuses(feature_repo, tmp_path):
    """The exit-3 recovery rule (`status`, then obey `next`) must not loop."""
    agent = _review_green_with_open_finding(feature_repo, tmp_path)
    agent.run("context", "--section", "docs", expect=ExitCode.PRECONDITION)

    env = agent.run("status")

    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")


def test_a_blocking_finding_at_the_guard_gets_the_blocking_instruction(
    feature_repo, tmp_path, monkeypatch
):
    configure_base(feature_repo, DOCS_ON)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    _inject(feature_repo, monkeypatch, _open("F001", action=FindingAction.ASK_USER))

    env = agent.run("context", "--section", "docs", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert "blocking finding" in env["next"]["instruction"]


def test_verify_does_not_skip_docs_past_an_open_auto_fix_finding(feature_repo, tmp_path):
    configure_base(feature_repo, DOCS_OFF)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    ask = {**_low_auto_fix(), "severity": "high", "action": "ask_user", "title": "intended?"}
    low = {**_low_auto_fix(), "line": 2}
    env = agent.run("submit-findings", "--file", review_findings_json(tmp_path, [ask, low]))
    assert env["state"] == "REVIEW_BLOCKED"
    agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "accepted",
        "--reason",
        "accepted_risk",
        "--note",
        "the owner confirmed the API",
    )

    env = agent.run("verify")

    assert env["state"] == "REVIEW_GREEN"
    assert env["next"]["command"].startswith("agentic-preflight respond --id F002")


def test_status_points_to_abort_after_mergeback_refuses(feature_repo, tmp_path, monkeypatch):
    configure_base(feature_repo, DOCS_OFF)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    _inject(feature_repo, monkeypatch, _open("F001"))
    agent.run("mergeback", expect=ExitCode.PRECONDITION)

    env = agent.run("status")

    assert env["next"]["command"] == "agentic-preflight abort"


def test_mergeback_from_review_green_is_still_a_wrong_state(feature_repo, tmp_path):
    """`respond` can still repair this run, so mergeback must not tell it to abort."""
    agent = _review_green_with_open_finding(feature_repo, tmp_path)

    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "wrong_state"


@pytest.mark.parametrize("mode", ["strict", "in_place"])
def test_a_run_stuck_by_the_old_crash_gets_correct_recovery_advice(
    feature_repo, tmp_path, monkeypatch, mode
):
    """Reproduce the old crash, which left the run in MERGEBACK_PENDING after merging."""
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = '{mode}'\n")
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    high = {**_low_auto_fix(), "severity": "high"}
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, [high]))
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "fix", cwd=wt)
    fix = git("rev-parse", "HEAD", cwd=wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    assert agent.run("status")["data"]["fix_commits"] == [fix]
    _inject(feature_repo, monkeypatch, _open("F002"))

    with monkeypatch.context() as patch:
        patch.setattr(mergeback_module, "require_resolved_findings", lambda *a, **k: None)
        crashed = agent.run("mergeback", expect=ExitCode.ERROR)
    assert crashed["error"]["code"] == "internal_error"
    assert agent.run("status")["state"] == "MERGEBACK_PENDING"

    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert env["next"]["command"] == "agentic-preflight abort"
    assert env["data"]["fix_commits"] == [fix]
    instruction = env["next"]["instruction"]
    if mode == "strict":
        assert "already moved your branch" in instruction
        assert "before you abort" not in instruction
    else:
        assert "cherry-pick" not in instruction
    assert agent.run("status")["next"]["command"] == "agentic-preflight abort"
