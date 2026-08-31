import pytest

from agentic_preflight.machine import (
    STATE_DESCRIPTIONS,
    Action,
    IllegalTransition,
    State,
    legal_actions,
    next_state,
    recovery_hint,
)


def test_start_moves_created_to_worktree_ready():
    assert next_state(State.CREATED, Action.CREATE_WORKTREE) == State.WORKTREE_READY


def test_sync_is_load_bearing_before_review():
    assert next_state(State.WORKTREE_READY, Action.BEGIN_SYNC) == State.SYNC_RUNNING
    assert next_state(State.SYNC_RUNNING, Action.SYNC_PASSED) == State.SYNC_GREEN
    assert next_state(State.SYNC_GREEN, Action.BEGIN_REVIEW) == State.REVIEW_AWAITING_FINDINGS


def test_local_checks_run_review_then_docs_then_lint_then_test():
    assert next_state(State.REVIEW_GREEN, Action.BEGIN_DOCS) == State.DOCS_AWAITING_FINDINGS
    assert next_state(State.DOCS_GREEN, Action.RUN_LINT) == State.LINT_RUNNING
    assert next_state(State.LINT_GREEN, Action.RUN_TEST) == State.TEST_RUNNING
    assert next_state(State.LINT_GREEN, Action.SKIP_TEST) == State.TEST_GREEN
    assert next_state(State.TEST_GREEN, Action.BEGIN_MERGEBACK) == State.MERGEBACK_PENDING


def test_committed_stage_repairs_restart_with_fresh_review_coverage():
    assert next_state(State.LINT_RED, Action.LINT_FIX_RESTART) == State.REVIEW_AWAITING_FINDINGS
    assert next_state(State.TEST_RED, Action.TEST_FIX_RESTART) == State.REVIEW_AWAITING_FINDINGS


def test_non_equivalent_mergeback_resolution_can_restart_review():
    assert (
        next_state(State.MERGEBACK_CONFLICT, Action.INVALIDATE_REVIEW)
        == State.REVIEW_AWAITING_FINDINGS
    )
    assert (
        next_state(State.MERGEBACK_PENDING, Action.INVALIDATE_REVIEW)
        == State.REVIEW_AWAITING_FINDINGS
    )


def test_findings_submission_goes_directly_to_green_or_blocked():
    assert next_state(State.REVIEW_AWAITING_FINDINGS, Action.SUBMIT_CLEAN) == State.REVIEW_GREEN
    assert (
        next_state(State.REVIEW_AWAITING_FINDINGS, Action.SUBMIT_BLOCKING) == State.REVIEW_BLOCKED
    )
    assert next_state(State.REVIEW_BLOCKED, Action.RESPOND) == State.REVIEW_BLOCKED
    assert next_state(State.REVIEW_BLOCKED, Action.RESOLVE_GREEN) == State.REVIEW_GREEN
    assert next_state(State.REVIEW_GREEN, Action.RESPOND) == State.REVIEW_GREEN
    assert next_state(State.DOCS_GREEN, Action.RESPOND) == State.DOCS_GREEN


def test_command_review_has_explicit_running_failure_and_retry_states():
    assert (
        next_state(State.REVIEW_AWAITING_FINDINGS, Action.RUN_REVIEW_COMMAND)
        == State.REVIEW_COMMAND_RUNNING
    )
    assert (
        next_state(State.REVIEW_COMMAND_RUNNING, Action.REVIEW_COMMAND_FAILED)
        == State.REVIEW_COMMAND_RED
    )
    assert (
        next_state(State.REVIEW_COMMAND_RED, Action.RETRY_REVIEW_COMMAND)
        == State.REVIEW_COMMAND_RUNNING
    )
    assert (
        next_state(State.REVIEW_COMMAND_RUNNING, Action.REVIEW_COMMAND_PASSED)
        == State.REVIEW_AWAITING_FINDINGS
    )


def test_illegal_transition_raises_with_the_legal_actions_named():
    with pytest.raises(IllegalTransition) as exc:
        next_state(State.CREATED, Action.SUBMIT_CLEAN)
    assert "CREATED" in str(exc.value)
    assert "CREATE_WORKTREE" in str(exc.value)


def test_transition_table_and_recovery_hints_share_one_description():
    for state, description in STATE_DESCRIPTIONS.items():
        assert recovery_hint(state) is description
        for action, target in description.transitions.items():
            assert next_state(state, action) is target


def test_judgment_cycles_generate_equivalent_recovery_commands():
    assert recovery_hint(State.REVIEW_AWAITING_FINDINGS).command == (
        recovery_hint(State.DOCS_AWAITING_FINDINGS).command
    )
    assert recovery_hint(State.REVIEW_BLOCKED).command == recovery_hint(State.DOCS_BLOCKED).command


def test_every_non_terminal_state_has_a_declarative_recovery_command():
    for state in State:
        if state not in {State.DONE, State.ABORTED, State.ORPHANED}:
            assert recovery_hint(state).command


def test_stage_skipping_is_not_expressible():
    """No action from a review state may reach a lint/test/push state.

    This is the structural anti-skip guarantee: it is enforced by the absence
    of table entries, not by prose in SKILL.md.
    """
    review_states = [s for s in State if s.name.startswith("REVIEW_")]
    forbidden = {State.LINT_RUNNING, State.TEST_RUNNING, State.PUSHED}
    for state in review_states:
        if state is State.REVIEW_GREEN:
            continue
        for action in legal_actions(state):
            assert next_state(state, action) not in forbidden
