"""`abort` must not delete validation-branch commits that nothing registered.

In isolated worktree modes, `respond` and a red stage's restart register fix commits,
and merge-back registers the whole validated history at `TEST_GREEN`. A commit made
outside those points lives only on the validation branch, which `abort` deletes.
"""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

from agentic_preflight import worktree
from agentic_preflight.envelope import ExitCode
from agentic_preflight.gitx import GitError
from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

fix_commits_module = importlib.import_module("agentic_preflight.runs.fix_commits")
lifecycle_module = importlib.import_module("agentic_preflight.runs.lifecycle")

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
    # The branch, not just the worktree, holds them, and abort deletes both.
    assert "validation branch" in refused["next"]["instruction"]
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
    fix = _commit(wt, "FIXED", "fix after the tests")

    def unreadable(*_args, **_kwargs):
        raise worktree.CopiedFileInCommit("commit deadbeef touches copied file .env")

    monkeypatch.setattr(fix_commits_module, "derive_fix_commits", unreadable)

    # Nothing is recorded, so only --force is needed, and it is never blocked.
    env = agent.run("abort", "--force")
    assert env["state"] == "ABORTED"
    # The best-effort listing still names the discarded commit.
    assert env["data"]["discarded_fix_commits"] == [fix]


def test_an_unexpected_failure_never_blocks_abort_force(feature_repo, tmp_path, monkeypatch):
    """A failure that derivation does not handle still lets --force through."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "fix after the tests")

    def broken(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(lifecycle_module, "branch_fix_commits", broken)

    env = agent.run("abort", "--force")

    assert env["state"] == "ABORTED"
    assert env["data"]["discarded_fix_commits"] == [fix]


def test_an_in_place_abort_is_unchanged(feature_repo, tmp_path):
    configure_base(feature_repo, STRICT.replace("'strict'", "'in_place'"))
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))

    env = agent.run("abort")

    assert env["state"] == "ABORTED"
    assert env["data"]["discarded_fix_commits"] == []


def test_abort_protects_a_commit_made_in_review_before_respond(feature_repo, tmp_path):
    """The window between committing a fix and answering it is the usual case."""
    configure_base(feature_repo, STRICT)
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    finding = {"path": "src/app.py", "severity": "high", "action": "auto_fix", "title": "defect"}
    blocked = agent.run("submit-findings", "--file", findings_json(tmp_path, [finding]))
    assert blocked["state"] == "REVIEW_BLOCKED"
    fix = _commit(wt, "FIXED", "fix not yet answered")

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert refused["data"]["fix_commits"] == [fix]


def test_abort_reads_the_branch_when_the_checkout_is_detached(feature_repo, tmp_path):
    """Release deletes the branch, so a detached checkout must not hide its commits."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=wt)
    fix = _commit(wt, "FIXED", "fix after the tests")
    git("checkout", "-q", "--detach", head, cwd=wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["data"]["fix_commits"] == [fix]


def test_an_unreadable_history_stops_a_plain_abort(feature_repo, tmp_path):
    """A merge on the validation branch cannot be derived; it may still hold work."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=wt)
    git("switch", "-q", "-c", "side", cwd=wt)
    _commit(wt, "SIDE", "side work")
    git("switch", "-q", "-", cwd=wt)
    write(wt, "README.md", "work on the validation branch\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=wt)
    git("branch", "-q", "-D", "side", cwd=wt)
    assert git("rev-parse", "HEAD", cwd=wt) != head

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert "cannot be read" in refused["error"]["message"]
    assert refused["next"]["command"] == "agentic-preflight abort --force"
    work_and_merge = git("rev-list", f"{head}..HEAD", cwd=wt).split()
    # The refusal names what the agent should check before forcing.
    assert sorted(refused["data"]["fix_commits"]) == sorted(work_and_merge)
    forced = agent.run("abort", "--force")
    assert forced["state"] == "ABORTED"
    # The report still names every discarded commit, so it can be recovered.
    assert sorted(forced["data"]["discarded_fix_commits"]) == sorted(work_and_merge)


def test_a_merge_on_the_branch_stops_abort_when_the_checkout_is_gone(feature_repo, tmp_path):
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("switch", "-q", "-c", "side", cwd=wt)
    _commit(wt, "SIDE", "side work")
    git("switch", "-q", "-", cwd=wt)
    write(wt, "README.md", "work on the validation branch\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=wt)
    git("branch", "-q", "-D", "side", cwd=wt)
    shutil.rmtree(wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert "cannot be read" in refused["error"]["message"]


def test_the_aborted_event_records_what_force_discarded(feature_repo, tmp_path):
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "fix after the tests")

    env = agent.run("abort", "--force")

    events_file = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
        / "runs"
        / env["run_id"]
        / "events.jsonl"
    )
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    aborted = next(event for event in events if event.get("event") == "aborted")
    assert aborted["discarded_fix_commits"] == [fix]


def test_abort_reads_the_branch_when_the_checkout_is_gone(feature_repo, tmp_path):
    """The branch ref lives in the shared repository and outlives a deleted checkout."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "fix after the tests")
    shutil.rmtree(wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["data"]["fix_commits"] == [fix]
    assert agent.run("abort", "--force")["state"] == "ABORTED"


def test_abort_force_names_the_branch_commits_when_the_checkout_is_gone(feature_repo, tmp_path):
    """With the checkout gone and a merge on the branch, the report reads the branch ref."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=wt)
    git("switch", "-q", "-c", "side", cwd=wt)
    _commit(wt, "SIDE", "side work")
    git("switch", "-q", "-", cwd=wt)
    write(wt, "README.md", "work on the validation branch\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=wt)
    git("branch", "-q", "-D", "side", cwd=wt)
    on_branch = git("rev-list", f"{head}..HEAD", cwd=wt).split()
    shutil.rmtree(wt)

    forced = agent.run("abort", "--force")

    assert forced["state"] == "ABORTED"
    assert sorted(forced["data"]["discarded_fix_commits"]) == sorted(on_branch)


def test_a_checkout_detached_at_an_older_commit_does_not_block_abort(feature_repo, tmp_path):
    """Nothing after the reviewed head is on HEAD or the branch, so nothing is at risk."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("checkout", "-q", "--detach", "HEAD~1", cwd=wt)

    env = agent.run("abort")

    assert env["state"] == "ABORTED"


def test_an_empty_commit_on_the_branch_is_not_work(feature_repo, tmp_path):
    """Empty commits are left out whether or not the checkout still exists."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "nothing", cwd=wt)
    shutil.rmtree(wt)

    env = agent.run("abort")

    assert env["state"] == "ABORTED"


def test_an_empty_commit_alone_does_not_block_abort_on_an_unreadable_history(
    feature_repo, tmp_path
):
    """A branch that no longer descends from the reviewed head is listed plainly."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("reset", "-q", "--hard", "HEAD~1", cwd=wt)
    git("commit", "-q", "--allow-empty", "-m", "nothing", cwd=wt)

    env = agent.run("abort")

    assert env["state"] == "ABORTED"


def test_a_missing_reusable_runner_keeps_its_branch(feature_repo, tmp_path):
    """Releasing a missing runner leaves the branch, so its commits are not at risk."""
    configure_base(feature_repo, STRICT.replace("'strict'", "'reusable'"))
    agent = ScriptedAgent(feature_repo)
    wt = agent.run("start")["data"]["worktree_path"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    branch = git("branch", "--show-current", cwd=wt)
    fix = _commit(wt, "FIXED", "fix after the tests")
    shutil.rmtree(wt)

    env = agent.run("abort")

    assert env["state"] == "ABORTED"
    assert git("rev-parse", f"refs/heads/{branch}", cwd=feature_repo) == fix


def test_an_in_place_abort_with_a_commit_is_unchanged(feature_repo, tmp_path):
    """In place, the commit is on the source branch, which abort keeps."""
    configure_base(feature_repo, STRICT.replace("'strict'", "'in_place'"))
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    fix = _commit(feature_repo, "FIXED", "commit in place")

    env = agent.run("abort")

    assert env["state"] == "ABORTED"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == fix


def test_a_checkout_git_cannot_open_stops_a_plain_abort(feature_repo, tmp_path):
    """A missing worktree entry must not read as "nothing follows the reviewed head"."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    fix = _commit(wt, "FIXED", "real work")
    # As after `git worktree prune`: the checkout and its .git file remain.
    shutil.rmtree(git("rev-parse", "--absolute-git-dir", cwd=wt))

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    # The branch ref lives in the shared repository, so it is still listed.
    assert fix in refused["data"]["fix_commits"]
    forced = agent.run("abort", "--force")
    assert fix in forced["data"]["discarded_fix_commits"]


def test_a_root_commit_on_the_branch_is_still_listed(feature_repo, tmp_path):
    """A commit without a parent cannot be compared with one, so it counts as work."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    branch = git("branch", "--show-current", cwd=wt)
    git("checkout", "-q", "--orphan", "unrelated", cwd=wt)
    git("commit", "-q", "-m", "unrelated root", cwd=wt)
    _commit(wt, "MORE", "more work")
    git("branch", "-q", "-M", "unrelated", branch, cwd=wt)
    on_branch = git("rev-list", "HEAD", cwd=wt).split()

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)
    assert sorted(refused["data"]["fix_commits"]) == sorted(on_branch)
    forced = agent.run("abort", "--force")
    assert sorted(forced["data"]["discarded_fix_commits"]) == sorted(on_branch)


def test_only_a_missing_ref_reads_as_an_absent_branch(feature_repo, tmp_path):
    """Both branch readers treat any other failure as unreadable, never as empty."""
    tip_exists = fix_commits_module._tip_exists
    assert tip_exists(str(feature_repo), "HEAD")
    assert not tip_exists(str(feature_repo), "refs/heads/no-such-branch")
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    with pytest.raises(GitError):
        tip_exists(str(outside), "HEAD")


def test_a_detached_head_is_reported_when_the_checkout_cannot_be_opened(feature_repo, tmp_path):
    """The shared repository still records the worktree's HEAD."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    on_branch = _commit(wt, "ON BRANCH", "work on the branch")
    git("checkout", "-q", "--detach", cwd=wt)
    detached = _commit(wt, "DETACHED", "work on a detached HEAD")
    (Path(wt) / ".git").unlink()

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert {on_branch, detached} <= set(refused["data"]["fix_commits"])
    forced = agent.run("abort", "--force")
    assert {on_branch, detached} <= set(forced["data"]["discarded_fix_commits"])


def test_an_unreadable_history_and_uncommitted_changes_are_named_together(feature_repo, tmp_path):
    """One refusal names both, so a following --force discards nothing unnamed."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("switch", "-q", "-c", "side", cwd=wt)
    _commit(wt, "SIDE", "side work")
    git("switch", "-q", "-", cwd=wt)
    write(wt, "README.md", "work on the validation branch\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "work", cwd=wt)
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=wt)
    write(wt, "notes.txt", "not committed\n")

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert "cannot be read" in refused["error"]["message"]
    assert refused["data"]["fix_commits"]
    assert refused["data"]["uncommitted_paths"] == ["notes.txt"]


def test_a_detached_head_counts_when_the_checkout_directory_is_gone(feature_repo, tmp_path):
    """The shared repository records the worktree's HEAD until release prunes it."""
    agent, wt = _at_test_green(feature_repo, tmp_path)
    git("checkout", "-q", "--detach", cwd=wt)
    detached = _commit(wt, "DETACHED", "work on a detached HEAD")
    shutil.rmtree(wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["data"]["fix_commits"] == [detached]
