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
from pathlib import Path

import pytest

from agentic_preflight import runs
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import Finding, FindingAction, Severity, Stage
from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent
from tests.driver import findings_json as review_findings_json

# `agentic_preflight.runs` re-exports a `mergeback` function under the module's name.
mergeback_module = importlib.import_module("agentic_preflight.runs.mergeback")
fix_commits_module = importlib.import_module("agentic_preflight.runs.fix_commits")

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


def test_a_run_left_in_lint_green_is_told_to_abort(feature_repo, tmp_path, monkeypatch):
    """An earlier release could leave a run between lint and test with an open finding."""
    configure_base(feature_repo, DOCS_OFF)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"
    _inject(feature_repo, monkeypatch, _open("F001"))

    assert agent.run("status")["next"]["command"] == "agentic-preflight abort"
    env = agent.run("stage", "run", "test", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert env["next"]["command"] == "agentic-preflight abort"
    assert agent.run("status")["state"] == "LINT_GREEN"


def test_an_unregistered_fix_commit_survives_the_refusal(feature_repo, tmp_path, monkeypatch):
    """A commit made after the last `respond` is on the branch but not yet recorded.

    Merge-back registers it only after the refusal point, so the refusal names it and
    records it; a plain `abort` must then stop instead of deleting the branch.
    """
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
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
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED AGAIN'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "later fix", cwd=wt)
    later = git("rev-parse", "HEAD", cwd=wt)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    assert agent.run("status")["data"]["fix_commits"] == [fix]
    _inject(feature_repo, monkeypatch, _open("F002"))

    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert env["data"]["fix_commits"] == [fix, later]
    assert "before you abort" in env["next"]["instruction"]
    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)
    assert refused["error"]["code"] == "unmerged_work"
    assert refused["data"]["fix_commits"] == [fix, later]
    assert git("rev-parse", "HEAD", cwd=wt) == later


def test_status_keeps_the_conflict_report_when_it_points_to_abort(
    feature_repo, tmp_path, monkeypatch
):
    """A conflicted run still needs the report of the paths its source branch holds."""
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    run_id = agent.run("status")["run_id"]
    agent.run("context")
    high = {**_low_auto_fix(), "severity": "high"}
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, [high]))
    write(wt, "src/app.py", "WORKTREE CONTENT\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "worktree rewrite", cwd=wt)
    agent.run(
        "respond", "--id", "F001", "--action", "fixed", "--commit", git("rev-parse", "HEAD", cwd=wt)
    )
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    write(feature_repo, "src/app.py", "BRANCH CONTENT\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "-m", "branch rewrite", cwd=feature_repo)
    # Re-point the recorded source head so the staleness guard does not fire first.
    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )
    run_path = state_root / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    doc["source_head_sha"] = git("rev-parse", "HEAD", cwd=feature_repo)
    run_path.write_text(json.dumps(doc))
    assert agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)["state"] == "MERGEBACK_CONFLICT"
    _inject(feature_repo, monkeypatch, _open("F002"))

    status = agent.run("status")

    assert status["next"]["command"] == "agentic-preflight abort"
    assert status["data"]["mergeback_conflict"]["conflicting_files"] == ["src/app.py"]


def test_the_moved_branch_check_reads_the_source_branch_not_the_checkout(
    feature_repo, tmp_path, monkeypatch
):
    """Another branch checked out at the old tip must not hide that the source branch moved."""
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    high = {**_low_auto_fix(), "severity": "high"}
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, [high]))
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "fix", cwd=wt)
    agent.run(
        "respond", "--id", "F001", "--action", "fixed", "--commit", git("rev-parse", "HEAD", cwd=wt)
    )
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    _inject(feature_repo, monkeypatch, _open("F002"))
    with monkeypatch.context() as patch:
        patch.setattr(mergeback_module, "require_resolved_findings", lambda *a, **k: None)
        agent.run("mergeback", expect=ExitCode.ERROR)
    source_sha = agent.run("status")["data"]["mergeback_attempt"]["source_sha"]
    git("switch", "-q", "-c", "elsewhere", source_sha, cwd=feature_repo)

    instruction = agent.run("status")["next"]["instruction"]

    assert "already moved your branch" in instruction


