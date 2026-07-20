import pytest
from pydantic import ValidationError

from agentic_cli.machine import State
from agentic_cli.models import (
    Finding,
    FindingAction,
    FindingStatus,
    FindingSubmission,
    RunDoc,
    Severity,
    Stage,
)


def _submission(**over):
    base = dict(
        path="src/auth.py",
        line=12,
        severity="high",
        action="auto_fix",
        title="Password compared with ==",
        detail="Use a constant-time comparison.",
    )
    base.update(over)
    return base


def test_submission_accepts_the_fields_the_agent_owns():
    sub = FindingSubmission(**_submission())
    assert sub.severity is Severity.HIGH
    assert sub.action is FindingAction.AUTO_FIX


def test_submission_rejects_an_agent_supplied_id():
    """A hallucinated ID must be a hard validation error, not silently honoured."""
    with pytest.raises(ValidationError) as exc:
        FindingSubmission(**_submission(id="F001"))
    assert "id" in str(exc.value)


def test_submission_rejects_an_agent_supplied_stage():
    """Stage is derived from the active state at submission time, never sent."""
    with pytest.raises(ValidationError):
        FindingSubmission(**_submission(stage="docs"))


def test_finding_carries_the_code_assigned_identity():
    sub = FindingSubmission(**_submission())
    finding = Finding.from_submission(sub, id="F001", stage=Stage.REVIEW)
    assert finding.id == "F001"
    assert finding.stage is Stage.REVIEW
    assert finding.status is FindingStatus.OPEN
    assert finding.fix_commit is None
    assert finding.title == sub.title


def test_run_doc_round_trips_through_json():
    run = RunDoc(
        run_id="r_abc123",
        state=State.CREATED,
        branch="feature/x",
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
    )
    restored = RunDoc.model_validate_json(run.model_dump_json())
    assert restored == run
    assert restored.seq == 0
    assert restored.fix_commits == []
