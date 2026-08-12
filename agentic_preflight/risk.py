"""Deterministic risk classification over paths and recorded findings.

Diff size is deliberately absent. ``[diff] max_bytes`` is an attention budget:
it decides whether the complete diff fits in review context. Risk instead comes
from repository-owned path policy and the findings the review recorded.
"""

from __future__ import annotations

from typing import Literal

from . import findings as findingsmod
from .config import PolicySection
from .diff import path_matches
from .models import (
    Finding,
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

    configured_blocking = {
        Stage.REVIEW: review_blocking_severities,
        Stage.DOCS: docs_blocking_severities,
    }
    has_open_blocker = any(
        findingsmod.blocking(
            [finding for finding in findings if finding.stage is stage],
            blocking_severities=blocking_severities,
        )
        for stage, blocking_severities in configured_blocking.items()
    )

    level = max(
        (reason.level for reason in reasons),
        key=lambda candidate: _RANK[candidate],
        default=RiskLevel.LOW,
    )
    requires_human = level is RiskLevel.HIGH
    verdict = (
        Verdict.CHANGES_REQUIRED
        if has_open_blocker
        else Verdict.NEEDS_HUMAN
        if requires_human
        else Verdict.CLEAR
    )
    return RiskAssessment(
        level=level,
        verdict=verdict,
        requires_human_review=requires_human,
        reasons=reasons,
    )


def include_attested_findings(
    assessment: RiskAssessment, findings_summary: dict[str, int]
) -> RiskAssessment:
    """Preserve finding-derived risk when importing portable green evidence."""
    severity = next(
        (
            severity
            for severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
            if findings_summary.get(severity.value, 0) > 0
        ),
        None,
    )
    if severity is None:
        return assessment
    imported_level = _FINDING_RISK[severity]
    if _RANK[assessment.level] >= _RANK[imported_level]:
        return assessment
    requires_human = imported_level is RiskLevel.HIGH
    return RiskAssessment(
        level=imported_level,
        verdict=Verdict.NEEDS_HUMAN if requires_human else Verdict.CLEAR,
        requires_human_review=requires_human,
        reasons=[
            *assessment.reasons,
            RiskReason(kind="finding", level=imported_level, severity=severity),
        ],
    )
