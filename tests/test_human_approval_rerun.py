"""Exercise the review relay and the protected-base re-run companion with inert fixtures."""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github/workflows"
HEAD = "a" * 40
BASE = "b" * 40


def _read(name):
    return (WORKFLOWS / name).read_text()


def _job(workflow, job, following=None):
    block = workflow.split(f"\n  {job}:\n", 1)[1]
    return block.split(f"\n  {following}:\n", 1)[0] if following else block


def _step_script(workflow, label):
    step = workflow.split(f"      - name: {label}\n", 1)[1].split("        run: |\n", 1)[1]
    lines = []
    for line in step.splitlines():
        if line and not line.startswith(" " * 10):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


# When GitHub created the relay run in the bot tests.
RELAY_AT = "2026-11-13T12:00:00Z"


OWNER_REVIEW = {
    "data": {"approval_mode": "owner_review", "requires_human_approval": True, "base_sha": BASE}
}


def _runs(*runs):
    return [{"workflow_runs": list(runs)}]


def _run(
    run_id,
    run_number,
    *,
    head_sha=HEAD,
    status="completed",
    numbers=(),
    head_repository="contributor/project",
):
    return {
        "id": run_id,
        "run_number": run_number,
        "head_sha": head_sha,
        "head_repository": {"full_name": head_repository} if head_repository else None,
        "status": status,
        "pull_requests": [{"number": number} for number in numbers],
    }


def _review(login="owner-login", association="OWNER", *, state="COMMENTED", body="approved"):
    return {
        "id": 1,
        "user": {"login": login, "type": "User"},
        "author_association": association,
        "state": state,
        "body": body,
        "commit_id": HEAD,
    }


def _pull_request(*, state="open", head_sha=HEAD, base_sha=BASE, head_repo=None):
    if head_repo is None:
        head_repo = {"full_name": "contributor/project"}
    return {
        "state": state,
        "base": {"sha": base_sha},
        "head": {"sha": head_sha, "repo": head_repo or None},
    }


