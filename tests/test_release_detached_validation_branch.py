"""Releasing a validation checkout must not delete commits its branch holds.

`start --replace` and `gc` release an isolated validation checkout, which deletes
its `ap/<run_id>` branch. A checkout detached at the reviewed head hides what that
branch holds, so the release decision reads the branch tip as well as `HEAD`.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import configure_base, git, write
from tests.driver import ScriptedAgent

REUSABLE = "[docs]\nenabled = false\n\n[worktree]\nmode = 'reusable'\n"
STRICT = "[docs]\nenabled = false\n\n[worktree]\nmode = 'strict'\n"


def _hidden_commit(feature_repo: Path, config: str) -> tuple[ScriptedAgent, str, Path, str]:
    """Start a run, commit on its validation branch, then detach at the reviewed head."""
    configure_base(feature_repo, config)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start", "--intent", "first objective")
    wt = Path(started["data"]["worktree_path"])
    head = git("rev-parse", "HEAD", cwd=wt)
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "fix outside respond", cwd=wt)
    fix = git("rev-parse", "HEAD", cwd=wt)
    git("checkout", "-q", "--detach", head, cwd=wt)
    return agent, started["run_id"], wt, fix


def _record(feature_repo: Path, run_id: str) -> dict:
    root = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo))
    path = root / "agentic-preflight" / "runs" / run_id / "run.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_replace_keeps_a_reusable_runner_whose_branch_holds_a_hidden_commit(feature_repo):
    agent, run_id, wt, fix = _hidden_commit(feature_repo, REUSABLE)

    agent.run("start", "--replace", "--intent", "different objective")

    assert _record(feature_repo, run_id)["worktree_released"] is False
    assert wt.exists()
    assert git("rev-parse", f"refs/heads/ap/{run_id}", cwd=feature_repo) == fix


def test_replace_still_releases_an_idle_reusable_runner(feature_repo):
    """The branch check keeps the runner only when the branch holds something."""
    configure_base(feature_repo, REUSABLE)
    agent = ScriptedAgent(feature_repo)
    run_id = agent.run("start", "--intent", "first objective")["run_id"]

    agent.run("start", "--replace", "--intent", "different objective")

    assert _record(feature_repo, run_id)["worktree_released"] is True


def test_gc_retains_a_strict_checkout_whose_branch_holds_a_hidden_commit(feature_repo):
    agent, run_id, wt, fix = _hidden_commit(feature_repo, STRICT)
    agent.run("start", "--replace", "--intent", "different objective")

    env = agent.run("gc")

    assert run_id not in env["data"]["removed"]
    assert {
        "run_id": run_id,
        "reason": "validation worktree holds unrecorded commits",
        "worktree_path": str(wt),
    } in env["data"]["retained"]
    assert wt.exists()
    assert git("rev-parse", f"refs/heads/ap/{run_id}", cwd=feature_repo) == fix


def _rewritten_branch(feature_repo: Path, config: str) -> tuple[ScriptedAgent, str, Path, str]:
    """Start a run, rewrite its validation branch off the reviewed head, then detach there."""
    configure_base(feature_repo, config)
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start", "--intent", "first objective")
    run_id, wt = started["run_id"], Path(started["data"]["worktree_path"])
    head = git("rev-parse", "HEAD", cwd=wt)
    git("reset", "-q", "--hard", "HEAD~1", cwd=wt)
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'REWRITTEN'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "rewrite the reviewed head", cwd=wt)
    rewritten = git("rev-parse", "HEAD", cwd=wt)
    git("checkout", "-q", "--detach", head, cwd=wt)
    return agent, run_id, wt, rewritten


def test_replace_keeps_a_reusable_runner_whose_branch_was_rewritten(feature_repo):
    """An unreadable branch history keeps the runner leased for `gc`."""
    agent, run_id, wt, rewritten = _rewritten_branch(feature_repo, REUSABLE)

    agent.run("start", "--replace", "--intent", "different objective")

    assert _record(feature_repo, run_id)["worktree_released"] is False
    assert wt.exists()
    assert git("rev-parse", f"refs/heads/ap/{run_id}", cwd=feature_repo) == rewritten


def test_gc_retains_a_strict_checkout_whose_branch_was_rewritten(feature_repo):
    """A branch that no longer descends from the reviewed head cannot be read: keep it."""
    agent, run_id, wt, rewritten = _rewritten_branch(feature_repo, STRICT)
    agent.run("start", "--replace", "--intent", "different objective")

    env = agent.run("gc")

    assert {
        "run_id": run_id,
        "reason": "validation worktree could not be inspected",
        "worktree_path": str(wt),
    } in env["data"]["retained"]
    assert git("rev-parse", f"refs/heads/ap/{run_id}", cwd=feature_repo) == rewritten
