"""Review and documentation finding phases."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import ValidationError

from .. import diff as diffmod
from .. import findings as findingsmod
from .. import gitx, runtime
from .. import risk as riskmod
from ..attestation import output_digest
from ..envelope import Envelope
from ..errors import (
    DiffTooLarge,
    InvalidFindings,
    MaxAttempts,
    StageFailed,
)
from ..machine import Action, State
from ..models import (
    FindingSubmission,
    ReviewCoverage,
    ReviewSubmission,
    RunDoc,
    Stage,
    StageRecord,
)
from ..stages import change_scope, shellstage
from ..stages import docs as docsstage
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _load_current,
    _now,
    _require_finding_stage,
    _require_state,
    _require_worktree,
)


def _bundle_for(session: Session, run: RunDoc) -> diffmod.DiffBundle:
    return diffmod.build_bundle(
        run.worktree_path or session.repo_root,
        run.merge_base_sha,
        "HEAD",
        exclude=session.config.diff.exclude,
    )


ReviewExecutor = Literal["in_harness", "command"]


def _effective_review_executor(session: Session, run: RunDoc) -> ReviewExecutor:
    if run.risk is not None and run.risk.level.value in session.config.review.require_command_for:
        return "command"
    return cast(ReviewExecutor, session.config.review.executor)


def _context_data(
    session: Session, run: RunDoc, *, section: str, bundle: diffmod.DiffBundle
) -> dict[str, Any]:
    """Build the one canonical data bundle used by context and command review."""
    worktree_path = _require_worktree(run)
    review_manifest = (
        diffmod.build_review_manifest(worktree_path, bundle) if section == "review" else None
    )
    data: dict[str, Any] = {
        "section": section,
        "worktree_path": run.worktree_path,
        "base": run.merge_base_sha,
        "head": gitx.rev_parse(worktree_path, "HEAD"),
        "intent": run.intent,
        "intent_source": run.intent_source,
        "changed_files": bundle.files,
        "excluded_files": bundle.excluded,
        "diff": bundle.text,
        "diff_bytes": bundle.total_bytes,
        "risk": (run.risk.model_dump(mode="json") if run.risk is not None else None),
    }
    if review_manifest is not None:
        data["review_coverage"] = review_manifest.as_dict()
    if section == "docs":
        inventory = docsstage.build_inventory(
            worktree_path, bundle.files, session.config.docs.paths
        )
        data["doc_surface"] = [entry.as_dict() for entry in inventory]
        data["require_changelog"] = session.config.docs.require_changelog
    return data


def _open_docs_stage(session: Session, run: RunDoc) -> RunDoc:
    """Move REVIEW_GREEN into the docs sub-machine."""
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.BEGIN_DOCS)
        return doc


def _skip_docs_if_disabled(session: Session, run: RunDoc) -> RunDoc:
    """Skip the docs stage as an explicit transition, never a silent pass.

    ``[docs] enabled = false`` exists for repos with no meaningful doc surface.
    Modelling it as a real transition to DOCS_GREEN keeps the attestation honest:
    the stage was skipped by configuration, and that is a recorded fact rather
    than an absence.
    """
    if session.config.docs.enabled or run.state is not State.REVIEW_GREEN:
        return run
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.SKIP_DOCS)
        run = doc
    session.store.append_event(run.run_id, {"event": "docs_skipped", "reason": "disabled"})
    return run


def _skip_test_if_not_applicable(session: Session, run: RunDoc) -> RunDoc:
    """Record an explicit test skip for documentation/CI-only diffs."""
    if run.state is not State.LINT_GREEN:
        return run
    changed = gitx.changed_files(run.worktree_path or session.repo_root, run.merge_base_sha, "HEAD")
    if not change_scope.tests_are_not_applicable(
        changed, extra_doc_paths=session.config.docs.paths
    ):
        return run
    reason = "changes are limited to documentation and CI configuration"
    with session.store.transaction(run.run_id) as doc:
        doc.stages[Stage.TEST] = StageRecord(
            status="skipped",
            reason=reason,
            finished_at=_now(),
            head_sha=gitx.rev_parse(doc.worktree_path or session.repo_root, "HEAD"),
        )
        _apply(doc, Action.SKIP_TEST)
        run = doc
    session.store.append_event(
        run.run_id,
        {"event": "test_skipped", "reason": reason, "changed_files": changed},
    )
    return run


def _invalidate_stage_result(run: RunDoc, stage: Stage) -> None:
    """Discard stale process evidence without resetting its convergence guard."""
    prior = run.stages.pop(stage, None)
    if prior is not None:
        run.stages[stage] = StageRecord(attempts=prior.attempts)


def _reopen_review_if_coverage_stale(session: Session, run: RunDoc) -> tuple[RunDoc, bool]:
    """Invalidate review evidence when the validation snapshot has moved."""
    coverage = run.review_coverage
    worktree_path = _require_worktree(run)
    current_head = gitx.rev_parse(worktree_path, "HEAD")
    if coverage is not None and coverage.head_sha == current_head:
        return run, False
    with session.store.transaction(run.run_id) as doc:
        reviewed_head = doc.review_coverage.head_sha if doc.review_coverage is not None else None
        doc.review_coverage = None
        _invalidate_stage_result(doc, Stage.REVIEW)
        _invalidate_stage_result(doc, Stage.LINT)
        _invalidate_stage_result(doc, Stage.TEST)
        _apply(doc, Action.INVALIDATE_REVIEW)
        run = doc
    session.store.append_event(
        run.run_id,
        {
            "event": "review_coverage_invalidated",
            "reviewed_head": reviewed_head,
            "current_head": current_head,
        },
    )
    return run, True


def context(session: Session, *, section: str = "review") -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)

    if section == "docs" and run.state in {
        State.REVIEW_GREEN,
        State.DOCS_AWAITING_FINDINGS,
    }:
        run, reopened = _reopen_review_if_coverage_stale(session, run)
        if reopened:
            return context(session, section="review")

    if section == "docs":
        _require_state(
            run,
            State.REVIEW_GREEN,
            State.DOCS_AWAITING_FINDINGS,
            command="context --section docs",
        )
        if run.state is State.REVIEW_GREEN:
            run = _open_docs_stage(session, run)
    else:
        _require_state(
            run,
            State.WORKTREE_READY,
            State.REVIEW_AWAITING_FINDINGS,
            command="context",
        )

    bundle = _bundle_for(session, run)
    report = diffmod.check_budget(bundle, session.config.diff.max_bytes)
    if report.over_budget:
        raise DiffTooLarge(
            f"the diff is {report.total_bytes} bytes, over the {report.max_bytes} byte budget",
            state=run.state.value,
            run_id=run.run_id,
            stage=section,
            data={
                "mode": "diff_too_large",
                "total_bytes": report.total_bytes,
                "max_bytes": report.max_bytes,
                "by_file": [list(item) for item in report.by_file],
            },
            next_instruction=(
                "Narrow the diff before reviewing it. Add generated or vendored paths "
                "to `[diff] exclude` in .agentic-preflight.toml, or raise `[diff] max_bytes` "
                "if the change really is this large. The diff is never truncated, so "
                "reviewing it partially is not an option."
            ),
            next_command="agentic-preflight context",
        )

    data = _context_data(session, run, section=section, bundle=bundle)

    envelope = _envelope_for(run, stage=section, data=data)
    if section == "docs":
        envelope.next_instruction = (
            "Ask one question of the diff: would a reader following the current "
            "docs now be wrong? Submit findings against documentation files only. "
            "Zero findings is a normal and common outcome."
        )
        envelope.next_command = "agentic-preflight submit-findings --file findings.json"
    elif _effective_review_executor(session, run) == "command":
        envelope.next_instruction = (
            "Run the configured independent reviewer. The complete review bundle will be "
            "sent to its standard input and its strict JSON submission will be validated."
        )
        envelope.next_command = "agentic-preflight review run"
    return envelope


def _parse_submissions(payload, *, stage: Stage) -> tuple[list[FindingSubmission], str | None]:
    if stage is Stage.REVIEW:
        try:
            submission = ReviewSubmission.model_validate(payload)
        except ValidationError as exc:
            raise InvalidFindings(_describe_validation(exc)) from exc
        return submission.findings, submission.coverage.manifest

    if isinstance(payload, dict):
        payload = payload.get("findings", [])
    if not isinstance(payload, list):
        raise InvalidFindings(
            "expected a JSON list of findings, or an object with a `findings` key"
        )
    try:
        return [FindingSubmission.model_validate(item) for item in payload], None
    except ValidationError as exc:
        raise InvalidFindings(_describe_validation(exc)) from exc


def _assign_review_units(
    submissions: list[FindingSubmission], manifest: diffmod.ReviewManifest
) -> list[FindingSubmission]:
    """Bind each finding to a manifest unit, inferring only unambiguous cases."""
    by_id = {unit.id: unit for unit in manifest.units}
    assigned: list[FindingSubmission] = []
    for submission in submissions:
        if submission.unit is not None:
            unit = by_id.get(submission.unit)
            if unit is None:
                raise InvalidFindings(
                    f"finding cites unknown review unit {submission.unit!r}; "
                    f"valid units: {sorted(by_id)}"
                )
            if unit.path != submission.path:
                raise InvalidFindings(
                    f"review unit {unit.id} belongs to {unit.path!r}, not "
                    f"finding path {submission.path!r}"
                )
            assigned.append(submission)
            continue

        candidates = [unit for unit in manifest.units if unit.path == submission.path]
        if submission.line is not None:
            containing = [
                unit
                for unit in candidates
                if unit.kind == "hunk"
                and unit.new_start is not None
                and unit.new_count is not None
                and unit.new_count > 0
                and unit.new_start <= submission.line < unit.new_start + unit.new_count
            ]
            if len(containing) == 1:
                assigned.append(submission.model_copy(update={"unit": containing[0].id}))
                continue
        if len(candidates) == 1:
            assigned.append(submission.model_copy(update={"unit": candidates[0].id}))
            continue
        raise InvalidFindings(
            f"finding against {submission.path!r} must name a review `unit`; "
            f"the path has {[unit.id for unit in candidates] or 'no review units'}"
        )
    return assigned


def _describe_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        if error["type"] == "extra_forbidden" and error["loc"][-1] in {
            "id",
            "stage",
            "code_owned",
        }:
            parts.append(
                f"{location}: not a field you may set — id, stage, and code_owned are "
                f"assigned by agentic-preflight, never supplied by the agent"
            )
        elif error["type"] == "extra_forbidden":
            parts.append(f"{location}: unrecognised field")
        else:
            parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def submit_findings(
    session: Session, payload, *, _executor: ReviewExecutor = "in_harness"
) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_AWAITING_FINDINGS,
        State.DOCS_AWAITING_FINDINGS,
        command="submit-findings",
    )

    stage = _require_finding_stage(run)
    if (
        stage is Stage.REVIEW
        and _executor == "in_harness"
        and _effective_review_executor(session, run) == "command"
    ):
        raise InvalidFindings(
            "this run requires the configured independent review command; "
            "in-harness review findings are not accepted",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage.value,
            data={"mode": "needs_command", "risk": run.risk.level.value if run.risk else None},
            next_instruction="Run the independent reviewer for this risk level.",
            next_command="agentic-preflight review run",
        )
    worktree_path = _require_worktree(run)
    bundle = _bundle_for(session, run)
    submissions, coverage_manifest = _parse_submissions(payload, stage=stage)
    manifest = None
    coverage = None
    if stage is Stage.REVIEW:
        manifest = diffmod.build_review_manifest(worktree_path, bundle)
        if coverage_manifest != manifest.manifest:
            raise InvalidFindings(
                "review coverage does not match the current diff; fetch fresh "
                "`context` and submit its review_coverage.manifest"
            )
        submissions = _assign_review_units(submissions, manifest)
        cited = sorted({submission.unit for submission in submissions if submission.unit})
        all_units = [unit.id for unit in manifest.units]
        coverage = ReviewCoverage(
            manifest=manifest.manifest,
            head_sha=manifest.head_sha,
            total_units=len(all_units),
            cited_units=cited,
            clean_units=[unit for unit in all_units if unit not in set(cited)],
            excluded_files=list(manifest.excluded_files),
        )
    elif any(submission.unit is not None for submission in submissions):
        raise InvalidFindings("docs findings cannot cite review units")
    existing = session.store.load_findings(run.run_id)

    inventory = None
    if stage is Stage.DOCS:
        # Docs findings may target files the diff never touched — that is the
        # point of the stage — so the changed-file constraint relaxes to the
        # documentation allowlist rather than disappearing.
        inventory = docsstage.build_inventory(
            worktree_path, bundle.files, session.config.docs.paths
        )
        allowed = docsstage.allowlist(inventory)
    else:
        allowed = set(bundle.files)

    blocking_severities = (
        session.config.review.blocking_severities
        if stage is Stage.REVIEW
        else session.config.docs.blocking_severities
    )

    try:
        accepted = findingsmod.validate_and_assign(
            submissions,
            stage=stage,
            worktree_path=worktree_path,
            allowed_paths=allowed,
            existing=existing,
            max_findings=session.config.review.max_findings,
        )
    except findingsmod.FindingRejected as exc:
        raise InvalidFindings(str(exc)) from exc

    combined = existing + accepted

    if stage is Stage.DOCS and session.config.docs.require_changelog:
        if inventory is None:
            raise InvalidFindings("documentation inventory is unavailable")
        injected = docsstage.changelog_finding(
            inventory, bundle.files, finding_id=findingsmod.next_id(combined)
        )
        if injected is not None:
            accepted = [*accepted, injected]
            combined = [*combined, injected]
    session.store.save_findings(run.run_id, combined)

    stage_findings = [f for f in combined if f.stage is stage]
    blocking = findingsmod.blocking(stage_findings, blocking_severities=blocking_severities)
    assessment = riskmod.assess(
        run.changed_files or bundle.files,
        combined,
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )

    with session.store.transaction(run.run_id) as doc:
        doc.risk = assessment
        if coverage is not None:
            doc.review_coverage = coverage
            entry = doc.stages.get(Stage.REVIEW) or StageRecord()
            entry.status = "green"
            entry.executor = _executor
            entry.finished_at = _now()
            entry.head_sha = coverage.head_sha
            if _executor == "in_harness":
                entry.command = None
                entry.exit_code = None
                entry.output_sha256 = None
                entry.log_path = None
            doc.stages[Stage.REVIEW] = entry
        _apply(doc, Action.SUBMIT_FINDINGS)
        _apply(doc, Action.TRIAGE_BLOCKING if blocking else Action.TRIAGE_CLEAN)
        run = doc

    run = _skip_docs_if_disabled(session, run)

    session.store.append_event(
        run.run_id,
        {
            "event": "findings_submitted",
            "stage": stage.value,
            "count": len(accepted),
            "coverage": coverage.model_dump(mode="json") if coverage is not None else None,
            "risk": assessment.model_dump(mode="json"),
        },
    )

    return _envelope_for(
        run,
        stage=stage.value,
        data={
            "accepted": [f.model_dump(mode="json") for f in accepted],
            "total": len(combined),
            "coverage": coverage.summary() if coverage is not None else None,
            "risk": assessment.model_dump(mode="json"),
        },
        blocking=[f.model_dump(mode="json") for f in blocking],
    )


def _review_command_failed(
    session: Session,
    run: RunDoc,
    *,
    command: str,
    exit_code: int,
    clean_output: str,
    log_path: str,
    reason: str,
) -> RunDoc:
    """Persist one bounded command-review failure and move to the retry state."""
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
    if _effective_review_executor(session, run) != "command":
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

    if run.state is State.REVIEW_COMMAND_RUNNING:
        run = _review_command_failed(
            session,
            run,
            command=command,
            exit_code=125,
            clean_output="[agentic-preflight] previous review command was interrupted",
            log_path="",
            reason="interrupted",
        )
    entry = run.stages.get(Stage.REVIEW) or StageRecord()
    if entry.attempts >= session.config.stage.max_attempts:
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

    bundle = _bundle_for(session, run)
    report = diffmod.check_budget(bundle, session.config.diff.max_bytes)
    if report.over_budget:
        raise DiffTooLarge(
            f"the diff is {report.total_bytes} bytes, over the {report.max_bytes} byte budget",
            state=run.state.value,
            run_id=run.run_id,
            stage=Stage.REVIEW.value,
            next_command="agentic-preflight context",
        )
    data = _context_data(session, run, section="review", bundle=bundle)
    stdin_text = json.dumps(data, sort_keys=True, separators=(",", ":"))

    with session.store.transaction(run.run_id) as doc:
        _apply(
            doc,
            Action.RETRY_REVIEW_COMMAND
            if doc.state is State.REVIEW_COMMAND_RED
            else Action.RUN_REVIEW_COMMAND,
        )
        run = doc

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
            ReviewSubmission.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            failure_reason = f"invalid review submission: {exc}"

    if failure_reason is not None:
        run = _review_command_failed(
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
        entry.head_sha = gitx.rev_parse(wt, "HEAD")
        doc.stages[Stage.REVIEW] = entry
        _apply(doc, Action.REVIEW_COMMAND_PASSED)
        run = doc
    try:
        envelope = submit_findings(session, payload, _executor="command")
    except InvalidFindings as exc:
        run = _review_command_failed(
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


def verify(session: Session) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_SUBMITTED,
        State.REVIEW_AWAITING_RESPONSES,
        State.REVIEW_FIXING,
        State.REVIEW_GREEN,
        State.DOCS_SUBMITTED,
        State.DOCS_AWAITING_RESPONSES,
        State.DOCS_FIXING,
        State.DOCS_GREEN,
        command="verify",
    )

    stage = _require_finding_stage(run)
    blocking_severities = (
        session.config.review.blocking_severities
        if stage is Stage.REVIEW
        else session.config.docs.blocking_severities
    )
    stage_findings = [f for f in session.store.load_findings(run.run_id) if f.stage is stage]
    outstanding = findingsmod.blocking(stage_findings, blocking_severities=blocking_severities)

    if outstanding:
        raise StageFailed(
            f"{len(outstanding)} finding(s) still block the {stage.value} stage",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage.value,
            data={"stage": stage.value},
            blocking=[f.model_dump(mode="json") for f in outstanding],
            next_instruction="Resolve each blocking finding with `respond`, then verify again.",
            next_command="agentic-preflight respond --id <id> --action fixed --commit <sha>",
        )

    run, reopened = _reopen_review_if_coverage_stale(session, run)
    if reopened:
        return _envelope_for(
            run,
            stage=Stage.REVIEW.value,
            data={"coverage_invalidated": True},
            next_instruction=(
                "The reviewed snapshot changed. Fetch the current diff and account "
                "for every review unit before continuing."
            ),
            next_command="agentic-preflight context",
        )

    if run.state not in (State.REVIEW_GREEN, State.DOCS_GREEN):
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.RESOLVE_GREEN)
            run = doc

    run = _skip_docs_if_disabled(session, run)

    return _envelope_for(
        run,
        stage=stage.value,
        data={"stage": stage.value, "blocking_count": 0},
    )
