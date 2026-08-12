from agentic_preflight.config import PolicySection
from agentic_preflight.models import (
    Finding,
    FindingAction,
    FindingStatus,
    RiskLevel,
    Severity,
    Stage,
    Verdict,
)
from agentic_preflight.risk import assess, include_attested_findings


def _finding(
    *,
    severity: Severity = Severity.HIGH,
    status: FindingStatus = FindingStatus.OPEN,
    action: FindingAction = FindingAction.AUTO_FIX,
    stage: Stage = Stage.REVIEW,
    code_owned: bool = False,
) -> Finding:
    return Finding(
        id="F001",
        stage=stage,
        code_owned=code_owned,
        status=status,
        path="src/auth.py",
        line=1,
        severity=severity,
        action=action,
        title="review finding",
    )


def _assess(
    paths,
    findings=(),
    policy=None,
    *,
    review_blocking_severities=("critical", "high"),
    docs_blocking_severities=("critical", "high"),
):
    return assess(
        list(paths),
        list(findings),
        policy=policy or PolicySection(),
        review_blocking_severities=list(review_blocking_severities),
        docs_blocking_severities=list(docs_blocking_severities),
    )


def test_unmatched_change_is_low_risk_and_passes():
    result = _assess(["src/app.py"])
    assert result.level is RiskLevel.LOW
    assert result.verdict is Verdict.CLEAR
    assert result.reasons == []


def test_path_policy_classifies_risk_without_using_diff_size():
    policy = PolicySection(
        high_risk_paths=["db/migrations/**"],
        medium_risk_paths=["dependencies/**"],
    )
    high = _assess(["db/migrations/0042_add_index.sql"], policy=policy)
    medium = _assess(["dependencies/catalog.toml"], policy=policy)
    assert high.level is RiskLevel.HIGH
    assert high.verdict is Verdict.NEEDS_HUMAN
    assert high.requires_human_review is True
    assert medium.level is RiskLevel.MEDIUM


def test_human_review_path_forces_a_human_verdict():
    result = _assess(
        [".github/workflows/ci.yml"],
        policy=PolicySection(human_review_paths=[".github/workflows/**"]),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.requires_human_review is True
    assert result.reasons[0].pattern == ".github/workflows/**"


def test_an_open_blocking_finding_requires_changes():
    result = _assess(["src/auth.py"], [_finding()])
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_fixed_high_finding_keeps_high_risk_but_no_longer_blocks():
    result = _assess(
        ["src/auth.py"],
        [_finding(status=FindingStatus.FIXED)],
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert result.requires_human_review is True
    assert result.reasons[0].finding_id == "F001"


def test_ask_user_blocks_at_any_severity():
    result = _assess(
        ["src/copy.py"],
        [_finding(severity=Severity.LOW, action=FindingAction.ASK_USER)],
    )
    assert result.level is RiskLevel.LOW
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_code_owned_finding_requires_changes_when_severity_is_excluded():
    result = _assess(
        ["CHANGELOG.md"],
        [
            _finding(
                stage=Stage.DOCS,
                code_owned=True,
            )
        ],
        docs_blocking_severities=("critical",),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_agent_finding_excluded_by_policy_does_not_require_changes():
    result = _assess(
        ["README.md"],
        [_finding(stage=Stage.DOCS)],
        docs_blocking_severities=("critical",),
    )
    assert result.level is RiskLevel.HIGH
    assert result.verdict is Verdict.NEEDS_HUMAN


def test_changes_required_takes_precedence_until_the_finding_is_resolved():
    result = _assess(
        ["policy.yml"],
        [_finding()],
        PolicySection(human_review_paths=["policy.yml"]),
    )
    assert result.verdict is Verdict.CHANGES_REQUIRED


def test_imported_attestation_preserves_medium_and_high_finding_risk():
    base = _assess(["src/app.py"])

    medium = include_attested_findings(base, {"medium": 2})
    assert medium.level is RiskLevel.MEDIUM
    assert medium.verdict is Verdict.CLEAR
    assert medium.requires_human_review is False
    assert medium.reasons[-1].severity is Severity.MEDIUM

    high = include_attested_findings(base, {"critical": 1})
    assert high.level is RiskLevel.HIGH
    assert high.verdict is Verdict.NEEDS_HUMAN
    assert high.requires_human_review is True
    assert high.reasons[-1].severity is Severity.CRITICAL
