"""M1 resolution loop: respond, fix-commit verification, abort, gc, events."""

import json

import pytest

from agentic_cli.envelope import ExitCode
from tests.conftest import git, write
from tests.driver import ScriptedAgent


@pytest.fixture
def agent(feature_repo):
    return ScriptedAgent(feature_repo)


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


BLOCKING = [{
    "path": "src/app.py", "line": 1, "severity": "high",
    "action": "auto_fix", "title": "loud flag is never used",
}]


@pytest.fixture
def blocked(agent, tmp_path):
    """A run parked in REVIEW_AWAITING_RESPONSES with F001 outstanding."""
    env = agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    return agent, env["data"]["worktree_path"]


def fix_commit(worktree_path, path="src/app.py", message="fix the flag"):
    write(worktree_path, path, "def greet(name, loud=False):\n    return 'HI' if loud else 'hi'\n")
    git("add", "-A", cwd=worktree_path)
    git("commit", "-m", message, cwd=worktree_path)
    return git("rev-parse", "HEAD", cwd=worktree_path)


# -- respond, happy path ----------------------------------------------------


def test_responding_fixed_with_a_real_commit_clears_the_finding(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    env = agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    assert env["state"] == "REVIEW_FIXING"
    assert env["data"]["finding"]["status"] == "fixed"
    assert env["data"]["finding"]["fix_commit"].startswith(sha[:8])


def test_verify_goes_green_once_nothing_blocks(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run("verify")
    assert env["state"] == "REVIEW_GREEN"


def test_the_fix_commit_is_recorded_on_the_run(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run("status")
    assert sha in env["data"]["fix_commits"]


def test_dismissing_a_finding_requires_a_note(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond", "--id", "F001", "--action", "dismissed",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "invalid_response"
    assert "note" in env["error"]["message"]


def test_dismissing_with_a_note_clears_the_finding(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond", "--id", "F001", "--action", "dismissed",
        "--note", "intentional: the flag ships next PR",
    )
    assert env["data"]["finding"]["status"] == "dismissed"
    agent.run("verify")


# -- respond, the claim is checked -----------------------------------------


def test_an_invented_commit_sha_is_rejected(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond", "--id", "F001", "--action", "fixed", "--commit", "0" * 40,
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "invalid_response"
    assert "does not exist" in env["error"]["message"]


def test_a_commit_that_does_not_touch_the_findings_file_is_rejected(blocked):
    """The agent's claim is verified against the commit, not taken on trust."""
    agent, wt = blocked
    write(wt, "README.md", "# demo\n\nunrelated edit\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "unrelated", cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt)

    env = agent.run(
        "respond", "--id", "F001", "--action", "fixed", "--commit", sha,
        expect=ExitCode.PRECONDITION,
    )
    assert "does not touch" in env["error"]["message"]
    assert "src/app.py" in env["error"]["message"]


def test_a_fix_commit_containing_a_copied_file_is_rejected(agent, feature_repo, tmp_path):
    """Guard 2 of the secret-containment pair, enforced at respond time.

    The .env must pre-date the run, so that `start` actually copies it — that is
    the situation the guard exists for.
    """
    write(feature_repo, ".env", "SECRET=hunter2\n")
    env = agent.run("start")
    wt = env["data"]["worktree_path"]
    assert env["data"]["copied_files"] == [".env"]

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))

    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'hi'\n")
    git("add", "-A", cwd=wt)
    git("add", "-f", ".env", cwd=wt)
    git("commit", "-m", "fix, and oops the env file", cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt)

    env = agent.run(
        "respond", "--id", "F001", "--action", "fixed", "--commit", sha,
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "copied_file_in_commit"
    assert ".env" in env["error"]["message"]


def test_copied_file_contents_never_appear_in_any_envelope(agent, feature_repo):
    """Copies are never read, logged, or echoed — only their names are carried."""
    write(feature_repo, ".env", "SECRET=hunter2\n")
    start_env = agent.run("start")
    ctx_env = agent.run("context")
    status_env = agent.run("status")

    for envelope in (start_env, ctx_env, status_env):
        assert "hunter2" not in json.dumps(envelope)


def test_fixed_without_a_commit_is_rejected(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond", "--id", "F001", "--action", "fixed",
        expect=ExitCode.PRECONDITION,
    )
    assert "--commit" in env["error"]["message"]


def test_an_unknown_finding_id_lists_the_valid_ids(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond", "--id", "F999", "--action", "dismissed", "--note", "x",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "unknown_finding"
    assert "F001" in env["error"]["message"]


def test_responding_twice_to_the_same_finding_is_rejected(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run(
        "respond", "--id", "F001", "--action", "dismissed", "--note", "x",
        expect=ExitCode.PRECONDITION,
    )
    assert "already" in env["error"]["message"]


def test_respond_is_illegal_before_findings_are_submitted(agent):
    agent.run("start")
    env = agent.run(
        "respond", "--id", "F001", "--action", "dismissed", "--note", "x",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "wrong_state"


# -- events and logs --------------------------------------------------------


def test_events_records_the_run_history(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("events")
    kinds = [e["event"] for e in env["data"]["events"]]
    assert "run_created" in kinds
    assert "findings_submitted" in kinds
    assert "finding_resolved" in kinds


def test_events_are_ordered_oldest_first(blocked):
    agent, _ = blocked
    env = agent.run("events")
    assert env["data"]["events"][0]["event"] == "run_created"


# -- abort ------------------------------------------------------------------


def test_abort_ends_the_run_and_removes_the_worktree(blocked):
    agent, wt = blocked
    env = agent.run("abort")
    assert env["state"] == "ABORTED"
    from pathlib import Path
    assert not Path(wt).exists()


def test_abort_clears_the_current_pointer(blocked):
    agent, _ = blocked
    agent.run("abort")
    env = agent.run("status")
    assert env["data"]["has_run"] is False


def test_abort_warns_when_fix_commits_would_be_lost(blocked):
    """Unmerged work is reported, never silently discarded."""
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)
    assert env["error"]["code"] == "unmerged_work"
    assert sha in env["error"]["message"]


def test_abort_force_discards_unmerged_work(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run("abort", "--force")
    assert env["state"] == "ABORTED"


def test_abort_is_legal_with_no_run(feature_repo):
    agent = ScriptedAgent(feature_repo)
    env = agent.run("abort", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "no_run"


# -- gc ---------------------------------------------------------------------


def test_gc_reports_a_clean_store_as_nothing_to_do(agent):
    agent.run("start")
    env = agent.run("gc")
    assert env["data"]["removed"] == []


def test_gc_reclaims_an_aborted_runs_leftovers(blocked):
    agent, _ = blocked
    agent.run("abort")
    env = agent.run("gc")
    assert env["ok"] is True


def test_gc_never_deletes_unmerged_work_without_force(blocked, feature_repo):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("gc")
    assert env["data"]["removed"] == []
    assert env["data"]["retained"]
    from pathlib import Path
    assert Path(wt).exists()


def test_gc_reconciles_a_worktree_with_no_run_directory(agent, feature_repo):
    """An orphan: git knows about the worktree, the store no longer does."""
    import shutil
    from pathlib import Path

    env = agent.run("start")
    run_id = env["run_id"]
    state_root = Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo)
    ) / "agentic-cli"

    shutil.rmtree(state_root / "runs" / run_id)
    (state_root / "current").unlink()

    env = agent.run("gc")
    assert run_id in env["data"]["orphans"]
