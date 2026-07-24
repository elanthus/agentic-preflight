import pytest

from agentic_preflight.machine import (
    Action,
    IllegalTransition,
    State,
    legal_actions,
    next_state,
)


def test_start_moves_created_to_worktree_ready():
    assert next_state(State.CREATED, Action.CREATE_WORKTREE) == State.WORKTREE_READY


def test_sync_is_load_bearing_before_review():
    assert next_state(State.WORKTREE_READY, Action.BEGIN_SYNC) == State.SYNC_RUNNING
    assert next_state(State.SYNC_RUNNING, Action.SYNC_PASSED) == State.SYNC_GREEN
    assert next_state(State.SYNC_GREEN, Action.BEGIN_REVIEW) == State.REVIEW_AWAITING_FINDINGS


def test_local_checks_run_review_then_test_then_docs_then_lint():
    assert next_state(State.REVIEW_GREEN, Action.RUN_TEST) == State.TEST_RUNNING
    assert next_state(State.TEST_GREEN, Action.BEGIN_DOCS) == State.DOCS_AWAITING_FINDINGS
    assert next_state(State.DOCS_GREEN, Action.RUN_LINT) == State.LINT_RUNNING
    assert next_state(State.LINT_GREEN, Action.BEGIN_MERGEBACK) == State.MERGEBACK_PENDING


def test_illegal_transition_raises_with_the_legal_actions_named():
    with pytest.raises(IllegalTransition) as exc:
        next_state(State.CREATED, Action.SUBMIT_FINDINGS)
    assert "CREATED" in str(exc.value)
    assert "CREATE_WORKTREE" in str(exc.value)


def test_stage_skipping_is_not_expressible():
    """No action from a review state may reach a lint/test/push state.

    This is the structural anti-skip guarantee: it is enforced by the absence
    of table entries, not by prose in SKILL.md.
    """
    review_states = [s for s in State if s.name.startswith("REVIEW_")]
    forbidden = {State.LINT_RUNNING, State.TEST_RUNNING, State.PUSHED, State.PR_OPEN}
    for state in review_states:
        if state is State.REVIEW_GREEN:
            continue
        for action in legal_actions(state):
            assert next_state(state, action) not in forbidden
