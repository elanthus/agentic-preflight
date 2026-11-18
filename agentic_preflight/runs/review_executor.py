"""External process adapter for independent review execution."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, NamedTuple

from .. import diff as diffmod
from .. import gitx
from ..attestation import output_digest
from ..cli_contract import ReviewRunData
from ..envelope import Envelope
from ..errors import DiffTooLarge, InvalidFindings, MaxAttempts, StageFailed
from ..machine import Action, State
from ..models import RunDoc, Stage, StageRecord, StageStatus
from ..reviewers.orchestration import REPORT_PROMPT_LIMIT, REPORT_TYPE
from ..stages import shellstage
from . import contracts, review_protocol
from ._session import (
    Session,
    apply_action,
    assert_fresh,
    check_restart_limit,
    load_current,
    require_publishable,
    require_state,
    require_worktree,
    utc_now,
)
from ._shell import _run_protected, _withheld_message
from .review import submit_findings

# A reviewer's report is reviewer-controlled text. Only values that match these
# narrow shapes reach the event log, so it cannot carry prose or a copied secret.
_REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,99}")
_REPORT_PHASE = re.compile(r"[a-z_]{1,40}")
_REPORT_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPORT_OMITTED_LIMIT = 1_000_000


ReportProblem = Literal["invalid_name", "invalid_prompts", "too_many_prompts"]


class ReviewerReport(NamedTuple):
    """A reviewer's report, or why the one it printed was ignored. Exactly one is set."""

    report: dict[str, Any] | None
    problem: ReportProblem | None


def reviewer_report(clean_output: str) -> ReviewerReport | None:
    """Return what the reviewer says reviewed the change, or ``None`` for no report.

    The packaged reviewer ends its stderr with one JSON line of type
    :data:`REPORT_TYPE`. The last such line in the redacted output is read, the
    same bytes the stage log and its digest hold. A report with a missing or
    malformed value is ignored whole, and the result names the problem so the
    log can say that a report was printed and dropped. A report is the
    reviewer's claim: nothing checks that the named model produced the findings.
    """
    for line in reversed(clean_output.splitlines()):
        if REPORT_TYPE not in line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == REPORT_TYPE:
            return _checked_report(value)
    return None


def _checked_report(value: dict[str, Any]) -> ReviewerReport:
    names = {key: value.get(key) for key in ("provider", "model", "effort")}
    prompts = value.get("prompts")
    if not all(isinstance(name, str) and _REPORT_NAME.fullmatch(name) for name in names.values()):
        return ReviewerReport(None, "invalid_name")
    if not isinstance(prompts, list):
        return ReviewerReport(None, "invalid_prompts")
    if len(prompts) > REPORT_PROMPT_LIMIT:
        return ReviewerReport(None, "too_many_prompts")
    omitted = value.get("prompts_omitted", 0)
    if type(omitted) is not int or not 0 <= omitted <= _REPORT_OMITTED_LIMIT:
        return ReviewerReport(None, "invalid_prompts")
    for prompt in prompts:
        if (
            not isinstance(prompt, dict)
            or set(prompt) != {"phase", "sha256"}
            or not isinstance(prompt["phase"], str)
            or not _REPORT_PHASE.fullmatch(prompt["phase"])
            or not isinstance(prompt["sha256"], str)
            or not _REPORT_SHA256.fullmatch(prompt["sha256"])
        ):
            return ReviewerReport(None, "invalid_prompts")
    return ReviewerReport({**names, "prompts": prompts, "prompts_omitted": omitted}, None)


def run_review_command(session: Session) -> Envelope:
    """Run the configured independent reviewer over the canonical review bundle."""
    run = load_current(session)
    check_restart_limit(run)
    require_publishable(run, command="review run")
    assert_fresh(session, run)
    require_state(
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

    bundle, report = review_protocol.budgeted_bundle_for(session, run)
    if report.over_budget or bundle is None:
        raise DiffTooLarge(
            diffmod.over_budget_message(report),
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
    claim = reviewer_report(clean_output)
    if claim is not None:
        origin = {"stage": Stage.REVIEW.value, "source": "command_output"}
        session.store.append_event(
            run.run_id,
            {"event": "reviewer_reported", **origin, **claim.report}
            if claim.report is not None
            else {"event": "reviewer_report_ignored", **origin, "reason": claim.problem},
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
    envelope.data = ReviewRunData.model_validate(
        {
            **envelope.data.model_dump(exclude_unset=True),
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

    def begin() -> None:
        nonlocal run
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
        before_run=begin,
        capture_data={"phase": "contracts", "reviewer_invocations": 0},
        timed_as="contracts",
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
        apply_action(
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
            apply_action(doc, Action.RUN_REVIEW_COMMAND)
        entry = doc.stages.get(Stage.REVIEW) or StageRecord()
        entry.status = StageStatus.RED if failed else StageStatus.GREEN
        if failed:
            entry.attempts += 1
        entry.executor = "command"
        entry.command = command
        entry.exit_code = exit_code
        entry.output_sha256 = output_digest(clean_output)
        entry.log_path = log_path
        entry.reason = reason
        entry.finished_at = utc_now()
        entry.head_sha = gitx.rev_parse(require_worktree(doc), "HEAD")
        doc.stages[Stage.REVIEW] = entry
        apply_action(doc, Action.REVIEW_COMMAND_FAILED if failed else Action.REVIEW_COMMAND_PASSED)
        return doc
