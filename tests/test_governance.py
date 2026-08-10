import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_short_ap_console_alias_is_not_packaged():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"] == {"agentic-preflight": "agentic_preflight.cli:main"}


def test_ci_verifies_attestations_with_the_protected_base_version():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "trusted preflight attestation" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert 'agentic-preflight verify "$ATTESTED_SHA"' in workflow
    assert 'test ! -e "$UV_TOOL_BIN_DIR/ap"' in workflow


def test_high_risk_approval_runs_trusted_code_and_rechecks_on_reviews():
    workflow = (ROOT / ".github" / "workflows" / "human-approval.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "pull_request_review:" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "high-risk human approval" in workflow
    assert "agentic-preflight approval-check" in workflow


def test_codeowners_protects_the_policy_and_verifier_surfaces():
    codeowners = (ROOT / ".github" / "CODEOWNERS").read_text()
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
