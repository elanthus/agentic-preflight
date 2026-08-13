"""External process adapter for independent review execution."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .. import diff as diffmod
from .. import gitx, runtime
from ..attestation import output_digest
from ..envelope import Envelope
from ..errors import DiffTooLarge, InvalidFindings, StageFailed
from ..machine import State
from ..models import Stage
from ..stages import shellstage
from . import review_protocol, review_retry
from ._session import Session, _assert_fresh, _load_current, _require_state, _require_worktree


def run_review_command(session: Session) -> Envelope:
    """Run the configured independent reviewer over the canonical review bundle."""
    run = _load_current(session)
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
            next_command="agentic-preflight context",
        )
    command = session.config.review.command
    if not command:
        raise StageFailed(
            "the command review executor is required but [review] command is not configured",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            data={"mode": "needs_command", "stage": Stage.REVIEW.value},
            next_instruction="Configure the independent reviewer command and retry.",
            next_command="agentic-preflight review run",
        )

    run = review_retry.recover_interrupted(session, run, command=command)
    review_retry.require_attempt(session, run)

    bundle = review_protocol.bundle_for(session, run)
    report = diffmod.check_budget(bundle, session.config.diff.max_bytes)
    if report.over_budget:
        raise DiffTooLarge(
            f"the diff is {report.total_bytes} bytes, over the {report.max_bytes} byte budget",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            next_command="agentic-preflight context",
        )
    data = review_protocol.context_data(session, run, section="review", bundle=bundle)
    stdin_text = json.dumps(data, sort_keys=True, separators=(",", ":"))

    run = review_retry.begin(session, run)
    wt = _require_worktree(run)
    prepared = runtime.prepare_command(
        wt,
        command,
        manager=session.config.runtime.manager,
        strict=session.config.runtime.strict,
    )
    result = shellstage.run_stage(
        wt,
        prepared.command,
        timeout_seconds=session.config.stage.timeout_seconds,
        stdin_text=stdin_text,
        separate_stderr=True,
    )
    if not gitx.is_clean(wt):
        result.exit_code = result.exit_code or 1
        result.output += "\n[agentic-preflight] review command changed the worktree"
    secrets = shellstage.read_secrets(wt, run.copied_files)
    clean_output = shellstage.redact(result.output, secrets)
    log_path_obj = session.store.logs_dir(run.run_id) / "review.txt"
    log_path_obj.parent.mkdir(parents=True, exist_ok=True)
    log_path_obj.write_text(clean_output)
    log_path = str(log_path_obj)

    failure_reason = None
    payload: Any = None
    if not result.passed:
        failure_reason = "timeout" if result.timed_out else f"exit {result.exit_code}"
    else:
        try:
            payload = json.loads(result.stdout or "")
            review_protocol.validate_command_output(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            failure_reason = f"invalid review submission: {exc}"

    if failure_reason is not None:
        run = review_retry.fail(
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
                "timed_out": result.timed_out,
                "log_path": log_path,
                **shellstage.summarise(clean_output),
            },
            next_instruction="Inspect the reviewer output, correct the command, and retry.",
            next_command="agentic-preflight review run",
        )

    run = review_retry.pass_execution(
        session,
        run,
        command=command,
        clean_output=clean_output,
        log_path=log_path,
    )
    try:
        # Imported here to keep the process adapter independent of review
        # orchestration while retaining the existing public facade.
        from .review import submit_findings

        envelope = submit_findings(session, payload, _executor="command")
    except InvalidFindings as exc:
        run = review_retry.fail(
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
            data={"command": command, "exit_code": 0, "log_path": log_path},
            next_command="agentic-preflight review run",
        ) from exc
    envelope.data.update(
        {
            "executor": "command",
            "command": command,
            "exit_code": 0,
            "output_sha256": output_digest(clean_output),
            "log_path": log_path,
        }
    )
    return envelope