@pytest.fixture
def rerun(tmp_path):
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("Bash and jq are required to execute the workflow step")
    companion = _read("human-approval-rerun.yml")
    authorize = _step_script(companion, "Authorize the review for the reviewed head")
    rerun_step = _step_script(companion, "Re-run the trusted policy run for the reviewed head")
    # The two jobs run in order; the re-run job runs only when authorize allows it,
    # with the pull request number from authorize's outputs.
    script = (
        ": > github-output.txt\n(\n"
        + authorize
        + "\n)\nif grep -qx 'allowed=true' github-output.txt; then\n"
        + "  PR_NUMBER=$(sed -n 's/^pr_number=//p' github-output.txt)\n(\n"
        + rerun_step
        + "\n)\nfi\n"
    )

    def run(
        *,
        pull_request=None,
        relay="17\n",
        policy=OWNER_REVIEW,
        policy_exit=0,
        policy_fail_first=0,
        runs=None,
        download_exit=0,
        rerun_exit=0,
        rerun_error="HTTP 403: boom",
        run_status="completed",
        status_exit=0,
        actor="owner-login",
        reviews=None,
        permission="read",
        permission_exit=0,
        permission_error="Server Error (HTTP 500)",
        events=None,
        events_exit=0,
        reviews_exit=0,
        pr_fail_first=0,
        relay_created_at=RELAY_AT,
    ):
        (tmp_path / "pr-fixture.json").write_text(json.dumps(pull_request or _pull_request()))
        (tmp_path / "relay.txt").write_text(relay)
        (tmp_path / "policy-fixture.json").write_text(json.dumps(policy))
        (tmp_path / "runs.json").write_text(json.dumps(runs if runs is not None else _runs()))
        (tmp_path / "reviews.json").write_text(
            json.dumps([reviews if reviews is not None else [_review()]])
        )
        (tmp_path / "events.json").write_text(json.dumps([events or []]))
        harness = r"""
gh() {
  printf '%s\n' "$*" >> gh-calls.txt
  case "$1 $2" in
    "run download")
      if [ "$7" = human-approval-review ]; then
        [ "$TEST_DOWNLOAD_EXIT" = 0 ] || return "$TEST_DOWNLOAD_EXIT"
        mkdir -p "$9"
        cp relay.txt "$9/pr-number"
      else
        attempts=$(( $(cat policy-attempts.txt 2>/dev/null || echo 0) + 1 ))
        echo "$attempts" > policy-attempts.txt
        [ "$attempts" -gt "$TEST_POLICY_FAIL_FIRST" ] || return 1
        [ "$TEST_POLICY_EXIT" = 0 ] || return "$TEST_POLICY_EXIT"
        mkdir -p "$9"
        cp policy-fixture.json "$9/approval-policy.json"
      fi
      ;;
    "run rerun")
      [ "$TEST_RERUN_EXIT" = 0 ] || echo "gh: $TEST_RERUN_ERROR" >&2
      return "$TEST_RERUN_EXIT"
      ;;
    "api --paginate")
      case "$4" in
        */reviews*)
          [ "$TEST_REVIEWS_EXIT" = 0 ] || return "$TEST_REVIEWS_EXIT"
          cat reviews.json
          ;;
        */events*)
          [ "$TEST_EVENTS_EXIT" = 0 ] || return "$TEST_EVENTS_EXIT"
          cat events.json
          ;;
        *) cat runs.json ;;
      esac
      ;;
    api*)
      case "$2" in
        */pulls/*)
          pr_attempts=$(( $(cat pr-attempts.txt 2>/dev/null || echo 0) + 1 ))
          echo "$pr_attempts" > pr-attempts.txt
          [ "$pr_attempts" -gt "$TEST_PR_FAIL_FIRST" ] || return 1
          cat pr-fixture.json
          ;;
        */collaborators/*)
          if [ "$TEST_PERMISSION_EXIT" != 0 ]; then
            echo "gh: $TEST_PERMISSION_ERROR" >&2
            return "$TEST_PERMISSION_EXIT"
          fi
          echo "$TEST_PERMISSION"
          ;;
        */actions/runs/*)
          [ "$TEST_STATUS_EXIT" = 0 ] || return "$TEST_STATUS_EXIT"
          echo "$TEST_RUN_STATUS"
          ;;
      esac
      ;;
  esac
}
sleep() { echo slept >> sleeps.txt; }
RUNNER_TEMP=.
GITHUB_OUTPUT=github-output.txt
"""
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-e", "-u", "-o", "pipefail", "-c", harness + script],
            cwd=tmp_path,
            env={
                **os.environ,
                "GITHUB_REPOSITORY": "owner/project",
                "RELAY_RUN_ID": "41",
                "RUN_HEAD_SHA": HEAD,
                "RUN_HEAD_REPOSITORY": "contributor/project",
                "TEST_DOWNLOAD_EXIT": str(download_exit),
                "TEST_POLICY_EXIT": str(policy_exit),
                "TEST_POLICY_FAIL_FIRST": str(policy_fail_first),
                "TEST_RERUN_EXIT": str(rerun_exit),
                "TEST_RERUN_ERROR": rerun_error,
                "TEST_RUN_STATUS": run_status,
                "TEST_STATUS_EXIT": str(status_exit),
                "TRIGGERING_ACTOR": actor,
                "TEST_PERMISSION": permission,
                "TEST_PERMISSION_EXIT": str(permission_exit),
                "TEST_PERMISSION_ERROR": permission_error,
                "TEST_EVENTS_EXIT": str(events_exit),
                "TEST_REVIEWS_EXIT": str(reviews_exit),
                "TEST_PR_FAIL_FIRST": str(pr_fail_first),
                "RELAY_CREATED_AT": relay_created_at,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls_file = tmp_path / "gh-calls.txt"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
        sleeps_file = tmp_path / "sleeps.txt"
        run.sleeps = len(sleeps_file.read_text().splitlines()) if sleeps_file.exists() else 0
        return result, calls

    return run


def _reruns(calls):
    return [call for call in calls if call.startswith("run rerun ")]


def test_companion_reruns_every_policy_run_for_the_reviewed_head_oldest_first(rerun):
    result, calls = rerun(runs=_runs(_run(2, 7), _run(1, 5), _run(3, 9, head_sha="c" * 40)))
    assert result.returncode == 0, result.stderr
    assert calls[0] == (
        "run download 41 --repo owner/project --name human-approval-review "
        "--dir ./human-approval-review"
    )
    assert "api /repos/owner/project/pulls/17" in calls
    assert any(f"event=pull_request_target&head_sha={HEAD}" in call for call in calls)
    assert (
        "run download 2 --repo owner/project --name human-approval-policy "
        "--dir ./human-approval-policy"
    ) in calls
    assert _reruns(calls) == [
        "run rerun 1 --repo owner/project",
        "run rerun 2 --repo owner/project",
    ]
    assert "Re-running Human approval policy run 2" in result.stdout


def test_companion_matches_fork_runs_by_the_reviewed_head_repository(rerun):
    result, calls = rerun(
        runs=_runs(
            _run(1, 5), _run(2, 7, head_repository="someone/else"), _run(3, 8, head_repository=None)
        )
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize("requires", [False, None])
def test_companion_leaves_runs_that_need_no_human_approval_alone(rerun, requires):
    policy = {"data": {"approval_mode": "owner_review", "requires_human_approval": requires}}
    result, calls = rerun(policy=policy, runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert "Run 1 does not require human approval" in result.stdout
    assert _reruns(calls) == []


def test_companion_reports_a_refused_rerun_after_trying_every_run(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5), _run(2, 7)), rerun_exit=1)
    assert result.returncode == 1
    assert _reruns(calls) == [
        "run rerun 1 --repo owner/project",
        "run rerun 2 --repo owner/project",
    ]


def test_companion_shares_one_wait_budget_across_runs(rerun):
    result, calls = rerun(
        runs=_runs(*(_run(n, n, status="in_progress") for n in (1, 2, 3))),
        policy_fail_first=1000,
        run_status="in_progress",
    )
    assert result.returncode == 0, result.stderr
    assert rerun.sleeps == 120
    assert len([call for call in calls if "--name human-approval-policy" in call]) == 61 + 61 + 1
    for run_id in (1, 2, 3):
        assert f"::notice::Run {run_id} is still evaluating after the wait" in result.stdout
    assert _reruns(calls) == []


def test_companion_skips_runs_that_belong_to_another_pull_request(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 9, numbers=(18,)), _run(2, 7, numbers=(17,)), _run(3, 8))
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == [
        "run rerun 2 --repo owner/project",
        "run rerun 3 --repo owner/project",
    ]


