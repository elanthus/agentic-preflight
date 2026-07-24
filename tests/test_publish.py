"""M6 publish: provider detection, the gate token, push and PR via gh."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from agentic_preflight.publish import provider as providermod
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
        'if [ "$1" = "pr" ] && [ "$2" = "list" ]; then echo "[]"; exit 0; fi\n'
        'if [ "$1" = "pr" ] && [ "$2" = "view" ]; then\n'
        '  echo \'{"url":"https://github.com/owner/repo/pull/1",'
        '"state":"MERGED","mergedAt":"2026-07-22T12:00:00Z",'
        '"headRefName":"feature/x","baseRefName":"main"}\'\n'
        '  exit 0\n'
        'fi\n'
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
    write(feature_repo, ".agentic-preflight.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    env = agent.run("mergeback")
    assert env["state"] == "VERIFIED"
    return agent


@pytest.fixture
def verified_with_cherry_picked_fix(feature_repo, bare_remote, tmp_path, monkeypatch):
    """A verified fix whose cherry-picked SHA deliberately differs."""
    write(feature_repo, ".agentic-preflight.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[worktree]\nmode = 'reusable'\n")
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    start = agent.run("start")
    run_id = start["run_id"]
    wt = Path(start["data"]["worktree_path"])
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, [{
        "path": "src/app.py",
        "line": 1,
        "severity": "high",
        "action": "auto_fix",
        "title": "use the loud flag",
    }]))

    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")
    write(wt, "src/app.py",
          "def greet(name, loud=False):\n    return 'HI' if loud else f'hi {name}'\n")
    original = commit_all(wt, "use the loud flag")
    agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", original)
    agent.run("verify")
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")

    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-02T00:00:00+00:00")
    agent.run("mergeback")
    picked = git("rev-parse", "HEAD", cwd=feature_repo)
    assert original != picked
    return agent, run_id, wt, original, picked


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


def test_finish_closes_a_pushed_run_without_a_pull_request(verified):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    env = verified.run("finish")
    assert env["state"] == "DONE"
    assert env["next"]["command"] == "agentic-preflight gc"
    assert verified.run("status")["data"]["has_run"] is False


def test_finish_is_illegal_before_push(verified):
    env = verified.run("finish", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_gate_is_illegal_before_everything_is_verified(feature_repo, tmp_path):
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    env = agent.run("gate", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_manual_gate_mode_refuses_to_proceed_at_all(feature_repo, bare_remote, tmp_path):
    """For those who want a person to type the command themselves."""
    write(feature_repo, ".agentic-preflight.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[gate]\nmode = 'manual'\n")
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
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


def as_github_origin(repo, push_url=None):
    """Point origin at a GitHub URL after pushing.

    The bare remote is a filesystem path, which is correctly detected as
    non-GitHub. PR paths need a GitHub-shaped remote, and `gh` is stubbed, so
    nothing real is contacted.
    """
    git("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=repo)
    if push_url is not None:
        git("remote", "set-url", "--add", "--push", "origin", str(push_url), cwd=repo)


def test_pr_shells_out_to_gh(verified, gh_stub, feature_repo):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    env = verified.run("pr")
    assert env["state"] == "PR_OPEN"
    assert env["data"]["pr_url"].endswith("/pull/1")
    assert "pr create" in gh_stub.read_text()


def test_ci_failure_returns_logs_and_intent_to_the_host(
    verified, gh_stub, feature_repo, monkeypatch
):
    from agentic_preflight.publish import github as githubmod

    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    verified.run("pr")
    failed = githubmod.CheckResult(
        name="tests",
        status="COMPLETED",
        conclusion="FAILURE",
        run_id="123",
    )
    monkeypatch.setattr(
        githubmod,
        "pull_request_health",
        lambda *_: githubmod.PullRequestHealth(
            url="https://github.com/owner/repo/pull/1",
            state="OPEN",
            merge_state="BLOCKED",
            outcome="failed",
            checks=[failed],
            failed_checks=[failed],
        ),
    )
    monkeypatch.setattr(
        githubmod, "failed_check_logs", lambda *_: {"123": "tests/test_api.py failed"}
    )

    env = verified.run("ci", "--once")
    assert env["state"] == "CI_FAILED"
    assert env["data"]["host_driven"] is True
    assert env["data"]["failed_logs"]["123"] == "tests/test_api.py failed"
    assert env["data"]["intent"]
    assert env["next"]["command"] == "agentic-preflight abort --force"


def test_finish_refuses_to_close_a_run_with_an_unmerged_cleanup_lifecycle(
    verified, gh_stub, feature_repo
):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    assert verified.run("pr")["state"] == "PR_OPEN"
    env = verified.run("finish", expect=ExitCode.PRECONDITION)
    assert env["error"]["code"] == "wrong_state"


def test_cleanup_previews_every_related_resource_before_deleting(
    verified, gh_stub, feature_repo, bare_remote
):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo, bare_remote)
    verified.run("pr")

    env = verified.run("cleanup")

    assert env["state"] == "PR_OPEN"
    assert env["data"]["token"]
    assert env["data"]["worktree_branch"].startswith("ap/")
    assert env["data"]["local_branch"] == "feature/x"
    assert env["data"]["remote_branch"] == "origin/feature/x"
    assert env["data"]["switch_to"] == "main"
    assert git("branch", "--list", "feature/x", cwd=feature_repo)
    assert git("branch", "--list", env["data"]["worktree_branch"], cwd=feature_repo)


def test_cleanup_rejects_a_wrong_confirmation_token(
    verified, gh_stub, feature_repo, bare_remote
):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo, bare_remote)
    verified.run("pr")
    verified.run("cleanup")

    env = verified.run(
        "cleanup", "--confirm", "wrong-token", expect=ExitCode.NEEDS_CONFIRM
    )

    assert env["error"]["code"] == "needs_confirm"
    assert git("branch", "--list", "feature/x", cwd=feature_repo)


def test_cleanup_refuses_until_github_reports_the_pr_merged(
    verified, gh_stub, feature_repo, bare_remote, monkeypatch
):
    from agentic_preflight.publish import github as githubmod
    from agentic_preflight.publish.github import PullRequestStatus

    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo, bare_remote)
    verified.run("pr")
    monkeypatch.setattr(
        githubmod,
        "pull_request_status",
        lambda cwd, url: PullRequestStatus(url, "OPEN", None, "feature/x", "main"),
    )

    env = verified.run("cleanup", expect=ExitCode.NEEDS_HUMAN)

    assert env["error"]["code"] == "needs_human"
    assert env["data"]["pr_state"] == "OPEN"
    assert git("branch", "--list", "feature/x", cwd=feature_repo)


def test_confirmed_cleanup_releases_runner_and_removes_local_and_remote_branches(
    verified, gh_stub, feature_repo, bare_remote
):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo, bare_remote)
    verified.run("pr")
    preview = verified.run("cleanup")
    worktree_path = Path(preview["data"]["worktree_path"])

    env = verified.run("cleanup", "--confirm", preview["data"]["token"])

    assert env["state"] == "DONE"
    assert env["data"]["cleaned"] is True
    assert git("branch", "--show-current", cwd=feature_repo) == "main"
    assert git("branch", "--list", "feature/x", cwd=feature_repo) == ""
    assert git("branch", "--list", preview["data"]["worktree_branch"], cwd=feature_repo) == ""
    assert git("branch", "--list", "feature/x", cwd=bare_remote) == ""
    assert worktree_path.exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree_path) == "HEAD"
    assert preview["data"]["token"] not in gh_stub.read_text()
    assert verified.run("status")["data"]["has_run"] is False


def test_pr_title_flag_overrides_the_default(verified, gh_stub, feature_repo):
    token = verified.run("gate")["data"]["token"]
    verified.run("push", "--confirm", token)
    as_github_origin(feature_repo)
    verified.run("pr", "--title", "A useful title")
    assert "--title A useful title" in gh_stub.read_text()


def test_publish_config_sets_the_gate_pr_title(feature_repo, bare_remote, tmp_path):
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
        "\n[publish]\npr_title = 'Configured title'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
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
    write(feature_repo, ".agentic-preflight.toml",
          "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n"
          "\n[publish]\ndraft_pr = true\n")
    commit_all(feature_repo, "configure agentic-preflight")
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
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


def test_gc_reclaims_a_finished_run_whose_fixes_were_cherry_picked(
    verified_with_cherry_picked_fix, feature_repo
):
    from agentic_preflight import gitx

    agent, run_id, wt, original, picked = verified_with_cherry_picked_fix
    assert gitx.commit_patch_id(feature_repo, original) == gitx.commit_patch_id(
        feature_repo, picked
    )

    token = agent.run("gate")["data"]["token"]
    agent.run("push", "--confirm", token)
    agent.run("finish")
    env = agent.run("gc")

    assert run_id in env["data"]["removed"]
    assert env["data"]["retained"] == []
    assert wt.exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "HEAD"
    assert git("branch", "--list", f"ap/{run_id}", cwd=feature_repo) == ""


# -- the honest caveat ------------------------------------------------------


def test_the_token_is_readable_from_status(verified):
    """Not a security boundary, and the README says so. It is ceremony that makes
    an accidental push impossible and an unconfirmed one a visible violation."""
    token = verified.run("gate")["data"]["token"]
    assert verified.run("status")["data"]["gate_token"] == token
