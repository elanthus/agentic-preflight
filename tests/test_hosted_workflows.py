"""Execute the hosted shell callers with deterministic external-command responses."""

import json
import os
import shutil
import subprocess
import textwrap

import pytest

from tests.test_governance import ROOT, _policy_script

HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "feature/$(touch${IFS}injected)"


@pytest.fixture
def run_step(tmp_path):
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("Bash and jq are required to execute the Linux workflow steps")

    def run(
        filename, *, code=0, fork=False, mode="manual_merge", auto_merge=False, environment=True
    ):
        payload = {
            "ok": code == 0,
            "data": {
                "reason": "missing_note" if code else None,
                "approved": mode == "manual_merge",
                "approval_mode": mode,
                "approval_environment": "high-risk-review",
                "requires_human_approval": True,
                "availability": {"expected_head": HEAD, "base_sha": BASE},
            },
        }
        (tmp_path / "response.json").write_text(json.dumps(payload))
        (tmp_path / "environment-response.json").write_text(
            json.dumps(
                [
                    {
                        "environments": [
                            {
                                "name": "high-risk-review",
                                "protection_rules": [
                                    {
                                        "type": "required_reviewers",
                                        "reviewers": [{}] if environment else [],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            )
        )
        (tmp_path / "output.txt").write_text("")
        # Seed stale success to prove the command redirection replaces it.
        (tmp_path / "approval-policy.json").write_text('{"ok":true,"data":{"approved":true}}')
        (tmp_path / "attestation-result.json").write_text('{"ok":true}')
        harness = r"""
agentic-preflight() {
  printf '%s\n' "$@" > helper-args.txt
  printf 'call\n' >> helper-calls.txt
  cat response.json
  return "$TEST_EXIT"
}
git() { printf '%s\n' "$@" >> git-args.txt; }
gh() {
  printf '%s\n' "$@" >> gh-args.txt
  case "$*" in
    *reviews*) echo '[]' ;;
    *environments*) cat environment-response.json ;;
    *) echo "$TEST_AUTO_MERGE" ;;
  esac
}
RUNNER_TEMP=.
GITHUB_OUTPUT=output.txt
"""
        env = {
            **os.environ,
            "HEAD_SHA": HEAD,
            "ATTESTED_SHA": HEAD,
            "BASE_SHA": BASE,
            "HEAD_REF": BRANCH,
            "GITHUB_REPOSITORY": "owner/project",
            "HEAD_REPOSITORY": "contributor/project" if fork else "owner/project",
            "HEAD_REPOSITORY_URL": "https://github.com/contributor/project.git",
            "PR_AUTHOR": "author",
            "PR_NUMBER": "88",
            "TEST_EXIT": str(code),
            "TEST_AUTO_MERGE": str(auto_merge).lower(),
        }
        result = subprocess.run(
            [
                bash,
                "--noprofile",
                "--norc",
                "-e",
                "-u",
                "-o",
                "pipefail",
                "-c",
                harness + _policy_script(filename),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, tmp_path

    return run


@pytest.mark.parametrize("filename", ["ci.yml", "human-approval.yml"])
@pytest.mark.parametrize("code", [2, 4])
def test_failures_print_json_keep_exit_and_never_publish_outputs(run_step, filename, code):
    result, path = run_step(filename, code=code)
    assert result.returncode == code, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["reason"] == "missing_note"
    assert (path / "output.txt").read_text() == ""
    assert (path / "helper-calls.txt").read_text().splitlines() == ["call"]


@pytest.mark.parametrize("filename", ["ci.yml", "human-approval.yml"])
@pytest.mark.parametrize("fork", [True, False])
def test_success_passes_fixed_event_and_resolved_fork_remote(run_step, filename, fork):
    result, path = run_step(filename, fork=fork)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    args = (path / "helper-args.txt").read_text().splitlines()
    assert args[:2] == ["hosted-check", HEAD]
    assert args[args.index("--base") + 1] == BASE
    assert args[args.index("--head-ref") + 1] == "refs/heads/" + BRANCH
    assert args[args.index("--source-remote") + 1] == (
        "preflight-contributor" if fork else "origin"
    )
    assert not (path / "injected").exists()
    if fork:
        assert (path / "git-args.txt").read_text().splitlines() == [
            "remote",
            "add",
            "preflight-contributor",
            "https://github.com/contributor/project.git",
        ]
    else:
        assert not (path / "git-args.txt").exists()
    if filename == "human-approval.yml":
        assert args[args.index("--mode") + 1] == "approval"
        assert "--report-only" in args
        assert (path / "output.txt").read_text().splitlines() == [
            "approved=true",
            "approval_mode=manual_merge",
            "environment=high-risk-review",
            "requires_approval=true",
        ]
    else:
        assert "--mode" not in args


def test_auto_merge_remains_blocked(run_step):
    result, path = run_step("human-approval.yml", auto_merge=True)
    assert result.returncode == 1
    assert "Disable auto-merge" in result.stderr
    assert (path / "output.txt").read_text() == ""


@pytest.mark.parametrize("environment", [True, False])
def test_environment_gate_configuration_remains_required(run_step, environment):
    result, path = run_step("human-approval.yml", mode="environment", environment=environment)
    assert result.returncode == (0 if environment else 1), result.stderr
    output = (path / "output.txt").read_text()
    if environment:
        assert "approved=false" in output
        assert "approval_mode=environment" in output
    else:
        assert output == ""
        assert "must exist with a required reviewer" in result.stderr


def test_failed_policy_cannot_be_green_from_stale_approval_outputs(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    workflow = (ROOT / ".github/workflows/human-approval.yml").read_text()
    script = textwrap.dedent(
        workflow.split("      - name: Enforce the configured approval result", 1)[1].split(
            "        run: |\n", 1
        )[1]
    )
    result = subprocess.run(
        [bash, "-e", "-u", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "POLICY_RESULT": "failure",
            "APPROVED": "true",
            "REQUIRES_APPROVAL": "false",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "No high-risk approval is required" not in result.stdout
