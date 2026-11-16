"""M4 merge-back: cherry-pick, the clean-abort contract, tree-equivalence."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

BLOCKING = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "needs a fix",
    }
]


def _create_empty_cherry_pick_stop(repo: Path) -> tuple[str, str, Path, Path]:
    """Start a real two-commit sequence whose first pick is already applied."""
    starting_branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    git("switch", "-c", "user-pick-source", cwd=repo)
    write(repo, "user-a.txt", "already present\n")
    first = commit_all(repo, "user commit A")
    write(repo, "user-b.txt", "still pending\n")
    second = commit_all(repo, "user commit B")
    git("switch", starting_branch, cwd=repo)
    git("cherry-pick", first, cwd=repo)

    stopped = subprocess.run(
        ["git", "cherry-pick", first, second],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert stopped.returncode == 1
    cherry_pick_head = Path(git("rev-parse", "--git-path", "CHERRY_PICK_HEAD", cwd=repo))
    sequencer = Path(git("rev-parse", "--git-path", "sequencer", cwd=repo))
    if not cherry_pick_head.is_absolute():
        cherry_pick_head = repo / cherry_pick_head
    if not sequencer.is_absolute():
        sequencer = repo / sequencer
    assert cherry_pick_head.is_file()
    assert sequencer.is_dir()
    assert git("status", "--porcelain", cwd=repo) == ""
    return first, second, cherry_pick_head, sequencer


@pytest.fixture
def ready(feature_repo, tmp_path):
    """Drive a run to TEST_GREEN, optionally with a fix commit in the worktree."""

    def build(*, with_fix=True, fix_content=None, fresh_base=False):
        configure_base(
            feature_repo,
            "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
            "\n[worktree]\nmode = 'reusable'\n",
        )
        if fresh_base:
            git("switch", "main", cwd=feature_repo)
            write(feature_repo, "upstream.txt", "new upstream content\n")
            commit_all(feature_repo, "advance main before validation")
            git("switch", "feature/x", cwd=feature_repo)
        agent = ScriptedAgent(feature_repo)
        env = agent.run("start")
        wt = env["data"]["worktree_path"]
        agent.run("context")

        if with_fix:
            agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
            write(
                wt,
                "src/app.py",
                fix_content or "def greet(name, loud=False):\n    return 'FIXED'\n",
            )
            git("add", "-A", cwd=wt)
            git("commit", "-m", "apply the fix", cwd=wt)
            sha = git("rev-parse", "HEAD", cwd=wt)
            agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
            agent.run("verify")
            agent.run("context")
            agent.run("submit-findings", "--file", findings_json(tmp_path, []))
        else:
            agent.run("submit-findings", "--file", findings_json(tmp_path, []))

        agent.run("stage", "run", "lint")
        env = agent.run("stage", "run", "test")
        assert env["state"] == "TEST_GREEN"
        return agent, wt

    return build


# -- the happy path ---------------------------------------------------------


@pytest.mark.parametrize("tampered", [False, True])
def test_pending_mergeback_rejects_an_interrupted_source_rebase(
    ready, feature_repo, monkeypatch, tampered
):
    from agentic_preflight import sync

    agent, wt = ready(fresh_base=True)
    source = git("rev-parse", "HEAD", cwd=feature_repo)
    original = sync.sync_onto

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("interrupted after source rebase")

    with monkeypatch.context() as fault:
        fault.setattr(sync, "sync_onto", interrupted)
        agent.run("mergeback", expect=1)
    rebased = git("rev-parse", "HEAD", cwd=feature_repo)
    assert rebased != source
    assert git("rev-parse", "HEAD^{tree}", cwd=feature_repo) != git(
        "rev-parse", "HEAD^{tree}", cwd=wt
    )
    status = agent.run("status")
    assert status["state"] == "MERGEBACK_PENDING"
    assert status["data"]["stale"] is False
    if tampered:
        write(feature_repo, "unexpected.txt", "unreviewed content\n")
        commit_all(feature_repo, "unrelated change after rebase")
    tip = git("rev-parse", "HEAD", cwd=feature_repo)
    rejected = agent.run("mergeback", expect=3)
    assert rejected["error"]["code"] == "stale_run"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == tip


def test_mergeback_treats_a_head_on_a_tag_as_a_branch_mismatch(ready, feature_repo):
    agent, _ = ready(with_fix=True)
    git("tag", "pinned", cwd=feature_repo)
    git("symbolic-ref", "HEAD", "refs/tags/pinned", cwd=feature_repo)
    env = agent.run("mergeback", expect=3)
    assert env["error"]["code"] == "stale_run"
    assert "recorded source branch" in env["error"]["message"]


def _interrupt_persisted_retry(agent, monkeypatch):
    from agentic_preflight import store as storemod

    original = storemod.atomic_write

    def interrupted(path, payload):
        original(path, payload)
        if path.name == "run.json":
            record = json.loads(payload)
            if record["state"] == "MERGEBACK_PENDING":
                assert record["mergeback_attempt"]["retrying_conflict"] is True
                raise OSError("interrupted after retry transaction")

    with monkeypatch.context() as fault:
        fault.setattr(storemod, "atomic_write", interrupted)
        agent.run("mergeback", expect=1)
    status = agent.run("status")
    assert status["state"] == "MERGEBACK_PENDING"
    assert status["data"]["stale"] is False


@pytest.mark.parametrize("boundary", ["git", "attestation", "run_record"])
def test_pending_mergeback_reconciles_completed_git_effects(
    ready, feature_repo, monkeypatch, boundary
):
    from agentic_preflight import attestation
    from agentic_preflight import mergeback as mergebackmod
    from agentic_preflight import store as storemod

    agent, wt = ready(with_fix=True)
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    original_pick = mergebackmod.cherry_pick_fixes
    original_write = storemod.atomic_write

    def interrupted_pick(*args, **kwargs):
        original_pick(*args, **kwargs)
        raise OSError("interrupted after Git completed")

    def interrupted_note(*args, **kwargs):
        raise OSError("attestation storage unavailable")

    def interrupted_record(path, payload):
        if path.name == "run.json" and json.loads(payload)["state"] == "VERIFIED":
            raise OSError("interrupted before recording verified state")
        original_write(path, payload)

    with monkeypatch.context() as fault:
        if boundary == "git":
            fault.setattr(mergebackmod, "cherry_pick_fixes", interrupted_pick)
        elif boundary == "attestation":
            fault.setattr(attestation, "write", interrupted_note)
        else:
            fault.setattr(storemod, "atomic_write", interrupted_record)
        agent.run("mergeback", expect=1)

    applied = git("rev-parse", "HEAD", cwd=feature_repo)
    assert applied != before
    status = agent.run("status")
    assert status["state"] == "MERGEBACK_PENDING"
    assert status["next"]["command"] == "agentic-preflight mergeback"
    assert status["data"]["mergeback_attempt"]["source_sha"] == before

    def never_reapply(*args, **kwargs):
        pytest.fail("a completed mergeback must not cherry-pick a second time")

    monkeypatch.setattr(mergebackmod, "cherry_pick_fixes", never_reapply)
    recovered = agent.run("mergeback")
    assert recovered["state"] == "VERIFIED"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == applied
    assert attestation.verify(feature_repo, applied).tree_sha == git(
        "rev-parse", "HEAD^{tree}", cwd=wt
    )


@pytest.mark.parametrize("change", ["source", "validation", "branch", "tag"])
def test_pending_mergeback_rejects_changes_outside_recorded_attempt(
    ready, feature_repo, monkeypatch, change
):
    from agentic_preflight import attestation

    agent, wt = ready(with_fix=True)

    def interrupted(*args, **kwargs):
        raise OSError("attestation unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(attestation, "write", interrupted)
        agent.run("mergeback", expect=1)
    if change == "branch":
        git("switch", "-c", "different-branch", cwd=feature_repo)
    elif change == "tag":
        git("tag", "pinned", cwd=feature_repo)
        git("symbolic-ref", "HEAD", "refs/tags/pinned", cwd=feature_repo)
    else:
        target = feature_repo if change == "source" else Path(wt)
        write(target, "unexpected.txt", "unreviewed content\n")
        commit_all(target, "unrelated change after interruption")
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    env = agent.run("mergeback", expect=3)
    assert env["error"]["code"] == "stale_run"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before
    status = agent.run("status")
    assert status["data"]["stale"] is True
    assert status["next"]["command"].startswith("agentic-preflight start")


def test_default_in_place_mode_records_repairs_and_attests_without_cherry_pick(
    feature_repo, tmp_path
):
    configure_base(
        feature_repo, "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
    )
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
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    merged = agent.run("mergeback")

    assert merged["data"]["worktree_mode"] == "in_place"
    assert merged["data"]["pre_sha"] == merged["data"]["post_sha"] == fix_sha
    assert merged["data"]["applied"] == []
    assert merged["data"]["tree_equivalent"] is True


def test_mergeback_applies_fix_commits_and_transfers_the_green_attestation(ready, feature_repo):
    """Cherry-picking changes the commit SHA but must preserve the verified tree."""
    agent, _ = ready()
    env = agent.run("mergeback")

    assert env["state"] == "VERIFIED"
    assert "FIXED" in (feature_repo / "src" / "app.py").read_text(encoding="utf-8")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=feature_repo) == "feature/x"
    assert "apply the fix" in git("log", "--format=%s", cwd=feature_repo)
    assert env["data"]["tree_equivalent"] is True
    assert env["data"]["local_tree_sha"] == env["data"]["worktree_tree_sha"]
    assert env["data"]["green_transferred"] is True
    assert env["data"]["post_sha"] == git("rev-parse", "HEAD", cwd=feature_repo)
    assert env["data"]["pre_sha"] != env["data"]["post_sha"]


def test_mergeback_with_no_fix_commits_is_a_no_op(ready, feature_repo):
    agent, _ = ready(with_fix=False)
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before


# -- preconditions ----------------------------------------------------------


def test_mergeback_allows_an_unrelated_untracked_file(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "scratch.txt", "uncommitted\n")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert (feature_repo / "scratch.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_mergeback_allows_an_unrelated_tracked_edit(ready, feature_repo):
    agent, _ = ready()
    write(feature_repo, "README.md", "uncommitted documentation\n")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert (feature_repo / "README.md").read_text(encoding="utf-8") == "uncommitted documentation\n"


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


@pytest.mark.parametrize("interrupted_retry", [False, True])
def test_conflict_aborts_and_restores_the_branch_exactly(
    feature_repo, tmp_path, monkeypatch, interrupted_retry
):
    """Construct a guaranteed conflict and assert the full abort contract."""
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )

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
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    # Now make the branch diverge on the same content, and re-point the run's
    # recorded source head so the staleness guard does not fire first. The
    # reviewed head stays the base of the validated history.
    write(feature_repo, "src/app.py", "TOTALLY DIFFERENT BRANCH CONTENT\n")
    commit_all(feature_repo, "branch rewrite")
    new_head = git("rev-parse", "HEAD", cwd=feature_repo)

    from pathlib import Path

    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )
    run_path = state_root / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
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
    if interrupted_retry:
        _interrupt_persisted_retry(agent, monkeypatch)
    verified = agent.run("mergeback")
    assert verified["state"] == "VERIFIED"
    assert verified["data"]["tree_equivalent"] is True


@pytest.mark.parametrize("interrupted_retry", [False, True])
def test_different_human_conflict_resolution_restarts_review(
    feature_repo, tmp_path, monkeypatch, interrupted_retry
):
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )

    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    wt = Path(started["data"]["worktree_path"])
    run_id = started["run_id"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))

    write(wt, "src/app.py", "VERIFIED FIX\n")
    fix_sha = commit_all(wt, "worktree fix")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix_sha)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    write(feature_repo, "src/app.py", "SOURCE CONFLICT\n")
    source_head = commit_all(feature_repo, "create source conflict")
    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )
    run_path = state_root / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    doc["source_head_sha"] = source_head
    run_path.write_text(json.dumps(doc))

    agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    result = subprocess.run(
        ["git", "cherry-pick", fix_sha], cwd=feature_repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    write(feature_repo, "src/app.py", "HUMAN RESOLUTION\n")
    git("add", "src/app.py", cwd=feature_repo)
    git("cherry-pick", "--continue", cwd=feature_repo)

    if interrupted_retry:
        _interrupt_persisted_retry(agent, monkeypatch)
    restarted = agent.run("mergeback")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert restarted["data"]["tree_equivalent"] is False
    assert restarted["data"]["validation_restarted"] is True
    assert restarted["next"]["command"] == "agentic-preflight context"
    assert (wt / "src/app.py").read_text(encoding="utf-8") == "HUMAN RESOLUTION\n"
    status = agent.run("status")
    assert status["data"]["review_coverage"] is None
    assert status["data"]["fix_commits"] == []
    assert status["data"]["stages"]["review"]["status"] == "pending"
    assert status["data"]["stages"]["lint"]["status"] == "pending"
    assert status["data"]["stages"]["test"]["status"] == "pending"
    assert agent.run("context")["data"]["head"] == git("rev-parse", "HEAD", cwd=feature_repo)


def test_human_conflict_resolution_cannot_adopt_a_copied_file(feature_repo, tmp_path):
    write(feature_repo, ".env", "SECRET=original-secret\n")
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )

    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    wt = Path(started["data"]["worktree_path"])
    run_id = started["run_id"]
    assert started["data"]["copied_files"] == [".env"]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))

    write(wt, "src/app.py", "VERIFIED FIX\n")
    fix_sha = commit_all(wt, "worktree fix")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", fix_sha)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")

    write(feature_repo, "src/app.py", "SOURCE CONFLICT\n")
    source_head = commit_all(feature_repo, "create source conflict")
    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )
    run_path = state_root / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    doc["source_head_sha"] = source_head
    run_path.write_text(json.dumps(doc))

    agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    result = subprocess.run(
        ["git", "cherry-pick", fix_sha], cwd=feature_repo, capture_output=True, text=True
    )
    assert result.returncode != 0
    write(feature_repo, "src/app.py", "HUMAN RESOLUTION\n")
    write(feature_repo, ".env", "SECRET=force-added-secret\n")
    git("add", "src/app.py", cwd=feature_repo)
    git("add", "-f", ".env", cwd=feature_repo)
    git("cherry-pick", "--continue", cwd=feature_repo)

    blocked = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert blocked["error"]["code"] == "copied_file_in_commit"
    assert ".env" in blocked["error"]["message"]
    assert (wt / "src/app.py").read_text(encoding="utf-8") == "VERIFIED FIX\n"
    assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=original-secret\n"
    assert agent.run("status")["state"] == "MERGEBACK_PENDING"


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


@pytest.mark.parametrize("use_ref", [False, True])
def test_conflict_leaves_no_cherry_pick_in_progress(tmp_repo, use_ref):
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
            tmp_repo,
            ["side" if use_ref else side_sha],
            worktree_branch="ap/x",
            worktree_path=str(tmp_repo),
        )

    assert exc.value.report.restored is True
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == pre_sha
    assert git("status", "--porcelain", cwd=tmp_repo) == ""
    assert not (tmp_repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_cherry_pick_refuses_to_abort_the_users_paused_sequence(tmp_repo):
    from agentic_preflight import mergeback

    _, second, cherry_pick_head, sequencer = _create_empty_cherry_pick_stop(tmp_repo)
    before_head = git("rev-parse", "HEAD", cwd=tmp_repo)
    before_status = git("status", "--porcelain", cwd=tmp_repo)

    caught: Exception | None = None
    try:
        mergeback.cherry_pick_fixes(
            tmp_repo,
            [second],
            worktree_branch="ap/x",
            worktree_path=str(tmp_repo),
        )
    except Exception as exc:  # noqa: BLE001 - identifies the BASE failure by assertion below
        caught = exc

    assert cherry_pick_head.is_file(), "mergeback aborted the user's paused cherry-pick"
    assert sequencer.is_dir()
    assert type(caught).__name__ == "OperationInProgress"
    assert getattr(caught, "operation", None) == "cherry-pick"
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == before_head
    assert git("status", "--porcelain", cwd=tmp_repo) == before_status


def test_mergeback_reports_the_users_paused_cherry_pick_as_a_precondition(ready, feature_repo):
    first, second, _, _ = _create_empty_cherry_pick_stop(feature_repo)
    git("cherry-pick", "--abort", cwd=feature_repo)
    agent, _ = ready()

    stopped = subprocess.run(
        ["git", "cherry-pick", first, second],
        cwd=feature_repo,
        capture_output=True,
        text=True,
    )
    assert stopped.returncode == 1
    cherry_pick_head = Path(
        git(
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "CHERRY_PICK_HEAD",
            cwd=feature_repo,
        )
    )
    before_head = git("rev-parse", "HEAD", cwd=feature_repo)
    before_status = git("status", "--porcelain", cwd=feature_repo)

    refused = agent.run("mergeback", expect=ExitCode.PRECONDITION)

    assert refused["error"]["code"] == "operation_in_progress"
    assert refused["data"] == {"operation": "cherry-pick", "path": str(feature_repo)}
    assert refused["next"]["command"] == "git status"
    assert refused["state"] == "TEST_GREEN"
    assert cherry_pick_head.is_file()
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before_head
    assert git("status", "--porcelain", cwd=feature_repo) == before_status

    git("cherry-pick", "--abort", cwd=feature_repo)
    assert agent.run("mergeback")["state"] == "VERIFIED"


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
    assert (tmp_repo / "README.md").read_text(encoding="utf-8").startswith("# demo")
    assert (tmp_repo / "unrelated.txt").read_text(encoding="utf-8") == "keep me\n"


def test_cherry_pick_preserves_user_resolution_started_after_precondition(tmp_repo, monkeypatch):
    from agentic_preflight import gitx, mergeback

    git("switch", "-c", "user-pick", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "USER\n")
    user_sha = commit_all(tmp_repo, "user change")
    git("switch", "main", cwd=tmp_repo)
    git("switch", "-c", "preflight-fix", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "FIX\n")
    fix_sha = commit_all(tmp_repo, "preflight fix")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "MAIN\n")
    before_head = commit_all(tmp_repo, "main change")
    real_run = gitx.run
    user_status = None

    def start_user_pick(cwd, *args, **kwargs):
        nonlocal user_status
        if args == ("cherry-pick", fix_sha):
            assert real_run(cwd, "cherry-pick", user_sha, check=False).returncode != 0
            write(tmp_repo, "src/app.py", "USER RESOLUTION\n")
            real_run(cwd, "add", "src/app.py")
            user_status = git("status", "--porcelain", cwd=tmp_repo)
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", start_user_pick)
    with pytest.raises(mergeback.MergebackConflict) as caught:
        mergeback.cherry_pick_fixes(
            tmp_repo, [fix_sha], worktree_branch="ap/x", worktree_path=str(tmp_repo)
        )

    assert (tmp_repo / ".git" / "CHERRY_PICK_HEAD").is_file()
    assert git("rev-parse", "CHERRY_PICK_HEAD", cwd=tmp_repo) == user_sha
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == before_head
    assert git("status", "--porcelain", cwd=tmp_repo) == user_status
    assert (tmp_repo / "src/app.py").read_text() == "USER RESOLUTION\n"
    assert caught.value.report.restored is False


@pytest.mark.parametrize(
    "state", ["LINT_GREEN", "TEST_GREEN", "MERGEBACK_PENDING", "MERGEBACK_CONFLICT"]
)
@pytest.mark.parametrize("mutation", ["origin", "config"])
def test_corrupt_current_evidence_preserves_source_and_reports_recovery(
    ready, feature_repo, state, mutation
):
    from agentic_preflight.digests import json_digest
    from agentic_preflight.errors import STAGE_EVIDENCE_RECOVERY
    from agentic_preflight.store import Store

    agent, wt = ready()
    status = agent.run("status")
    run_id = status["run_id"]
    store = Store(feature_repo / ".git" / "agentic-preflight")
    path = store.run_path(run_id)
    raw = json.loads(path.read_text())
    item = raw["evidence"]["review"]
    if mutation == "origin":
        item["origin_sha256"] = "f" * 64
    else:
        item["origin"]["config_sha256"] = "f" * 64
        item["origin_sha256"] = json_digest(item["origin"])
    raw["state"] = state
    if state == "MERGEBACK_PENDING":
        raw["mergeback_attempt"] = {
            "source_sha": git("rev-parse", "HEAD", cwd=feature_repo),
            "validation_sha": git("rev-parse", "HEAD", cwd=wt),
            "validation_tree": git("rev-parse", "HEAD^{tree}", cwd=wt),
            "retrying_conflict": False,
        }
    path.write_text(json.dumps(raw))
    before = json.loads(path.read_text())
    source_head = git("rev-parse", "HEAD", cwd=feature_repo)
    validation_head = git("rev-parse", "HEAD", cwd=wt)
    inspected = agent.run("status")
    assert inspected["data"]["readable"] is True
    assert inspected["data"]["evidence_valid"] is False
    assert inspected["data"]["evidence_failure"]["code"] == "stage_evidence_invalid"
    assert inspected["next"]["instruction"] == STAGE_EVIDENCE_RECOVERY
    assert inspected["next"]["command"] == "agentic-preflight abort"
    inventory = agent.run("status", "--all")
    assert any(item["run_id"] == run_id for item in inventory["data"]["runs"])
    assert all(item.get("readable", True) for item in inventory["data"]["runs"])
    if state != "LINT_GREEN":
        failed = agent.run("mergeback", expect=ExitCode.PRECONDITION)
        assert failed["error"]["code"] == "stage_evidence_invalid"
        assert failed["data"]["reason"] == "invalid_origin_digest"
        assert failed["next"]["instruction"] == STAGE_EVIDENCE_RECOVERY
    assert git("rev-parse", "HEAD", cwd=feature_repo) == source_head
    assert git("rev-parse", "HEAD", cwd=wt) == validation_head
    after = json.loads(path.read_text())
    assert after["evidence"] == before["evidence"]
    assert after["state"] == before["state"]
    assert after["mergeback_attempt"] == before["mergeback_attempt"]
    assert git("notes", "--ref=agentic-preflight", "list", cwd=feature_repo) == ""
    aborted = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)
    assert aborted["error"]["code"] == "unmerged_work"
    assert Path(wt).is_dir()
    assert git("rev-parse", "HEAD", cwd=wt) == validation_head
    assert json.loads(path.read_text())["evidence"] == before["evidence"]


# -- conflict retry after the tool's own rebase (#340) ----------------------


def _conflict_after_own_rebase(ready, feature_repo, monkeypatch, *, conflicts=1):
    """Stop merge-back with a cherry-pick conflict after it rebased the source branch.

    Merge-back rebases the source onto the run's recorded base before it replays
    the validated history, so the source head no longer equals the head the run
    started from. Replaying the validated history onto that rebased head is
    normally clean, so the conflict is injected for the first ``conflicts``
    attempts; each injected attempt leaves the branch exactly where it found it.
    """
    from agentic_preflight import mergeback as mergebackmod

    agent, wt = ready(fresh_base=True)
    started_from = git("rev-parse", "HEAD", cwd=feature_repo)
    fix_commits = agent.run("status")["data"]["fix_commits"]
    assert len(fix_commits) == 1
    original = mergebackmod.cherry_pick_fixes
    remaining = [conflicts]

    def conflicting(repo, commits, *, worktree_branch, worktree_path):
        if remaining[0] == 0:
            return original(
                repo, commits, worktree_branch=worktree_branch, worktree_path=worktree_path
            )
        remaining[0] -= 1
        head = git("rev-parse", "HEAD", cwd=repo)
        raise mergebackmod.MergebackConflict(
            f"cherry-picking {commits[0][:8]} conflicted with the branch",
            mergebackmod.ConflictReport(
                pre_sha=head,
                conflicting_commit=commits[0],
                conflicting_files=["src/app.py"],
                fix_commits=list(commits),
                worktree_path=str(worktree_path),
                restored=True,
            ),
        )

    monkeypatch.setattr(mergebackmod, "cherry_pick_fixes", conflicting)
    env = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert env["state"] == "MERGEBACK_CONFLICT"
    assert env["next"]["command"] == "agentic-preflight mergeback"
    rebased = git("rev-parse", "HEAD", cwd=feature_repo)
    assert rebased != started_from
    return agent, Path(wt), fix_commits, rebased


def _notes(repo: Path) -> str:
    return git("notes", "--ref=refs/notes/agentic-preflight", "list", cwd=repo).strip()


def test_untouched_conflict_retry_replays_the_fixes_instead_of_discarding_them(
    ready, feature_repo, monkeypatch
):
    agent, wt, fix_commits, rebased = _conflict_after_own_rebase(
        ready, feature_repo, monkeypatch, conflicts=2
    )
    validated_head = git("rev-parse", "HEAD", cwd=wt)

    # The tool's own rebase is not a human resolution: an unresolved conflict
    # stays a conflict, and nothing is reset or forgotten.
    still = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert still["state"] == "MERGEBACK_CONFLICT"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == rebased
    assert git("rev-parse", "HEAD", cwd=wt) == validated_head
    assert agent.run("status")["data"]["fix_commits"] == fix_commits
    assert _notes(feature_repo) == ""

    verified = agent.run("mergeback")

    assert verified["state"] == "VERIFIED"
    assert verified["data"]["applied"] == fix_commits
    assert verified["data"]["tree_equivalent"] is True
    assert "FIXED" in (feature_repo / "src" / "app.py").read_text(encoding="utf-8")
    assert git("rev-parse", "HEAD", cwd=wt) == validated_head
    assert _notes(feature_repo) != ""


def test_conflict_retry_refuses_to_discard_fixes_missing_from_the_source(
    ready, feature_repo, monkeypatch
):
    agent, wt, fix_commits, _ = _conflict_after_own_rebase(ready, feature_repo, monkeypatch)
    validated_head = git("rev-parse", "HEAD", cwd=wt)

    # A commit between attempts that does not carry the fix is not a resolution.
    write(feature_repo, "unrelated.txt", "not the fix\n")
    unrelated = commit_all(feature_repo, "unrelated work during the conflict")

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == fix_commits
    assert fix_commits[0] in refused["error"]["message"]
    assert refused["state"] == "MERGEBACK_CONFLICT"
    assert refused["next"]["command"] == "agentic-preflight mergeback"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == unrelated
    assert git("rev-parse", "HEAD", cwd=wt) == validated_head
    status = agent.run("status")
    assert status["data"]["fix_commits"] == fix_commits
    assert status["state"] == "MERGEBACK_CONFLICT"
    assert _notes(feature_repo) == ""

    # Applying the named commit is the recovery; the combined tree differs from
    # the validated one, so review restarts on it with the fix in place.
    git("cherry-pick", fix_commits[0], cwd=feature_repo)
    restarted = agent.run("mergeback")
    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert restarted["data"]["validation_restarted"] is True
    assert "FIXED" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    assert (wt / "unrelated.txt").exists()


def test_human_resolution_after_the_tools_rebase_restarts_review(ready, feature_repo, monkeypatch):
    agent, wt, fix_commits, rebased = _conflict_after_own_rebase(ready, feature_repo, monkeypatch)

    # The human resolves by applying the fix with different content, as a
    # conflicted `git cherry-pick` followed by `--continue` would.
    git("cherry-pick", "--no-commit", fix_commits[0], cwd=feature_repo)
    write(feature_repo, "src/app.py", "HUMAN RESOLUTION\n")
    git("add", "src/app.py", cwd=feature_repo)
    git("commit", "--no-edit", "-C", fix_commits[0], cwd=feature_repo)
    resolved = git("rev-parse", "HEAD", cwd=feature_repo)
    assert resolved != rebased

    restarted = agent.run("mergeback")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert restarted["data"]["tree_equivalent"] is False
    assert restarted["data"]["validation_restarted"] is True
    assert git("rev-parse", "HEAD", cwd=wt) == resolved
    assert (wt / "src" / "app.py").read_text(encoding="utf-8") == "HUMAN RESOLUTION\n"
    assert agent.run("status")["data"]["fix_commits"] == []
    assert _notes(feature_repo) == ""


def test_replay_that_restores_the_unfixed_content_is_refused(ready, feature_repo, monkeypatch):
    agent, wt, fix_commits, _ = _conflict_after_own_rebase(ready, feature_repo, monkeypatch)
    unfixed = (feature_repo / "src" / "app.py").read_text(encoding="utf-8")

    # The commit claims the fix's author, date, and subject, but its resolution put
    # the unfixed content back and only added an unrelated file.
    git("cherry-pick", "--no-commit", fix_commits[0], cwd=feature_repo)
    write(feature_repo, "src/app.py", unfixed)
    write(feature_repo, "unrelated.txt", "not the fix\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--no-edit", "-C", fix_commits[0], cwd=feature_repo)

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == fix_commits
    assert agent.run("status")["data"]["fix_commits"] == fix_commits
    assert "FIXED" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    assert _notes(feature_repo) == ""


def test_resolution_with_the_validated_content_is_accepted(ready, feature_repo, monkeypatch):
    agent, wt, _, _ = _conflict_after_own_rebase(ready, feature_repo, monkeypatch)

    # Taking the fixed file from the validation branch under a new message, with
    # another change in the same commit, carries the fix's exact content.
    git("checkout", git("rev-parse", "HEAD", cwd=wt), "--", "src/app.py", cwd=feature_repo)
    write(feature_repo, "extra.txt", "also changed\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "-m", "resolve by hand", cwd=feature_repo)

    restarted = agent.run("mergeback")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert restarted["data"]["validation_restarted"] is True
    assert "FIXED" in (wt / "src" / "app.py").read_text(encoding="utf-8")


def test_tweaked_resolution_recovers_by_recording_the_empty_pick(ready, feature_repo, monkeypatch):
    agent, wt, fix_commits, _ = _conflict_after_own_rebase(ready, feature_repo, monkeypatch)

    # A hand-written variant of the fix under the human's own message has neither
    # the fix's content nor its provenance.
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED!'\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "-m", "my own take on the fix", cwd=feature_repo)
    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert "git commit --allow-empty" in refused["next"]["instruction"]

    # The documented recovery: pick the fix, keep the intended content, and record
    # the now-empty pick instead of skipping it.
    picked = subprocess.run(
        ["git", "cherry-pick", fix_commits[0]], cwd=feature_repo, capture_output=True, text=True
    )
    assert picked.returncode != 0
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED!'\n")
    git("add", "src/app.py", cwd=feature_repo)
    subprocess.run(
        ["git", "commit", "--allow-empty", "--no-edit"],
        cwd=feature_repo,
        check=True,
        capture_output=True,
    )

    restarted = agent.run("mergeback")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert "FIXED!" in (wt / "src" / "app.py").read_text(encoding="utf-8")


def test_out_of_order_replay_conflicts_for_real_and_keeps_the_fixes(
    feature_repo, tmp_path, monkeypatch
):
    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'strict'\n",
    )
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "upstream.txt", "new upstream content\n")
    commit_all(feature_repo, "advance main before validation")
    git("switch", "feature/x", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    wt = Path(started["data"]["worktree_path"])
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIRST'\n")
    first = commit_all(wt, "first fix")
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'SECOND'\n")
    second = commit_all(wt, "second fix")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", second)
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    validated_head = git("rev-parse", "HEAD", cwd=wt)

    # Record the overlapping fixes out of order, so the replay after merge-back's own
    # rebase hits a real Git conflict that the real abort path restores.
    run_path = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
        / "runs"
        / started["run_id"]
        / "run.json"
    )

    def record_fixes(commits):
        doc = json.loads(run_path.read_text(encoding="utf-8"))
        doc["fix_commits"] = commits
        run_path.write_text(json.dumps(doc))

    import importlib

    runs_mergeback = importlib.import_module("agentic_preflight.runs.mergeback")

    original = runs_mergeback._register_validated_history

    def out_of_order(session, run):
        run = original(session, run)
        record_fixes([second, first])
        return session.store.load_run(run.run_id)

    with monkeypatch.context() as patch:
        patch.setattr(runs_mergeback, "_register_validated_history", out_of_order)
        conflict = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert conflict["state"] == "MERGEBACK_CONFLICT"
    assert conflict["data"]["branch_restored"] is True
    rebased = git("rev-parse", "HEAD", cwd=feature_repo)

    retry = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert retry["state"] == "MERGEBACK_CONFLICT"
    assert retry["error"]["code"] == "mergeback_conflict"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == rebased
    assert git("rev-parse", "HEAD", cwd=wt) == validated_head
    assert agent.run("status")["data"]["fix_commits"] == [second, first]
    assert _notes(feature_repo) == ""

    record_fixes([first, second])
    verified = agent.run("mergeback")
    assert verified["state"] == "VERIFIED"
    assert "SECOND" in (feature_repo / "src" / "app.py").read_text(encoding="utf-8")


def _commit_at(repo: Path, message: str, date: str) -> str:
    """Commit everything with a fixed author date, so replay identities stay distinct."""
    git("add", "-A", cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": date},
    )
    return git("rev-parse", "HEAD", cwd=repo)


def _multi_fix_conflict(
    feature_repo, tmp_path, monkeypatch, fixes, *, same_date=False, respond_with=-1, deletions=None
):
    """Validate ``fixes`` (message, {path: content}) as separate commits, then conflict.

    Each fix gets a distinct author date unless ``same_date`` is set. The first merge-back rebases the source onto
    the advanced base and stops with an injected cherry-pick conflict.
    """
    from agentic_preflight import mergeback as mergebackmod

    configure_base(
        feature_repo,
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[worktree]\nmode = 'reusable'\n",
    )
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "upstream.txt", "new upstream content\n")
    commit_all(feature_repo, "advance main before validation")
    git("switch", "feature/x", cwd=feature_repo)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    wt = Path(started["data"]["worktree_path"])
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    commits = []
    for index, (message, files) in enumerate(fixes):
        for path, content in files.items():
            write(wt, path, content)
        for path in (deletions or {}).get(index, []):
            (wt / path).unlink()
        day = 1 if same_date else index + 1
        commits.append(_commit_at(wt, message, f"2001-01-0{day}T00:00:00+0000"))
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", commits[respond_with])
    agent.run("verify")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"

    original = mergebackmod.cherry_pick_fixes
    remaining = [1]

    def conflicting(repo, picks, *, worktree_branch, worktree_path):
        if remaining[0] == 0:
            return original(
                repo, picks, worktree_branch=worktree_branch, worktree_path=worktree_path
            )
        remaining[0] -= 1
        raise mergebackmod.MergebackConflict(
            "injected conflict",
            mergebackmod.ConflictReport(
                pre_sha=git("rev-parse", "HEAD", cwd=repo),
                conflicting_commit=picks[0],
                conflicting_files=["src/app.py"],
                fix_commits=list(picks),
                worktree_path=str(worktree_path),
                restored=True,
            ),
        )

    monkeypatch.setattr(mergebackmod, "cherry_pick_fixes", conflicting)
    assert agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)["state"] == "MERGEBACK_CONFLICT"
    assert agent.run("status")["data"]["fix_commits"] == commits
    return agent, wt, commits


def test_empty_pick_of_a_later_fix_on_the_same_file_is_refused(feature_repo, tmp_path, monkeypatch):
    agent, wt, (first, second) = _multi_fix_conflict(
        feature_repo,
        tmp_path,
        monkeypatch,
        [
            ("first fix", {"src/app.py": "def greet(name, loud=False):\n    return 'FIRST'\n"}),
            ("second fix", {"src/app.py": "def greet(name, loud=False):\n    return 'SECOND'\n"}),
        ],
    )
    validated_head = git("rev-parse", "HEAD", cwd=wt)

    # The user applies the first fix, keeps its content for the second, and records
    # the second as an empty pick. The second fix's change is not on the branch.
    git("cherry-pick", first, cwd=feature_repo)
    git("commit", "--allow-empty", "--no-edit", "-C", second, cwd=feature_repo)

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == [second]
    assert refused["state"] == "MERGEBACK_CONFLICT"
    assert git("rev-parse", "HEAD", cwd=wt) == validated_head
    assert agent.run("status")["data"]["fix_commits"] == [first, second]
    assert _notes(feature_repo) == ""


def test_resolution_keeps_a_path_a_later_fix_reverted(feature_repo, tmp_path, monkeypatch):
    original = (feature_repo / "src" / "app.py").read_text(encoding="utf-8")
    agent, wt, (fix_a, fix_b) = _multi_fix_conflict(
        feature_repo,
        tmp_path,
        monkeypatch,
        [
            (
                "fix a",
                {"src/app.py": "def greet(name, loud=False):\n    return 'A'\n", "y.txt": "A\n"},
            ),
            ("fix b", {"src/app.py": original}),
        ],
    )
    assert git("show", f"{fix_a}^:src/app.py", cwd=wt) == original.rstrip("\n")

    # The human applies both fixes but resolves y.txt differently: the reverted path
    # holds the validated content, and the other path holds the human's resolution.
    git("cherry-pick", "--no-commit", fix_a, fix_b, cwd=feature_repo)
    write(feature_repo, "y.txt", "HUMAN\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--no-edit", "-C", fix_a, cwd=feature_repo)
    git("commit", "--allow-empty", "--no-edit", "-C", fix_b, cwd=feature_repo)

    restarted = agent.run("mergeback")

    assert restarted["state"] == "REVIEW_AWAITING_FINDINGS"
    assert restarted["data"]["validation_restarted"] is True
    assert (wt / "y.txt").read_text(encoding="utf-8") == "HUMAN\n"


def test_author_date_tells_same_subject_fixes_apart(feature_repo, tmp_path, monkeypatch):
    agent, _, (one, two) = _multi_fix_conflict(
        feature_repo,
        tmp_path,
        monkeypatch,
        [
            ("repair", {"a.txt": "one\n"}),
            ("repair", {"b.txt": "two\n", "src/app.py": "def greet(name):\n    return 'FIXED'\n"}),
        ],
    )

    # Two replays under the second fix's identity: the first fix's identity differs
    # only by author date, so it is not accepted from them.
    git("cherry-pick", "--no-commit", one, cwd=feature_repo)
    write(feature_repo, "a.txt", "one, resolved\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--no-edit", "-C", two, cwd=feature_repo)
    git("cherry-pick", "--no-commit", two, cwd=feature_repo)
    write(feature_repo, "b.txt", "two, resolved\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--no-edit", "-C", two, cwd=feature_repo)

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == [one]


def test_clean_pick_identity_cannot_vouch_for_a_twin_fix(feature_repo, tmp_path, monkeypatch):
    agent, _, (twin, picked) = _multi_fix_conflict(
        feature_repo,
        tmp_path,
        monkeypatch,
        [
            ("repair", {"x.txt": "X\n"}),
            ("repair", {"src/app.py": "def greet(name):\n    return 'FIXED'\n"}),
        ],
        same_date=True,
    )

    # The second fix lands by a clean pick, which shares the first fix's author,
    # author date, and subject. The first fix's path changes, but not to its content.
    git("cherry-pick", picked, cwd=feature_repo)
    write(feature_repo, "x.txt", "SOMETHING ELSE\n")
    commit_all(feature_repo, "unrelated x.txt change")

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == [twin]


def test_commit_vouching_for_one_fix_cannot_vouch_for_another(feature_repo, tmp_path, monkeypatch):
    agent, _, (fix, add, delete, readd) = _multi_fix_conflict(
        feature_repo,
        tmp_path,
        monkeypatch,
        [
            ("fix app", {"src/app.py": "def greet(name):\n    return 'FIXED'\n"}),
            ("add x", {"x.txt": "X\n"}),
            ("delete x", {}),
            ("add x again", {"x.txt": "X\n"}),
        ],
        respond_with=0,
        deletions={2: ["x.txt"]},
    )

    # The original first three fixes reach the branch; the last fix's change, the same
    # patch as "add x", does not: x.txt gets other content under another identity.
    git("merge", "--no-edit", delete, cwd=feature_repo)
    write(feature_repo, "x.txt", "OTHER\n")
    commit_all(feature_repo, "unrelated x.txt")

    refused = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)

    assert refused["error"]["code"] == "fix_commits_unmerged"
    assert refused["data"]["missing_fix_commits"] == [readd]
    assert agent.run("status")["data"]["fix_commits"] == [fix, add, delete, readd]
