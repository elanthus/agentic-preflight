"""Trusted merge-approval policy for an attested pull-request head."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import attestation, gitx, risk
from .config import load_config
from .models import RiskLevel

_DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def _flatten_reviews(value: object) -> list[Mapping[str, Any]]:
    """Accept one GitHub API page or ``gh api --slurp`` pages."""
    if not isinstance(value, list):
        raise ValueError("reviews JSON must be an array")
    flattened: list[Mapping[str, Any]] = []
    pending: list[object] = list(value)
    while pending:
        item = pending.pop(0)
        if isinstance(item, list):
            pending[0:0] = item
        elif isinstance(item, Mapping):
            flattened.append(item)
        else:
            raise ValueError("each review must be a JSON object")
    return flattened


def current_human_approvers(
    reviews: object,
    *,
    head_sha: str,
    pull_request_author: str,
) -> list[str]:
    """Return non-bot, non-author approvals for the exact current head.

    Only the latest decisive review from each person counts. Comments do not
    erase an approval, while a later dismissal or changes-requested review does.
    """
    latest: dict[str, tuple[int, str]] = {}
    for review in _flatten_reviews(reviews):
        user = review.get("user")
        if not isinstance(user, Mapping):
            continue
        login = user.get("login")
        user_type = user.get("type")
        state = str(review.get("state", "")).upper()
        if (
            not isinstance(login, str)
            or user_type != "User"
            or login.casefold() == pull_request_author.casefold()
            or review.get("commit_id") != head_sha
            or state not in _DECISIVE_REVIEW_STATES
        ):
            continue
        review_id = review.get("id")
        order = review_id if isinstance(review_id, int) else 0
        if login not in latest or order >= latest[login][0]:
            latest[login] = (order, state)
    return sorted(login for login, (_, state) in latest.items() if state == "APPROVED")


def evaluate(
    repo: Path | str,
    *,
    base_sha: str,
    head_sha: str,
    reviews: object,
    pull_request_author: str,
) -> dict[str, Any]:
    """Evaluate whether the exact PR head satisfies its merge-review policy."""
    repo = Path(repo)
    value = attestation.verify(repo, head_sha)
    cfg = load_config(repo)
    changed_files = gitx.changed_files(repo, base_sha, head_sha)
    path_assessment = risk.assess(
        changed_files,
        [],
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )
    severe_findings = sum(
        value.findings_summary.get(severity, 0) for severity in ("critical", "high")
    )
    requires_approval = path_assessment.level is RiskLevel.HIGH or severe_findings > 0
    approvers = current_human_approvers(
        reviews,
        head_sha=value.sha,
        pull_request_author=pull_request_author,
    )
    approved = not requires_approval or bool(approvers)
    return {
        "approved": approved,
        "requires_human_approval": requires_approval,
        "risk_level": "high" if requires_approval else path_assessment.level.value,
        "head_sha": value.sha,
        "base_sha": gitx.rev_parse(repo, base_sha),
        "changed_files": changed_files,
        "path_reasons": [reason.model_dump(mode="json") for reason in path_assessment.reasons],
        "severe_findings": severe_findings,
        "human_approvers": approvers,
    }
