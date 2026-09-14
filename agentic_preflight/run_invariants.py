"""Cross-field invariants for persisted run records.

These rules cover lifecycle relationships that the state-transition table and
Pydantic field validation cannot express independently. All candidate rules
were retained: the current coordinators construct each related record field in
the same transaction as its state transition. ``stale`` is deliberately not an
invariant because it can annotate many nonterminal states while directing the
caller to start again.
"""

from __future__ import annotations

from .machine import TERMINAL_STATES, State
from .models import RunDoc
from .store import StoreError


def violations(run: RunDoc) -> list[str]:
    """Return the stable names of every run-record rule that ``run`` violates."""
    failed: list[str] = []
    initial_setup_failure = run.setup_failure is not None and run.setup_failure.scope == "initial"

    if (
        initial_setup_failure
        and run.state is not State.SETUP_FAILED
        and run.state not in TERMINAL_STATES
    ):
        failed.append("initial_setup_failure_requires_setup_failed_state")
    if run.state is State.SETUP_FAILED and not initial_setup_failure:
        failed.append("setup_failed_requires_initial_setup_failure")
    if run.state is State.MERGEBACK_PENDING and run.mergeback_attempt is None:
        failed.append("mergeback_pending_requires_attempt")
    if run.state in {State.TEST_DELEGATED, State.PUBLICATION_READY} and run.test_delegation is None:
        failed.append("delegated_state_requires_test_delegation")
    if run.state is State.VERIFIED and run.test_delegation is not None:
        failed.append("verified_forbids_test_delegation")
    if run.state is State.AWAITING_PUSH_CONFIRM and run.gate_token is None:
        failed.append("awaiting_push_requires_gate_token")
    if run.state in {State.PUSHED, State.DONE} and run.pushed_sha is None:
        failed.append("pushed_state_requires_pushed_sha")
    if run.pushed_sha is not None and run.state not in {
        State.PUSHED,
        State.DONE,
        *TERMINAL_STATES,
    }:
        failed.append("pushed_sha_requires_pushed_or_terminal_state")
    if run.orphaned_reason is not None and run.state not in TERMINAL_STATES:
        failed.append("orphaned_reason_requires_terminal_state")
    if run.state is State.ORPHANED and run.orphaned_reason is None:
        failed.append("orphaned_requires_reason")
    if run.worktree_released and run.state not in TERMINAL_STATES:
        failed.append("released_worktree_requires_terminal_state")

    return failed


class RunInvariantViolation(StoreError):
    """A transaction produced a run record that is unsafe to persist."""

    def __init__(self, run_id: str, violations: list[str]) -> None:
        names = list(violations)
        super().__init__(f"run {run_id} violates record invariants: {', '.join(names)}")
        self.run_id = run_id
        self.violations = names
