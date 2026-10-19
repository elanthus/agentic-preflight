"""Verify the hosted approval exemption against real contribution histories."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import commit_all, git, write

SCRIPT = Path(__file__).parent.parent / ".github/scripts/verify_dependabot.py"
verify = runpy.run_path(str(SCRIPT))["verify"]


def snapshot(repo, base, head):
    commits = [
        {
            "sha": sha,
            "parents": [
                {"sha": parent}
                for parent in git("show", "-s", "--format=%P", sha, cwd=repo).split()
            ],
            "author": {"login": "dependabot[bot]", "type": "Bot"},
            "committer": {"login": "web-flow"},
            "commit": {"verification": {"verified": True, "reason": "valid"}},
        }
        for sha in git("rev-list", "--reverse", f"{base}..{head}", cwd=repo).split()
    ]
    pr = {
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "head": {"sha": head, "repo": {"full_name": "owner/repo"}},
        "base": {"sha": base, "repo": {"full_name": "owner/repo"}},
        "commits": len(commits),
    }
    return {
        "repo": repo,
        "pr": pr,
        "commits": [commits],
        "repository": "owner/repo",
        "head": head,
        "base": base,
    }


@pytest.fixture
def bot_change(tmp_repo):
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "-c", "dependabot/update", cwd=tmp_repo)
    write(tmp_repo, ".github/workflows/ci.yml", "uses: owner/action@new-major\n")
    head = commit_all(tmp_repo, "bot dependency update")
    return snapshot(tmp_repo, base, head)


def test_signed_bot_update_has_no_ecosystem_or_major_version_restriction(bot_change):
    assert verify(**bot_change)["verified"] is True


def test_multiple_verified_bot_commits_qualify(bot_change):
    repo = bot_change["repo"]
    write(repo, "uv.lock", "new lockfile\n")
    head = commit_all(repo, "second bot update")
    assert verify(**snapshot(repo, bot_change["base"], head))["verified"] is True


@pytest.mark.parametrize("field", ["login", "type", "head_repo", "base_repo", "head", "base"])
def test_identity_and_snapshot_mismatches_are_not_exempt(bot_change, field):
    pr = bot_change["pr"]
    if field in {"login", "type"}:
        pr["user"][field] = "human"
    elif field.endswith("_repo"):
        pr[field.removesuffix("_repo")]["repo"]["full_name"] = "outsider/repo"
    else:
        pr[field]["sha"] = "b" * 40
    assert verify(**bot_change)["reason"] == "snapshot_mismatch"


@pytest.mark.parametrize(
    "field",
    [
        "author",
        "type",
        "signature",
        "missing_signature",
        "committer",
        "missing_committer",
        "reason",
    ],
)
def test_unsigned_or_human_contributions_are_not_exempt(bot_change, field):
    commit = bot_change["commits"][0][0]
    if field == "author":
        commit["author"]["login"] = "human"
    elif field == "type":
        commit["author"]["type"] = "User"
    elif field == "signature":
        commit["commit"]["verification"]["verified"] = False
    elif field == "missing_signature":
        del commit["commit"]["verification"]
    elif field == "committer":
        commit["committer"]["login"] = "human"
    elif field == "missing_committer":
        commit["committer"] = None
    else:
        commit["commit"]["verification"]["reason"] = "unknown_key"
    assert verify(**bot_change)["verified"] is False


@pytest.mark.parametrize("mutation", ["empty", "omitted", "duplicate", "count", "parents", "sha"])
def test_incomplete_or_inconsistent_api_history_is_not_exempt(bot_change, mutation):
    items = bot_change["commits"][0]
    if mutation == "empty":
        bot_change["commits"] = []
    elif mutation == "omitted":
        write(bot_change["repo"], "extra.txt", "human change\n")
        bot_change["head"] = commit_all(bot_change["repo"], "human commit")
        bot_change["pr"]["head"]["sha"] = bot_change["head"]
    elif mutation == "duplicate":
        items.append(copy.deepcopy(items[0]))
        bot_change["pr"]["commits"] = len(items)
    elif mutation == "count":
        bot_change["pr"]["commits"] = 300
    elif mutation == "parents":
        items[0]["parents"] = []
    else:
        items[0]["sha"] = "--all"
    assert verify(**bot_change)["verified"] is False


def test_clean_unsigned_base_merge_preserves_exemption(bot_change):
    repo = bot_change["repo"]
    git("switch", "main", cwd=repo)
    write(repo, "base.txt", "protected base update\n")
    base = commit_all(repo, "advance base")
    git("switch", "dependabot/update", cwd=repo)
    git("merge", "--no-edit", "main", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    data = snapshot(repo, base, head)
    data["commits"][0][-1]["author"] = None
    data["commits"][0][-1]["commit"]["verification"]["verified"] = False
    assert verify(**data)["verified"] is True


def test_merge_with_extra_content_is_not_exempt(bot_change):
    repo = bot_change["repo"]
    git("switch", "main", cwd=repo)
    write(repo, "base.txt", "protected base update\n")
    base = commit_all(repo, "advance base")
    git("switch", "dependabot/update", cwd=repo)
    git("merge", "--no-commit", "main", cwd=repo)
    write(repo, "hidden.txt", "extra contribution\n")
    head = commit_all(repo, "merge with extra content")
    assert verify(**snapshot(repo, base, head))["reason"] == "merge_changed_content"


def test_conflict_resolution_is_not_exempt(bot_change):
    repo = bot_change["repo"]
    git("switch", "main", cwd=repo)
    write(repo, ".github/workflows/ci.yml", "conflicting base change\n")
    base = commit_all(repo, "advance base")
    git("switch", "dependabot/update", cwd=repo)
    result = subprocess.run(["git", "merge", "main"], cwd=repo, capture_output=True, check=False)
    assert result.returncode != 0
    write(repo, ".github/workflows/ci.yml", "human conflict resolution\n")
    head = commit_all(repo, "resolve conflict")
    assert verify(**snapshot(repo, base, head))["reason"] == "merge_changed_content"


def test_merging_another_branch_is_not_exempt(bot_change):
    repo = bot_change["repo"]
    git("switch", "-c", "other", bot_change["base"], cwd=repo)
    write(repo, "other.txt", "unprotected content\n")
    commit_all(repo, "other contribution")
    git("switch", "dependabot/update", cwd=repo)
    git("merge", "--no-edit", "other", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    assert (
        verify(**snapshot(repo, bot_change["base"], head))["reason"] == "merge_parent_outside_base"
    )


@pytest.mark.parametrize("commits", [None, {}, [None], [[None]]])
def test_malformed_provenance_fails_closed(bot_change, commits):
    bot_change["commits"] = commits
    assert verify(**bot_change)["verified"] is False


def test_cli_reports_snapshot_bound_result(bot_change, tmp_path):
    pr_file = tmp_path / "pr.json"
    commits_file = tmp_path / "commits.json"
    pr_file.write_text(json.dumps(bot_change["pr"]))
    commits_file.write_text(json.dumps(bot_change["commits"]))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pr-file",
            str(pr_file),
            "--commits-file",
            str(commits_file),
            "--repository",
            "owner/repo",
            "--head",
            bot_change["head"],
            "--base",
            bot_change["base"],
        ],
        cwd=bot_change["repo"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "verified": True,
        "head_sha": bot_change["head"],
        "base_sha": bot_change["base"],
        "reason": "verified_dependabot",
    }
