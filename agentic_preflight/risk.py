"""Deterministic risk classification over paths and recorded findings.

Diff size is deliberately absent. ``[diff] max_bytes`` is an attention budget:
it decides whether the complete diff fits in review context. Risk instead comes
from repository-owned path policy and the findings the review recorded.
"""

from __future__ import annotations

from typing import Literal

from .config import PolicySection
from .diff import path_matches
from .models import (
    Finding,
    FindingAction,
    FindingStatus,
    RiskAssessment,
    RiskLevel,
    RiskReason,
    Severity,
    Stage,
    Verdict,
)

RiskReasonKind = Literal["human_review_path", "high_risk_path", "medium_risk_path", "finding"]

_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_FINDING_RISK = {
    Severity.LOW: RiskLevel.LOW,
    Severity.MEDIUM: RiskLevel.MEDIUM,
    Severity.HIGH: RiskLevel.HIGH,
    Severity.CRITICAL: RiskLevel.HIGH,
}


def _path_reasons(paths: list[str], patterns: list[str], *, kind: RiskReasonKind, level: RiskLevel):
    for path in sorted(set(paths)):
        for pattern in patterns:
            if path_matches(path, pattern):
                yield RiskReason(kind=kind, level=level, path=path, pattern=pattern)


def assess(
    changed_files: list[str],
    findings: list[Finding],
    *,
    policy: PolicySection,
    review_blocking_severities: list[str],
    docs_blocking_severities: list[str],
) -> RiskAssessment:
    """Return the explainable policy verdict for one exact reviewed change."""
    reasons = [
        *_path_reasons(
            changed_files,
            policy.human_review_paths,
            kind="human_review_path",
            level=RiskLevel.HIGH,
        ),
        *_path_reasons(
            changed_files,
            policy.high_risk_paths,
            kind="high_risk_path",
            level=RiskLevel.HIGH,
        ),
        *_path_reasons(
            changed_files,
            policy.medium_risk_paths,
            kind="medium_risk_path",
            level=RiskLevel.MEDIUM,
        ),
    ]

    for finding in sorted(findings, key=lambda item: item.id):
        level = _FINDING_RISK[finding.severity]
        if level is RiskLevel.LOW:
            continue
        reasons.append(
            RiskReason(
                kind="finding",
                level=level,
                path=finding.path,
                finding_id=finding.id,
                severity=finding.severity,
            )
        )

    requires_human = any(reason.kind == "human_review_path" for reason in reasons)
    configured_blocking = {
        Stage.REVIEW: {Severity(value) for value in review_blocking_severities},
        Stage.DOCS: {Severity(value) for value in docs_blocking_severities},
    }
    has_open_blocker = any(
        finding.status is FindingStatus.OPEN
        and (
            finding.severity in configured_blocking[finding.stage]
            or finding.action is FindingAction.ASK_USER
        )
        for finding in findings
    )

    level = max(
        (reason.level for reason in reasons),
        key=lambda candidate: _RANK[candidate],
        default=RiskLevel.LOW,
    )
    verdict = (
        Verdict.NEEDS_HUMAN
        if requires_human
        else Verdict.CHANGES_REQUIRED
        if has_open_blocker
        else Verdict.CLEAR
    )
    return RiskAssessment(
        level=level,
        verdict=verdict,
        requires_human_review=requires_human,
        reasons=reasons,
    )
