"""`abort` must not delete validation-branch commits that nothing registered.

In isolated worktree modes, `respond` and a red stage's restart register fix commits,
and merge-back registers the whole validated history at `TEST_GREEN`. A commit made
outside those points lives only on the validation branch, which `abort` deletes.
"""

from __future__ import annotations

import importlib
import json

from agentic_preflight import worktree
from agentic_preflight.envelope import ExitCode
from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

fix_commits_module = importlib.import_module("agentic_preflight.runs.fix_commits")

STRICT = (
    "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n\n"
    "[worktree]\nmode = 'strict'\n"
)


def _commit(wt, text: str, message: str) -> str:
    write(wt, "src/app.py", f"def greet(name, loud=False):\n    return {text!r}\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", message, cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt)


def _at_test_green(feature_repo, tmp_path) -> tuple[ScriptedAgent, str]:
    configure_base(feature_repo, STRICT)
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    return agent, wt


def test_abort_refuses_to_delete_a_commit_nothing_registered(feature_repo, tmp_path):
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "fix after the tests")
    assert agent.run("status")["data"]["fix_commits"] == []

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert refused["data"]["fix_commits"] == [fix]
    assert git("rev-parse", "HEAD", cwd=wt) == fix


def test_abort_force_reports_the_unregistered_commit_it_discards(feature_repo, tmp_path):
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "fix after the tests")

    env = agent.run("abort", "--force")

    assert env["state"] == "ABORTED"
    assert env["data"]["discarded_fix_commits"] == [fix]


def test_abort_keeps_a_recorded_fix_when_the_checkout_is_detached(feature_repo, tmp_path):
    """A detached or reset checkout must not hide a recorded fix from `abort`."""
    configure_base(feature_repo, STRICT)
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    head = git("rev-parse", "HEAD", cwd=wt)
    agent.run("context")
    finding = {
        "path": "src/app.py",
        "severity": "high",
        "action": "auto_fix",
        "title": "a real defect",
    }
    agent.run("submit-findings", "--file", findings_json(tmp_path, [finding]))
    fix = _commit(wt, "FIXED", "fix")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"
    git("checkout", "-q", "--detach", head, cwd=wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert fix in refused["data"]["fix_commits"]


def test_an_unreadable_history_never_blocks_abort_force(feature_repo, tmp_path, monkeypatch):
    agent, wt = _at_test_green(feature_repo, tmp_path)
    _commit(wt, "FIXED", "fix after the tests")

    def unreadable(*_args, **_kwargs):
        raise worktree.CopiedFileInCommit("commit deadbeef touches copied file .env")

    monkeypatch.setattr(fix_commits_module, "derive_fix_commits", unreadable)

    # The recorded list is empty, so only --force is needed, and it is never blocked.
    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_an_in_place_abort_is_unchanged(feature_repo, tmp_path):
    configure_base(feature_repo, STRICT.replace("'strict'", "'in_place'"))
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run("abort")

    assert env["state"] == "ABORTED"
    assert json.loads(json.dumps(env["data"]))["discarded_fix_commits"] == []