@pytest.mark.parametrize("relay", ["", "abc\n", "0\n", "017\n", "1 2\n", "17;id\n", "-1\n"])
def test_companion_rejects_a_relay_that_is_not_a_pull_request_number(rerun, relay):
    result, calls = rerun(relay=relay)
    assert result.returncode == 1
    assert "did not name a pull request number" in result.stderr
    assert not any(call.startswith("api") for call in calls)


def test_companion_fails_visibly_without_a_relay_artifact(rerun):
    result, calls = rerun(download_exit=1)
    assert result.returncode == 1
    assert not any(call.startswith("api") for call in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "closed"},
        {"head_sha": "c" * 40},
        {"head_repo": {"full_name": "someone/else"}},
        {"head_repo": {}},
        {"base_sha": "not-a-sha"},
    ],
)
def test_companion_does_nothing_for_a_pull_request_that_moved_on(rerun, changes):
    result, calls = rerun(pull_request=_pull_request(**changes))
    assert result.returncode == 0, result.stderr
    assert "no longer has the reviewed head" in result.stdout
    assert not any("actions/workflows" in call for call in calls)
    assert _reruns(calls) == []


@pytest.mark.parametrize("mode", ["manual_merge", "environment"])
def test_companion_leaves_runs_in_modes_that_do_not_read_reviews_alone(rerun, mode):
    result, calls = rerun(policy={"data": {"approval_mode": mode}}, runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert f"Approval mode {mode} does not read reviews" in result.stdout
    assert _reruns(calls) == []


@pytest.mark.parametrize("policy", [{"data": {}}, {"data": {"approval_mode": None}}, {}])
def test_companion_leaves_a_run_without_a_recorded_mode_alone(rerun, policy):
    result, calls = rerun(policy=policy, runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert "recorded no approval mode" in result.stdout
    assert _reruns(calls) == []


def test_companion_leaves_a_run_without_a_policy_artifact_alone(rerun):
    result, calls = rerun(policy_exit=1, runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert "recorded no policy evaluation" in result.stdout
    assert _reruns(calls) == []


def test_companion_reruns_in_peer_review_mode(rerun):
    result, calls = rerun(
        policy={"data": {"approval_mode": "peer_review", "requires_human_approval": True}},
        runs=_runs(_run(1, 5)),
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_does_nothing_without_a_policy_run_for_the_head(rerun):
    result, calls = rerun(runs=_runs(_run(3, 9, head_sha="c" * 40)))
    assert result.returncode == 0, result.stderr
    assert "No Human approval policy run exists" in result.stdout
    assert _reruns(calls) == []


def test_companion_reads_the_record_before_waiting_for_a_review_mode_run(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5, status="in_progress")))
    assert result.returncode == 0, result.stderr
    assert "still in progress" in result.stdout
    policy_call = next(i for i, call in enumerate(calls) if "--name human-approval-policy" in call)
    status_call = calls.index("api /repos/owner/project/actions/runs/1 --jq .status")
    assert policy_call < status_call
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_waits_for_the_record_of_a_run_that_is_still_evaluating(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5, status="in_progress")), policy_fail_first=2)
    assert result.returncode == 0, result.stderr
    assert "has not recorded its policy yet" in result.stdout
    assert len([call for call in calls if "--name human-approval-policy" in call]) == 3
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_never_waits_on_an_environment_run_that_is_pending_approval(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 5, status="in_progress")),
        policy={"data": {"approval_mode": "environment"}},
        run_status="in_progress",
    )
    assert result.returncode == 0, result.stderr
    assert "Approval mode environment does not read reviews" in result.stdout
    assert not any(call.endswith("--jq .status") for call in calls)
    assert _reruns(calls) == []


