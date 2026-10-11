"""Exercise the hosted Dependabot exception with inert API fixtures."""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HEAD = "a" * 40


@pytest.fixture
def classify(tmp_path):
    bash = shutil.which("bash")
    if bash is None or shutil.which("jq") is None:
        pytest.skip("Bash and jq are required")
    workflow = (ROOT / ".github/workflows/dependabot-policy.yml").read_text()
    script = textwrap.dedent(workflow.split("        run: |\n", 1)[1])

    def run(
        *,
        dependencies=None,
        files=None,
        head=HEAD,
        count=None,
        commits=None,
        commit_count=None,
        result="success",
        api_fail=False,
    ):
        if dependencies is None:
            dependencies = [
                {
                    "packageEcosystem": "uv",
                    "directory": "/",
                    "updateType": "version-update:semver-patch",
                }
            ]
        if files is None:
            files = [{"filename": "uv.lock", "status": "modified"}]
        if commits is None:
            commits = [{"sha": HEAD, "author": {"login": "dependabot[bot]"}}]
        (tmp_path / "commits.json").write_text(json.dumps([commits]))
        (tmp_path / "files.json").write_text(json.dumps([files]))
        (tmp_path / "pr.json").write_text(
            json.dumps(
                {
                    "head": {"sha": head},
                    "changed_files": len(files) if count is None else count,
                    "commits": len(commits) if commit_count is None else commit_count,
                }
            )
        )
        output = tmp_path / "output"
        output.write_text("")
        completed = subprocess.run(
            [
                bash,
                "-e",
                "-u",
                "-o",
                "pipefail",
                "-c",
                """
            gh() {
              if [ "$API_FAIL" = "true" ]; then return 1; fi
              case "$*" in
                *files*) cat files.json ;;
                *commits*) cat commits.json ;;
                *) cat pr.json ;;
              esac
            }
            """
                + script,
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "RUNNER_TEMP": str(tmp_path),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_REPOSITORY": "owner/project",
                "PR_NUMBER": "225",
                "HEAD_SHA": HEAD,
                "METADATA_RESULT": result,
                "DEPENDENCIES": json.dumps(dependencies),
                "API_FAIL": str(api_fail).lower(),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed, output.read_text().splitlines()[-1]

    return run


@pytest.mark.parametrize("ecosystem", ["uv", "pip"])
def test_patch_minor_groups_qualify(classify, ecosystem):
    result, output = classify(
        dependencies=[
            {
                "packageEcosystem": ecosystem,
                "directory": "/",
                "updateType": f"version-update:semver-{kind}",
            }
            for kind in ("patch", "minor")
        ]
    )
    assert result.returncode == 0, result.stderr
    assert output == "eligible=true"


@pytest.mark.parametrize("kind", ["major", "unknown"])
def test_any_ineligible_group_member_closes_exception(classify, kind):
    result, output = classify(
        dependencies=[
            {
                "packageEcosystem": "uv",
                "directory": "/",
                "updateType": f"version-update:semver-{level}",
            }
            for level in ("patch", kind)
        ]
    )
    assert result.returncode == 0
    assert output == "eligible=false"


@pytest.mark.parametrize(
    "dependencies",
    [
        [],
        [
            {
                "packageEcosystem": "github-actions",
                "directory": "/",
                "updateType": "version-update:semver-patch",
            }
        ],
        [
            {
                "packageEcosystem": "uv",
                "directory": "/other",
                "updateType": "version-update:semver-patch",
            }
        ],
        [{}],
    ],
)
def test_unrecognized_metadata_does_not_qualify(classify, dependencies):
    result, output = classify(dependencies=dependencies)
    assert result.returncode == 0
    assert output == "eligible=false"


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"filename": ".github/workflows/ci.yml", "status": "modified"}],
        [{"filename": "uv.lock", "status": "removed"}],
        [{"filename": "pyproject.toml", "status": "renamed", "previous_filename": "other"}],
        [
            {"filename": "uv.lock", "status": "modified"},
            {"filename": "README.md", "status": "modified"},
        ],
    ],
)
def test_changed_files_are_limited_to_modified_manifest_and_lockfile(classify, files):
    result, output = classify(files=files)
    assert result.returncode == 0
    assert output == "eligible=false"


@pytest.mark.parametrize("changes", [{"head": "b" * 40}, {"count": 3001}, {"api_fail": True}])
def test_races_truncation_and_api_failure_fail_closed(classify, changes):
    result, output = classify(**changes)
    assert result.returncode != 0
    assert output == "eligible=false"


@pytest.mark.parametrize("result", ["skipped", "failure"])
def test_missing_verified_metadata_fails_closed(classify, result):
    completed, output = classify(result=result)
    assert completed.returncode == 0
    assert output == "eligible=false"


def test_bot_identity_and_verified_metadata_are_required():
    workflow = (ROOT / ".github/workflows/dependabot-policy.yml").read_text()
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in workflow
    assert "github.event.pull_request.user.type == 'Bot'" in workflow
    assert "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    assert "uses: dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98" in workflow
    assert "skip-verification" not in workflow
    assert "skip-commit-verification" not in workflow
    assert "checkout@" not in workflow
    assert ": write" not in workflow


@pytest.mark.parametrize("eligible", ["true", "false", ""])
def test_approval_exception_is_limited_to_positive_eligibility(tmp_path, eligible):
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
            "DEPENDABOT_ELIGIBLE": eligible,
            "POLICY_RESULT": "skipped",
            "APPROVED": "true",
            "REQUIRES_APPROVAL": "false",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if eligible == "true" else 1)
    assert ("Verified Dependabot" in result.stdout) == (eligible == "true")


def test_auto_merge_uses_verified_policy_and_never_executes_pr_code():
    workflow = (ROOT / ".github/workflows/dependabot-auto-merge.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "uses: ./.github/workflows/dependabot-policy.yml" in workflow
    assert "if: needs.dependabot-policy.outputs.eligible == 'true'" in workflow
    assert "--auto --squash --match-head-commit" in workflow
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "checkout@" not in workflow
    assert "gh pr checkout" not in workflow
    assert "pull-requests: write" in workflow
    assert "contents: write" in workflow


def test_human_appended_commit_does_not_receive_bot_metadata_exception(classify):
    result, output = classify(
        commits=[
            {"sha": "b" * 40, "author": {"login": "dependabot[bot]"}},
            {"sha": HEAD, "author": {"login": "human"}},
        ]
    )
    assert result.returncode != 0
    assert output == "eligible=false"


@pytest.mark.parametrize(
    "changes",
    [
        {"commits": []},
        {"commits": [{"sha": "b" * 40}]},
        {"commit_count": 2},
    ],
)
def test_commit_list_must_bind_the_single_current_event_head(classify, changes):
    result, output = classify(**changes)
    assert result.returncode != 0
    assert output == "eligible=false"
