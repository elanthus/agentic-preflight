import pytest
from pydantic import ValidationError

from agentic_preflight import attestation as attestationmod
from agentic_preflight.machine import State
from agentic_preflight.models import (
    Attestation,
    AttestedStage,
    Finding,
    FindingAction,
    FindingStatus,
    FindingSubmission,
    ReviewCoverage,
    RiskAssessment,
    RiskLevel,
    RunDoc,
    SetupFailure,
    Severity,
    Stage,
    StageRecord,
)
from tests.attestation_helpers import make_attestation


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


def _run_doc(**over):
    fields = {
        "schema_version": 2,
        "run_id": "r_abc123",
        "state": State.CREATED,
        "branch": "feature/x",
        "base_ref": "main",
        "merge_base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "source_head_sha": "b" * 40,
        "intent_source": "user",
        "source_worktree_id": "source-worktree",
        "source_worktree_path": "/repos/source",
        "config_snapshot": {"worktree": {"mode": "in_place"}},
        "config_digest": "c" * 64,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(over)
    return RunDoc(**fields)


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


def test_submission_rejects_agent_supplied_code_ownership():
    """Only the CLI may mark a mechanical requirement as code-owned."""
    with pytest.raises(ValidationError):
        FindingSubmission(**_submission(code_owned=True))


def test_finding_carries_the_code_assigned_identity():
    sub = FindingSubmission(**_submission())
    finding = Finding.from_submission(sub, id="F001", stage=Stage.REVIEW)
    assert finding.id == "F001"
    assert finding.stage is Stage.REVIEW
    assert finding.code_owned is False
    assert finding.status is FindingStatus.OPEN
    assert finding.fix_commit is None
    assert finding.title == sub.title


def test_run_doc_round_trips_through_json():
    run = _run_doc(
        intent="preserve the public behavior",
        changed_files=["src/auth.py"],
        risk=RiskAssessment(level=RiskLevel.HIGH),
        setup_failure=SetupFailure(
            scope="baseline",
            stage=Stage.LINT,
            command="uv sync",
            exit_code=7,
            worktree_path="/repos/demo-baseline",
            next_instruction="Retry the baseline setup.",
            next_command="agentic-preflight stage run lint --baseline",
        ),
    )
    restored = RunDoc.model_validate_json(run.model_dump_json())
    assert restored == run
    assert restored.seq == 0
    assert restored.fix_commits == []
    assert restored.intent == "preserve the public behavior"
    assert restored.changed_files == ["src/auth.py"]
    assert restored.risk is not None
    assert restored.risk.level is RiskLevel.HIGH
    assert restored.setup_failure is not None
    assert restored.setup_failure.stage is Stage.LINT


def test_run_doc_requires_schema_version():
    raw = _run_doc().model_dump(mode="json")
    raw.pop("schema_version")

    with pytest.raises(ValidationError, match="schema_version"):
        RunDoc.model_validate(raw)


@pytest.mark.parametrize(
    "field",
    [
        "source_head_sha",
        "source_worktree_id",
        "source_worktree_path",
        "config_snapshot",
        "config_digest",
        "created_at",
        "intent_source",
    ],
)
def test_run_doc_requires_creation_time_fields(field):
    raw = _run_doc().model_dump(mode="json")
    raw.pop(field)

    with pytest.raises(ValidationError, match=field):
        RunDoc.model_validate(raw)


def _attestation_stages():
    return {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor="in_harness",
            coverage=ReviewCoverage(
                manifest="d" * 64,
                head_sha="e" * 40,
                total_units=1,
                cited_units=[],
                clean_units=["U0001"],
            ),
        ),
        Stage.DOCS: AttestedStage(status="green"),
        Stage.TEST: AttestedStage(
            status="green", command="pytest", exit_code=0, output_sha256="a" * 64
        ),
        Stage.LINT: AttestedStage(
            status="green", command="ruff check .", exit_code=0, output_sha256="b" * 64
        ),
    }


