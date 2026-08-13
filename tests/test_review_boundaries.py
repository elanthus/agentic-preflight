"""Focused contracts for the extracted review protocol and coverage boundary."""

from __future__ import annotations

import pytest

from agentic_preflight.diff import ReviewManifest, ReviewUnit
from agentic_preflight.errors import InvalidFindings
from agentic_preflight.models import FindingSubmission, Stage
from agentic_preflight.runs import review_coverage, review_protocol


def _manifest() -> ReviewManifest:
    return ReviewManifest(
        manifest="a" * 64,
        base_sha="1" * 40,
        head_sha="2" * 40,
        diff_sha256="b" * 64,
        units=(
            ReviewUnit(
                id="U0001",
                path="src/app.py",
                kind="hunk",
                digest="c" * 64,
                new_start=10,
                new_count=3,
            ),
            ReviewUnit(
                id="U0002",
                path="src/app.py",
                kind="hunk",
                digest="d" * 64,
                new_start=30,
                new_count=2,
            ),
        ),
        excluded_files=("uv.lock",),
    )


def test_coverage_validation_binds_a_line_and_accounts_for_clean_units():
    finding = FindingSubmission(
        path="src/app.py",
        line=11,
        severity="high",
        action="auto_fix",
        title="broken branch",
    )

    assigned, coverage = review_coverage.validate(
        [finding],
        manifest=_manifest(),
        submitted_manifest="a" * 64,
    )

    assert assigned[0].unit == "U0001"
    assert coverage.cited_units == ["U0001"]
    assert coverage.clean_units == ["U0002"]
    assert coverage.excluded_files == ["uv.lock"]


def test_coverage_validation_rejects_a_stale_protocol_manifest():
    with pytest.raises(InvalidFindings, match="does not match"):
        review_coverage.validate(
            [],
            manifest=_manifest(),
            submitted_manifest="0" * 64,
        )


def test_protocol_parsing_keeps_docs_and_review_payloads_distinct():
    findings, manifest = review_protocol.parse_submission(
        {"findings": []},
        stage=Stage.DOCS,
    )
    assert findings == []
    assert manifest is None

    with pytest.raises(InvalidFindings):
        review_protocol.parse_submission([], stage=Stage.REVIEW)
