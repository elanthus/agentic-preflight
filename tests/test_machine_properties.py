"""Properties of the state machine itself.

The graph-reachability tests here are stronger than random walking: rather than
hoping a fuzzer stumbles onto a stage-skipping path, they enumerate *every* path
and prove none exists.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_preflight.machine import (
    TRANSITIONS,
    Action,
    IllegalTransition,
    State,
    legal_actions,
    next_state,
)


def _reachable_paths(start: State, goal: State, *, avoid: set[State]) -> bool:
    """Is ``goal`` reachable from ``start`` without passing through ``avoid``?"""
    seen = {start}
    frontier = [start]
    while frontier:
        state = frontier.pop()
        if state is goal:
            return True
        for action in legal_actions(state):
            nxt = next_state(state, action)
            if nxt in avoid or nxt in seen:
                continue
            seen.add(nxt)
            frontier.append(nxt)
    return False


# -- the anti-skip guarantee, proved by exhaustion --------------------------


def test_pushed_is_unreachable_without_review_green():
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.REVIEW_GREEN})


def test_pushed_is_unreachable_without_fresh_remote_sync():
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.SYNC_GREEN})


def test_pushed_is_unreachable_without_docs_green():
    """Even when the docs stage is disabled, DOCS_GREEN is passed through via
    the explicit SKIP_DOCS transition — skipped, never bypassed."""
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.DOCS_GREEN})


def test_pushed_is_unreachable_without_lint_green():
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.LINT_GREEN})


def test_pushed_is_unreachable_without_test_green():
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.TEST_GREEN})


def test_pushed_is_unreachable_without_verified():
    assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={State.VERIFIED})


def test_pushed_is_unreachable_without_the_gate():
    assert not _reachable_paths(
        State.CREATED, State.PUSHED, avoid={State.AWAITING_PUSH_CONFIRM}
    )


def test_pushed_is_reachable_at_all():
    """The negative tests above would pass trivially on a disconnected graph."""
    assert _reachable_paths(State.CREATED, State.PUSHED, avoid=set())


def test_every_stage_gate_is_individually_load_bearing():
    """No single stage can be removed and still leave PUSHED reachable."""
    for gate in (
        State.SYNC_GREEN,
        State.REVIEW_GREEN,
        State.DOCS_GREEN,
        State.LINT_GREEN,
        State.TEST_GREEN,
        State.VERIFIED,
    ):
        assert not _reachable_paths(State.CREATED, State.PUSHED, avoid={gate}), (
            f"{gate.name} can be bypassed"
        )


# -- structural sanity ------------------------------------------------------


def test_terminal_states_have_no_outgoing_transitions():
    for state in (State.DONE, State.ABORTED, State.ORPHANED):
        assert legal_actions(state) == []


def test_every_non_terminal_state_can_be_aborted():
    """A run must never be able to wedge with no way out."""
    for state in State:
        if state in (State.DONE, State.ABORTED, State.ORPHANED):
            continue
        assert Action.ABORT in legal_actions(state)


def test_every_state_except_created_is_reachable():
    seen = {State.CREATED}
    frontier = [State.CREATED]
    while frontier:
        state = frontier.pop()
        for action in legal_actions(state):
            nxt = next_state(state, action)
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    unreachable = set(State) - seen
    assert unreachable == set(), f"unreachable states: {[s.name for s in unreachable]}"


def test_the_transition_table_is_deterministic():
    """One target per (state, action) — the property the tests above rely on."""
    assert len(TRANSITIONS) == len(set(TRANSITIONS.keys()))


# -- random walks -----------------------------------------------------------


@settings(max_examples=300)
@given(st.lists(st.sampled_from(list(Action)), max_size=40))
def test_random_action_sequences_never_raise_anything_unexpected(actions):
    """The only exception a caller should ever see is IllegalTransition."""
    state = State.CREATED
    for action in actions:
        try:
            state = next_state(state, action)
        except IllegalTransition:
            continue
        assert isinstance(state, State)


@settings(max_examples=300)
@given(st.lists(st.sampled_from(list(Action)), max_size=40))
def test_a_random_walk_never_reaches_pushed_without_the_full_chain(actions):
    state = State.CREATED
    visited = {state}
    for action in actions:
        try:
            state = next_state(state, action)
        except IllegalTransition:
            continue
        visited.add(state)

    if State.PUSHED in visited:
        for required in (
            State.REVIEW_GREEN,
            State.DOCS_GREEN,
            State.LINT_GREEN,
            State.TEST_GREEN,
            State.VERIFIED,
        ):
            assert required in visited, f"reached PUSHED without {required.name}"
