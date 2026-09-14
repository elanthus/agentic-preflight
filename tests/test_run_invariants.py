from __future__ import annotations

from collections.abc import Callable

import pytest

from agentic_preflight.ci_models import CISection
from agentic_preflight.ci_models import TestDelegation as DelegationRecord
from agentic_preflight.machine import State
from agentic_preflight.models import MergebackAttempt, RunDoc, SetupFailure
from agentic_preflight.run_invariants import RunInvariantViolation, violations
from agentic_preflight.store import Store
from tests.conftest import make_run


def _initial_failure() -> SetupFailure:
    return SetupFailure(
        scope="initial",
        command="uv sync",
        exit_code=1,
        worktree_path="/repos/validation",
        next_instruction="Abort and start again.",
        next_command="agentic-preflight abort --force",
    )


def _mergeback_attempt() -> MergebackAttempt:
    return MergebackAttempt(
        source_sha="a" * 40,
        validation_sha="b" * 40,
        validation_tree="c" * 40,
    )


def _delegation() -> DelegationRecord:
    return DelegationRecord(
        policy_revision="a" * 40,
        policy=CISection(
            test_authority="github_actions",
            repository_id=1,
            workflow_id=2,
            check_app_id=3,
            required_jobs=["tests"],
        ),
    )


def _run_for(state: State) -> RunDoc:
    run = make_run()
    run.state = state
    if state is State.SETUP_FAILED:
        run.setup_failure = _initial_failure()
    if state is State.MERGEBACK_PENDING:
        run.mergeback_attempt = _mergeback_attempt()
    if state in {State.TEST_DELEGATED, State.PUBLICATION_READY}:
        run.test_delegation = _delegation()
    if state is State.AWAITING_PUSH_CONFIRM:
        run.gate_token = "gate-token"  # noqa: S105 - inert model fixture
    if state in {State.PUSHED, State.DONE}:
        run.pushed_sha = "d" * 40
    if state is State.ORPHANED:
        run.orphaned_reason = "source worktree disappeared"
    return run


def _set_initial_failure(run: RunDoc) -> None:
    run.setup_failure = _initial_failure()


def _set_delegation(run: RunDoc) -> None:
    run.test_delegation = _delegation()


def _set_pushed_sha(run: RunDoc) -> None:
    run.pushed_sha = "d" * 40


def _set_orphaned_reason(run: RunDoc) -> None:
    run.orphaned_reason = "source worktree disappeared"


def _release_worktree(run: RunDoc) -> None:
    run.worktree_released = True


@pytest.mark.parametrize(
    ("rule", "state", "mutate"),
    [
        ("initial_setup_failure_requires_setup_failed_state", State.CREATED, _set_initial_failure),
        ("setup_failed_requires_initial_setup_failure", State.SETUP_FAILED, lambda run: None),
        ("mergeback_pending_requires_attempt", State.MERGEBACK_PENDING, lambda run: None),
        ("delegated_state_requires_test_delegation", State.TEST_DELEGATED, lambda run: None),
        ("verified_forbids_test_delegation", State.VERIFIED, _set_delegation),
        ("awaiting_push_requires_gate_token", State.AWAITING_PUSH_CONFIRM, lambda run: None),
        ("pushed_state_requires_pushed_sha", State.PUSHED, lambda run: None),
        ("pushed_sha_requires_pushed_or_terminal_state", State.CREATED, _set_pushed_sha),
        ("orphaned_reason_requires_terminal_state", State.CREATED, _set_orphaned_reason),
        ("orphaned_requires_reason", State.ORPHANED, lambda run: None),
        ("released_worktree_requires_terminal_state", State.CREATED, _release_worktree),
    ],
)
def test_violation_is_named(rule: str, state: State, mutate: Callable[[RunDoc], None]):
    run = make_run()
    run.state = state
    mutate(run)

    assert violations(run) == [rule]


@pytest.mark.parametrize("state", list(State))
def test_consistent_record_for_every_state_has_no_violations(state: State):
    assert violations(_run_for(state)) == []


def test_transaction_rejects_violation_without_changing_run_file(tmp_path):
    store = Store(tmp_path)
    run = store.create_run(make_run())
    path = store.run_path(run.run_id)
    before = path.read_bytes()

    with pytest.raises(RunInvariantViolation) as exc, store.transaction(run.run_id) as changed:
        changed.worktree_released = True

    assert exc.value.run_id == run.run_id
    assert exc.value.violations == ["released_worktree_requires_terminal_state"]
    assert path.read_bytes() == before
