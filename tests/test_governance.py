import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_short_ap_console_alias_is_not_packaged():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"agentic-preflight": "agentic_preflight.cli:main"}


def test_ci_verifies_attestations_with_the_protected_base_version():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "trusted preflight attestation" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert 'agentic-preflight verify "$ATTESTED_SHA"' in workflow
    assert 'test ! -e "$UV_TOOL_BIN_DIR/ap"' in workflow


def test_high_risk_approval_runs_trusted_code_and_rechecks_on_reviews():
    workflow = (ROOT / ".github" / "workflows" / "human-approval.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in workflow
    assert "pull_request_review:" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "high-risk human approval" in workflow
    assert "agentic-preflight approval-check" in workflow
    assert "--report-only" in workflow
    assert "auto_merge_enabled" in workflow
    assert "auto_merge_disabled" in workflow
    assert ".auto_merge != null" in workflow
    assert "Disable auto-merge" in workflow
    assert "manual_merge" in workflow
    assert "peer_review" in workflow
    assert "owner environment approval" in workflow
    assert "needs.policy.outputs.environment" in workflow
    assert "actions: read" in workflow
    assert "Environment $approval_environment must exist with a required reviewer." in workflow


def test_codeowners_protects_the_policy_and_verifier_surfaces():
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for path in (
        "/.agentic-preflight.toml",
        "/.github/CODEOWNERS",
        "/.github/workflows/",
        "/agentic_preflight/",
        "/agentic_preflight/risk.py",
        "/agentic_preflight/attestation.py",
        "/skill/",
    ):
        assert path in codeowners


def test_legacy_workflow_failure_json_is_visible_and_exit_is_preserved(tmp_path):
    import shutil
    import subprocess
    import textwrap

    import pytest

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    for filename, start, end in (
        ("ci.yml", "          verify_exit=0", "\n\n  test:"),
        ("human-approval.yml", "          policy_exit=0", "          approval_mode="),
    ):
        workflow = (ROOT / ".github/workflows" / filename).read_text()
        script = textwrap.dedent(workflow[workflow.index(start) : workflow.index(end)])
        result_file = tmp_path / "result.json"
        result_file.write_text('{"ok":true,"data":{"approved":true}}')
        # Execute the actual workflow fragment under GitHub's Bash error flags.
        # The function supplies the same failure envelope as either legacy CLI.
        harness = """
agentic-preflight() { echo '{"ok":false,"data":{"reason":"missing_note"}}'; return 2; }
ATTESTED_SHA=abc
HEAD_SHA=abc
BASE_SHA=def
reviews_file=unused
PR_AUTHOR=author
result_file=$1
"""
        result = subprocess.run(
            [
                bash,
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                "-c",
                harness + script + "\necho UNREACHABLE",
                "test",
                str(result_file),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2, result.stderr
        assert '"reason":"missing_note"' in result.stdout
        assert "UNREACHABLE" not in result.stdout
        assert '"approved":true' not in result.stdout


def test_helper_rollout_keeps_existing_protected_base_commands():
    for filename in ("ci.yml", "human-approval.yml"):
        workflow = (ROOT / ".github/workflows" / filename).read_text()
        assert "hosted-check" not in workflow
        assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
        assert "source_remote=preflight-contributor" in workflow
