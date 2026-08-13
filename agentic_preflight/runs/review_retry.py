"""Persistent retry lifecycle for the independent review command."""

from __future__ import annotations

from .. import gitx
from ..attestation import output_digest
from ..errors import MaxAttempts
from ..machine import Action, State
from ..models import RunDoc, Stage, StageRecord
from ._session import Session, _apply, _now, _require_worktree


def recover_interrupted(session: Session, run: RunDoc, *, command: str) -> RunDoc:
    """Turn a persisted running state into an explicit retryable failure."""
    if run.state is not State.REVIEW_COMMAND_RUNNING:
        return run
    return fail(
        session,
        run,
        command=command,
        exit_code=125,
        clean_output="[agentic-preflight] previous review command was interrupted",
        log_path="",
        reason="interrupted",
    )


def require_attempt(session: Session, run: RunDoc) -> None:
    """Stop a non-converging reviewer at the configured durable attempt bound."""
    entry = run.stages.get(Stage.REVIEW) or StageRecord()
    if entry.attempts < session.config.stage.max_attempts:
        return
    raise MaxAttempts(
        f"the review command has failed {entry.attempts} times "
        f"(max_attempts={session.config.stage.max_attempts})",
        state=run.state.value,
        run_id=run.run_id,
        stage=Stage.REVIEW.value,
        data={"attempts": entry.attempts, "stage": Stage.REVIEW.value},
        next_instruction="This independent reviewer needs human intervention.",
        next_command="agentic-preflight status",
    )


def begin(session: Session, run: RunDoc) -> RunDoc:
    """Persist the running state before launching the external process."""
    with session.store.transaction(run.run_id) as doc:
        _apply(
            doc,
            Action.RETRY_REVIEW_COMMAND
            if doc.state is State.REVIEW_COMMAND_RED
            else Action.RUN_REVIEW_COMMAND,
        )
        return doc


def fail(
    session: Session,
    run: RunDoc,
    *,
    command: str,
    exit_code: int,
    clean_output: str,
    log_path: str,
    reason: str,
) -> RunDoc:
    """Persist one bounded failure and move the command into its retry state."""
    with session.store.transaction(run.run_id) as doc:
        if doc.state is State.REVIEW_AWAITING_FINDINGS:
            _apply(doc, Action.RUN_REVIEW_COMMAND)
        entry = doc.stages.get(Stage.REVIEW) or StageRecord()
        entry.status = "red"
        entry.attempts += 1
        entry.executor = "command"
        entry.command = command
        entry.exit_code = exit_code
        entry.output_sha256 = output_digest(clean_output)
        entry.log_path = log_path
        entry.reason = reason
        entry.finished_at = _now()
        entry.head_sha = gitx.rev_parse(_require_worktree(doc), "HEAD")
        doc.stages[Stage.REVIEW] = entry
        _apply(doc, Action.REVIEW_COMMAND_FAILED)
        return doc


def pass_execution(
    session: Session,
    run: RunDoc,
    *,
    command: str,
    clean_output: str,
    log_path: str,
) -> RunDoc:
    """Record successful process evidence before validating domain findings."""
    with session.store.transaction(run.run_id) as doc:
        entry = doc.stages.get(Stage.REVIEW) or StageRecord()
        entry.status = "green"
        entry.executor = "command"
        entry.command = command
        entry.exit_code = 0
        entry.output_sha256 = output_digest(clean_output)
        entry.log_path = log_path
        entry.reason = None
        entry.finished_at = _now()
        entry.head_sha = gitx.rev_parse(_require_worktree(doc), "HEAD")
        doc.stages[Stage.REVIEW] = entry
        _apply(doc, Action.REVIEW_COMMAND_PASSED)
        return doc
