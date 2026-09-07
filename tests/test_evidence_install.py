"""Imported results must traverse the machine, including skip transitions."""

import pytest

from agentic_preflight.errors import WrongState
from agentic_preflight.machine import State
from agentic_preflight.models import AttestedStage, RunDoc, Stage
from agentic_preflight.runs._evidence_install import install_stage, stage_record


def _run(state):
    return RunDoc(
        run_id="import-test",
        state=state,
        branch="feature",
        base_ref="main",
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
    )


@pytest.mark.parametrize(
    ("stage", "state", "destination"),
    [
        (Stage.DOCS, State.REVIEW_GREEN, State.DOCS_GREEN),
        (Stage.DOCS, State.DOCS_AWAITING_FINDINGS, State.DOCS_GREEN),
        (Stage.TEST, State.LINT_GREEN, State.TEST_GREEN),
    ],
)
@pytest.mark.parametrize("status", ["green", "skipped"])
def test_import_handles_green_and_skipped_results(stage, state, destination, status):
    run = _run(state)
    record = stage_record(
        AttestedStage(status=status, reason="imported result"),
        finished_at="2026-09-06T00:00:00+00:00",
        head_sha=run.head_sha,
    )
    if state is State.DOCS_AWAITING_FINDINGS and status == "skipped":
        with pytest.raises(WrongState):
            install_stage(run, stage, record)
        assert run.state is state
        assert stage not in run.stages
        return
    install_stage(run, stage, record)
    assert run.state is destination
    assert run.stages[stage] == record
    assert run.stages[stage].reason == "imported result"


def test_import_cannot_bypass_an_unfinished_preceding_stage():
    run = _run(State.REVIEW_AWAITING_FINDINGS)
    record = stage_record(AttestedStage(status="green"), finished_at=None, head_sha=run.head_sha)
    with pytest.raises(WrongState):
        install_stage(run, Stage.LINT, record)
    assert run.state is State.REVIEW_AWAITING_FINDINGS
    assert Stage.LINT not in run.stages
