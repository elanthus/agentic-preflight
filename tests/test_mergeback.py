"""M4 merge-back: cherry-pick, the clean-abort contract, tree-equivalence."""

import json
import subprocess

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


BLOCKING = [{
    "path": "src/app.py", "line": 1, "severity": "high",
    "action": "auto_fix", "title": "needs a fix",
}]


@pytest.fixture
def ready(feature_repo, tmp_path):
    """Drive a run to LINT_GREEN, optionally with a fix commit in the worktree."""

    def build(*, with_fix=True, fix_content=None):
        write(feature_repo, ".agentic-preflight.toml",
              "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
              "\n[worktree]\nmode = 'reusable'\n")
        commit_all(feature_repo, "configure agentic-preflight")
        agent = ScriptedAgent(feature_repo)
        env = agent.run("start")
        wt = env["data"]["worktree_path"]
        agent.run("context")

        if with_fix:
            agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
            write(wt, "src/app.py",
                  fix_content or "def greet(name, loud=False):\n    return 'FIXED'\n")
            git("add", "-A", cwd=wt)
            git("commit", "-m", "apply the fix", cwd=wt)
            sha = git("rev-parse", "HEAD", cwd=wt)
            agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
            agent.run("verify")
        else:
            agent.run("submit-findings", "--file", findings_json(tmp_path, []))

        agent.run("stage", "run", "test")
        env = agent.run("stage", "run", "lint")
        assert env["state"] == "LINT_GREEN"
        return agent, wt

    return build


# -- the happy path ---------------------------------------------------------


def test_default_in_place_mode_records_repairs_and_attests_without_cherry_pick(
    feature_repo, tmp_path
):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    assert started["data"]["worktree_mode"] == "in_place"
    assert started["data"]["worktree_path"] == str(feature_repo)

    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    fix_sha = commit_all(feature_repo, "apply the fix directly")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix_sha)
    status = agent.run("status")
    assert status["data"]["head_sha"] == fix_sha
    assert status["data"]["source_head_sha"] == fix_sha

    agent.run("verify")
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    merged = agent.run("mergeback")

    assert merged["data"]["worktree_mode"] == "in_place"
    assert merged["data"]["pre_sha"] == merged["data"]["post_sha"] == fix_sha
    assert merged["data"]["applied"] == []
    assert merged["data"]["tree_equivalent"] is True


def test_mergeback_applies_fix_commits_to_the_branch(ready, feature_repo):
    agent, _ = ready()
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert "FIXED" in (feature_repo / "src" / "app.py").read_text()


def test_mergeback_with_no_fix_commits_is_a_no_op(ready, feature_repo):
    agent, _ = ready(with_fix=False)
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before


def test_the_users_branch_is_where_the_commits_land(ready, feature_repo):
    agent, _ = ready()
    agent.run("mergeback")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=feature_repo) == "feature/x"
    assert "apply the fix" in git("log", "--format=%s", cwd=feature_repo)


# -- tree-equivalence attestation -------------------------------------------


def test_a_clean_mergeback_attests_tree_equivalence(ready):
    """The mechanism reconciling 'ledger keyed on exact SHA' with
    'cherry-pick changes the SHA'."""
    agent, _ = ready()
    env = agent.run("mergeback")
    assert env["data"]["tree_equivalent"] is True
    assert env["data"]["local_tree_sha"] == env["data"]["worktree_tree_sha"]


def test_green_transfers_only_on_tree_equivalence(ready):
    agent, _ = ready()
    env = agent.run("mergeback")
    assert env["data"]["green_transferred"] is True


def test_mergeback_records_the_new_tip(ready, feature_repo):
    agent, _ = ready()
    env = agent.run("mergeback")
    assert env["data"]["post_sha"] == git("rev-parse", "HEAD", cwd=feature_repo)
    assert env["data"]["pre_sha"] != env["data"]["post_sha"]


# -- preconditions ----------------------------------------------------------


def test_mergeback_allows_an_unrelated_untracked_file(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "scratch.txt", "uncommitted\n")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert (feature_repo / "scratch.txt").read_text() == "uncommitted\n"


def test_mergeback_allows_an_unrelated_tracked_edit(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "README.md", "uncommitted documentation\n")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert (feature_repo / "README.md").read_text() == "uncommitted documentation\n"


def test_mergeback_refuses_a_change_to_an_affected_path(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "src/app.py", "uncommitted overlap\n")
    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "dirty_tree"
    assert "src/app.py" in env["data"]["affected_paths"]


def test_mergeback_refuses_when_the_branch_moved(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "other.txt", "moved on\n")
    commit_all(feature_repo, "unrelated commit")
    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "stale_run"


