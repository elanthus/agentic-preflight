"""Active gate ownership is scoped to one source worktree, not the clone."""

import json
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _second_feature_worktree(feature_repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "feature-y"
    git("branch", "feature/y", "main", cwd=feature_repo)
    git("worktree", "add", str(path), "feature/y", cwd=feature_repo)
    write(path, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    commit_all(path, "change the greeting")
    return path


def _state_root(repo: Path) -> Path:
    return (
        Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
        / "agentic-preflight"
    )


def test_two_linked_worktrees_can_run_gates_independently(feature_repo, tmp_path):
    second_repo = _second_feature_worktree(feature_repo, tmp_path)
    first_agent = ScriptedAgent(feature_repo, transport="subprocess")
    second_agent = ScriptedAgent(second_repo, transport="subprocess")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_agent.run, "start", "--intent", "prepare feature x")
        second_future = pool.submit(
            second_agent.run, "start", "--intent", "prepare feature y"
        )
        first = first_future.result(timeout=30)
        second = second_future.result(timeout=30)

    assert first["run_id"] != second["run_id"]
    assert first_agent.run("status")["run_id"] == first["run_id"]
    assert second_agent.run("status")["run_id"] == second["run_id"]
    assert first_agent.run("--run", second["run_id"], "status")["run_id"] == second["run_id"]

    first_agent.run("abort")

    assert second_agent.run("status")["run_id"] == second["run_id"]
    inventory = second_agent.run("status", "--all")["data"]
    assert {item["run_id"] for item in inventory["runs"]} >= {
        first["run_id"],
        second["run_id"],
    }
    assert second["run_id"] in inventory["active"].values()
    assert first["run_id"] not in inventory["active"].values()


def test_moving_the_source_head_orphans_the_stale_run_on_the_next_start(
    feature_repo, tmp_path
):
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start", "--intent", "prepare the original head")
    write(feature_repo, "src/extra.py", "value = 1\n")
    commit_all(feature_repo, "advance the source branch")

    second = agent.run("start", "--intent", "prepare the advanced head")

    assert second["run_id"] != first["run_id"]
    old = json.loads(
        (_state_root(feature_repo) / "runs" / first["run_id"] / "run.json").read_text()
    )
    assert old["state"] == "ORPHANED"
    assert old["orphaned_reason"] == "source worktree moved"


def test_commands_from_an_isolated_validator_resolve_the_owning_run(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    source_agent = ScriptedAgent(feature_repo)
    started = source_agent.run("start")
    validator = Path(started["data"]["worktree_path"])

    validator_agent = ScriptedAgent(validator)
    from_validator = validator_agent.run("status")

    assert from_validator["run_id"] == started["run_id"]
    assert from_validator["data"]["source_worktree_path"] == str(feature_repo)
    refused = validator_agent.run("start", expect=ExitCode.PRECONDITION)
    assert refused["run_id"] == started["run_id"]
    assert refused["data"]["source_worktree_path"] == str(feature_repo)


def test_gc_orphans_an_abandoned_run_whose_ownership_pointer_disappeared(feature_repo):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start")
    state_root = _state_root(feature_repo)
    for pointer in (state_root / "active").glob("*.run"):
        if pointer.read_text(encoding="utf-8").strip() == started["run_id"]:
            pointer.unlink()

    collected = agent.run("gc")

    assert started["run_id"] in collected["data"]["removed"]
    old = json.loads(
        (state_root / "runs" / started["run_id"] / "run.json").read_text()
    )
    assert old["state"] == "ORPHANED"
    assert old["orphaned_reason"] == "source worktree lease disappeared"


def test_replace_refuses_while_the_existing_run_is_executing(feature_repo):
    agent = ScriptedAgent(feature_repo)
    started = agent.run("start", "--intent", "first objective")
    lock_path = _state_root(feature_repo) / "runs" / started["run_id"] / ".operation.lock"
    holder_script = textwrap.dedent(
        """
        import sys
        from agentic_preflight import filelock
        with filelock.exclusive(sys.argv[1]):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        refused = agent.run(
            "start",
            "--replace",
            "--intent",
            "different objective",
            expect=ExitCode.PRECONDITION,
        )
        assert "executing a command" in refused["error"]["message"]
        assert refused["run_id"] == started["run_id"]
    finally:
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.wait(timeout=5)


def test_replace_preserves_an_isolated_validation_checkout(feature_repo):
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict validation")
    agent = ScriptedAgent(feature_repo)
    first = agent.run("start", "--intent", "first objective")
    first_validator = Path(first["data"]["worktree_path"])

    second = agent.run("start", "--replace", "--intent", "different objective")

    assert second["run_id"] != first["run_id"]
    assert first_validator.exists()
    old = json.loads(
        (_state_root(feature_repo) / "runs" / first["run_id"] / "run.json").read_text()
    )
    assert old["state"] == "ORPHANED"
    assert old["worktree_released"] is False
