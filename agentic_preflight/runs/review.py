"""Review and documentation finding phases."""

from __future__ import annotations

from .. import diff as diffmod
from .. import findings as findingsmod
from .. import gitx
from .. import risk as riskmod
from ..envelope import Envelope
from ..errors import (
    DiffTooLarge,
    InvalidFindings,
    StageFailed,
)
from ..machine import Action, State
from ..models import (
    RunDoc,
    Stage,
    StageRecord,
)
from ..stages import change_scope
from ..stages import docs as docsstage
from . import review_coverage, review_protocol
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
    _respond_command,
)


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


def context(session: Session, *, section: str = "review") -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)

    if section == "docs" and run.state in {
        State.REVIEW_GREEN,
        State.DOCS_AWAITING_FINDINGS,
    }:
        run, reopened = review_coverage.reopen_if_stale(session, run)
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

    bundle = review_protocol.bundle_for(session, run)
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

    data = review_protocol.context_data(session, run, section=section, bundle=bundle)

    envelope = _envelope_for(run, stage=section, data=data)
    if section == "docs":
        envelope.next_instruction = (
            "Ask one question of the diff: would a reader following the current "
            "docs now be wrong? Submit findings against documentation files only. "
            "Zero findings is a normal and common outcome."
        )
        envelope.next_command = "agentic-preflight submit-findings --file findings.json"
    elif review_protocol.effective_executor(session, run) == "command":
        envelope.next_instruction = (
            "Run the configured independent reviewer. The complete review bundle will be "
            "sent to its standard input and its strict JSON submission will be validated."
        )
        envelope.next_command = "agentic-preflight review run"
    return envelope


def submit_findings(
    session: Session,
    payload,
    *,
    _executor: review_protocol.ReviewExecutor = "in_harness",
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
        and review_protocol.effective_executor(session, run) == "command"
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
    bundle = review_protocol.bundle_for(session, run)
    submissions, coverage_manifest = review_protocol.parse_submission(payload, stage=stage)
    manifest = None
    coverage = None
    if stage is Stage.REVIEW:
        manifest = diffmod.build_review_manifest(worktree_path, bundle)
        submissions, coverage = review_coverage.validate(
            submissions,
            manifest=manifest,
            submitted_manifest=coverage_manifest,
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
        _apply(doc, Action.SUBMIT_BLOCKING if blocking else Action.SUBMIT_CLEAN)
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

    envelope = _envelope_for(
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
    if blocking:
        envelope.next_instruction = "Resolve each blocking finding with `respond`."
        envelope.next_command = _respond_command(blocking[0].id)
    else:
        actionable = findingsmod.actionable(stage_findings)
        if actionable:
            envelope.next_instruction = (
                "This stage is green, but an auto-fix finding is still open. Fix it, "
                "or record why it is not worth fixing with `--action accepted --note`."
            )
            envelope.next_command = _respond_command(actionable[0].id)
    return envelope


def verify(session: Session) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_BLOCKED,
        State.REVIEW_GREEN,
        State.DOCS_BLOCKED,
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

    run, reopened = review_coverage.reopen_if_stale(session, run)
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