def test_companion_notes_a_run_that_never_records_its_policy_without_failing(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 5, status="in_progress")),
        policy_fail_first=1000,
        run_status="in_progress",
    )
    assert result.returncode == 0, result.stderr
    assert "::notice::Run 1 is still evaluating after the wait" in result.stdout
    assert len([call for call in calls if "--name human-approval-policy" in call]) == 61
    assert _reruns(calls) == []


def test_companion_gives_up_on_a_review_mode_run_that_never_finishes(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5, status="in_progress")), run_status="in_progress")
    assert result.returncode == 1
    assert "did not finish within the wait" in result.stderr
    assert "30 days" not in result.stderr
    assert len([call for call in calls if call.endswith("--jq .status")]) == 60
    assert _reruns(calls) == []


def test_companion_keeps_going_when_status_lookups_fail(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 5, status="in_progress"), _run(2, 7)),
        status_exit=1,
    )
    assert result.returncode == 1
    assert "Run 1 did not finish within the wait" in result.stderr
    assert _reruns(calls) == ["run rerun 2 --repo owner/project"]


def test_companion_shows_github_reason_when_a_rerun_is_refused(rerun):
    reason = "HTTP 403: Unable to retry this workflow run because it was created over a month ago"
    result, calls = rerun(runs=_runs(_run(1, 5)), rerun_exit=1, rerun_error=reason)
    assert result.returncode == 1
    assert reason in result.stderr
    assert "older than 30 days" in result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_reruns_for_the_owners_own_approved_comment_review(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5)), actor="Owner-Login")
    assert result.returncode == 0, result.stderr
    assert "api --paginate --slurp /repos/owner/project/pulls/17/reviews?per_page=100" in calls
    assert not any("/collaborators/" in call for call in calls)
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
def test_companion_reruns_for_other_associated_reviewers(rerun, association):
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="peer",
        reviews=[_review("peer", association, state="APPROVED", body="")],
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"])
def test_companion_ignores_reviews_from_unassociated_users(rerun, association):
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="drive-by",
        reviews=[_review(), _review("drive-by", association, body="spam")],
    )
    assert result.returncode == 0, result.stderr
    assert "api /repos/owner/project/collaborators/drive-by/permission --jq .permission" in calls
    assert "drive-by is not an owner, member, or collaborator" in result.stdout
    assert _reruns(calls) == []
    assert not any("actions/workflows" in call for call in calls)


