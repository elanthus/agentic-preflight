"""M1 resolution loop: respond, fix-commit verification, abort, gc, events."""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import State
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


@pytest.fixture
def agent(feature_repo):
    return ScriptedAgent(feature_repo)


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps({"coverage": {"manifest": "$context", "examined": "all"}, "findings": items})
    )
    return str(path)


BLOCKING = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "loud flag is never used",
    }
]


@pytest.fixture
def blocked(agent, tmp_path):
    """A run parked in REVIEW_BLOCKED with F001 outstanding."""
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
    assert env["state"] == "REVIEW_BLOCKED"
    assert env["data"]["finding"]["status"] == "fixed"
    assert env["data"]["finding"]["fix_commit"].startswith(sha[:8])


def test_verify_reopens_review_when_a_fix_changes_the_snapshot(blocked, tmp_path):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run("verify")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert env["data"]["coverage_invalidated"] is True

    context = agent.run("context")
    assert context["data"]["review_coverage"]["head"] == sha
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
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
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "invalid_response"
    assert "note" in env["error"]["message"]


def test_dismissing_with_a_note_clears_the_finding(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "intentional: the flag ships next PR",
    )
    assert env["data"]["finding"]["status"] == "dismissed"
    agent.run("verify")


def test_respond_points_to_the_next_finding_while_the_stage_remains_blocked(agent, tmp_path):
    agent.run("start")
    agent.run("context")
    findings = [
        {**BLOCKING[0], "title": "first blocking finding"},
        {**BLOCKING[0], "title": "second blocking finding"},
    ]
    agent.run("submit-findings", "--file", findings_json(tmp_path, findings))

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "not part of this change",
    )

    assert env["state"] == "REVIEW_BLOCKED"
    assert env["data"]["remaining_blocking"] == 1
    assert (
        env["next"]["command"]
        == "agentic-preflight respond --id F002 --action fixed --commit <sha>"
    )

    status = agent.run("status")
    assert status["state"] == "REVIEW_BLOCKED"
    assert status["next"]["command"] == "agentic-preflight verify"


def test_respond_tolerates_the_stage_advancing_before_the_transaction_lock(blocked, monkeypatch):
    agent, _ = blocked
    original_transaction = Store.transaction
    advanced = False

    @contextmanager
    def advance_before_yield(self, run_id, *, expect_seq=None):
        nonlocal advanced
        with original_transaction(self, run_id, expect_seq=expect_seq) as doc:
            if not advanced:
                doc.state = State.REVIEW_GREEN
                advanced = True
            yield doc

    monkeypatch.setattr(Store, "transaction", advance_before_yield)

    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "resolved concurrently",
    )

    assert env["state"] == "REVIEW_GREEN"
    assert env["data"]["finding"]["status"] == "dismissed"


# -- respond, the claim is checked -----------------------------------------


def test_an_invented_commit_sha_is_rejected(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        "--commit",
        "0" * 40,
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
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        "--commit",
        sha,
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
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        "--commit",
        sha,
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
        "respond",
        "--id",
        "F001",
        "--action",
        "fixed",
        expect=ExitCode.PRECONDITION,
    )
    assert "--commit" in env["error"]["message"]


