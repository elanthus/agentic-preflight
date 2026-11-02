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


OWNER_REVIEW = 'reviewer = "elanthus"\n[approval]\nmode = "owner_review"\n'


def _runs(*runs):
    return [{"workflow_runs": list(runs)}]


def _run(run_id, run_number, *, head_sha=HEAD, status="completed", numbers=()):
    return {
        "id": run_id,
        "run_number": run_number,
        "head_sha": head_sha,
        "status": status,
        "pull_requests": [{"number": number} for number in numbers],
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
    if bash is None or shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("Bash, jq, and python3 are required to execute the workflow step")
    script = _step_script(
        _read("human-approval-rerun.yml"), "Re-run the trusted policy run for the reviewed head"
    )

    def run(
        *,
        pull_request=None,
        relay="17\n",
        config=OWNER_REVIEW,
        config_error="",
        runs=None,
        download_exit=0,
        rerun_exit=0,
        run_status="completed",
    ):
        (tmp_path / "pr-fixture.json").write_text(json.dumps(pull_request or _pull_request()))
        (tmp_path / "relay.txt").write_text(relay)
        (tmp_path / "config.toml").write_text(config)
        (tmp_path / "runs.json").write_text(json.dumps(runs if runs is not None else _runs()))
        harness = r"""
gh() {
  printf '%s\n' "$*" >> gh-calls.txt
  case "$1 $2" in
    "run download")
      [ "$TEST_DOWNLOAD_EXIT" = 0 ] || return "$TEST_DOWNLOAD_EXIT"
      mkdir -p "$9"
      cp relay.txt "$9/pr-number"
      ;;
    "run rerun") return "$TEST_RERUN_EXIT" ;;
    "api -H")
      if [ -n "$TEST_CONFIG_ERROR" ]; then
        echo "gh: $TEST_CONFIG_ERROR" >&2
        return 1
      fi
      cat config.toml
      ;;
    "api --paginate") cat runs.json ;;
    api*)
      case "$2" in
        */pulls/*) cat pr-fixture.json ;;
        */actions/runs/*) echo "$TEST_RUN_STATUS" ;;
      esac
      ;;
  esac
}
sleep() { :; }
RUNNER_TEMP=.
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
                "TEST_RERUN_EXIT": str(rerun_exit),
                "TEST_CONFIG_ERROR": config_error,
                "TEST_RUN_STATUS": run_status,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls_file = tmp_path / "gh-calls.txt"
        calls = calls_file.read_text().splitlines() if calls_file.exists() else []
        return result, calls

    return run


def _reruns(calls):
    return [call for call in calls if call.startswith("run rerun ")]


def test_companion_reruns_the_newest_policy_run_for_the_reviewed_head(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5), _run(2, 7), _run(3, 9, head_sha="c" * 40)))
    assert result.returncode == 0, result.stderr
    assert (
        calls[0]
        == "run download 41 --repo owner/project --name human-approval-review --dir ./human-approval-review"
    )
    assert "api /repos/owner/project/pulls/17" in calls
    assert any(f"contents/.agentic-preflight.toml?ref={BASE}" in call for call in calls)
    assert any(f"event=pull_request_target&head_sha={HEAD}" in call for call in calls)
    assert _reruns(calls) == ["run rerun 2 --repo owner/project"]
    assert "Re-running Human approval policy run 2" in result.stdout


def test_companion_prefers_the_run_that_belongs_to_the_pull_request(rerun):
    result, calls = rerun(runs=_runs(_run(1, 9), _run(2, 7, numbers=(17,))))
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 2 --repo owner/project"]


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
    assert not any("contents/" in call for call in calls)
    assert _reruns(calls) == []


@pytest.mark.parametrize(
    "config",
    [
        '[approval]\nmode = "manual_merge"\n',
        '[approval]\nmode = "environment"\nenvironment = "high-risk-review"\n',
        '[general]\nbase_ref = "main"\n',
        "",
    ],
)
def test_companion_reruns_only_in_the_review_modes(rerun, config):
    result, calls = rerun(config=config, runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert "does not read reviews" in result.stdout
    assert not any("actions/workflows" in call for call in calls)
    assert _reruns(calls) == []


def test_companion_reruns_in_peer_review_mode(rerun):
    result, calls = rerun(config='[approval]\nmode = "peer_review"\n', runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_treats_a_missing_base_configuration_as_the_default_mode(rerun):
    result, calls = rerun(config_error="Not Found (HTTP 404)", runs=_runs(_run(1, 5)))
    assert result.returncode == 0, result.stderr
    assert "default approval mode applies" in result.stdout
    assert _reruns(calls) == []


def test_companion_fails_visibly_when_the_base_configuration_cannot_be_read(rerun):
    result, calls = rerun(config_error="API rate limit exceeded (HTTP 403)", runs=_runs(_run(1, 5)))
    assert result.returncode == 1
    assert "HTTP 403" in result.stderr
    assert _reruns(calls) == []


def test_companion_fails_visibly_on_a_malformed_base_configuration(rerun):
    result, calls = rerun(config="[approval\nmode = ", runs=_runs(_run(1, 5)))
    assert result.returncode != 0
    assert _reruns(calls) == []


def test_companion_does_nothing_without_a_policy_run_for_the_head(rerun):
    result, calls = rerun(runs=_runs(_run(3, 9, head_sha="c" * 40)))
    assert result.returncode == 0, result.stderr
    assert "No Human approval policy run exists" in result.stdout
    assert _reruns(calls) == []


def test_companion_waits_for_a_running_policy_run_before_rerunning(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5, status="in_progress")))
    assert result.returncode == 0, result.stderr
    assert "still in progress" in result.stdout
    assert "api /repos/owner/project/actions/runs/1 --jq .status" in calls
    assert _reruns(calls) == ["run rerun 1 --repo owner/project"]


def test_companion_gives_up_on_a_run_that_never_finishes_without_rerunning(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5, status="in_progress")), run_status="in_progress")
    assert result.returncode == 1
    assert "did not finish within 10 minutes" in result.stderr
    assert "30 days" not in result.stderr
    assert len([call for call in calls if call.endswith("--jq .status")]) == 60
    assert _reruns(calls) == []


def test_companion_explains_a_rerun_that_github_refuses(rerun):
    result, calls = rerun(runs=_runs(_run(1, 5)), rerun_exit=1)
    assert result.returncode == 1
    assert "30 days" in result.stderr
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
    assert "A submitted review re-runs this check" in result.stdout


def test_recorded_approval_names_the_approved_head(enforce):
    result = enforce(APPROVED="true", APPROVED_HEAD="d" * 40)
    assert result.returncode == 0, result.stderr
    assert f"recorded for head {'d' * 40}" in result.stdout


def test_policy_workflow_runs_only_on_the_protected_base_event():
    policy = _read("human-approval.yml")
    events = " ".join(policy.split("\non:\n", 1)[1].split("\npermissions:", 1)[0].split())
    assert events == (
        "pull_request_target: types: [opened, reopened, synchronize, ready_for_review, "
        "auto_merge_enabled, auto_merge_disabled]"
    )
    assert "pull_request_review" not in events
    assert "name: high-risk human approval" in policy


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
    job = " ".join(_job(companion, "rerun").split())
    assert (
        "if: >- github.event.workflow_run.event == 'pull_request_review' && "
        "github.event.workflow_run.conclusion == 'success'"
    ) in job
    concurrency = companion.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    assert concurrency.split() == [
        "group:",
        "agentic-preflight-human-approval-rerun-${{",
        "github.event.workflow_run.head_sha",
        "}}",
    ]
