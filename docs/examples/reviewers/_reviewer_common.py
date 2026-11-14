"""Deprecated pointer to ``agentic_preflight.reviewers.common``.

The shared reviewer helpers moved into the package unchanged. This module re-exports them
for one release. See docs/independent-review.md.
"""

from __future__ import annotations

from agentic_preflight.reviewers.common import (
    ACTIONS,
    SEVERITIES,
    ReviewerError,
    contract_prompt,
    emit_review_output,
    emit_submission,
    extract_findings,
    read_context,
    read_optional,
    report_error,
    reviewer_effort,
    reviewer_prompt,
    run_cli,
    timeout_seconds,
)

__all__ = [
    "ACTIONS",
    "SEVERITIES",
    "ReviewerError",
    "contract_prompt",
    "emit_review_output",
    "emit_submission",
    "extract_findings",
    "read_context",
    "read_optional",
    "report_error",
    "reviewer_effort",
    "reviewer_prompt",
    "run_cli",
    "timeout_seconds",
]