def test_companion_ignores_an_actor_who_matches_only_another_users_review(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="drive-by",
        reviews=[
            _review(),
            _review("drive-by", "OWNER") | {"user": {"login": "x", "type": "User"}},
        ],
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == []


@pytest.mark.parametrize("permission", ["admin", "write"])
def test_companion_reruns_for_a_dismissal_by_a_user_with_write_access(rerun, permission):
    result, calls = rerun(runs=_runs(_run(1, 5)), actor="maintainer", permission=permission)
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize(
    "error", ["Server Error (HTTP 500)", "API rate limit exceeded (HTTP 403)", "timeout"]
)
def test_companion_reruns_when_the_permission_lookup_fails(rerun, error):
    # A failed lookup must not leave a stale success after a dismissal; the re-run
    # policy job reads the reviews itself.
    result, calls = rerun(
        runs=_runs(_run(1, 5)), actor="maintainer", permission_exit=1, permission_error=error
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read the repository permission of maintainer" in result.stdout
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_treats_a_permission_404_as_no_access(rerun):
    # A 404 is a definite answer, so a drive-by reviewer is still not authorized.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="drive-by",
        reviews=[_review(), _review("drive-by", "NONE", body="spam")],
        permission_exit=1,
        permission_error="drive-by is not a user (HTTP 404)",
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::" not in result.stdout
    assert "drive-by is not an owner, member, or collaborator" in result.stdout
    assert _reruns(calls) == []


@pytest.mark.parametrize("actor", ["owner-login", "drive-by", "review-app[bot]"])
def test_companion_reruns_when_the_reviews_are_unreadable(rerun, actor):
    result, calls = rerun(runs=_runs(_run(1, 5)), actor=actor, reviews_exit=1)
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read the reviews" in result.stdout
    assert not any("/collaborators/" in call or "/events" in call for call in calls)
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize("actor", ["owner-login", "review-app[bot]"])
def test_companion_reruns_when_the_reviews_are_malformed(rerun, actor):
    result, calls = rerun(
        runs=_runs(_run(1, 5)), actor=actor, reviews=["not a review"], events=[_dismissed()]
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read" in result.stdout
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_reruns_when_the_pull_request_lookup_fails_in_authorize(rerun):
    # The rerun job fetches the pull request again and checks its head before re-running.
    result, calls = rerun(runs=_runs(_run(1, 5)), pr_fail_first=1)
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read the pull request for pull request 17" in result.stdout
    assert calls.count("api /repos/owner/project/pulls/17") == 2
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize("actor", ["", "a/b", "x[bot]y"])
def test_companion_rejects_a_malformed_login_before_any_lookup_can_authorize(rerun, actor):
    result, calls = rerun(runs=_runs(_run(1, 5)), actor=actor, pr_fail_first=1)
    assert result.returncode == 0, result.stderr
    assert "not started by a repository user" in result.stdout
    assert not any(call.startswith("api ") for call in calls)
    assert _reruns(calls) == []


def test_companion_still_checks_the_head_after_an_unverified_authorization(rerun):
    result, calls = rerun(
        runs=_runs(_run(1, 5)), pull_request=_pull_request(state="closed"), pr_fail_first=1
    )
    assert result.returncode == 0, result.stderr
    assert "closed or no longer has the reviewed head" in result.stdout
    assert _reruns(calls) == []


@pytest.mark.parametrize(
    "actor",
    [
        "",
        "a/b",
        "x y",
        "[bot]",
        "a/b[bot]",
        "x[bot]y",
        "a_b_c",
        "_octo",
        "mona_",
        "mona_cat/x",
        "a%2Fb",
        "a" * 40,
        "_[bot]",
    ],
)
def test_companion_does_not_look_up_an_actor_that_is_not_a_user_login(rerun, actor):
    result, calls = rerun(runs=_runs(_run(1, 5)), actor=actor)
    assert result.returncode == 0, result.stderr
    assert "not started by a repository user" in result.stdout
    assert not any("/reviews" in call or "/collaborators/" in call for call in calls)
    assert _reruns(calls) == []


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_companion_reruns_for_an_enterprise_managed_user_reviewer(rerun, association):
    # Enterprise Managed User logins end in an underscore and the enterprise shortcode.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="mona-cat_octo",
        reviews=[_review("mona-cat_octo", association, state="APPROVED", body="")],
    )
    assert result.returncode == 0, result.stderr
    assert not any("/collaborators/" in call for call in calls)
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_reruns_for_an_enterprise_managed_user_dismisser(rerun):
    # A managed maintainer with write access and no review of their own dismissed one.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="mona-cat_octo",
        reviews=[_dismissed_approval()],
        permission="write",
    )
    assert result.returncode == 0, result.stderr
    assert (
        "api /repos/owner/project/collaborators/mona-cat_octo/permission --jq .permission" in calls
    )
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_accepts_a_39_character_login(rerun):
    login = "a" * 34 + "_octo"
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor=login,
        reviews=[_review(login, "OWNER", state="APPROVED", body="")],
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def _dismissed(actor="review-app[bot]", review_id=1, at="2026-11-13T11:59:50Z"):
    return {
        "event": "review_dismissed",
        "actor": {"login": actor},
        "created_at": at,
        "dismissed_review": {"review_id": review_id},
    }


def _bot_review(at, login="review-app[bot]", review_id=9):
    return {
        "id": review_id,
        "user": {"login": login, "type": "Bot"},
        "author_association": "NONE",
        "state": "COMMENTED",
        "body": "summary",
        "commit_id": HEAD,
        "submitted_at": at,
    }


def _dismissed_approval(association="OWNER"):
    return _review("approver", association, state="DISMISSED", body="")


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_companion_reruns_when_a_bot_dismisses_an_eligible_approval(rerun, association):
    # The reviewer approved, then a GitHub App dismissed that approval through the API.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=[_dismissed_approval(association)],
        events=[_dismissed("Review-App[bot]")],
    )
    assert result.returncode == 0, result.stderr
    assert "api --paginate --slurp /repos/owner/project/issues/17/events?per_page=100" in calls
    assert not any("/collaborators/" in call for call in calls)
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_allows_a_minute_of_clock_skew_on_a_bot_dismissal(rerun):
    # GitHub may stamp the dismissal a little after the relay run it caused.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=[_dismissed_approval()],
        events=[_dismissed(at="2026-11-13T12:00:45Z")],
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_reruns_for_later_activity_by_a_bot_that_dismissed_an_approval(rerun):
    # An old dismissal by the bot, then a later, different review action by the same bot.
    # This run is authorized too, and that is harmless by construction: only authorized
    # runs reach the rerun job's concurrency group, and every one of them re-runs all
    # policy runs for the current head against the current reviews, so a replaced pending
    # run loses nothing. The cost is a redundant re-run, bounded to a bot that really
    # dismissed an eligible review on this pull request.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=[_dismissed_approval(), _bot_review("2026-11-13T11:59:58Z")],
        events=[_dismissed(at="2026-11-13T09:00:00Z")],
    )
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.mark.parametrize(
    ("events", "reviews"),
    [
        ([_dismissed("maintainer")], [_dismissed_approval()]),
        ([_dismissed(review_id=2)], [_dismissed_approval()]),
        ([_dismissed(at="2026-11-13T12:01:01Z")], [_dismissed_approval()]),
        ([], [_dismissed_approval()]),
        ([{**_dismissed(), "event": "review_requested"}], [_dismissed_approval()]),
        ([_dismissed()], [_review()]),
        ([_dismissed()], [_review("approver", "OWNER", state="APPROVED", body="")]),
        ([_dismissed()], [_review("drive-by", "NONE", state="DISMISSED", body="")]),
        (
            [_dismissed()],
            [
                {
                    **_review("owner-app[bot]", "OWNER", state="DISMISSED", body=""),
                    "user": {"login": "owner-app[bot]", "type": "Bot"},
                }
            ],
        ),
    ],
    ids=[
        "dismissed-by-another-account",
        "dismissed-another-review",
        "dismissed-after-the-relay-run",
        "no-events",
        "other-event",
        "review-not-dismissed",
        "review-still-approved",
        "unassociated-review",
        "bot-type-review",
    ],
)
def test_companion_ignores_a_bot_that_did_not_dismiss_an_eligible_review(rerun, events, reviews):
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=reviews,
        events=events,
    )
    assert result.returncode == 0, result.stderr
    assert "is a bot account that had not dismissed" in result.stdout
    assert not any("/collaborators/" in call for call in calls)
    assert _reruns(calls) == []
    assert not any("actions/workflows" in call for call in calls)


