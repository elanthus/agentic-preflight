"""External process adapter for independent review execution."""

from __future__ import annotations

import json
from typing import Any

from .. import diff as diffmod
from .. import gitx
from ..attestation import output_digest
from ..envelope import Envelope
from ..errors import DiffTooLarge, InvalidFindings, MaxAttempts, StageFailed
from ..machine import Action, State
from ..models import RunDoc, Stage, StageRecord
from ..stages import shellstage
from . import contracts, review_protocol
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _check_restart_limit,
    _load_current,
    _now,
    _require_state,
    _require_worktree,
)
from ._shell import _run_protected, _withheld_message
from .review import submit_findings


def run_review_command(session: Session) -> Envelope:
    """Run the configured independent reviewer over the canonical review bundle."""
    run = _load_current(session)
    _check_restart_limit(run)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_AWAITING_FINDINGS,
        State.REVIEW_COMMAND_RED,
        State.REVIEW_COMMAND_RUNNING,
        command="review run",
    )
    if review_protocol.effective_executor(session, run) != "command":
        raise InvalidFindings(
            "review run is only available when the effective review executor is `command`",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"reviewer_invocations": 0},
            next_command="agentic-preflight context",
        )
    command = session.config.review.command
    if not command:
        raise StageFailed(
            "the command review executor is required but [review] command is not configured",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={
                "mode": "needs_command",
                "stage": Stage.REVIEW.value,
                "reviewer_invocations": 0,
            },
            next_instruction="Configure the independent reviewer command and retry.",
            next_command="agentic-preflight review run",
        )

    run = _recover_interrupted(session, run, command=command)
    _require_attempt(session, run)

    bundle = review_protocol.bundle_for(session, run)
    report = diffmod.check_budget(bundle, session.config.diff.max_bytes)
    if report.over_budget:
        raise DiffTooLarge(
            f"the diff is {report.total_bytes} bytes, over the {report.max_bytes} byte budget",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"reviewer_invocations": 0},
            next_command="agentic-preflight context",
        )
    run, planner_invocations = _prepare_contracts(session, run, bundle)
    data = review_protocol.context_data(session, run, section="review", bundle=bundle)
    stdin_text = json.dumps(data, sort_keys=True, separators=(",", ":"))

    def begin() -> None:
        nonlocal run
        run = _begin_review_command(session, run)

    protected, _ = _run_protected(
        session,
        run,
        command,
        log_name="review.txt",
        retry_command="agentic-preflight review run",
        what="review command",
        stage=Stage.REVIEW.value,
        stdin_text=stdin_text,
        separate_stderr=True,
        before_run=begin,
        capture_data={"reviewer_invocations": planner_invocations},
    )
    result = protected.result
    clean_output = protected.clean_output
    log_path = str(protected.log_path)
    redaction_error = protected.redaction_error
    redaction_failure_reason = protected.failure_reason
    if redaction_failure_reason is not None:
        run = _record_review_command(
            session,
            run,
            command=command,
            exit_code=result.exit_code,
            clean_output=clean_output,
            log_path=log_path,
            reason=redaction_failure_reason,
        )
        failure_data = {
            "command": command,
            "exit_code": result.exit_code,
            "reviewer_invocations": 1 + planner_invocations,
            "copied_files": run.copied_files,
            "log_path": log_path,
            **shellstage.summarise(clean_output),
        }
        if redaction_error is not None:
            failure_data["copied_file"] = str(redaction_error.path)
        message = _withheld_message("review command", redaction_error)
        raise StageFailed(
            message,
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data=failure_data,
            next_instruction=(
                "Restore every copied file to its intended contents, then retry the "
                "review command. Commands must not rewrite [worktree] copy_files."
            ),
            next_command="agentic-preflight review run",
        ) from redaction_error
    failure_reason = None
    payload: Any = None
    if not result.passed:
        failure_reason = "timeout" if result.timed_out else f"exit {result.exit_code}"
    else:
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError as exc:
            failure_reason = f"invalid review submission: {exc}"

    if failure_reason is not None:
        run = _record_review_command(
            session,
            run,
            command=command,
            exit_code=result.exit_code,
            clean_output=clean_output,
            log_path=log_path,
            reason=failure_reason,
        )
        raise StageFailed(
            f"the review command failed: {failure_reason}",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={
                "command": command,
                "exit_code": result.exit_code,
                "reviewer_invocations": 1 + planner_invocations,
                "timed_out": result.timed_out,
                "log_path": log_path,
                **shellstage.summarise(clean_output),
            },
            next_instruction="Inspect the reviewer output, correct the command, and retry.",
            next_command="agentic-preflight review run",
        )

    run = _record_review_command(
        session,
        run,
        command=command,
        clean_output=clean_output,
        log_path=log_path,
    )
    try:
        envelope = submit_findings(session, payload, _executor="command")
    except InvalidFindings as exc:
        run = _record_review_command(
            session,
            run,
            command=command,
            exit_code=0,
            clean_output=clean_output,
            log_path=log_path,
            reason=exc.message,
        )
        raise StageFailed(
            f"the review command returned an invalid submission: {exc.message}",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={
                "command": command,
                "exit_code": 0,
                "log_path": log_path,
                "reviewer_invocations": 1 + planner_invocations,
                "timed_out": result.timed_out,
                **shellstage.summarise(clean_output),
            },
            next_command="agentic-preflight review run",
        ) from exc
    envelope.data.update(
        {
            "executor": "command",
            "command": command,
            "exit_code": 0,
            "reviewer_invocations": 1 + planner_invocations,
            "output_sha256": output_digest(clean_output),
            "log_path": log_path,
            **({"contract_checks": data["contract_checks"]} if "contract_checks" in data else {}),
        }
    )
    return envelope


