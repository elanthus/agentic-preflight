"""M5 hook: init, hook-check as a pure predicate, and real git push tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

from agentic_cli.envelope import ExitCode
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


def hook_check(repo, stdin_text, extra_env=None):
    """Invoke hook-check exactly as the hook does: a real subprocess with stdin."""
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-m", "agentic_cli", "hook-check"],
        cwd=repo,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )


ZERO = "0" * 40


# -- init -------------------------------------------------------------------


def test_init_installs_a_pre_push_hook(feature_repo):
    agent = ScriptedAgent(feature_repo)
    env = agent.run("init")
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert "agentic-cli hook-check" in hook.read_text()
    assert env["data"]["hook_installed"] is True


def test_init_writes_a_config_file_if_absent(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    assert (Path(feature_repo) / ".agentic-cli.toml").exists()


def test_init_does_not_clobber_an_existing_config(feature_repo):
    write(feature_repo, ".agentic-cli.toml", "[general]\nbase_ref = 'develop'\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    assert "develop" in (Path(feature_repo) / ".agentic-cli.toml").read_text()


def test_init_refuses_to_overwrite_a_foreign_hook(feature_repo):
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho someone elses hook\n")
    agent = ScriptedAgent(feature_repo)
    env = agent.run("init", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "hook_exists"
    assert "someone elses hook" in hook.read_text()


def test_init_force_replaces_a_foreign_hook(feature_repo):
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho someone elses hook\n")
    agent = ScriptedAgent(feature_repo)
    agent.run("init", "--force")
    assert "agentic-cli hook-check" in hook.read_text()


def test_init_is_idempotent(feature_repo):
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    env = agent.run("init")
    assert env["ok"] is True


def test_init_reports_an_unpinned_node_project_and_external_worktree_root(feature_repo):
    write(
        feature_repo,
        "package.json",
        json.dumps({"engines": {"node": ">=24 <25"}}),
    )
    env = ScriptedAgent(feature_repo).run("init", "--no-hook")
    assert env["data"]["runtime"]["node_project"] is True
    assert ">=24 <25" in env["data"]["warnings"][0]
    assert "Pin Node" in env["next"]["instruction"]
    assert not Path(env["data"]["worktree_root"]).is_relative_to(feature_repo)


# -- hook-check as a pure predicate -----------------------------------------


def test_a_deletion_is_always_allowed(feature_repo):
    """All-zero local sha means a branch deletion; there is nothing to verify."""
    result = hook_check(feature_repo, f"refs/heads/x {ZERO} refs/heads/x abc123\n")
    assert result.returncode == 0


def test_an_unverified_commit_is_blocked(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == ExitCode.HOOK_BLOCK


def test_the_block_message_goes_to_stderr_and_names_the_skill(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert "agentic-cli: push blocked" in result.stderr
    assert "/agentic-cli" in result.stderr
    assert "$agentic-cli" in result.stderr
    assert sha[:7] in result.stderr
    assert "--no-verify" in result.stderr


def test_the_block_message_explains_an_amend(feature_repo, tmp_path):
    """The most common cause deserves the most specific message."""
    _green_run(feature_repo, tmp_path)
    green_sha = git("rev-parse", "HEAD", cwd=feature_repo)

    write(feature_repo, "src/app.py", "def greet(n):\n    return 'amended'\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--amend", "-m", "amended", cwd=feature_repo)
    new_sha = git("rev-parse", "HEAD", cwd=feature_repo)

    result = hook_check(
        feature_repo, f"refs/heads/feature/x {new_sha} refs/heads/feature/x {ZERO}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert green_sha[:7] in result.stderr
    assert "amended" in result.stderr or "added a commit" in result.stderr


def _green_run(repo, tmp_path):
    """Drive a full run to a recorded green ledger entry."""
    write(repo, ".agentic-cli.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n")
    commit_all(repo, "configure agentic-cli")
    agent = ScriptedAgent(repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    agent.run("mergeback")
    return agent


def test_a_green_commit_is_allowed(feature_repo, tmp_path):
    _green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == 0, result.stderr


def test_the_predicate_does_not_mutate_anything(feature_repo, tmp_path):
    _green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    before = git("rev-parse", "HEAD", cwd=feature_repo)
    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before
    assert git("status", "--porcelain", cwd=feature_repo) == ""


def test_multiple_refs_block_if_any_is_unverified(feature_repo, tmp_path):
    _green_run(feature_repo, tmp_path)
    green = git("rev-parse", "HEAD", cwd=feature_repo)
    stdin = (
        f"refs/heads/a {green} refs/heads/a {ZERO}\n"
        f"refs/heads/b {'b' * 40} refs/heads/b {ZERO}\n"
    )
    result = hook_check(feature_repo, stdin)
    assert result.returncode == ExitCode.HOOK_BLOCK


def test_empty_stdin_is_allowed(feature_repo):
    assert hook_check(feature_repo, "").returncode == 0


# -- a broken tool must not brick the repo ----------------------------------


def test_the_installed_hook_allows_and_warns_when_the_tool_is_missing(feature_repo):
    """Deliberate: a repo you cannot push from is worse than a skipped check."""
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    hook = Path(feature_repo) / ".git" / "hooks" / "pre-push"

    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = subprocess.run(
        ["sh", str(hook), "origin", "ssh://example/repo"],
        cwd=feature_repo,
        input=f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n",
        capture_output=True,
        text=True,
        # A real shell is present, but agentic-cli (which lives in .venv/bin)
        # is not — exactly the state of a fresh clone by someone who has not
        # installed the tool.
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "")},
    )
    assert result.returncode == 0
    assert "not found" in result.stderr.lower()


# -- force-push guard -------------------------------------------------------


def test_a_force_push_is_blocked_even_when_green(feature_repo, tmp_path):
    """Non-fast-forward rewrites history the remote already has."""
    _green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    # remote_sha that is not an ancestor of local_sha would be a force push;
    # here we use a sha the local tip does not descend from.
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert "force" in result.stderr.lower()


def test_a_force_push_block_does_not_claim_the_commit_is_unverified(feature_repo, tmp_path):
    """The header must not contradict the reason.

    A green commit blocked for being a force push is not 'unverified', and an
    agent reading that would re-run the gate instead of addressing the rewrite.
    """
    _green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    assert result.returncode == ExitCode.HOOK_BLOCK
    assert "force push" in result.stderr
    assert "no green run recorded" not in result.stderr


def test_an_unverified_block_still_says_so(feature_repo):
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert "no green run recorded" in result.stderr


def test_allow_force_push_config_permits_it(feature_repo, tmp_path):
    _green_run(feature_repo, tmp_path)
    write(feature_repo, ".agentic-cli.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[hook]\nallow_force_push = true\n")
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(
        feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {'c' * 40}\n"
    )
    # The tip is still green, and force is now permitted.
    assert result.returncode == 0, result.stderr


# -- the real thing: an actual git push -------------------------------------


def test_a_real_push_is_blocked_for_an_unverified_commit(feature_repo, bare_remote, tmp_path):
    ScriptedAgent(feature_repo).run("init")
    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "push blocked" in result.stderr


def test_a_real_push_succeeds_after_a_green_run(feature_repo, bare_remote, tmp_path):
    _green_run(feature_repo, tmp_path)
    ScriptedAgent(feature_repo).run("init")
    result = subprocess.run(
        ["git", "push", "origin", "feature/x"],
        cwd=feature_repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_amending_after_green_blocks_the_next_push(feature_repo, bare_remote, tmp_path):
    """The scenario the SHA-keyed ledger exists to catch."""
    _green_run(feature_repo, tmp_path)
    ScriptedAgent(feature_repo).run("init")

    write(feature_repo, "src/app.py", "def greet(n):\n    return 'sneaky change'\n")
    git("add", "-A", cwd=feature_repo)
    git("commit", "--amend", "-m", "sneaky", cwd=feature_repo)

    result = subprocess.run(
        ["git", "push", "--force", "origin", "feature/x"],
        cwd=feature_repo, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "push blocked" in result.stderr


def test_no_verify_bypasses_the_hook(feature_repo, bare_remote, tmp_path):
    """Documented escape hatch. The gate guards against mistakes, not malice."""
    ScriptedAgent(feature_repo).run("init")
    result = subprocess.run(
        ["git", "push", "--no-verify", "origin", "feature/x"],
        cwd=feature_repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# -- constraints ------------------------------------------------------------


def test_hook_check_is_fast(feature_repo, tmp_path):
    """Budget is 50ms; the hook runs on every push."""
    import time

    _green_run(feature_repo, tmp_path)
    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    start = time.monotonic()
    hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    # Generous: this measures interpreter startup too, which the budget excludes.
    assert time.monotonic() - start < 5.0


def test_hook_check_reads_only_the_ledger(feature_repo, tmp_path):
    """No run state, no network — just ledger.json."""
    _green_run(feature_repo, tmp_path)
    state_root = Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo)
    ) / "agentic-cli"
    import shutil
    shutil.rmtree(state_root / "runs")
    (state_root / "current").unlink(missing_ok=True)

    sha = git("rev-parse", "HEAD", cwd=feature_repo)
    result = hook_check(feature_repo, f"refs/heads/feature/x {sha} refs/heads/feature/x {ZERO}\n")
    assert result.returncode == 0, result.stderr
