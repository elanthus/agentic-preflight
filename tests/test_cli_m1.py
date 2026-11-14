"""M1 resolution loop: respond, fix-commit verification, abort, gc, events."""

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from agentic_preflight.machine import State
from agentic_preflight.store import Store
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent, findings_json

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
    def advance_before_yield(self, run_id, *, findings=None):
        nonlocal advanced
        with original_transaction(self, run_id, findings=findings) as doc:
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
    assert events[0]["event"] == "state_transition"
    assert events[0]["from"] is None
    assert events[0]["to"] == "CREATED"
    created = events[kinds.index("run_created")]
    assert "runtime" not in created["config_snapshot"]
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


def test_isolated_run_worktree_lands_under_the_user_cache(
    agent, feature_repo, tmp_path, monkeypatch
):
    cache = tmp_path / "cache-home"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation worktrees")

    env = agent.run("start")

    root = (cache / "agentic-preflight" / "worktrees").resolve()
    assert Path(env["data"]["worktree_path"]).resolve().is_relative_to(root)
    assert not (feature_repo.parent / ".agentic-preflight-worktrees").exists()


def test_gc_reports_but_keeps_a_runner_at_the_old_sibling_location(agent, feature_repo):
    legacy = feature_repo.parent / ".agentic-preflight-worktrees" / "old-runner"
    git("worktree", "add", "--detach", str(legacy), "HEAD", cwd=feature_repo)

    env = agent.run("gc", "--force")

    assert [Path(p).resolve() for p in env["data"]["legacy_worktrees"]] == [legacy.resolve()]
    assert "git worktree remove" in env["next"]["instruction"]
    assert legacy.is_dir()


@pytest.mark.parametrize("preserve", [None, "dirty", "locked", "leased"])
def test_gc_reclaims_only_eligible_legacy_runners(agent, feature_repo, preserve):
    legacy = feature_repo.parent / ".agentic-preflight-worktrees" / "old-clone" / "runner"
    git("worktree", "add", "--detach", str(legacy), "HEAD", cwd=feature_repo)
    if preserve == "dirty":
        write(legacy, "untracked-work.txt", "keep this work")
    elif preserve == "locked":
        git("worktree", "lock", str(legacy), cwd=feature_repo)
    elif preserve == "leased":
        git("checkout", "-b", "ap/leased-old-runner", cwd=legacy)

    env = agent.run("gc", "--force")

    assert legacy.exists() is (preserve is not None)
    assert env["data"]["reclaimed_runners"] == ([] if preserve else [str(legacy)])
    assert [Path(p).resolve() for p in env["data"]["legacy_worktrees"]] == (
        [legacy.resolve()] if preserve else []
    )


def test_gc_reports_a_clean_store_as_nothing_to_do(agent):
    agent.run("start")
    env = agent.run("gc")
    assert env["data"]["removed"] == []


def test_gc_reclaims_an_aborted_runs_leftovers(blocked):
    agent, _ = blocked
    agent.run("abort")
    env = agent.run("gc")
    assert env["ok"] is True


def _run_dir(repo, run_id):
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
    return common / "agentic-preflight" / "runs" / run_id


def _last_changed_days_ago(repo, run_id, days):
    path = _run_dir(repo, run_id) / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    stamp = datetime.now(UTC) - timedelta(days=days)
    raw["updated_at"] = stamp.isoformat(timespec="seconds")
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_gc_prunes_a_reclaimed_record_past_the_retention_window(blocked, feature_repo):
    agent, _ = blocked
    run_id = agent.run("abort")["run_id"]
    agent.run("gc")
    _last_changed_days_ago(feature_repo, run_id, 91)

    env = agent.run("gc")

    assert env["data"]["pruned"] == [run_id]
    assert run_id not in env["data"]["runs_known"]
    assert not _run_dir(feature_repo, run_id).exists()


def test_gc_keeps_records_inside_the_window_or_when_retention_is_off(blocked, feature_repo):
    agent, _ = blocked
    run_id = agent.run("abort")["run_id"]
    agent.run("gc")
    _last_changed_days_ago(feature_repo, run_id, 89)
    assert agent.run("gc")["data"]["pruned"] == []

    write(feature_repo, ".agentic-preflight.toml", "[retention]\nrun_days = 0\n")
    _last_changed_days_ago(feature_repo, run_id, 3650)
    assert agent.run("gc")["data"]["pruned"] == []
    assert _run_dir(feature_repo, run_id).exists()