@pytest.mark.parametrize("relay_created_at", ["", "not-a-time"])
def test_companion_reruns_a_bot_relay_whose_dismissal_cannot_be_evaluated(rerun, relay_created_at):
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=[_dismissed_approval()],
        events=[_dismissed()],
        relay_created_at=relay_created_at,
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read the dismissed review" in result.stdout
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_reruns_for_a_bot_when_the_issue_events_are_unreadable(rerun):
    # Skipping here could leave a stale success after a dismissal. A re-run cannot make
    # the policy pass wrongly, because the policy job reads the reviews itself.
    result, calls = rerun(
        runs=_runs(_run(1, 5)),
        actor="review-app[bot]",
        reviews=[_dismissed_approval()],
        events_exit=1,
    )
    assert result.returncode == 0, result.stderr
    assert "::warning::Could not read the issue events" in result.stdout
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


@pytest.fixture
def enforce(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    script = _step_script(_read("human-approval.yml"), "Enforce the configured approval result")

    def run(**variables):
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-e", "-u", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                **os.environ,
                "DEPENDABOT_ELIGIBLE": "false",
                "DEPENDABOT_EXEMPT": "",
                "POLICY_RESULT": "success",
                "REQUIRES_APPROVAL": "true",
                "APPROVAL_MODE": "owner_review",
                "ENVIRONMENT_RESULT": "skipped",
                "HEAD_SHA": HEAD,
                "APPROVED_HEAD": "",
                **variables,
            },
            capture_output=True,
            text=True,
            timeout=10,
        )

    return run


