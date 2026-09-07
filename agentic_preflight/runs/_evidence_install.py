"""Shared mechanics for already-validated successful stage evidence.

Callers own eligibility, provenance, coverage, findings and risk. In particular,
legacy exact reuse must not acquire refresh metadata through these helpers.
"""

from ..machine import Action, State
from ..models import AttestedStage, RunDoc, Stage, StageRecord
from ._session import _apply


def stage_record(result: AttestedStage, *, finished_at: str | None, head_sha: str) -> StageRecord:
    """Reconstruct the result fields common to exact and fingerprint reuse."""
    return StageRecord(
        status=result.status,
        command=result.command,
        reason=result.reason,
        exit_code=result.exit_code,
        output_sha256=result.output_sha256,
        finished_at=finished_at,
        head_sha=head_sha,
    )


def install_stage(doc: RunDoc, stage: Stage, record: StageRecord) -> None:
    """Install a result and replay its load-bearing stage transitions.

    Synchronization and mergeback remain the caller's responsibility. Refresh
    may reach docs either before or after its context has been delivered.
    """
    if stage is Stage.REVIEW:
        _apply(doc, Action.SUBMIT_CLEAN)
    elif stage is Stage.DOCS:
        if record.status == "skipped":
            _apply(doc, Action.SKIP_DOCS)
        else:
            if doc.state is State.REVIEW_GREEN:
                _apply(doc, Action.BEGIN_DOCS)
            _apply(doc, Action.SUBMIT_CLEAN)
    elif stage is Stage.LINT:
        _apply(doc, Action.RUN_LINT)
        _apply(doc, Action.LINT_PASSED)
    elif record.status == "skipped":
        _apply(doc, Action.SKIP_TEST)
    else:
        _apply(doc, Action.RUN_TEST)
        _apply(doc, Action.TEST_PASSED)
    doc.stages[stage] = record