def test_gc_never_prunes_a_run_whose_command_holds_the_lock(blocked, feature_repo):
    agent, _ = blocked
    run_id = agent.run("abort")["run_id"]
    agent.run("gc")
    _last_changed_days_ago(feature_repo, run_id, 91)
    store = Store(_run_dir(feature_repo, run_id).parent.parent)

    with store.operation(run_id):
        assert agent.run("gc")["data"]["pruned"] == []
    assert (_run_dir(feature_repo, run_id) / "run.json").exists()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX directory permissions"
)
def test_gc_retries_a_prune_that_failed_partway(blocked, feature_repo):
    agent, _ = blocked
    run_id = agent.run("abort")["run_id"]
    agent.run("gc")
    _last_changed_days_ago(feature_repo, run_id, 91)
    stuck = _run_dir(feature_repo, run_id) / "stuck"
    stuck.mkdir()
    (stuck / "file").write_text("x", encoding="utf-8")
    stuck.chmod(0o500)
    try:
        env = agent.run("gc")
    finally:
        stuck.chmod(0o700)

    assert env["data"]["pruned"] == []
    assert [item["run_id"] for item in env["data"]["retained"]] == [run_id]
    assert (_run_dir(feature_repo, run_id) / "run.json").exists()
    assert agent.run("gc")["data"]["pruned"] == [run_id]


def test_gc_skips_a_record_with_an_unreadable_timestamp(blocked, feature_repo):
    agent, _ = blocked
    run_id = agent.run("abort")["run_id"]
    agent.run("gc")
    path = _run_dir(feature_repo, run_id) / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["updated_at"] = "not a timestamp"
    path.write_text(json.dumps(raw), encoding="utf-8")

    env = agent.run("gc")

    assert env["ok"] is True
    assert env["data"]["pruned"] == []


def test_gc_never_prunes_a_run_that_is_still_active(agent, feature_repo):
    run_id = agent.run("start")["run_id"]
    _last_changed_days_ago(feature_repo, run_id, 3650)

    env = agent.run("gc")

    assert env["data"]["pruned"] == []
    assert _run_dir(feature_repo, run_id).exists()


def test_gc_never_deletes_unmerged_work_without_force(blocked, feature_repo):
    agent, wt = blocked
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)

    env = agent.run("gc")
    assert env["data"]["removed"] == []
    assert env["data"]["retained"]
    from pathlib import Path

    assert Path(wt).exists()


def _blocked_isolated_run(agent, feature_repo, tmp_path, mode):
    write(feature_repo, ".agentic-preflight.toml", f"[worktree]\nmode = '{mode}'\n")
    commit_all(feature_repo, f"use {mode} validation")
    env = agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    return env["run_id"], Path(env["data"]["worktree_path"])


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_gc_reclaims_and_prunes_a_force_aborted_run_whose_fixes_were_discarded(
    agent, feature_repo, tmp_path, mode
):
    """`abort --force` already chose to discard the fix commits; gc must not keep them."""
    run_id, wt = _blocked_isolated_run(agent, feature_repo, tmp_path, mode)
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    aborted = agent.run("abort", "--force")
    assert aborted["data"]["discarded_fix_commits"] == [sha]

    env = agent.run("gc")

    assert run_id in env["data"]["removed"]
    assert [item for item in env["data"]["retained"] if item.get("run_id") == run_id] == []
    _last_changed_days_ago(feature_repo, run_id, 91)
    assert agent.run("gc")["data"]["pruned"] == [run_id]
    assert not _run_dir(feature_repo, run_id).exists()


