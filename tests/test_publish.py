"""Quality gate, atomic push, finish, and garbage collection."""

import json
import subprocess
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


@pytest.fixture
def verified(feature_repo, bare_remote, tmp_path):
    """A run driven all the way to VERIFIED with a real remote configured."""
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    return agent


@pytest.fixture
def verified_with_cherry_picked_fix(feature_repo, bare_remote, tmp_path, monkeypatch):
    """A verified fix whose cherry-picked SHA deliberately differs."""
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    start = agent.run("start")
    run_id = start["run_id"]
    wt = Path(start["data"]["worktree_path"])
    agent.run("context")
    agent.run(
        "submit-findings",
        "--file",
        findings_json(
            tmp_path,
            [
                {
                    "path": "src/app.py",
                    "line": 1,
                    "severity": "high",
                    "action": "auto_fix",
                    "title": "use the loud flag",
                }
            ],
        ),
    )

    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")
    write(
        wt,
        "src/app.py",
        "def greet(name, loud=False):\n    return 'HI' if loud else f'hi {name}'\n",
    )
    original = commit_all(wt, "use the loud flag")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", original)
    agent.run("verify")
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")

    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T00:00:00+00:00")
    agent.run("mergeback")
    picked = git("rev-parse", "HEAD", cwd=feature_repo)
    assert original != picked
    return agent, run_id, wt, original, picked


def test_gate_mints_a_token_and_summarises_what_would_be_pushed(verified):
    env = verified.run("gate")
    assert env["state"] == "AWAITING_PUSH_CONFIRM"
    assert env["data"]["token"]
    assert env["data"]["remote"] == "origin"
    assert env["data"]["refspec"]
    assert env["data"]["commits"]


def test_the_gate_summary_names_the_branch_and_commit_subjects(verified):
    env = verified.run("gate")
    assert env["data"]["branch"] == "feature/x"
    subjects = json.dumps(env["data"]["commits"])
    assert "add loud flag" in subjects


def test_push_without_a_token_is_refused(verified):
    verified.run("gate")
    env = verified.run("push", expect=ExitCode.NEEDS_CONFIRM)
    assert env["error"]["code"] == "needs_confirm"


def test_push_with_a_wrong_token_is_refused(verified):
    verified.run("gate")
    env = verified.run("push", "--confirm", "not-the-token", expect=ExitCode.NEEDS_CONFIRM)
    assert env["error"]["code"] == "needs_confirm"


def test_push_with_the_right_token_succeeds(verified, feature_repo, bare_remote):
    token = verified.run("gate")["data"]["token"]
    env = verified.run("push", "--confirm", token)
    assert env["state"] == "PUSHED"
    assert env["next"]["command"] == "agentic-preflight finish"
    remote_sha = git("rev-parse", "feature/x", cwd=bare_remote)
    assert remote_sha == git("rev-parse", "HEAD", cwd=feature_repo)


def test_finish_closes_a_pushed_run(verified):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    env = verified.run("finish")
    assert env["state"] == "DONE"
    assert env["next"]["command"] == "agentic-preflight gc"
    assert verified.run("status")["data"]["has_run"] is False


def test_finish_is_illegal_before_push(verified):
    env = verified.run("finish", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_gate_is_illegal_before_everything_is_verified(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    env = agent.run("gate", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_manual_gate_mode_refuses_to_proceed_at_all(feature_repo, bare_remote, tmp_path):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[gate]\nmode = 'manual'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    agent.run("mergeback")

    env = agent.run("gate", expect=ExitCode.NEEDS_HUMAN)
    assert env["error"]["code"] == "manual_gate"
    assert "git push" in json.dumps(env["data"])


def test_human_review_path_allows_push_gate_and_marks_merge_review_requirement(
    feature_repo, bare_remote, tmp_path
):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[policy]\nhuman_review_paths = ['src/app.py']\n",
    )
    commit_all(feature_repo, "configure human review policy")
    agent = ScriptedAgent(feature_repo)
    start = agent.run("start")
    assert start["data"]["risk"]["verdict"] == "needs_human"
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    agent.run("mergeback")

    env = agent.run("gate")
    assert env["state"] == "AWAITING_PUSH_CONFIRM"
    assert env["data"]["token"]
    assert env["data"]["risk"]["verdict"] == "needs_human"
    assert env["data"]["risk"]["requires_human_review"] is True
    assert env["data"]["risk"]["reasons"][0]["path"] == "src/app.py"


def test_dry_run_push_changes_nothing(verified, bare_remote):
    token = verified.run("gate")["data"]["token"]
    env = verified.run("push", "--confirm", token, "--dry-run")
    assert env["data"]["dry_run"] is True
    result = subprocess.run(
        ["git", "rev-parse", "feature/x"], cwd=bare_remote, capture_output=True, text=True
    )
    assert result.returncode != 0


def test_gc_reclaims_a_finished_run_whose_fixes_were_cherry_picked(
    verified_with_cherry_picked_fix, feature_repo
):
    from agentic_preflight import gitx

    agent, run_id, wt, original, picked = verified_with_cherry_picked_fix
    assert gitx.commit_patch_id(feature_repo, original) == gitx.commit_patch_id(
        feature_repo, picked
    )

    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token)
    agent.run("finish")
    env = agent.run("gc")

    assert run_id in env["data"]["removed"]
    assert env["data"]["retained"] == []
    assert wt.exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "HEAD"
    assert git("branch", "--list", f"ap/{run_id}", cwd=feature_repo) == ""


def test_the_token_is_readable_from_status(verified):
    """The token is ceremony, not a security boundary."""
    token = verified.run("gate")["data"]["token"]
    assert verified.run("status")["data"]["gate_token"] == token
