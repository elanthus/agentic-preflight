"""Isolated-mode fix commits come from the validated history, not respond citations."""

from __future__ import annotations

import pytest

from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent, findings_json


def _finding(title: str) -> dict:
    return {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": title,
    }


def _start(feature_repo, mode: str = "strict") -> tuple[ScriptedAgent, str]:
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        f"\n[worktree]\nmode = '{mode}'\n",
    )
    agent = ScriptedAgent(feature_repo)
    env = agent.run("start")
    wt = env["data"]["worktree_path"]
    agent.run("context")
    return agent, wt


def _commit(wt: str, path: str, content: str, message: str) -> str:
    write(wt, path, content)
    git("add", "-A", cwd=wt)
    git("commit", "-m", message, cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


def _finish_and_merge(agent: ScriptedAgent, tmp_path) -> dict:
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    return agent.run("mergeback")


def _tree(path) -> str:
    return git("rev-parse", "HEAD^{tree}", cwd=path)


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_responses_out_of_commit_order_still_merge_back(feature_repo, tmp_path, mode):
    agent, wt = _start(feature_repo, mode)
    agent.run(
        "submit-findings",
        "--file",
        findings_json(tmp_path, [_finding("first problem"), _finding("second problem")]),
    )
    x = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'X'\n", "fix F002")
    y = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'Y'\n", "fix F001")

    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", y)
    env = agent.run("respond", "--id", "F002", "--action", "fixed", "--commit", x)
    assert env["data"]["finding"]["fix_commit"] == x

    assert agent.run("status")["data"]["fix_commits"] == [x, y]
    validated_tree = _tree(wt)
    merged = _finish_and_merge(agent, tmp_path)

    assert merged["state"] == "VERIFIED"
    assert merged["data"]["tree_equivalent"] is True
    assert _tree(feature_repo) == validated_tree


def test_an_uncited_regression_test_commit_is_merged_back(feature_repo, tmp_path):
    agent, wt = _start(feature_repo)
    agent.run("submit-findings", "--file", findings_json(tmp_path, [_finding("needs a fix")]))
    test_commit = _commit(wt, "tests/test_app.py", "def test_greet():\n    pass\n", "add test")
    fix = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n", "fix")

    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)

    assert agent.run("status")["data"]["fix_commits"] == [test_commit, fix]
    validated_tree = _tree(wt)
    merged = _finish_and_merge(agent, tmp_path)

    assert merged["state"] == "VERIFIED"
    assert merged["data"]["tree_equivalent"] is True
    assert len(merged["data"]["applied"]) == 2
    assert _tree(feature_repo) == validated_tree
    assert (feature_repo / "tests/test_app.py").is_file()


def test_a_commit_made_after_the_last_respond_is_merged_back(feature_repo, tmp_path):
    """A repair revalidated by a fresh review is replayed even without a new response."""
    agent, wt = _start(feature_repo)
    agent.run("submit-findings", "--file", findings_json(tmp_path, [_finding("needs a fix")]))
    fix = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n", "fix")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run("verify")
    later = _commit(wt, "README.md", "more docs\n", "document the fix")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    validated_tree = _tree(wt)

    merged = agent.run("mergeback")

    assert merged["state"] == "VERIFIED"
    assert merged["data"]["tree_equivalent"] is True
    assert agent.run("status")["data"]["fix_commits"] == [fix, later]
    assert _tree(feature_repo) == validated_tree


@pytest.mark.parametrize("mode", ["strict", "in_place"])
def test_a_repair_committed_after_an_interrupted_stage_is_registered(
    feature_repo, tmp_path, monkeypatch, mode
):
    agent, wt = _start(feature_repo, mode)
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"

    from agentic_preflight.runs import stages as stagesmod

    def killed(*args, **kwargs):
        raise RuntimeError("stage process killed")

    # The stage begins (LINT_RUNNING is persisted), then dies before its result lands.
    with monkeypatch.context() as fault:
        fault.setattr(stagesmod, "_persist_stage_result", killed)
        agent.run("stage", "run", "lint", expect=1)
    assert agent.run("status")["state"] == "LINT_RUNNING"
    repair = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'R'\n", "repair")

    env = agent.run("stage", "run", "lint")

    assert env["data"]["validation_restarted"] is True
    assert repair in agent.run("status")["data"]["fix_commits"]


def test_an_empty_commit_in_the_validation_history_is_not_replayed(feature_repo, tmp_path):
    agent, wt = _start(feature_repo)
    agent.run("submit-findings", "--file", findings_json(tmp_path, [_finding("needs a fix")]))
    git("commit", "--allow-empty", "-m", "empty marker", cwd=wt)
    fix = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n", "fix")
    git("commit", "--allow-empty", "-m", "another empty marker", cwd=wt)

    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)

    assert agent.run("status")["data"]["fix_commits"] == [fix]
    validated_tree = _tree(wt)
    merged = _finish_and_merge(agent, tmp_path)

    assert merged["state"] == "VERIFIED"
    assert merged["data"]["tree_equivalent"] is True
    assert merged["data"]["applied"] == [fix]
    assert _tree(feature_repo) == validated_tree


def test_a_merge_in_the_validation_history_is_refused_with_a_recovery(feature_repo, tmp_path):
    agent, wt = _start(feature_repo)
    agent.run("submit-findings", "--file", findings_json(tmp_path, [_finding("needs a fix")]))
    reviewed = git("rev-parse", "HEAD", cwd=wt)
    git("switch", "-c", "side", cwd=wt)
    _commit(wt, "notes.txt", "side work\n", "side work")
    git("switch", "-", cwd=wt)
    fix = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n", "fix")
    git("merge", "--no-ff", "-m", "merge side", "side", cwd=wt)
    merge = git("rev-parse", "HEAD", cwd=wt)

    refused = agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix, expect=3)

    assert refused["error"]["code"] == "nonlinear_fix_history"
    assert refused["data"]["merge_commits"] == [merge]
    assert reviewed in refused["next"]["command"]
    assert agent.run("status")["data"]["fix_commits"] == []

    git("rebase", reviewed, cwd=wt)
    fix = git("log", "-1", "--format=%H", "--grep=^fix$", cwd=wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    validated_tree = _tree(wt)
    merged = _finish_and_merge(agent, tmp_path)

    assert merged["state"] == "VERIFIED"
    assert _tree(feature_repo) == validated_tree


def test_in_place_registration_keeps_an_empty_commit_so_the_head_advances(
    feature_repo, tmp_path, monkeypatch
):
    """Only isolated replay drops empty commits; in place, the recorded head is HEAD."""
    agent, wt = _start(feature_repo, "in_place")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    from agentic_preflight.runs import stages as stagesmod

    def killed(*args, **kwargs):
        raise RuntimeError("stage process killed")

    with monkeypatch.context() as fault:
        fault.setattr(stagesmod, "_persist_stage_result", killed)
        agent.run("stage", "run", "lint", expect=1)
    repair = _commit(wt, "src/app.py", "def greet(name, loud=False):\n    return 'R'\n", "repair")
    git("commit", "--allow-empty", "-m", "empty marker", cwd=wt)
    marker = git("rev-parse", "HEAD", cwd=wt)

    assert agent.run("stage", "run", "lint")["data"]["validation_restarted"] is True

    status = agent.run("status")["data"]
    assert status["fix_commits"] == [repair, marker]
    assert status["head_sha"] == marker