def _prepare_contracts(
    session: Session,
    run: RunDoc,
    bundle: diffmod.DiffBundle,
) -> tuple[RunDoc, int]:
    if not session.config.review.contracts.enabled:
        return run, 0
    manifest = review_protocol.grounded_manifest(session, run, bundle)
    if contracts.receipt_for(session, run, manifest) is not None:
        return run, 0
    command = session.config.review.contracts.planner_command
    if not command:
        raise InvalidFindings(
            "a contract plan is required before command review",
            next_instruction="Submit a plan locally or configure review.contracts.planner_command.",
            next_command="agentic-preflight review contracts --file contracts.json",
        )
    data = review_protocol.context_data(session, run, section="review", bundle=bundle)
    data["section"] = "contracts"
    run = _begin_review_command(session, run)
    protected, _ = _run_protected(
        session,
        run,
        command,
        log_name="contract-planner.txt",
        retry_command="agentic-preflight review run",
        what="contract planner",
        stage="review",
        stdin_text=json.dumps(data, sort_keys=True, separators=(",", ":")),
        separate_stderr=True,
    )
    result = protected.result
    reason = protected.failure_reason
    if reason is None:
        if not result.passed:
            reason = (
                "contract planner timed out"
                if result.timed_out
                else f"contract planner exited {result.exit_code}"
            )
        else:
            try:
                payload = json.loads(result.stdout or "")
                contracts.record_plan(session, run, payload, bundle)
            except (ValueError, InvalidFindings) as exc:
                reason = f"invalid contract plan: {exc}"[:2000]
    run = _record_review_command(
        session,
        run,
        command=command,
        clean_output=protected.clean_output,
        log_path=str(protected.log_path),
        exit_code=result.exit_code,
        reason=reason,
    )
    if reason is not None:
        raise StageFailed(
            reason,
            run_id=run.run_id,
            state=run.state.value,
            stage="review",
            data={
                "phase": "contracts",
                "reviewer_invocations": 1,
                "log_path": str(protected.log_path),
            },
            next_command="agentic-preflight review run",
        )
    return run, 1


def _recover_interrupted(session: Session, run: RunDoc, *, command: str) -> RunDoc:
    """Turn a persisted running state into an explicit retryable failure."""
    if run.state is not State.REVIEW_COMMAND_RUNNING:
        return run
    return _record_review_command(
        session,
        run,
        command=command,
        exit_code=125,
        clean_output="[agentic-preflight] previous review command was interrupted",
        log_path="",
        reason="interrupted",
    )


def _require_attempt(session: Session, run: RunDoc) -> None:
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
        data={
            "attempts": entry.attempts,
            "stage": Stage.REVIEW.value,
            "reviewer_invocations": 0,
        },
        next_instruction="This independent reviewer needs human intervention.",
        next_command="agentic-preflight status",
    )


def _begin_review_command(session: Session, run: RunDoc) -> RunDoc:
    """Persist the running state before launching the external process."""
    with session.store.transaction(run.run_id) as doc:
        _apply(
            doc,
            Action.RETRY_REVIEW_COMMAND
            if doc.state is State.REVIEW_COMMAND_RED
            else Action.RUN_REVIEW_COMMAND,
        )
        return doc


def _record_review_command(
    session: Session,
    run: RunDoc,
    *,
    command: str,
    clean_output: str,
    log_path: str,
    exit_code: int = 0,
    reason: str | None = None,
) -> RunDoc:
    """Record process evidence, counting only failures before domain validation."""
    failed = reason is not None
    with session.store.transaction(run.run_id) as doc:
        if failed and doc.state is State.REVIEW_AWAITING_FINDINGS:
            _apply(doc, Action.RUN_REVIEW_COMMAND)
        entry = doc.stages.get(Stage.REVIEW) or StageRecord()
        entry.status = "red" if failed else "green"
        if failed:
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
        _apply(doc, Action.REVIEW_COMMAND_FAILED if failed else Action.REVIEW_COMMAND_PASSED)
        return doc
