"""Verify the hosted approval exemption against real contribution histories."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import commit_all, git, write

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github/scripts/verify_dependabot.py"
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
        "event_name": "pull_request_target",
        "event": event(head, base),
    }


BOT_ID = 49699333


def event(
    head, base, *, action="synchronize", login="dependabot[bot]", kind="Bot", sender_id=BOT_ID
):
    """Return a pull_request_target payload whose sender set the head."""
    payload = {
        "action": action,
        "sender": {"login": login, "type": kind, "id": sender_id},
        "repository": {"full_name": "owner/repo", "default_branch": "main"},
        "pull_request": {
            "user": {"login": "dependabot[bot]", "type": "Bot"},
            "head": {"sha": head, "ref": "dependabot/update", "repo": {"full_name": "owner/repo"}},
            "base": {"sha": base, "ref": "main", "repo": {"full_name": "owner/repo"}},
        },
    }
    if action == "synchronize":
        payload["before"] = "c" * 40
        payload["after"] = head
    return payload


@pytest.fixture
def bot_change(tmp_repo):
    base = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "-c", "dependabot/update", cwd=tmp_repo)
    write(tmp_repo, ".github/workflows/ci.yml", "uses: owner/action@new-major\n")
    head = commit_all(tmp_repo, "bot dependency update")
    return snapshot(tmp_repo, base, head)


def test_signed_bot_update_has_no_ecosystem_or_major_version_restriction(bot_change):
    assert verify(**bot_change)["verified"] is True


def test_head_opened_by_dependabot_qualifies(bot_change):
    bot_change["event"] = event(bot_change["head"], bot_change["base"], action="opened")
    assert verify(**bot_change)["verified"] is True


@pytest.mark.parametrize(
    ("login", "kind"),
    [("maintainer", "User"), ("dependabot[bot]", "User"), ("github-actions[bot]", "Bot")],
)
@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_head_set_by_another_actor_with_dependabot_metadata_is_not_exempt(
    bot_change, action, login, kind
):
    # Every commit, the pull request, and the history look like Dependabot's work, as
    # after a person pushes a GitHub-signed commit attributed to the bot to its branch.
    bot_change["event"] = event(
        bot_change["head"], bot_change["base"], action=action, login=login, kind=kind
    )
    result = verify(**bot_change)
    assert result["verified"] is False
    assert result["reason"] == "head_set_by_other_actor"


@pytest.mark.parametrize("sender_id", [1, 49699334, "49699333", None, True])
def test_sender_must_be_dependabots_account_id(bot_change, sender_id):
    # A login and type that look like Dependabot's are not enough without its account id.
    bot_change["event"]["sender"]["id"] = sender_id
    assert verify(**bot_change)["reason"] == "head_set_by_other_actor"


def test_sender_without_account_id_fails_closed(bot_change):
    del bot_change["event"]["sender"]["id"]
    assert verify(**bot_change)["reason"] == "unreadable_provenance"


@pytest.mark.parametrize("base_ref", ["release", "scratch", "", None])
def test_pull_request_into_another_branch_is_not_exempt(bot_change, base_ref):
    # A retarget does not re-run the workflow, so only the default branch can qualify.
    bot_change["event"]["pull_request"]["base"]["ref"] = base_ref
    assert verify(**bot_change)["reason"] == "base_not_default_branch"


@pytest.mark.parametrize("default_branch", [None, 7, ["main"]])
def test_unreadable_default_branch_fails_closed(bot_change, default_branch):
    bot_change["event"]["repository"]["default_branch"] = default_branch
    assert verify(**bot_change)["verified"] is False


def test_retarget_event_fails_closed(bot_change):
    payload = event(bot_change["head"], bot_change["base"], action="edited")
    payload["changes"] = {"base": {"ref": {"from": "scratch"}, "sha": {"from": "d" * 40}}}
    bot_change["event"] = payload
    assert verify(**bot_change)["reason"] == "head_actor_unknown"


@pytest.mark.parametrize("field", ["repository", "base_ref"])
def test_missing_base_branch_fields_fail_closed(bot_change, field):
    if field == "repository":
        del bot_change["event"]["repository"]
    else:
        del bot_change["event"]["pull_request"]["base"]["ref"]
    assert verify(**bot_change)["reason"] == "unreadable_provenance"


@pytest.mark.parametrize(
    "action",
    ["reopened", "ready_for_review", "auto_merge_enabled", "auto_merge_disabled", "edited", None],
)
def test_events_that_do_not_set_the_head_fail_closed(bot_change, action):
    # Even a Dependabot sender does not show who set a head that changed while the
    # pull request was closed, or who set it before an auto-merge or draft toggle.
    bot_change["event"] = event(bot_change["head"], bot_change["base"], action=action)
    result = verify(**bot_change)
    assert result["verified"] is False
    assert result["reason"] == "head_actor_unknown"


@pytest.mark.parametrize(
    "event_name", ["pull_request", "pull_request_review", "workflow_run", "push", ""]
)
def test_other_event_types_fail_closed(bot_change, event_name):
    bot_change["event_name"] = event_name
    assert verify(**bot_change)["reason"] == "head_actor_unknown"


@pytest.mark.parametrize("field", ["after", "head", "base"])
def test_event_that_did_not_set_this_head_is_not_exempt(bot_change, field):
    payload = bot_change["event"]
    if field == "after":
        payload["after"] = "b" * 40
    else:
        payload["pull_request"][field]["sha"] = "b" * 40
    assert verify(**bot_change)["reason"] == "event_mismatch"


@pytest.mark.parametrize("payload", [None, [], "synchronize"])
def test_missing_event_payload_fails_closed(bot_change, payload):
    bot_change["event"] = payload
    assert verify(**bot_change)["reason"] == "head_actor_unknown"


@pytest.mark.parametrize("field", ["sender", "after", "pull_request", "sender_login"])
def test_malformed_event_payload_fails_closed(bot_change, field):
    payload = bot_change["event"]
    if field == "sender_login":
        del payload["sender"]["login"]
    elif field == "sender":
        payload["sender"] = None
    else:
        del payload[field]
    assert verify(**bot_change)["reason"] == "unreadable_provenance"


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
    # The same clean merge pushed by a person, for example with Update branch, does not.
    data["event"] = event(head, base, login="maintainer", kind="User")
    assert verify(**data)["reason"] == "head_set_by_other_actor"


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


@pytest.mark.parametrize(("login", "verified"), [("dependabot[bot]", True), ("maintainer", False)])
def test_cli_reports_snapshot_bound_result(bot_change, tmp_path, login, verified):
    pr_file = tmp_path / "pr.json"
    commits_file = tmp_path / "commits.json"
    event_file = tmp_path / "event.json"
    pr_file.write_text(json.dumps(bot_change["pr"]))
    commits_file.write_text(json.dumps(bot_change["commits"]))
    bot_change["event"]["sender"]["login"] = login
    event_file.write_text(json.dumps(bot_change["event"]))
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
            "--event-name",
            "pull_request_target",
            "--event-file",
            str(event_file),
        ],
        cwd=bot_change["repo"],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "verified": verified,
        "head_sha": bot_change["head"],
        "base_sha": bot_change["base"],
        "reason": "verified_dependabot" if verified else "head_set_by_other_actor",
    }


def test_workflow_passes_the_replayed_event_payload_to_the_verifier():
    workflow = (ROOT / ".github/workflows/human-approval.yml").read_text()
    step = workflow.split("      - name: Verify Dependabot contributions", 1)[1].split(
        "\n  environment:\n", 1
    )[0]
    assert "EVENT_NAME: ${{ github.event_name }}" in step
    assert '--event-name "$EVENT_NAME" --event-file "$GITHUB_EVENT_PATH"' in step
    # A review-triggered re-run has another triggering actor; the head's actor is the
    # replayed payload's sender, so the step must not consult the re-running account.
    assert "triggering_actor" not in workflow
    script = SCRIPT.read_text()
    assert "BOT_ID = 49699333" in script
    assert 'event["repository"]["default_branch"]' in script
