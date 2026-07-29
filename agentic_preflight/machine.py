"""The state machine: state enum, transition table, and legality queries.

Every other module consumes this. The anti-skip guarantee of the whole tool
rests on a single property of the table below: there is no entry that carries a
run from a review/docs state into a lint, test, or push state. Stage-skipping is
not forbidden by prose, it is *unrepresentable*.

Transitions are a pure function: ``(State, Action) -> State``, exactly one
target per pair. Where the design sketches a conditional branch (a submitted
review goes to ``AWAITING_RESPONSES`` when blocking findings exist and to
``GREEN`` when it is clean) the outcome is folded into the *action* rather than
resolved inside the transition. Code chooses which action to fire; the table
stays deterministic and therefore property-testable.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    CREATED = "CREATED"
    WORKTREE_READY = "WORKTREE_READY"
    SYNC_RUNNING = "SYNC_RUNNING"
    SYNC_CONFLICT = "SYNC_CONFLICT"
    SYNC_GREEN = "SYNC_GREEN"

    REVIEW_AWAITING_FINDINGS = "REVIEW_AWAITING_FINDINGS"
    REVIEW_SUBMITTED = "REVIEW_SUBMITTED"
    REVIEW_AWAITING_RESPONSES = "REVIEW_AWAITING_RESPONSES"
    REVIEW_FIXING = "REVIEW_FIXING"
    REVIEW_GREEN = "REVIEW_GREEN"

    DOCS_AWAITING_FINDINGS = "DOCS_AWAITING_FINDINGS"
    DOCS_SUBMITTED = "DOCS_SUBMITTED"
    DOCS_AWAITING_RESPONSES = "DOCS_AWAITING_RESPONSES"
    DOCS_FIXING = "DOCS_FIXING"
    DOCS_GREEN = "DOCS_GREEN"

    LINT_RUNNING = "LINT_RUNNING"
    LINT_GREEN = "LINT_GREEN"
    LINT_RED = "LINT_RED"

    TEST_RUNNING = "TEST_RUNNING"
    TEST_GREEN = "TEST_GREEN"
    TEST_RED = "TEST_RED"

    MERGEBACK_PENDING = "MERGEBACK_PENDING"
    MERGEBACK_CONFLICT = "MERGEBACK_CONFLICT"
    VERIFIED = "VERIFIED"
    AWAITING_PUSH_CONFIRM = "AWAITING_PUSH_CONFIRM"
    PUSHED = "PUSHED"
    PR_OPEN = "PR_OPEN"
    CI_MONITORING = "CI_MONITORING"
    CI_FAILED = "CI_FAILED"
    CHECKS_PASSED = "CHECKS_PASSED"
    CI_TIMED_OUT = "CI_TIMED_OUT"
    PR_MERGED = "PR_MERGED"
    DONE = "DONE"

    ABORTED = "ABORTED"
    ORPHANED = "ORPHANED"


class Action(StrEnum):
    CREATE_WORKTREE = "CREATE_WORKTREE"
    BEGIN_SYNC = "BEGIN_SYNC"
    SYNC_PASSED = "SYNC_PASSED"
    SYNC_FAILED = "SYNC_FAILED"
    BEGIN_REVIEW = "BEGIN_REVIEW"

    SUBMIT_FINDINGS = "SUBMIT_FINDINGS"
    TRIAGE_CLEAN = "TRIAGE_CLEAN"
    TRIAGE_BLOCKING = "TRIAGE_BLOCKING"
    RESPOND = "RESPOND"
    RESOLVE_GREEN = "RESOLVE_GREEN"

    BEGIN_DOCS = "BEGIN_DOCS"
    SKIP_DOCS = "SKIP_DOCS"

    RUN_LINT = "RUN_LINT"
    LINT_PASSED = "LINT_PASSED"
    LINT_FAILED = "LINT_FAILED"
    RETRY_LINT = "RETRY_LINT"
    LINT_FIX_RESTART = "LINT_FIX_RESTART"

    RUN_TEST = "RUN_TEST"
    SKIP_TEST = "SKIP_TEST"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    RETRY_TEST = "RETRY_TEST"

    BEGIN_MERGEBACK = "BEGIN_MERGEBACK"
    MERGEBACK_OK = "MERGEBACK_OK"
    MERGEBACK_FAILED = "MERGEBACK_FAILED"
    MERGEBACK_RETRY = "MERGEBACK_RETRY"

    GATE = "GATE"
    PUSH = "PUSH"
    OPEN_PR = "OPEN_PR"
    BEGIN_CI = "BEGIN_CI"
    RETRY_CI = "RETRY_CI"
    CI_PENDING = "CI_PENDING"
    CI_FAILURE = "CI_FAILURE"
    CI_PASSED = "CI_PASSED"
    CI_TIMEOUT = "CI_TIMEOUT"
    CI_MERGED = "CI_MERGED"
    CI_CLOSED = "CI_CLOSED"
    FINISH = "FINISH"
    CLEANUP = "CLEANUP"

    ABORT = "ABORT"
    ORPHAN = "ORPHAN"


class IllegalTransition(Exception):
    """Raised when a command is issued from a state that does not allow it."""

    def __init__(self, state: State, action: Action) -> None:
        allowed = ", ".join(a.name for a in legal_actions(state)) or "(none)"
        super().__init__(
            f"action {action.name} is not legal in state {state.name}; "
            f"legal actions here: {allowed}"
        )
        self.state = state
        self.action = action


def _stage_cycle(
    prefix: str,
    awaiting: State,
    submitted: State,
    awaiting_responses: State,
    fixing: State,
    green: State,
) -> dict[tuple[State, Action], State]:
    """The sub-machine shared by the two agent-judgment stages (review, docs)."""
    return {
        (awaiting, Action.SUBMIT_FINDINGS): submitted,
        (submitted, Action.TRIAGE_CLEAN): green,
        (submitted, Action.TRIAGE_BLOCKING): awaiting_responses,
        (awaiting_responses, Action.RESPOND): fixing,
        (fixing, Action.RESPOND): fixing,
        (fixing, Action.RESOLVE_GREEN): green,
        (awaiting_responses, Action.RESOLVE_GREEN): green,
    }


TRANSITIONS: dict[tuple[State, Action], State] = {
    (State.CREATED, Action.CREATE_WORKTREE): State.WORKTREE_READY,
    (State.WORKTREE_READY, Action.BEGIN_SYNC): State.SYNC_RUNNING,
    (State.SYNC_RUNNING, Action.SYNC_PASSED): State.SYNC_GREEN,
    (State.SYNC_RUNNING, Action.SYNC_FAILED): State.SYNC_CONFLICT,
    (State.SYNC_GREEN, Action.BEGIN_REVIEW): State.REVIEW_AWAITING_FINDINGS,
    **_stage_cycle(
        "review",
        State.REVIEW_AWAITING_FINDINGS,
        State.REVIEW_SUBMITTED,
        State.REVIEW_AWAITING_RESPONSES,
        State.REVIEW_FIXING,
        State.REVIEW_GREEN,
    ),
    (State.REVIEW_GREEN, Action.RUN_TEST): State.TEST_RUNNING,
    (State.REVIEW_GREEN, Action.SKIP_TEST): State.TEST_GREEN,
    (State.TEST_RUNNING, Action.TEST_PASSED): State.TEST_GREEN,
    (State.TEST_RUNNING, Action.TEST_FAILED): State.TEST_RED,
    (State.TEST_RED, Action.RETRY_TEST): State.TEST_RUNNING,
    (State.TEST_GREEN, Action.BEGIN_DOCS): State.DOCS_AWAITING_FINDINGS,
    (State.TEST_GREEN, Action.SKIP_DOCS): State.DOCS_GREEN,
    **_stage_cycle(
        "docs",
        State.DOCS_AWAITING_FINDINGS,
        State.DOCS_SUBMITTED,
        State.DOCS_AWAITING_RESPONSES,
        State.DOCS_FIXING,
        State.DOCS_GREEN,
    ),
    (State.DOCS_GREEN, Action.RUN_LINT): State.LINT_RUNNING,
    (State.LINT_RUNNING, Action.LINT_PASSED): State.LINT_GREEN,
    (State.LINT_RUNNING, Action.LINT_FAILED): State.LINT_RED,
    (State.LINT_RED, Action.RETRY_LINT): State.LINT_RUNNING,
    (State.LINT_RED, Action.LINT_FIX_RESTART): State.REVIEW_GREEN,
    (State.LINT_GREEN, Action.BEGIN_MERGEBACK): State.MERGEBACK_PENDING,
    (State.MERGEBACK_PENDING, Action.MERGEBACK_OK): State.VERIFIED,
    (State.MERGEBACK_PENDING, Action.MERGEBACK_FAILED): State.MERGEBACK_CONFLICT,
    (State.MERGEBACK_CONFLICT, Action.MERGEBACK_RETRY): State.MERGEBACK_PENDING,
    (State.VERIFIED, Action.GATE): State.AWAITING_PUSH_CONFIRM,
    (State.AWAITING_PUSH_CONFIRM, Action.PUSH): State.PUSHED,
    (State.PUSHED, Action.OPEN_PR): State.PR_OPEN,
    (State.PUSHED, Action.FINISH): State.DONE,
    (State.PR_OPEN, Action.BEGIN_CI): State.CI_MONITORING,
    (State.CI_MONITORING, Action.CI_PENDING): State.CI_MONITORING,
    (State.CI_MONITORING, Action.CI_FAILURE): State.CI_FAILED,
    (State.CI_MONITORING, Action.CI_PASSED): State.CHECKS_PASSED,
    (State.CI_MONITORING, Action.CI_TIMEOUT): State.CI_TIMED_OUT,
    (State.CI_MONITORING, Action.CI_MERGED): State.PR_MERGED,
    (State.CI_MONITORING, Action.CI_CLOSED): State.DONE,
    (State.CI_FAILED, Action.RETRY_CI): State.CI_MONITORING,
    (State.CHECKS_PASSED, Action.RETRY_CI): State.CI_MONITORING,
    (State.CI_TIMED_OUT, Action.RETRY_CI): State.CI_MONITORING,
    (State.PR_OPEN, Action.CLEANUP): State.DONE,
    (State.CI_MONITORING, Action.CLEANUP): State.DONE,
    (State.CI_FAILED, Action.CLEANUP): State.DONE,
    (State.CHECKS_PASSED, Action.CLEANUP): State.DONE,
    (State.CI_TIMED_OUT, Action.CLEANUP): State.DONE,
    (State.PR_MERGED, Action.CLEANUP): State.DONE,
}

#: A run may be abandoned from any state that is not already terminal.
TERMINAL_STATES = frozenset({State.DONE, State.ABORTED, State.ORPHANED})

for _state in State:
    if _state not in TERMINAL_STATES:
        TRANSITIONS[(_state, Action.ABORT)] = State.ABORTED
        TRANSITIONS[(_state, Action.ORPHAN)] = State.ORPHANED
del _state


def next_state(state: State, action: Action) -> State:
    """Resolve a transition, or raise ``IllegalTransition``."""
    try:
        return TRANSITIONS[(state, action)]
    except KeyError:
        raise IllegalTransition(state, action) from None


def legal_actions(state: State) -> list[Action]:
    """Every action the table allows from ``state``, in enum-declaration order."""
    allowed = {a for (s, a) in TRANSITIONS if s == state}
    return [a for a in Action if a in allowed]