def _stuck_at_test_green_with_a_new_commit(feature_repo, tmp_path, monkeypatch):
    """A run an earlier release left at TEST_GREEN, whose agent then committed a fix."""
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    _inject(feature_repo, monkeypatch, _open("F001"))
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "fix after the tests", cwd=wt)
    return agent, wt, git("rev-parse", "HEAD", cwd=wt)


def test_abort_after_status_keeps_an_unregistered_fix_commit(feature_repo, tmp_path, monkeypatch):
    """Following `status` straight to `abort` must not delete a commit nothing recorded."""
    agent, wt, fix = _stuck_at_test_green_with_a_new_commit(feature_repo, tmp_path, monkeypatch)

    status = agent.run("status")
    assert status["next"]["command"] == "agentic-preflight abort"
    assert status["data"]["fix_commits"] == [fix]
    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert refused["data"]["fix_commits"] == [fix]
    assert git("rev-parse", "HEAD", cwd=wt) == fix


def test_mergeback_reopens_review_for_a_changed_run_instead_of_refusing(
    feature_repo, tmp_path, monkeypatch
):
    """Back in review, `respond` is legal again, so the run need not be aborted."""
    agent, _wt, _fix = _stuck_at_test_green_with_a_new_commit(feature_repo, tmp_path, monkeypatch)

    env = agent.run("mergeback")

    assert env["state"].startswith("REVIEW")
    assert env["error"] is None


def test_lint_refuses_a_review_finding_left_open_at_docs_green(feature_repo, tmp_path, monkeypatch):
    """Evidence reuse can carry a run from REVIEW_GREEN to DOCS_GREEN past the docs guard."""
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    assert agent.run("submit-findings", "--file", _docs_findings_json(tmp_path, []))["state"] == (
        "DOCS_GREEN"
    )
    _inject(feature_repo, monkeypatch, _open("F009"))

    env = agent.run("stage", "run", "lint", expect=ExitCode.PRECONDITION)

    assert env["error"]["code"] == "open_findings"
    assert [item["id"] for item in env["blocking"]] == ["F009"]
    assert "respond --id F009" in env["next"]["command"]


def test_abort_keeps_recorded_fix_commits_when_the_checkout_is_detached(feature_repo, tmp_path):
    """A detached or reset checkout must not hide a recorded fix from `abort`."""
    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    head = git("rev-parse", "HEAD", cwd=wt)
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
    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"
    git("checkout", "-q", "--detach", head, cwd=wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert fix in refused["data"]["fix_commits"]


def test_an_unreadable_branch_history_never_blocks_status_or_abort_force(
    feature_repo, tmp_path, monkeypatch
):
    """`status` and `abort --force` are the way out, so a failed derivation falls back."""
    from agentic_preflight import worktree

    configure_base(feature_repo, f"{DOCS_OFF}\n[worktree]\nmode = 'strict'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", review_findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    _inject(feature_repo, monkeypatch, _open("F001"))

    def unreadable(*_args, **_kwargs):
        raise worktree.CopiedFileInCommit("commit deadbeef touches copied file .env")

    monkeypatch.setattr(fix_commits_module, "derive_fix_commits", unreadable)

    assert agent.run("status")["next"]["command"] == "agentic-preflight abort"
    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_abort_force_reports_the_unregistered_commits_it_discards(
    feature_repo, tmp_path, monkeypatch
):
    agent, _wt, fix = _stuck_at_test_green_with_a_new_commit(feature_repo, tmp_path, monkeypatch)

    env = agent.run("abort", "--force")

    assert env["state"] == "ABORTED"
    assert env["data"]["discarded_fix_commits"] == [fix]
