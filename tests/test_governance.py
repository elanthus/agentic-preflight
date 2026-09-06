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
    assert 'agentic-preflight hosted-check "$ATTESTED_SHA"' in workflow
    assert 'test ! -e "$UV_TOOL_BIN_DIR/ap"' in workflow


def test_high_risk_approval_runs_trusted_code_and_rechecks_on_reviews():
    workflow = (ROOT / ".github" / "workflows" / "human-approval.yml").read_text(encoding="utf-8")
    assert "pull_request_target:" in workflow
    assert "pull_request_review:" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "high-risk human approval" in workflow
    assert 'agentic-preflight hosted-check "$HEAD_SHA"' in workflow
    assert "--mode approval" in workflow
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


def test_workflows_share_the_protected_availability_helper():
    for filename in ("ci.yml", "human-approval.yml"):
        workflow = (ROOT / ".github/workflows" / filename).read_text()
        policy_step = _policy_script(filename)
        assert policy_step.count("agentic-preflight hosted-check") == 1
        assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
        assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
        assert "HEAD_REF: ${{ github.event.pull_request.head.ref }}" in workflow
        assert (
            "HEAD_REPOSITORY_URL: ${{ github.event.pull_request.head.repo.clone_url }}" in workflow
        )
        assert "uv tool install --python 3.11 ." in workflow
        assert '--base "$BASE_SHA"' in policy_step
        assert '--head-ref "refs/heads/$HEAD_REF"' in policy_step
        assert '--source-remote "$source_remote"' in policy_step
        assert "git fetch" not in policy_step
        assert "sleep " not in policy_step
        assert "approval-check" not in policy_step


def _policy_script(filename):
    import textwrap

    workflow = (ROOT / ".github/workflows" / filename).read_text()
    label = (
        "Fetch and verify the pull request attestation"
        if filename == "ci.yml"
        else "Evaluate approval policy for the exact head"
    )
    end = "\n  test:" if filename == "ci.yml" else "\n  environment:"
    block = workflow[workflow.index("      - name: " + label) : workflow.index(end)]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])