def test_an_unknown_finding_id_lists_the_valid_ids(blocked):
    agent, _ = blocked
    env = agent.run(
        "respond",
        "--id",
        "F999",
        "--action",
        "dismissed",
        "--note",
        "x",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "unknown_finding"
    assert "F001" in env["error"]["message"]


def test_responding_twice_to_the_same_finding_is_rejected(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "x",
        expect=ExitCode.PRECONDITION,
    )
    assert "already" in env["error"]["message"]


def test_respond_is_illegal_before_findings_are_submitted(agent):
    agent.run("start")
    env = agent.run(
        "respond",
        "--id",
        "F001",
        "--action",
        "dismissed",
        "--note",
        "x",
        expect=ExitCode.PRECONDITION,
    )
    assert env["error"]["code"] == "wrong_state"


# -- events and logs --------------------------------------------------------


def test_events_record_ordered_history_with_the_resolved_config_snapshot(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("events")
    events = env["data"]["events"]
    kinds = [event["event"] for event in events]
    assert "run_created" in kinds
    assert "findings_submitted" in kinds
    assert "finding_resolved" in kinds
    assert (
        kinds.index("run_created")
        < kinds.index("findings_submitted")
        < kinds.index("finding_resolved")
    )
    created = events[0]
    assert created["event"] == "run_created"
    assert created["config_snapshot"]["runtime"]["manager"] == "auto"
    assert len(created["config_digest"]) == 64


# -- abort ------------------------------------------------------------------


def test_abort_ends_an_in_place_run_and_clears_the_current_pointer(blocked):
    agent, wt = blocked
    env = agent.run("abort")
    assert env["state"] == "ABORTED"
    assert Path(wt).exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=Path(wt)) == "feature/x"
    env = agent.run("status")
    assert env["data"]["has_run"] is False


def test_abort_preserves_in_place_fix_commits(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("abort")
    assert env["data"]["discarded_fix_commits"] == []
    assert env["data"]["preserved_in_place_commits"] == [sha]
    assert git("rev-parse", "HEAD", cwd=Path(wt)) == sha


def test_abort_force_discards_unmerged_work(blocked):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    env = agent.run("abort", "--force")
    assert env["state"] == "ABORTED"


def test_reusable_runner_is_reused_but_secrets_and_nonignored_files_are_not(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "use reusable validation runner")
    write(feature_repo, ".gitignore", ".env\nnode_modules/\n")
    commit_all(feature_repo, "ignore dependency cache")
    write(feature_repo, ".env", "SECRET=first\n")
    first = agent.run("start")
    runner = Path(first["data"]["worktree_path"])
    write(runner, "node_modules/cache/index.js", "cached\n")
    write(runner, "scratch.txt", "not a cache\n")

    agent.run("abort")

    assert runner.exists()
    assert not (runner / ".env").exists()
    assert not (runner / "scratch.txt").exists()
    assert (runner / "node_modules/cache/index.js").is_file()

    second = agent.run("start")
    assert Path(second["data"]["worktree_path"]) == runner
    assert (runner / "node_modules/cache/index.js").is_file()


def test_failed_setup_still_cleans_copied_files_from_a_reusable_runner(agent, feature_repo):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[worktree]\nmode = 'reusable'\nsetup_command = 'exit 7'\n",
    )
    commit_all(feature_repo, "configure failing reusable setup")
    write(feature_repo, ".env", "SECRET=first\n")

    failed = agent.run("start", expect=ExitCode.STAGE_FAILED)
    runner = Path(failed["data"]["worktree_path"])
    assert (runner / ".env").is_file()
    status = agent.run("status")
    assert status["data"]["setup_failure"]["scope"] == "initial"
    assert status["next"]["command"] == "agentic-preflight abort --force"

    agent.run("abort", "--force")

    assert runner.exists()
    assert not (runner / ".env").exists()


def test_strict_mode_removes_each_run_worktree(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict worktrees")
    strict_agent = ScriptedAgent(feature_repo)

    started = strict_agent.run("start")
    path = Path(started["data"]["worktree_path"])
    strict_agent.run("abort")

    assert not path.exists()


def test_switching_to_strict_retires_the_idle_reusable_runner(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "use reusable validation runner")
    reusable = Path(agent.run("start")["data"]["worktree_path"])
    agent.run("abort")
    assert reusable.exists()

    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "switch validation to strict mode")
    strict = agent.run("start")

    assert not reusable.exists()
    assert Path(strict["data"]["worktree_path"]).name == strict["run_id"]


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

    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "use reusable validation runner")
    env = agent.run("start")
    run_id = env["run_id"]
    state_root = (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
        / "agentic-preflight"
    )

    shutil.rmtree(state_root / "runs" / run_id)
    (state_root / "current").unlink()

    env = agent.run("gc")
    assert run_id in env["data"]["orphans"]