def test_waiting_approval_names_the_head_and_the_rerun(enforce):
    result = enforce(APPROVED="false")
    assert result.returncode == 1
    assert f"Waiting for an eligible owner_review review of head {HEAD}" in result.stdout
    assert "A review from an owner, member, or collaborator" in result.stdout
    assert "with write access, re-runs this check" in result.stdout


def test_recorded_approval_names_the_approved_head(enforce):
    result = enforce(APPROVED="true", APPROVED_HEAD="d" * 40)
    assert result.returncode == 0, result.stderr
    assert f"recorded for head {'d' * 40}" in result.stdout


def test_recorded_approval_without_an_approved_head_names_the_current_head(enforce):
    result = enforce(APPROVED="true", APPROVED_HEAD="")
    assert result.returncode == 0, result.stderr
    assert f"recorded for head {HEAD}." in result.stdout


def test_explicit_false_is_the_only_result_that_skips_approval(enforce):
    result = enforce(REQUIRES_APPROVAL="false", APPROVED="false")
    assert result.returncode == 0, result.stderr
    assert "No high-risk approval is required." in result.stdout


@pytest.mark.parametrize("requires", ["", "null", "False", "0", "yes"])
def test_a_missing_or_malformed_approval_result_fails_closed(enforce, requires):
    result = enforce(REQUIRES_APPROVAL=requires, APPROVED="false")
    assert result.returncode == 1
    assert "No high-risk approval is required" not in result.stdout
    assert f"requires_approval='{requires}'" in result.stderr


def test_policy_workflow_runs_only_on_the_protected_base_event():
    policy = _read("human-approval.yml")
    events = " ".join(policy.split("\non:\n", 1)[1].split("\npermissions:", 1)[0].split())
    assert events == (
        "pull_request_target: types: [opened, reopened, synchronize, ready_for_review, "
        "auto_merge_enabled, auto_merge_disabled]"
    )
    assert "pull_request_review" not in events
    assert "name: high-risk human approval" in policy
    record = " ".join(
        policy.split("      - name: Record the evaluated policy for the re-run companion\n", 1)[1]
        .split("\n      - name:", 1)[0]
        .split()
    )
    assert "if: always()" in record
    assert "name: human-approval-policy path: ${{ runner.temp }}/approval-policy.json" in record
    assert "overwrite: true" in record


