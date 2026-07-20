"""Typed failures that carry their own exit code and recovery instruction.

Every error the agent can hit knows three things: which exit code it maps to,
what to tell the agent, and which command to run next. That last part is the
anti-wandering device applied to the failure path — an error that says only
"wrong state" invites guessing, while one that names the next legal command does
not.
"""

from __future__ import annotations

from typing import Any

from .envelope import ExitCode


class AgenticError(Exception):
    code = "internal"
    exit_code = ExitCode.USAGE

    def __init__(
        self,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        blocking: list[Any] | None = None,
        next_instruction: str | None = None,
        next_command: str | None = None,
        state: str | None = None,
        run_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: Where the run is. Carried on failures too: an agent that has just hit
        #: an error needs its bearings more than one that succeeded, and
        #: re-deriving them would mean a second call.
        self.state = state
        self.run_id = run_id
        self.stage = stage
        self.detail = detail
        self.data = data or {}
        #: A failure can still be informative. `verify` reports *which* findings
        #: block, so the agent can act rather than re-derive the list itself.
        self.blocking = blocking or []
        self.next_instruction = next_instruction
        self.next_command = next_command


class NoRun(AgenticError):
    code = "no_run"
    exit_code = ExitCode.PRECONDITION

    def __init__(self, message: str = "no active run in this repository") -> None:
        super().__init__(
            message,
            next_instruction="Start a run first.",
            next_command="agentic-cli start",
        )


class WrongState(AgenticError):
    code = "wrong_state"
    exit_code = ExitCode.PRECONDITION


class StaleRun(AgenticError):
    """The branch tip moved after review began.

    Never continue against a moved head: that is precisely how a false green
    enters the ledger — the agent reviewed one tree and the ledger records
    another.
    """

    code = "stale_run"
    exit_code = ExitCode.PRECONDITION

    def __init__(self, message: str, **kwargs) -> None:
        kwargs.setdefault(
            "next_instruction",
            "The commit under review changed, so everything verified so far "
            "refers to a tree that no longer exists. Start a fresh run.",
        )
        kwargs.setdefault("next_command", "agentic-cli start")
        super().__init__(message, **kwargs)


class DirtyTree(AgenticError):
    code = "dirty_tree"
    exit_code = ExitCode.PRECONDITION


class EmptyDiff(AgenticError):
    code = "empty_diff"
    exit_code = ExitCode.PRECONDITION


class InvalidFindings(AgenticError):
    code = "invalid_findings"
    exit_code = ExitCode.PRECONDITION


class DiffTooLarge(AgenticError):
    """Over budget is a refusal, never a truncation.

    Handing over a silently shortened diff would let the agent believe it
    reviewed the whole change. Stopping loudly costs a turn; a false green costs
    the entire guarantee.
    """

    code = "diff_too_large"
    exit_code = ExitCode.STAGE_FAILED


class StageFailed(AgenticError):
    code = "stage_failed"
    exit_code = ExitCode.STAGE_FAILED


class UnknownFinding(AgenticError):
    code = "unknown_finding"
    exit_code = ExitCode.PRECONDITION


class InvalidResponse(AgenticError):
    code = "invalid_response"
    exit_code = ExitCode.PRECONDITION


class UnmergedWork(AgenticError):
    """Destroying work is the one outcome nobody can undo, so it needs a yes."""

    code = "unmerged_work"
    exit_code = ExitCode.NEEDS_CONFIRM


class MaxAttempts(AgenticError):
    """A stage failed repeatedly. Stop rather than let the agent loop forever."""

    code = "max_attempts"
    exit_code = ExitCode.NEEDS_HUMAN


class MergebackConflictError(AgenticError):
    """A cherry-pick conflicted and was cleanly aborted.

    Exits 4 (human resolution required) rather than 2: no amount of agent
    retrying resolves a genuine content conflict, and an agent that tries will
    make a code decision nobody asked it to make.
    """

    code = "mergeback_conflict"
    exit_code = ExitCode.NEEDS_HUMAN


class NoLog(AgenticError):
    code = "no_log"
    exit_code = ExitCode.PRECONDITION


class NeedsHuman(AgenticError):
    code = "needs_human"
    exit_code = ExitCode.NEEDS_HUMAN


class NeedsConfirm(AgenticError):
    code = "needs_confirm"
    exit_code = ExitCode.NEEDS_CONFIRM
