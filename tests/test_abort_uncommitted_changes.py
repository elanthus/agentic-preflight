"""`abort` must not delete uncommitted changes in an isolated validation worktree.

Releasing a `strict` worktree removes it, and releasing a `reusable` runner resets and
cleans it, so an uncommitted fix there is lost. `gc` already retains such a worktree;
a plain `abort` refuses the same way, and `--force` still discards the changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
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


def _start(agent: ScriptedAgent, feature_repo: Path, mode: str) -> tuple[dict, Path]:
    write(feature_repo, ".agentic-preflight.toml", f"[worktree]\nmode = '{mode}'\n")
    commit_all(feature_repo, f"use {mode} validation")
    env = agent.run("start")
    return env, Path(env["data"]["worktree_path"])


def _edit(wt: Path) -> None:
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'HI' if loud else 'hi'\n")
    write(wt, "notes/draft.txt", "half-finished fix\n")


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_abort_refuses_to_discard_uncommitted_changes(agent, feature_repo, mode):
    started, wt = _start(agent, feature_repo, mode)
    _edit(wt)

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert sorted(refused["data"]["uncommitted_paths"]) == ["notes/draft.txt", "src/app.py"]
    assert refused["data"]["fix_commits"] == []
    assert refused["data"]["worktree_path"] == str(wt)
    assert "notes/draft.txt" in refused["error"]["message"]
    assert "src/app.py" in refused["error"]["message"]
    assert refused["next"]["command"] == "agentic-preflight abort --force"
    # Nothing was released and the run is still live.
    assert (wt / "notes" / "draft.txt").read_text(encoding="utf-8") == "half-finished fix\n"
    assert "HI" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    status = agent.run("status")
    assert status["run_id"] == started["run_id"]
    assert status["state"] != "ABORTED"


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_abort_force_discards_uncommitted_changes(agent, feature_repo, mode):
    _, wt = _start(agent, feature_repo, mode)
    _edit(wt)

    env = agent.run("abort", "--force")

    assert env["state"] == "ABORTED"
    assert not (wt / "notes" / "draft.txt").exists()
    if mode == "strict":
        assert not wt.exists()
    else:
        assert "HI" not in (wt / "src" / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["strict", "reusable"])
def test_a_copied_file_is_not_uncommitted_work(agent, feature_repo, mode):
    """The copied `.env` is not the agent's work, even when Git can see it."""
    write(feature_repo, ".env", "SECRET=hunter2\n")
    started, wt = _start(agent, feature_repo, mode)
    assert started["data"]["copied_files"] == [".env"]
    # Staging the ignored copy makes it show in `git status`.
    git("add", "-f", ".env", cwd=wt)
    assert ".env" in git("status", "--porcelain", cwd=wt)

    env = agent.run("abort")

    assert env["state"] == "ABORTED"
    assert not (wt / ".env").exists()


def test_a_copied_file_is_left_out_of_the_refusal(agent, feature_repo):
    write(feature_repo, ".env", "SECRET=hunter2\n")
    _, wt = _start(agent, feature_repo, "strict")
    git("add", "-f", ".env", cwd=wt)
    write(wt, "notes/draft.txt", "half-finished fix\n")

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["data"]["uncommitted_paths"] == ["notes/draft.txt"]
    assert "hunter2" not in str(refused)


def test_abort_names_fix_commits_and_uncommitted_changes_together(agent, feature_repo, tmp_path):
    """One refusal names everything `--force` would discard."""
    _, wt = _start(agent, feature_repo, "strict")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, BLOCKING))
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'HI' if loud else 'hi'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "fix the flag", cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt)
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
    write(wt, "notes/draft.txt", "half-finished follow-up\n")

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert refused["data"]["fix_commits"] == [sha]
    assert refused["data"]["uncommitted_paths"] == ["notes/draft.txt"]
    assert sha in refused["error"]["message"]
    assert "notes/draft.txt" in refused["error"]["message"]


def test_in_place_abort_leaves_uncommitted_changes_alone(agent, feature_repo):
    env = agent.run("start")
    wt = Path(env["data"]["worktree_path"])
    assert wt == feature_repo
    write(wt, "notes/draft.txt", "half-finished fix\n")

    aborted = agent.run("abort")

    assert aborted["state"] == "ABORTED"
    assert (wt / "notes" / "draft.txt").read_text(encoding="utf-8") == "half-finished fix\n"


def test_abort_refuses_when_the_worktree_cannot_be_inspected(agent, feature_repo):
    """A worktree Git cannot read may still hold work, so a plain abort stops."""
    _, wt = _start(agent, feature_repo, "strict")
    write(wt, "notes/draft.txt", "half-finished fix\n")
    (wt / ".git").write_text("gitdir: /nonexistent/agentic-preflight-test\n", encoding="utf-8")

    refused = agent.run("abort", expect=ExitCode.NEEDS_CONFIRM)

    assert refused["error"]["code"] == "unmerged_work"
    assert "could not be inspected" in refused["error"]["message"]
    assert refused["data"]["worktree_path"] == str(wt)
    assert refused["next"]["command"] == "agentic-preflight abort --force"
    assert (wt / "notes" / "draft.txt").is_file()