def test_mergeback_is_illegal_before_tests_pass(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    env = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


# -- the conflict contract: the single most important test -----------------


def test_conflict_aborts_and_restores_the_branch_exactly(feature_repo, tmp_path):
    """Construct a guaranteed conflict and assert the full abort contract."""
    write(feature_repo, ".agentic-preflight.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "configure agentic-preflight")

    agent = ScriptedAgent(feature_repo)
    env = agent.run("start")
    wt = env["data"]["worktree_path"]
    run_id = env["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))

    # The worktree rewrites the file wholesale.
    write(wt, "src/app.py", "TOTALLY DIFFERENT WORKTREE CONTENT\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "worktree rewrite", cwd=wt)
    fix_sha = git("rev-parse", "HEAD", cwd=wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix_sha)
    agent.run("verify")
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")

    # Now make the branch diverge on the same content, and re-point the run's
    # recorded head so the staleness guard does not fire first.
    write(feature_repo, "src/app.py", "TOTALLY DIFFERENT BRANCH CONTENT\n")
    commit_all(feature_repo, "branch rewrite")
    new_head = git("rev-parse", "HEAD", cwd=feature_repo)

    from pathlib import Path
    state_root = Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo)
    ) / "agentic-preflight"
    run_path = state_root / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text())
    doc["head_sha"] = new_head
    doc["source_head_sha"] = new_head
    run_path.write_text(json.dumps(doc))

    pre_sha = new_head
    env = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert env["state"] == "MERGEBACK_CONFLICT"
    assert env["error"]["code"] == "mergeback_conflict"
    # The branch is exactly where it was.
    assert git("rev-parse", "HEAD", cwd=feature_repo) == pre_sha
    assert git("status", "--porcelain", cwd=feature_repo) == ""
    # No cherry-pick left in progress.
    assert not (Path(feature_repo) / ".git" / "CHERRY_PICK_HEAD").exists()
    # The work is still there to rescue.
    assert Path(wt).exists()
    assert fix_sha in json.dumps(env["data"])
    # And the agent is told how, explicitly.
    assert env["data"]["conflicting_files"] == ["src/app.py"]
    assert "git cherry-pick" in json.dumps(env["data"]["resolution"])

    # The report survives the failed process and status points at a legal retry.
    status = agent.run("status")
    assert status["data"]["mergeback_conflict"]["conflicting_commit"] == fix_sha
    assert status["next"]["command"] == "agentic-preflight mergeback"

    # Repeating mergeback is legal; a still-unresolved conflict remains durable.
    retry = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert retry["state"] == "MERGEBACK_CONFLICT"

    # A human can apply the exact verified content, then retry only the
    # attestation instead of discarding every completed stage.
    result = subprocess.run(
        ["git", "cherry-pick", fix_sha], cwd=feature_repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    write(feature_repo, "src/app.py", "TOTALLY DIFFERENT WORKTREE CONTENT\n")
    git("add", "src/app.py", cwd=feature_repo)
    git("cherry-pick", "--continue", cwd=feature_repo)
    verified = agent.run("mergeback")
    assert verified["state"] == "VERIFIED"
    assert verified["data"]["tree_equivalent"] is True


def test_conflict_never_auto_resolves(tmp_repo, monkeypatch):
    """No -X ours/theirs, no rerere, ever — asserted against real git argv.

    Checked behaviourally rather than by scanning the source, so it holds no
    matter how the module is refactored.
    """
    from agentic_preflight import gitx, mergeback

    # A guaranteed conflict: two branches rewriting the same line differently.
    git("switch", "-c", "side", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "SIDE\n")
    commit_all(tmp_repo, "side edit")
    side_sha = git("rev-parse", "HEAD", cwd=tmp_repo)

    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "MAIN\n")
    commit_all(tmp_repo, "main edit")

    recorded: list[tuple[str, ...]] = []
    real_run = gitx.run

    def recording_run(cwd, *args, **kwargs):
        recorded.append(args)
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(mergeback.gitx, "run", recording_run)

    with pytest.raises(mergeback.MergebackConflict):
        mergeback.cherry_pick_fixes(
            tmp_repo, [side_sha], worktree_branch="ap/x", worktree_path=str(tmp_repo)
        )

    flat = " ".join(" ".join(args) for args in recorded)
    for forbidden in ("-X", "--strategy-option", "rerere", "theirs", "ours"):
        assert forbidden not in flat, f"merge-back invoked git with {forbidden}"
    assert "--abort" in flat, "a conflicting cherry-pick must be aborted"


def test_conflict_leaves_no_cherry_pick_in_progress(tmp_repo):
    """CHERRY_PICK_HEAD must be gone: a half-finished pick wedges the repo."""

    from agentic_preflight import mergeback

    git("switch", "-c", "side", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "SIDE\n")
    commit_all(tmp_repo, "side edit")
    side_sha = git("rev-parse", "HEAD", cwd=tmp_repo)

    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "MAIN\n")
    commit_all(tmp_repo, "main edit")
    pre_sha = git("rev-parse", "HEAD", cwd=tmp_repo)

    with pytest.raises(mergeback.MergebackConflict) as exc:
        mergeback.cherry_pick_fixes(
            tmp_repo, [side_sha], worktree_branch="ap/x", worktree_path=str(tmp_repo)
        )

    assert exc.value.report.restored is True
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == pre_sha
    assert git("status", "--porcelain", cwd=tmp_repo) == ""
    assert not (tmp_repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_conflict_aborts_the_entire_fix_stack_and_preserves_unrelated_work(tmp_repo):
    from agentic_preflight import mergeback

    git("switch", "-c", "fix-stack", cwd=tmp_repo)
    write(tmp_repo, "README.md", "first fix\n")
    first = commit_all(tmp_repo, "first fix")
    write(tmp_repo, "src/app.py", "FIX STACK\n")
    second = commit_all(tmp_repo, "second conflicting fix")

    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "MAIN CONFLICT\n")
    commit_all(tmp_repo, "main conflict")
    pre_sha = git("rev-parse", "HEAD", cwd=tmp_repo)
    write(tmp_repo, "unrelated.txt", "keep me\n")

    with pytest.raises(mergeback.MergebackConflict) as exc:
        mergeback.cherry_pick_fixes(
            tmp_repo,
            [first, second],
            worktree_branch="ap/x",
            worktree_path=str(tmp_repo),
        )

    assert exc.value.report.restored is True
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == pre_sha
    assert (tmp_repo / "README.md").read_text().startswith("# demo")
    assert (tmp_repo / "unrelated.txt").read_text() == "keep me\n"
