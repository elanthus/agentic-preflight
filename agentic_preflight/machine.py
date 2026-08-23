"""The state machine: state enum, transition table, and legality queries.

Every other module consumes this. The anti-skip guarantee of the whole tool
rests on a single property of the table below: there is no entry that carries a
run from a review/docs state into a lint, test, or push state. Stage-skipping is
not forbidden by prose, it is *unrepresentable*.

Transitions are a pure function: ``(State, Action) -> State``, exactly one
target per pair. Where the design sketches a conditional branch (a findings
submission goes to ``BLOCKED`` when blocking findings exist and to ``GREEN``
when it is clean) the outcome is folded into the *action* rather than
resolved inside the transition. Code chooses which action to fire; the table
stays deterministic and therefore property-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    CREATED = "CREATED"
    WORKTREE_READY = "WORKTREE_READY"
    SYNC_RUNNING = "SYNC_RUNNING"
    SYNC_CONFLICT = "SYNC_CONFLICT"
    SYNC_GREEN = "SYNC_GREEN"

    REVIEW_AWAITING_FINDINGS = "REVIEW_AWAITING_FINDINGS"
    REVIEW_COMMAND_RUNNING = "REVIEW_COMMAND_RUNNING"
    REVIEW_COMMAND_RED = "REVIEW_COMMAND_RED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    REVIEW_GREEN = "REVIEW_GREEN"

    DOCS_AWAITING_FINDINGS = "DOCS_AWAITING_FINDINGS"
    DOCS_BLOCKED = "DOCS_BLOCKED"
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
    DONE = "DONE"

    ABORTED = "ABORTED"
    ORPHANED = "ORPHANED"


class Action(StrEnum):
    CREATE_WORKTREE = "CREATE_WORKTREE"
    BEGIN_SYNC = "BEGIN_SYNC"
    SYNC_PASSED = "SYNC_PASSED"
    SYNC_FAILED = "SYNC_FAILED"
    BEGIN_REVIEW = "BEGIN_REVIEW"

    SUBMIT_CLEAN = "SUBMIT_CLEAN"
    SUBMIT_BLOCKING = "SUBMIT_BLOCKING"
    RUN_REVIEW_COMMAND = "RUN_REVIEW_COMMAND"
    REVIEW_COMMAND_PASSED = "REVIEW_COMMAND_PASSED"
    REVIEW_COMMAND_FAILED = "REVIEW_COMMAND_FAILED"
    RETRY_REVIEW_COMMAND = "RETRY_REVIEW_COMMAND"
    INVALIDATE_REVIEW = "INVALIDATE_REVIEW"
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
    TEST_FIX_RESTART = "TEST_FIX_RESTART"

    BEGIN_MERGEBACK = "BEGIN_MERGEBACK"
    MERGEBACK_OK = "MERGEBACK_OK"
    MERGEBACK_FAILED = "MERGEBACK_FAILED"
    MERGEBACK_RETRY = "MERGEBACK_RETRY"

    GATE = "GATE"
    PUSH = "PUSH"
    FINISH = "FINISH"

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


@dataclass(frozen=True)
class StateDescription:
    """Outbound transitions and the default recovery move for one state."""

    transitions: dict[Action, State] = field(default_factory=dict)
    instruction: str | None = None
    command: str | None = None


def _state(
    instruction: str | None,
    command: str | None,
    *transitions: tuple[Action, State],
) -> StateDescription:
    return StateDescription(dict(transitions), instruction, command)


def _stage_cycle(
    label: str,
    awaiting: State,
    blocked: State,
    green: State,
    *,
    invalidate_to: State | None = None,
) -> dict[State, StateDescription]:
    """The sub-machine shared by the two agent-judgment stages (review, docs)."""
    invalidate = ((Action.INVALIDATE_REVIEW, invalidate_to),) if invalidate_to is not None else ()
    awaiting_instruction = (
        "Review every delivered unit, then submit snapshot-bound coverage and findings."
        if label == "review"
        else "Review the docs surface, then submit findings (an empty list is valid)."
    )
    awaiting_transitions = [
        (Action.SUBMIT_CLEAN, green),
        (Action.SUBMIT_BLOCKING, blocked),
    ]
    if label == "review":
        awaiting_transitions.append((Action.RUN_REVIEW_COMMAND, State.REVIEW_COMMAND_RUNNING))
    return {
        awaiting: _state(
            awaiting_instruction,
            "agentic-preflight submit-findings --file findings.json",
            *awaiting_transitions,
            *invalidate,
        ),
        blocked: _state(
            "Check the blocking set and continue resolving findings.",
            "agentic-preflight verify",
            (Action.RESPOND, blocked),
            (Action.RESOLVE_GREEN, green),
            *invalidate,
        ),
    }


_S, _A = State, Action
_STATUS = "agentic-preflight status"

STATE_DESCRIPTIONS: dict[State, StateDescription] = {
    _S.CREATED: _state(
        "Create the worktree.",
        'agentic-preflight start --intent "<objective and acceptance criteria>"',
        (_A.CREATE_WORKTREE, _S.WORKTREE_READY),
    ),
    _S.WORKTREE_READY: _state(
        "Synchronize with the fresh remote base.", _STATUS, (_A.BEGIN_SYNC, _S.SYNC_RUNNING)
    ),
    _S.SYNC_RUNNING: _state(
        "Remote synchronization is running.",
        _STATUS,
        (_A.SYNC_PASSED, _S.SYNC_GREEN),
        (_A.SYNC_FAILED, _S.SYNC_CONFLICT),
    ),
    _S.SYNC_CONFLICT: _state(
        "The fresh-base rebase conflicted. Preserve the report and restart.",
        "agentic-preflight abort --force",
    ),
    _S.SYNC_GREEN: _state(
        "Begin review of the synchronized diff.",
        "agentic-preflight context",
        (_A.BEGIN_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    **_stage_cycle(
        "review",
        _S.REVIEW_AWAITING_FINDINGS,
        _S.REVIEW_BLOCKED,
        _S.REVIEW_GREEN,
        invalidate_to=_S.REVIEW_AWAITING_FINDINGS,
    ),
    _S.REVIEW_COMMAND_RUNNING: _state(
        "The independent review command is running.",
        _STATUS,
        (_A.REVIEW_COMMAND_PASSED, _S.REVIEW_AWAITING_FINDINGS),
        (_A.REVIEW_COMMAND_FAILED, _S.REVIEW_COMMAND_RED),
    ),
    _S.REVIEW_COMMAND_RED: _state(
        "Inspect the failed independent review command before retrying.",
        "agentic-preflight logs --stage review",
        (_A.RETRY_REVIEW_COMMAND, _S.REVIEW_COMMAND_RUNNING),
    ),
    _S.REVIEW_GREEN: _state(
        "Review is green. Check whether documentation is now stale.",
        "agentic-preflight context --section docs",
        (_A.BEGIN_DOCS, _S.DOCS_AWAITING_FINDINGS),
        (_A.SKIP_DOCS, _S.DOCS_GREEN),
        (_A.INVALIDATE_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    **_stage_cycle(
        "docs",
        _S.DOCS_AWAITING_FINDINGS,
        _S.DOCS_BLOCKED,
        _S.DOCS_GREEN,
        invalidate_to=_S.REVIEW_AWAITING_FINDINGS,
    ),
    _S.DOCS_GREEN: _state(
        "Docs are green. Run lint.",
        "agentic-preflight stage run lint",
        (_A.RUN_LINT, _S.LINT_RUNNING),
        (_A.INVALIDATE_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.LINT_RUNNING: _state(
        "Lint execution was interrupted; inspect the recorded run.",
        _STATUS,
        (_A.LINT_PASSED, _S.LINT_GREEN),
        (_A.LINT_FAILED, _S.LINT_RED),
    ),
    _S.LINT_RED: _state(
        "Inspect the failed lint stage before retrying.",
        "agentic-preflight logs --stage lint",
        (_A.RETRY_LINT, _S.LINT_RUNNING),
        (_A.LINT_FIX_RESTART, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.LINT_GREEN: _state(
        "Lint is green. Run targeted tests.",
        "agentic-preflight stage run test",
        (_A.RUN_TEST, _S.TEST_RUNNING),
        (_A.SKIP_TEST, _S.TEST_GREEN),
    ),
    _S.TEST_RUNNING: _state(
        "Test execution was interrupted; inspect the recorded run.",
        _STATUS,
        (_A.TEST_PASSED, _S.TEST_GREEN),
        (_A.TEST_FAILED, _S.TEST_RED),
    ),
    _S.TEST_RED: _state(
        "Inspect the failed test stage before retrying.",
        "agentic-preflight logs --stage test",
        (_A.RETRY_TEST, _S.TEST_RUNNING),
        (_A.TEST_FIX_RESTART, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.TEST_GREEN: _state(
        "Tests passed or were not applicable. Merge the fixes back.",
        "agentic-preflight mergeback",
        (_A.BEGIN_MERGEBACK, _S.MERGEBACK_PENDING),
        (_A.INVALIDATE_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.MERGEBACK_PENDING: _state(
        "Mergeback was interrupted; inspect the recorded run.",
        _STATUS,
        (_A.MERGEBACK_OK, _S.VERIFIED),
        (_A.MERGEBACK_FAILED, _S.MERGEBACK_CONFLICT),
        (_A.INVALIDATE_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.MERGEBACK_CONFLICT: _state(
        "Resolve the reported conflict or restore the affected paths, then retry mergeback.",
        "agentic-preflight mergeback",
        (_A.MERGEBACK_RETRY, _S.MERGEBACK_PENDING),
        (_A.INVALIDATE_REVIEW, _S.REVIEW_AWAITING_FINDINGS),
    ),
    _S.VERIFIED: _state(
        "Everything is green. Open the gate.",
        "agentic-preflight gate",
        (_A.GATE, _S.AWAITING_PUSH_CONFIRM),
    ),
    _S.AWAITING_PUSH_CONFIRM: _state(
        "Show the user the gate summary and ask before pushing.",
        "agentic-preflight push --confirm <token>",
        (_A.PUSH, _S.PUSHED),
    ),
    _S.PUSHED: _state(
        "Close the pushed validation run.", "agentic-preflight finish", (_A.FINISH, _S.DONE)
    ),
    _S.DONE: _state(None, None),
    _S.ABORTED: _state(None, None),
    _S.ORPHANED: _state(None, None),
}
del _S, _A, _STATUS

TRANSITIONS: dict[tuple[State, Action], State] = {
    (state, action): target
    for state, description in STATE_DESCRIPTIONS.items()
    for action, target in description.transitions.items()
}

#: A run may be abandoned from any state that is not already terminal.
TERMINAL_STATES = frozenset({State.DONE, State.ABORTED, State.ORPHANED})

for _candidate in State:
    if _candidate not in TERMINAL_STATES:
        TRANSITIONS[(_candidate, Action.ABORT)] = State.ABORTED
        TRANSITIONS[(_candidate, Action.ORPHAN)] = State.ORPHANED
del _candidate


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


def recovery_hint(state: State) -> StateDescription:
    """Return the recovery/default-next hint declared beside a state's transitions."""
    return STATE_DESCRIPTIONS[state]