def test_docs_do_not_claim_the_required_check_name_cannot_be_imitated():
    """Required checks match on name, so the docs state the real guarantee."""
    docs = " ".join((ROOT / "docs/attestations-and-ci.md").read_text().split())
    assert "cannot be satisfied that way" not in docs
    assert "A required check matches on the check name" in docs
    assert "Select **GitHub Actions** as the expected source of each required check" in docs
    assert "requires the workflow itself to pass, pinned to a protected ref" in docs


def test_review_relay_has_no_permissions_and_no_check_of_its_own():
    relay = _read("human-approval-review.yml")
    events = " ".join(relay.split("\non:\n", 1)[1].split("\npermissions:", 1)[0].split())
    assert events == "pull_request_review: types: [submitted, edited, dismissed]"
    assert "\npermissions: {}\n" in relay
    assert "actions/checkout" not in relay
    assert "name: high-risk human approval" not in relay
    assert "GH_TOKEN" not in relay
    job = " ".join(_job(relay, "relay").split())
    assert "PR_NUMBER: ${{ github.event.pull_request.number }}" in job
    assert "name: human-approval-review" in job
    assert "retention-days: 1" in job


def test_companion_runs_from_the_default_branch_with_rerun_permission_only():
    companion = _read("human-approval-rerun.yml")
    events = " ".join(companion.split("\non:\n", 1)[1].split("\npermissions:", 1)[0].split())
    assert events == "workflow_run: workflows: [Human approval review] types: [completed]"
    permissions = companion.split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]
    assert permissions.split() == [
        "actions:",
        "write",
        "contents:",
        "read",
        "pull-requests:",
        "read",
    ]
    assert "actions/checkout" not in companion
    assert "name: high-risk human approval" not in companion
    # No workflow-level concurrency: an ineligible review must never enter the group.
    assert "\nconcurrency:" not in companion
    # Only the rerun job holds a concurrency group; it re-runs every policy run for the
    # head (test_companion_reruns_every_policy_run_for_the_reviewed_head_oldest_first).
    assert companion.count("concurrency:") == 1
    authorize = _job(companion, "authorize", "rerun")
    flat_authorize = " ".join(authorize.split())
    assert (
        "if: >- github.event.workflow_run.event == 'pull_request_review' && "
        "github.event.workflow_run.conclusion == 'success'"
    ) in flat_authorize
    assert "concurrency:" not in authorize
    assert (
        "TRIGGERING_ACTOR: ${{ github.event.workflow_run.triggering_actor.login }}"
        in flat_authorize
    )
    # The bot path is tied to the relay run by its trusted creation time.
    assert "RELAY_CREATED_AT: ${{ github.event.workflow_run.created_at }}" in flat_authorize
    assert "allowed: ${{ steps.authorize.outputs.allowed }}" in flat_authorize
    assert "pr_number: ${{ steps.authorize.outputs.pr_number }}" in flat_authorize
    assert "gh run rerun" not in authorize
    rerun = _job(companion, "rerun")
    flat_rerun = " ".join(rerun.split())
    # The re-run job is skipped, never queued, unless authorize allowed it, and only
    # then takes the per-head concurrency slot.
    assert (
        "needs: authorize if: needs.authorize.outputs.allowed == 'true' runs-on: ubuntu-latest"
        in flat_rerun
    )
    assert (
        "concurrency: group: agentic-preflight-human-approval-rerun-"
        "${{ needs.authorize.outputs.pr_number }}-${{ github.event.workflow_run.head_sha }} #"
        in flat_rerun
    )
    assert flat_rerun.index("if: needs.authorize") < flat_rerun.index("concurrency:")
    assert "cancel-in-progress" not in rerun
    assert "PR_NUMBER: ${{ needs.authorize.outputs.pr_number }}" in flat_rerun
    assert "TRIGGERING_ACTOR" not in rerun
    assert 'gh run download "$RELAY_RUN_ID"' not in rerun