def test_attestation_requires_a_complete_stage_set_and_shell_evidence():
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    attestation = Attestation(**payload)
    assert attestation.schema_version == 7
    assert attestation.outcome == "verified"
    assert attestation.stages[Stage.TEST].command == "pytest"

    payload["schema_version"] = 6
    with pytest.raises(ValidationError, match="schema_version"):
        Attestation(**payload)
    payload["schema_version"] = 7

    payload["stages"] = {**_attestation_stages(), Stage.TEST: AttestedStage(status="green")}
    with pytest.raises(ValidationError, match="lacks process evidence"):
        Attestation(**payload)


def test_attestation_allows_an_explicit_shell_stage_skip_without_fake_evidence():
    stages = _attestation_stages()
    stages[Stage.TEST] = AttestedStage(status="skipped", reason="docs-only change")
    value = make_attestation(stages=stages, branch="feature/docs")
    assert value.stages[Stage.TEST].output_sha256 is None


def test_attestation_rejects_a_skip_without_a_reason():
    stages = _attestation_stages()
    stages[Stage.TEST] = AttestedStage(status="skipped")
    with pytest.raises(ValidationError, match="lacks a reason"):
        make_attestation(stages=stages, branch="feature/docs")


def test_command_review_attestation_requires_process_evidence():
    stages = _attestation_stages()
    review = stages[Stage.REVIEW]
    stages[Stage.REVIEW] = review.model_copy(update={"executor": "command"})
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload["stages"] = {
        stage.value: value.model_dump(mode="json") for stage, value in stages.items()
    }
    with pytest.raises(ValidationError, match="command review lacks process evidence"):
        Attestation(**payload)

    stages[Stage.REVIEW] = review.model_copy(
        update={
            "executor": "command",
            "command": "reviewer --json",
            "exit_code": 0,
            "output_sha256": "f" * 64,
        }
    )
    payload["stages"]["review"] = stages[Stage.REVIEW].model_dump(mode="json")
    payload["evidence"]["review"]["origin"]["result"] = payload["stages"]["review"]
    payload["evidence"]["review"]["origin"]["fingerprint"]["executor"] = "command"
    payload["evidence"]["review"]["fingerprint"]["executor"] = "command"
    from agentic_preflight.digests import json_digest

    payload["evidence"]["review"]["origin_sha256"] = json_digest(
        payload["evidence"]["review"]["origin"]
    )
    assert Attestation(**payload).stages[Stage.REVIEW].executor == "command"


def test_attestation_requires_schema_version_and_explicit_outcome():
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        Attestation.model_validate(payload)

    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload.pop("outcome")
    with pytest.raises(ValidationError, match="outcome"):
        Attestation.model_validate(payload)


@pytest.mark.parametrize("schema_version", [5, 6])
def test_decode_rejects_old_attestation_notes(schema_version):
    payload = make_attestation(stages=_attestation_stages()).model_dump(mode="json")
    payload["schema_version"] = schema_version
    import json

    with pytest.raises(attestationmod.InvalidAttestation) as error:
        attestationmod.decode(json.dumps(payload))
    assert error.value.reason == "incompatible_schema"


def test_attestation_build_refuses_to_invent_a_green_review_stage():
    coverage = ReviewCoverage(
        manifest="d" * 64,
        head_sha="e" * 40,
        total_units=1,
        clean_units=["U0001"],
    )
    run = _run_doc(
        run_id="r_test",
        state=State.TEST_GREEN,
        merge_base_sha="c" * 40,
        head_sha="a" * 40,
        source_head_sha="a" * 40,
        config_digest="f" * 64,
        review_coverage=coverage,
        stages={
            Stage.LINT: StageRecord(
                status="green", command="ruff check .", exit_code=0, output_sha256="1" * 64
            ),
            Stage.TEST: StageRecord(
                status="green", command="pytest", exit_code=0, output_sha256="2" * 64
            ),
        },
    )

    with pytest.raises(attestationmod.InvalidAttestation, match="review stage"):
        attestationmod.build(
            run,
            sha="a" * 40,
            tree_sha="b" * 40,
            docs_enabled=False,
            findings_summary={},
        )
