"""The whole gate, end to end, as a scripted agent would drive it."""

import json
import os
import stat
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent

CONFIG = """[general]
base_ref = "main"

[commands]
lint = "true"
test = "true"

[docs]
enabled = true
"""


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


@pytest.fixture
def gh_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  auth) exit 0 ;;\n'
        '  pr) if [ "$2" = "list" ]; then echo "[]"; else echo "https://github.com/owner/repo/pull/7"; fi; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


def test_the_full_happy_path(feature_repo, bare_remote, tmp_path, gh_stub):
    """review -> test -> docs -> lint -> mergeback -> gate -> push -> pr -> ledger."""
    write(feature_repo, ".agentic-preflight.toml", CONFIG)
    commit_all(feature_repo, "configure agentic-preflight")

    agent = ScriptedAgent(feature_repo)

    env = agent.run("init")
    assert env["data"]["hook_installed"] is True

    env = agent.run("start")
    run_id = env["run_id"]
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"

    env = agent.run("context")
    assert "loud=False" in env["data"]["diff"]

    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "REVIEW_GREEN"

    assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"

    env = agent.run("context", "--section", "docs")
    assert env["data"]["doc_surface"]

    env = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert env["state"] == "DOCS_GREEN"

    assert agent.run("stage", "run", "lint")["state"] == "LINT_GREEN"

    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    assert env["data"]["tree_equivalent"] is True

    env = agent.run("gate")
    token = env["data"]["token"]

    env = agent.run("push", "--confirm", token)
    assert env["state"] == "PUSHED"

    git("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=feature_repo)
    env = agent.run("pr")
    assert env["state"] == "PR_OPEN"
    assert env["data"]["pr_url"].endswith("/pull/7")

    # The ledger records this exact tip as green across every enabled stage.
    state_root = Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo)
    ) / "agentic-preflight"
    ledger = json.loads((state_root / "ledger.json").read_text())
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert head in ledger["entries"]
    entry = ledger["entries"][head]
    assert entry["run_id"] == run_id
    assert set(entry["stages"]) >= {"review", "docs", "lint", "test"}


def test_every_step_obeys_the_next_pointer(feature_repo, tmp_path):
    """An agent that only ever runs `next.command` should reach the gate.

    This is the anti-wandering device working as intended: no independent
    knowledge of the workflow is required.
    """
    write(feature_repo, ".agentic-preflight.toml", CONFIG)
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)

    env = agent.run("start")
    empty = findings_json(tmp_path, [])

    seen_states = [env["state"]]
    for _ in range(12):
        command = env["next"]["command"]
        if command is None or "gate" in command:
            break
        argv = command.replace("agentic-preflight ", "").split()
        if argv[0] == "submit-findings":
            argv = ["submit-findings", "--file", empty]
        env = agent.run(*argv)
        seen_states.append(env["state"])

    assert "REVIEW_GREEN" in seen_states
    assert "DOCS_GREEN" in seen_states
    assert "LINT_GREEN" in seen_states
    assert "TEST_GREEN" in seen_states
    assert env["state"] in ("VERIFIED", "AWAITING_PUSH_CONFIRM")


def test_seq_increases_monotonically_across_a_run(feature_repo, tmp_path):
    write(feature_repo, ".agentic-preflight.toml", CONFIG)
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)

    agent.run("start")
    seqs = [agent.run("status")["data"]["seq"]]
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    seqs.append(agent.run("status")["data"]["seq"])
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    seqs.append(agent.run("status")["data"]["seq"])

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_finding_ids_are_never_reused_across_stages(feature_repo, tmp_path):
    write(feature_repo, ".agentic-preflight.toml", CONFIG)
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)

    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, [
        {"path": "src/app.py", "severity": "low", "action": "no_op", "title": "a"},
        {"path": "src/app.py", "severity": "low", "action": "no_op", "title": "b"},
    ]))
    agent.run("stage", "run", "test")
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", findings_json(tmp_path, [
        {"path": "README.md", "severity": "low", "action": "no_op", "title": "c"},
    ]))

    ids = [f["id"] for f in agent.run("status")["data"]["findings"]]
    assert ids == ["F001", "F002", "F003"]
    assert len(set(ids)) == len(ids)


def test_a_blocked_run_cannot_reach_the_gate(feature_repo, tmp_path):
    """The gate is unreachable while anything blocks — structurally, not by prose."""
    write(feature_repo, ".agentic-preflight.toml", CONFIG)
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)

    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, [
        {"path": "src/app.py", "severity": "critical", "action": "ask_user",
         "title": "is this the intended public API?"},
    ]))

    for argv in (("gate",), ("stage", "run", "lint"), ("mergeback",)):
        env = agent.run(*argv, expect=ExitCode.PRECONDITION)
        assert env["error"]["code"] == "wrong_state"
