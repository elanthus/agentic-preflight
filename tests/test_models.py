import pytest
from pydantic import ValidationError

from agentic_preflight.machine import State
from agentic_preflight.models import (
    Attestation,
    AttestedStage,
    Finding,
    FindingAction,
    FindingStatus,
    FindingSubmission,
    RunDoc,
    Severity,
    Stage,
)


def _submission(**over):
    base = {
        "path": "src/auth.py",
        "line": 12,
        "severity": "high",
        "action": "auto_fix",
        "title": "Password compared with ==",
        "detail": "Use a constant-time comparison.",
    }
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
        source_head_sha="b" * 40,
        intent="preserve the public behavior",
    )
    restored = RunDoc.model_validate_json(run.model_dump_json())
    assert restored == run
    assert restored.seq == 0
    assert restored.fix_commits == []
    assert restored.intent == "preserve the public behavior"


def _attestation_stages():
    return {
        Stage.REVIEW: AttestedStage(status="green"),
        Stage.DOCS: AttestedStage(status="green"),
        Stage.TEST: AttestedStage(
            status="green", command="pytest", exit_code=0, output_sha256="a" * 64
        ),
        Stage.LINT: AttestedStage(
            status="green", command="ruff check .", exit_code=0, output_sha256="b" * 64
        ),
    }


def test_attestation_requires_a_complete_stage_set_and_shell_evidence():
    payload = {
        "sha": "a" * 40,
        "tree_sha": "b" * 40,
        "branch": "feature/x",
        "base_ref": "main",
        "merge_base_sha": "c" * 40,
        "run_id": "r_test",
        "green_at": "2026-01-01T00:00:00+00:00",
        "stages": _attestation_stages(),
    }
    assert Attestation(**payload).stages[Stage.TEST].command == "pytest"

    payload["stages"] = {**_attestation_stages(), Stage.TEST: AttestedStage(status="green")}
    with pytest.raises(ValidationError, match="lacks process evidence"):
        Attestation(**payload)


def test_attestation_allows_an_explicit_shell_stage_skip_without_fake_evidence():
    stages = _attestation_stages()
    stages[Stage.TEST] = AttestedStage(status="skipped", reason="docs-only change")
    value = Attestation(
        sha="a" * 40,
        tree_sha="b" * 40,
        branch="feature/docs",
        base_ref="main",
        merge_base_sha="c" * 40,
        run_id="r_test",
        green_at="2026-01-01T00:00:00+00:00",
        stages=stages,
    )
    assert value.stages[Stage.TEST].output_sha256 is None


def test_attestation_rejects_a_skip_without_a_reason():
    stages = _attestation_stages()
    stages[Stage.TEST] = AttestedStage(status="skipped")
    with pytest.raises(ValidationError, match="lacks a reason"):
        Attestation(
            sha="a" * 40,
            tree_sha="b" * 40,
            branch="feature/docs",
            base_ref="main",
            merge_base_sha="c" * 40,
            run_id="r_test",
            green_at="2026-01-01T00:00:00+00:00",
            stages=stages,
        )