def test_abort_reports_a_locked_strict_worktree_and_keeps_it(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    started = agent.run("start")
    wt = Path(started["data"]["worktree_path"])
    git("worktree", "lock", str(wt), cwd=feature_repo)

    env = agent.run("abort", expect=ExitCode.ERROR)

    assert "locked" in env["error"]["message"]
    assert "do not unlock it yourself" in env["error"]["message"]
    assert wt.is_dir()
    assert agent.run("status")["state"] == started["state"]

    git("worktree", "unlock", str(wt), cwd=feature_repo)
    assert agent.run("abort")["state"] == "ABORTED"
    assert not wt.exists()


def test_gc_retains_a_locked_strict_worktree_and_collects_the_rest(agent, feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    started = agent.run("start")
    run_id = started["run_id"]
    wt = Path(started["data"]["worktree_path"])
    git("worktree", "lock", str(wt), cwd=feature_repo)
    # The user moves the source branch, so gc orphans the stale run.
    write(feature_repo, "src/other.py", "x = 1\n")
    commit_all(feature_repo, "unrelated source work")

    env = agent.run("gc", "--force")

    assert env["ok"] is True
    assert run_id not in env["data"]["removed"]
    assert {
        "run_id": run_id,
        "reason": "validation worktree is locked",
        "worktree_path": str(wt),
    } in env["data"]["retained"]
    assert wt.is_dir()
    assert "ask whether to unlock it" in env["next"]["instruction"]
    assert env["next"]["command"] is None


def test_gc_instruction_covers_every_retained_reason(agent, feature_repo, tmp_path):
    """An unrecorded-work run must not hide the guidance for a locked worktree."""
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    dirty = agent.run("start")
    dirty_wt = Path(dirty["data"]["worktree_path"])
    write(dirty_wt, "notes/draft.txt", "half-finished fix\n")

    other = tmp_path / "feature-y"
    git("branch", "feature/y", "main", cwd=feature_repo)
    git("worktree", "add", str(other), "feature/y", cwd=feature_repo)
    write(other, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    write(other, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit_all(other, "change the greeting")
    other_agent = ScriptedAgent(other)
    locked = other_agent.run("start")
    locked_wt = Path(locked["data"]["worktree_path"])
    git("worktree", "lock", str(locked_wt), cwd=feature_repo)
    # Both sources move on, so gc orphans both stale runs.
    for source in (feature_repo, other):
        write(source, "src/other.py", "x = 1\n")
        commit_all(source, "unrelated source work")

    env = agent.run("gc")

    reasons = {item.get("run_id"): item.get("reason") for item in env["data"]["retained"]}
    assert reasons[dirty["run_id"]] == "validation worktree has uncommitted changes"
    assert reasons[locked["run_id"]] == "validation worktree is locked"
    instruction = env["next"]["instruction"]
    assert "`gc --force` discards any work they hold" in instruction
    assert "Do not unlock it yourself." in instruction
    assert instruction.index("gc --force") < instruction.index("Do not unlock")
    assert env["next"]["command"] is None
    assert dirty_wt.is_dir()
    assert locked_wt.is_dir()


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_gc_keeps_an_aborted_runs_fix_commits_without_a_forced_abort_marker(
    agent, feature_repo, tmp_path, mode
):
    """Only a recorded forced abort discards fixes; anything else stays retained."""
    run_id, wt = _blocked_isolated_run(agent, feature_repo, tmp_path, mode)
    sha = fix_commit(wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    agent.run("abort", "--force")
    events = _run_dir(feature_repo, run_id) / "events.jsonl"
    kept = [
        line
        for line in events.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") != "aborted"
    ]
    events.write_text("\n".join(kept) + "\n", encoding="utf-8")

    env = agent.run("gc")

    assert run_id not in env["data"]["removed"]
    assert {
        "run_id": run_id,
        "reason": "unmerged fix commits",
        "fix_commits": [sha],
    } in env["data"]["retained"]


@pytest.mark.parametrize("mode", ["strict", "reusable"])
@pytest.mark.parametrize("work", ["unresponded_commit", "uncommitted_file"])
def test_gc_retains_a_validation_worktree_holding_unrecorded_work(
    agent, feature_repo, tmp_path, mode, work
):
    """Work that `respond` never recorded is still work: gc must not discard it."""
    run_id, wt = _blocked_isolated_run(agent, feature_repo, tmp_path, mode)
    if work == "unresponded_commit":
        sha = fix_commit(wt)
    else:
        write(wt, "notes/draft.txt", "half-finished fix\n")
    # The user moves the source branch, so gc orphans the stale run.
    write(feature_repo, "src/other.py", "x = 1\n")
    commit_all(feature_repo, "unrelated source work")

    env = agent.run("gc")

    assert run_id not in env["data"]["removed"]
    retained = [item for item in env["data"]["retained"] if item.get("run_id") == run_id]
    assert retained == [
        {
            "run_id": run_id,
            "reason": (
                "validation worktree holds unrecorded commits"
                if work == "unresponded_commit"
                else "validation worktree has uncommitted changes"
            ),
            "worktree_path": str(wt),
        }
    ]
    assert wt.exists()
    if work == "unresponded_commit":
        assert git("rev-parse", "HEAD", cwd=wt) == sha
        assert git("rev-parse", f"ap/{run_id}", cwd=feature_repo) == sha
    else:
        assert (wt / "notes" / "draft.txt").read_text(encoding="utf-8") == "half-finished fix\n"
    record = Store(_run_dir(feature_repo, run_id).parent.parent).load_run(run_id)
    assert record.state is State.ORPHANED
    assert record.worktree_released is False
    assert "`gc --force` discards any work they hold" in env["next"]["instruction"]
    assert env["next"]["command"] is None

    forced = agent.run("gc", "--force")
    assert run_id in forced["data"]["removed"]


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_gc_releases_a_validation_worktree_whose_head_has_the_recorded_tree(
    agent, feature_repo, tmp_path, mode
):
    """A rewritten commit with the recorded head's tree holds no work to keep."""
    run_id, wt = _blocked_isolated_run(agent, feature_repo, tmp_path, mode)
    recorded = git("rev-parse", "HEAD", cwd=wt)
    git("commit", "--amend", "-m", "rewritten without changing the tree", cwd=wt)
    assert git("rev-parse", "HEAD", cwd=wt) != recorded
    write(feature_repo, "src/other.py", "x = 1\n")
    commit_all(feature_repo, "unrelated source work")

    env = agent.run("gc")

    assert run_id in env["data"]["removed"]
    assert [item for item in env["data"]["retained"] if item.get("run_id") == run_id] == []


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_a_healthy_isolated_run_is_not_reported_as_an_orphan(agent, feature_repo, mode):
    """A branch checked out in a linked worktree is listed by git with a `+` mark."""
    write(feature_repo, ".agentic-preflight.toml", f"[worktree]\nmode = '{mode}'\n")
    commit_all(feature_repo, f"use {mode} validation")
    run_id = agent.run("start")["run_id"]

    env = agent.run("gc")

    assert env["data"]["orphans"] == []
    assert env["data"]["removed"] == []
    assert (env.get("next") or {}).get("command") is None
    inventory = agent.run("status", "--all")
    (summary,) = [item for item in inventory["data"]["runs"] if item["run_id"] == run_id]
    assert summary["active"] is True
    assert summary["state"] != "ORPHANED"
    assert summary["orphaned_reason"] is None
    assert agent.run("status")["run_id"] == run_id


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
    for pointer in (state_root / "active").glob("*.run"):
        if pointer.read_text(encoding="utf-8").strip() == run_id:
            pointer.unlink()

    env = agent.run("gc")
    assert run_id in env["data"]["orphans"]


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("bad_id", ["r_a_unreadable", "r_z_unreadable"])
@pytest.mark.parametrize(
    "kind",
    [
        "unknown_field",
        "invalid_value",
        "malformed_json",
        "invalid_record",
        "invalid_encoding",
        "io_error",
    ],
)
def test_gc_retains_unreadable_resources_and_collects_other_runs(
    feature_repo, tmp_path, monkeypatch, force, bad_id, kind
):
    from agentic_preflight import runs
    from tests.conftest import make_run, unreadable_run_bytes

    session = runs.open_session(feature_repo)
    store = session.store
    for run_id in (bad_id, "r_m_collectible"):
        wt = tmp_path / run_id
        git("worktree", "add", "-b", f"ap/{run_id}", str(wt), "main", cwd=feature_repo)
        run = make_run(run_id)
        run.state = State.DONE
        run.head_sha = run.source_head_sha = git("rev-parse", "main", cwd=feature_repo)
        run.pushed_sha = run.head_sha
        run.worktree_path = str(wt)
        run.worktree_branch = f"ap/{run_id}"
        run.config_snapshot = {"worktree": {"mode": "strict"}}
        store.create_run(run)
    bad = store.load_run(bad_id)
    path = store.run_path(bad_id)
    original = unreadable_run_bytes(bad, kind)
    path.write_bytes(original)
    store.set_active(session.owner_id, bad_id)
    store.set_active("another-owner", bad_id)
    aliases = {p.name: p.read_bytes() for p in store.active_dir.glob("*.run")}
    branch = git("rev-parse", f"ap/{bad_id}", cwd=feature_repo)
    if kind == "io_error":
        read = Path.read_text
        stat = Path.stat

        def denied(record, *args, **kwargs):
            if record == path:
                raise PermissionError(13, "DO_NOT_ECHO_RECORD_VALUES")
            return read(record, *args, **kwargs)

        def denied_stat(record, *args, **kwargs):
            if record == path:
                raise PermissionError(13, "DO_NOT_ECHO_RECORD_VALUES")
            return stat(record, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
        monkeypatch.setattr(Path, "stat", denied_stat)
    result = ScriptedAgent(feature_repo).run("gc", *(["--force"] if force else []))
    assert result["data"]["removed"] == ["r_m_collectible"]
    assert not (tmp_path / "r_m_collectible").exists()
    assert bad_id in result["data"]["runs_known"]
    assert bad_id not in result["data"]["orphans"]
    retained = result["data"]["retained"]
    assert len(retained) == 1
    assert retained[0]["run_id"] == bad_id
    assert retained[0]["path"] == str(path)
    assert retained[0]["reason"] == (
        "unreadable" if kind in {"invalid_encoding", "io_error"} else "invalid"
    )
    assert "DO_NOT_ECHO_RECORD_VALUES" not in json.dumps(result)
    assert result["next"]["command"] is None
    assert "compatible" in result["next"]["instruction"]
    assert path.read_bytes() == original
    assert (tmp_path / bad_id).exists()
    assert git("rev-parse", f"ap/{bad_id}", cwd=feature_repo) == branch
    assert {p.name: p.read_bytes() for p in store.active_dir.glob("*.run")} == aliases


def test_gc_cleanup_failures_still_surface(feature_repo, monkeypatch):
    from agentic_preflight import runs
    from tests.conftest import make_run

    store = runs.open_session(feature_repo).store
    run = make_run()
    run.state = State.DONE
    run.pushed_sha = run.head_sha
    run.worktree_path = str(feature_repo)
    store.create_run(run)

    def failed(*args):
        raise OSError("cleanup failed")

    monkeypatch.setattr("agentic_preflight.runs.lifecycle.release_run_worktree", failed)
    result = ScriptedAgent(feature_repo).run("gc", expect=1)
    assert result["error"]["code"] == "internal_error"
    assert store.load_run(run.run_id).worktree_released is False


@pytest.mark.parametrize("failing", ["status", "tree"])
def test_gc_survives_git_failing_to_start_in_the_validation_worktree(
    tmp_path, monkeypatch, failing
):
    """`subprocess` raises OSError, not GitError, when Git cannot start in the checkout."""
    import subprocess

    from agentic_preflight import gitx
    from agentic_preflight.runs.lifecycle import (
        UNINSPECTABLE_WORKTREE,
        UNRECORDED_COMMITS,
        _unrecorded_work,
    )
    from tests.conftest import make_run

    def denied(cwd, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(cwd))

    if failing == "status":
        monkeypatch.setattr(gitx, "run", denied)
    else:
        clean = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        monkeypatch.setattr(gitx, "run", lambda cwd, *args, **kwargs: clean)
        monkeypatch.setattr(gitx, "rev_parse", lambda cwd, ref: "c" * 40)
        monkeypatch.setattr(gitx, "tree_sha", denied)

    assert _unrecorded_work(make_run(), str(tmp_path)) == (
        UNINSPECTABLE_WORKTREE if failing == "status" else UNRECORDED_COMMITS
    )


def test_gc_reads_every_path_of_a_porcelain_status_including_rename_sources():
    from agentic_preflight.runs.lifecycle import _porcelain_paths

    status = " M src/app.py\0R  new.py\0old.py\0?? notes/a b.txt\0"
    assert _porcelain_paths(status) == ["src/app.py", "new.py", "old.py", "notes/a b.txt"]
    assert _porcelain_paths("") == []
