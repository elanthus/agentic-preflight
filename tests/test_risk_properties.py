"""Monotonicity properties of :func:`agentic_preflight.risk.assess`.

Risk is the maximum over path rules and recorded findings, and the verdict escalates
from ``clear`` through ``needs_human`` to ``changes_required``. A branch therefore
cannot lower its risk by touching another file or by recording another finding: if it
could, an agent could dilute a high-risk change into a low-risk one.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_preflight.config import PolicySection, ReviewSection
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

_LEVEL = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
_VERDICT = {Verdict.CLEAR: 0, Verdict.NEEDS_HUMAN: 1, Verdict.CHANGES_REQUIRED: 2}

_SEGMENTS = ["src", "docs", "auth", ".github", "app.py", "README.md", "uv.lock"]
paths = st.lists(st.sampled_from(_SEGMENTS), min_size=1, max_size=3).map("/".join)
patterns = st.sampled_from(
    ["src/**", "auth/", "**/*.py", "*.md", ".github/**", "uv.lock", "docs/*", "src/auth/**"]
)

policies = st.builds(
    PolicySection,
    human_review_paths=st.lists(patterns, max_size=2),
    high_risk_paths=st.lists(patterns, max_size=2),
    medium_risk_paths=st.lists(patterns, max_size=2),
)
reviews = st.builds(
    ReviewSection,
    blocking_severities=st.lists(
        st.sampled_from(["critical", "high", "medium", "low"]), unique=True, max_size=4
    ),
    trusted_paths=st.lists(patterns, max_size=1),
    command=st.none() | st.sampled_from(["python3 tools/review.py", "reviewer --strict"]),
)
docs_blocking = st.lists(
    st.sampled_from(["critical", "high", "medium", "low"]), unique=True, max_size=4
)


@st.composite
def findings(draw, count: int | None = None) -> list[Finding]:
    size = count if count is not None else draw(st.integers(min_value=0, max_value=4))
    return [
        Finding(
            id=f"F{index:03d}",
            stage=draw(st.sampled_from([Stage.REVIEW, Stage.DOCS])),
            status=draw(st.sampled_from(list(FindingStatus))),
            path=draw(paths),
            severity=draw(st.sampled_from(list(Severity))),
            action=draw(st.sampled_from(list(FindingAction))),
            title="finding",
        )
        for index in range(size)
    ]


def _assert_not_lower(before, after) -> None:
    assert _LEVEL[after.level] >= _LEVEL[before.level]
    assert _VERDICT[after.verdict] >= _VERDICT[before.verdict]
    assert after.requires_human_review or not before.requires_human_review
    # Every earlier reason is still reported; adding input only adds explanations.
    assert all(reason in after.reasons for reason in before.reasons)


@settings(max_examples=200)
@given(
    changed=st.lists(paths, max_size=4),
    extra=paths,
    recorded=findings(),
    policy=policies,
    review=reviews,
    docs=docs_blocking,
)
def test_another_changed_path_never_lowers_risk(changed, extra, recorded, policy, review, docs):
    kwargs = {"policy": policy, "review": review, "docs_blocking_severities": docs}
    before = assess(changed, recorded, **kwargs)
    after = assess([*changed, extra], recorded, **kwargs)
    _assert_not_lower(before, after)


@settings(max_examples=200)
@given(
    changed=st.lists(paths, max_size=4),
    recorded=findings(count=5),
    keep=st.integers(min_value=0, max_value=4),
    policy=policies,
    review=reviews,
    docs=docs_blocking,
)
def test_another_finding_never_lowers_risk(changed, recorded, keep, policy, review, docs):
    """``recorded[keep]`` is the added finding; ids stay unique."""
    kwargs = {"policy": policy, "review": review, "docs_blocking_severities": docs}
    before = assess(changed, recorded[:keep], **kwargs)
    after = assess(changed, recorded[: keep + 1], **kwargs)
    _assert_not_lower(before, after)


@settings(max_examples=150)
@given(
    changed=st.lists(paths, max_size=4),
    recorded=findings(),
    policy=policies,
    review=reviews,
    docs=docs_blocking,
    summary=st.dictionaries(
        st.sampled_from([severity.value for severity in Severity]),
        st.integers(min_value=0, max_value=3),
        max_size=4,
    ),
)
def test_importing_attested_findings_never_lowers_risk(
    changed, recorded, policy, review, docs, summary
):
    before = assess(changed, recorded, policy=policy, review=review, docs_blocking_severities=docs)
    after = include_attested_findings(before, summary)
    assert _LEVEL[after.level] >= _LEVEL[before.level]
    assert after.requires_human_review or not before.requires_human_review
    assert all(reason in after.reasons for reason in before.reasons)


@settings(max_examples=100)
@given(changed=st.lists(paths, max_size=5), recorded=findings(), review=reviews, docs=docs_blocking)
def test_risk_ignores_the_order_of_changed_paths_and_findings(changed, recorded, review, docs):
    policy = PolicySection(
        human_review_paths=[".github/**"], high_risk_paths=["auth/"], medium_risk_paths=["*.md"]
    )
    kwargs = {"policy": policy, "review": review, "docs_blocking_severities": docs}
    forward = assess(changed, recorded, **kwargs)
    backward = assess(list(reversed(changed)), list(reversed(recorded)), **kwargs)
    assert forward == backward


@pytest.mark.xfail(
    strict=True,
    reason=(
        "include_attested_findings rebuilds the verdict from the imported level alone, "
        "so an open local blocker's changes_required becomes needs_human. Its one caller, "
        "gate, runs only in a verified state, where no blocker should be open."
    ),
)
def test_importing_attested_findings_keeps_an_open_blocker():
    blocker = Finding(
        id="F001",
        stage=Stage.REVIEW,
        path="src/app.py",
        severity=Severity.MEDIUM,
        action=FindingAction.AUTO_FIX,
        title="finding",
    )
    before = assess(
        ["src/app.py"],
        [blocker],
        policy=PolicySection(),
        review=ReviewSection(blocking_severities=["medium"]),
        docs_blocking_severities=[],
    )
    assert before.verdict is Verdict.CHANGES_REQUIRED

    after = include_attested_findings(before, {"high": 1})

    assert after.verdict is Verdict.CHANGES_REQUIRED
