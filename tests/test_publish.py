"""M6 publish: provider detection, the gate token, push and PR via gh."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentic_cli.envelope import ExitCode
from agentic_cli.publish import provider as providermod
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def findings_json(tmp_path, items):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


# -- provider detection -----------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_host",
    [
        ("git@github.com:owner/repo.git", "github.com"),
        ("https://github.com/owner/repo.git", "github.com"),
        ("ssh://git@github.com/owner/repo.git", "github.com"),
        ("git@github.example.com:owner/repo.git", "github.example.com"),
        ("https://github.example.com/owner/repo", "github.example.com"),
    ],
)
def test_host_is_parsed_from_ssh_and_https_forms(url, expected_host):
    assert providermod.parse_remote(url).host == expected_host


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git",
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_owner_and_repo_are_parsed(url):
    remote = providermod.parse_remote(url)
    assert remote.owner == "owner"
    assert remote.repo == "repo"


def test_github_enterprise_is_recognised_as_github():
    """Host-aware, so GHE works rather than being mistaken for something else."""
    assert providermod.parse_remote("git@github.example.com:o/r.git").provider == "github"


def test_a_non_github_remote_is_unsupported_not_misdetected():
    remote = providermod.parse_remote("git@gitlab.com:owner/repo.git")
    assert remote.provider == "unsupported"


def test_a_compare_url_is_built_for_manual_fallback():
    remote = providermod.parse_remote("git@github.com:owner/repo.git")
    url = providermod.compare_url(remote, base="main", head="feature/x")
    assert url == "https://github.com/owner/repo/compare/main...feature/x?expand=1"


# -- the gh stub ------------------------------------------------------------


@pytest.fixture
def gh_stub(tmp_path, monkeypatch):
    """A fake `gh` on PATH that records argv, so we can assert we never pass a
    token and never touch the network."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = bin_dir / "gh.log"
    script = bin_dir / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        '  auth) exit 0 ;;\n'
        '  pr) echo "https://github.com/owner/repo/pull/1" ; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.fixture
def verified(feature_repo, bare_remote, tmp_path):
    """A run driven all the way to VERIFIED with a real remote configured."""
    write(feature_repo, ".agentic-cli.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n")
    commit_all(feature_repo, "configure agentic-cli")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    return agent


# -- the gate ---------------------------------------------------------------


def test_gate_mints_a_token_and_summarises_what_would_be_pushed(verified):
    env = verified.run("gate")
    assert env["state"] == "AWAITING_PUSH_CONFIRM"
    assert env["data"]["token"]
    assert env["data"]["remote"] == "origin"
    assert env["data"]["refspec"]
    assert env["data"]["commits"]


def test_the_gate_summary_names_the_branch_and_commit_subjects(verified):
    env = verified.run("gate")
    assert env["data"]["branch"] == "feature/x"
    subjects = json.dumps(env["data"]["commits"])
    assert "add loud flag" in subjects
    assert env["data"]["pr_title"] == "feature/x"


def test_push_without_a_token_is_refused(verified):
    verified.run("gate")
    env = verified.run("push", expect=ExitCode.NEEDS_CONFIRM)
    assert env["error"]["code"] == "needs_confirm"


def test_push_with_a_wrong_token_is_refused(verified):
    verified.run("gate")
    env = verified.run("push", "--confirm", "not-the-token", expect=ExitCode.NEEDS_CONFIRM)
    assert env["error"]["code"] == "needs_confirm"


def test_push_with_the_right_token_succeeds(verified, feature_repo, bare_remote):
    token = verified.run("gate")["data"]["token"]
    env = verified.run("push", "--confirm", token)
    assert env["state"] == "PUSHED"
    remote_sha = git("rev-parse", "feature/x", cwd=bare_remote)
    assert remote_sha == git("rev-parse", "HEAD", cwd=feature_repo)


def test_gate_is_illegal_before_everything_is_verified(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    env = agent.run("gate", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_manual_gate_mode_refuses_to_proceed_at_all(feature_repo, bare_remote, tmp_path):
    """For those who want a person to type the command themselves."""
    write(feature_repo, ".agentic-cli.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[gate]\nmode = 'manual'\n")
    commit_all(feature_repo, "configure agentic-cli")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")

    env = agent.run("gate", expect=ExitCode.NEEDS_HUMAN)
    assert env["error"]["code"] == "manual_gate"
    assert "git push" in json.dumps(env["data"])


def test_dry_run_push_changes_nothing(verified, bare_remote):
    token = verified.run("gate")["data"]["token"]
    env = verified.run("push", "--confirm", token, "--dry-run")
    assert env["data"]["dry_run"] is True
    result = subprocess.run(
        ["git", "rev-parse", "feature/x"], cwd=bare_remote, capture_output=True, text=True
    )
    assert result.returncode != 0  # branch never reached the remote


# -- pull requests via gh ---------------------------------------------------


def as_github_origin(repo):
    """Point origin at a GitHub URL after pushing.

    The bare remote is a filesystem path, which is correctly detected as
    non-GitHub. PR paths need a GitHub-shaped remote, and `gh` is stubbed, so
    nothing real is contacted.
    """
    git("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=repo)


def test_pr_shells_out_to_gh(verified, gh_stub, feature_repo):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    env = verified.run("pr")
    assert env["state"] == "PR_OPEN"
    assert env["data"]["pr_url"].endswith("/pull/1")
    assert "pr create" in gh_stub.read_text()


def test_pr_title_flag_overrides_the_default(verified, gh_stub, feature_repo):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    verified.run("pr", "--title", "A useful title")
    assert "--title A useful title" in gh_stub.read_text()


def test_publish_config_sets_the_gate_pr_title(feature_repo, bare_remote, tmp_path):
    write(
        feature_repo,
        ".agentic-cli.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[publish]\npr_title = 'Configured title'\n",
    )
    commit_all(feature_repo, "configure agentic-cli")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    assert agent.run("gate")["data"]["pr_title"] == "Configured title"


def test_we_never_pass_a_token_to_gh(verified, gh_stub, feature_repo):
    """gh owns auth. No token reading, no keyring, no GITHUB_TOKEN plumbing."""
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    verified.run("pr")
    recorded = gh_stub.read_text()
    for forbidden in ("--token", "GITHUB_TOKEN", "ghp_", "Authorization"):
        assert forbidden not in recorded


def test_draft_pr_config_is_honoured(feature_repo, bare_remote, tmp_path, gh_stub):
    write(feature_repo, ".agentic-cli.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[publish]\ndraft_pr = true\n")
    commit_all(feature_repo, "configure agentic-cli")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    agent.run("pr")
    assert "--draft" in gh_stub.read_text()


def test_a_missing_gh_falls_back_to_a_prefilled_compare_url(
    verified, monkeypatch, feature_repo
):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    # git is still present; gh (in /opt/homebrew/bin or similar) is not.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = verified.run("pr", expect=ExitCode.NEEDS_HUMAN)
    assert env["error"]["code"] == "gh_unavailable"
    assert "compare/main...feature/x" in env["data"]["compare_url"]


def test_pr_is_illegal_before_pushing(verified):
    env = verified.run("pr", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


# -- the honest caveat ------------------------------------------------------


def test_the_token_is_readable_from_status(verified):
    """Not a security boundary, and the README says so. It is ceremony that makes
    an accidental push impossible and an unconfirmed one a visible violation."""
    token = verified.run("gate")["data"]["token"]
    assert verified.run("status")["data"]["gate_token"] == token
