import tomllib
from pathlib import Path

import pytest

from agentic_preflight.config import Config
from agentic_preflight.models import (
    Finding,
    FindingAction,
    FindingStatus,
    RiskLevel,
    Severity,
    Stage,
    Verdict,
)
from agentic_preflight.risk import assess

ROOT = Path(__file__).parent.parent


def test_short_ap_console_alias_is_not_packaged():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"agentic-preflight": "agentic_preflight.cli:main"}


def test_built_wheel_smoke_checks_the_removed_console_alias():
    for filename in ("ci.yml", "release.yml"):
        wheel_workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
        assert "run: bash scripts/smoke-wheel.sh" in wheel_workflow
    smoke = (ROOT / "scripts" / "smoke-wheel.sh").read_text(encoding="utf-8")
    assert 'test ! -e "$UV_TOOL_BIN_DIR/ap"' in smoke


def repository_config():
    return Config.model_validate(tomllib.loads((ROOT / ".agentic-preflight.toml").read_text()))


def path_assessment(paths):
    cfg = repository_config()
    return assess(
        paths,
        [],
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )


def owners_for(path):
    owners = []
    for line in (ROOT / ".github/CODEOWNERS").read_text().splitlines():
        fields = line.split()
        if not fields or line.startswith("#"):
            continue
        # This repository uses only anchored literal paths and directories.
        pattern = fields[0]
        assert pattern.startswith("/")
        assert not any(char in pattern for char in "*?![]")
        literal = pattern.removeprefix("/")
        if path == literal or (literal.endswith("/") and path.startswith(literal)):
            owners = fields[1:]
    return owners


@pytest.mark.parametrize(
    "path",
    [
        ".agentic-preflight.toml",
        ".github/CODEOWNERS",
        ".github/workflows/new-workflow.yml",
        "SECURITY.md",
        "agentic_preflight/approval.py",
        "agentic_preflight/attestation.py",
        "agentic_preflight/cli_policy.py",
        "agentic_preflight/risk.py",
        "agentic_preflight/hook.py",
        "agentic_preflight/publish/new_gate.py",
        "agentic_preflight/runs/publish.py",
    ],
)
def test_sensitive_changes_require_human_review_and_a_code_owner(path):
    # Ordinary code/tests in the same PR cannot dilute a sensitive path match.
    result = path_assessment([path, "agentic_preflight/housekeeping.py", "tests/test_risk.py"])
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.requires_human_review
    assert owners_for(path) == ["@elanthus"]


@pytest.mark.parametrize(
    "path",
    [
        "agentic_preflight/housekeeping.py",
        "agentic_preflight/cli_runs.py",
        "agentic_preflight/grounding.py",
        "agentic_preflight/stages/docs.py",
        "agentic_preflight/stages/shellstage.py",
        "agentic_preflight/new_helper.py",
        # Shared helpers and evidence-reuse decisions, not merge or publication gates.
        "agentic_preflight/config.py",
        "agentic_preflight/findings.py",
        "agentic_preflight/fingerprints.py",
        "agentic_preflight/gitx.py",
        "agentic_preflight/machine.py",
        "agentic_preflight/models.py",
        "agentic_preflight/refresh_validation.py",
        "agentic_preflight/run_invariants.py",
        "agentic_preflight/runs/review_coverage.py",
        "agentic_preflight/stages/change_scope.py",
        "agentic_preflight/wire_schema.py",
        "tests/test_risk.py",
        "skill/SKILL.md",
        "skill/reference/docs-rubric.md",
        "skill/reference/findings-schema.md",
        "skill/reference/playbooks.md",
        "skill/reference/commands.md",
        "skill/reference/workflow.md",
    ],
)
def test_routine_changes_are_medium_risk_without_mandatory_ownership(path):
    result = path_assessment([path])
    assert result.level is RiskLevel.MEDIUM
    assert result.verdict is Verdict.CLEAR
    assert not result.requires_human_review
    assert owners_for(path) == []


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/worktree-modes.md",
        "docs/RELEASING.md",
        "CHANGELOG.md",
        "COMPATIBILITY.md",
    ],
)
def test_ordinary_prose_remains_low_risk(path):
    assert path_assessment([path]).level is RiskLevel.LOW
    assert owners_for(path) == []


def test_human_review_and_codeowners_lists_stay_aligned():
    # These independently enforced policies must not silently diverge.
    expected = {
        "/" + path.removesuffix("**") for path in repository_config().policy.human_review_paths
    }
    actual = {
        line.split()[0]
        for line in (ROOT / ".github/CODEOWNERS").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert actual == expected


def test_dependency_updates_are_audited_without_human_risk_triggers():
    from agentic_preflight.config import Config
    from agentic_preflight.models import RiskLevel
    from agentic_preflight.risk import assess

    cfg = Config.model_validate(tomllib.loads((ROOT / ".agentic-preflight.toml").read_text()))

    def level(paths):
        return assess(
            paths,
            [],
            policy=cfg.policy,
            review_blocking_severities=cfg.review.blocking_severities,
            docs_blocking_severities=cfg.docs.blocking_severities,
        ).level

    assert level(["pyproject.toml", "uv.lock"]) is not RiskLevel.HIGH
    assert level(["uv.lock", "agentic_preflight/approval.py"]) is RiskLevel.HIGH
    assert level(["pyproject.toml", ".github/workflows/ci.yml"]) is RiskLevel.HIGH
    owners = (ROOT / ".github/CODEOWNERS").read_text()
    assert "/pyproject.toml" not in owners
    assert "/uv.lock" not in owners
    assert "/.github/dependabot.yml" in owners
    assert "dependency vulnerability audit" in (ROOT / ".github/workflows/security.yml").read_text()


@pytest.mark.parametrize("path", ["skill/SKILL.md", "COMPATIBILITY.md"])
@pytest.mark.parametrize("status", [FindingStatus.OPEN, FindingStatus.FIXED])
def test_documentation_findings_still_require_human_review(path, status):
    cfg = repository_config()
    finding = Finding(
        id="F001",
        stage=Stage.REVIEW,
        path=path,
        severity=Severity.HIGH,
        action=FindingAction.AUTO_FIX,
        status=status,
        title="Instruction permits bypassing approval",
    )
    result = assess(
        [path],
        [finding],
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )
    assert result.level is RiskLevel.HIGH
    assert result.requires_human_review
    assert result.verdict is (
        Verdict.CHANGES_REQUIRED if status is FindingStatus.OPEN else Verdict.NEEDS_HUMAN
    )
