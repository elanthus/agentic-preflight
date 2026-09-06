"""Capture, discover and advance independently applicable stage evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from .. import findings, gitx, risk
from ..digests import json_digest
from ..fingerprints import (
    Classification,
    Disposition,
    DocsFingerprint,
    ReasonCode,
    ReviewFingerprint,
    classify_docs,
    classify_review,
    compute_docs_fingerprint,
    compute_review_fingerprint,
)
from ..machine import Action, State
from ..models import (
    AttestedStage,
    OriginalExecution,
    RunDoc,
    Stage,
    StageEvidence,
    StageFingerprint,
    StageRecord,
)
from ..refresh_validation import (
    base_supports_refresh,
    contract_is_committed,
    rebound_coverage,
    shell_execution_config,
    verify_stage,
)
from ..shell_fingerprints import ShellFingerprint, classify_shell, compute_shell_fingerprint
from . import review_protocol
from ._session import Session, _apply, _now, _require_worktree


def fingerprint(
    session: Session, run: RunDoc, stage: Stage, *, command: str | None = None
) -> StageFingerprint:
    wt = _require_worktree(run)
    head = gitx.rev_parse(wt, "HEAD")
    snapshot = run.config_snapshot or session.config.model_dump(mode="json")
    if stage in {Stage.LINT, Stage.TEST}:
        configured = getattr(session.config.commands, stage.value)
        result = compute_shell_fingerprint(
            wt,
            base_sha=run.merge_base_sha,
            head_sha=head,
            command=command if command is not None else configured or "",
            contract=getattr(session.config.reuse, stage.value),
            execution_config=shell_execution_config(snapshot, stage),
            copied_files=run.copied_files,
        )
        if not contract_is_committed(wt, head, stage, getattr(session.config.reuse, stage.value)):
            result = result.model_copy(
                update={"unavailable": ReasonCode.CONTRACT_UNDECLARED, "inputs_sha256": None}
            )
        return result
    bundle = review_protocol.bundle_for(session, run)
    manifest = review_protocol.grounded_manifest(session, run, bundle)
    if stage is Stage.REVIEW:
        return compute_review_fingerprint(
            wt,
            base_sha=run.merge_base_sha,
            head_sha=head,
            manifest=manifest,
            executor=review_protocol.effective_executor(session, run),
            intent=run.intent or "",
            config_snapshot=snapshot,
        )
    return compute_docs_fingerprint(
        wt,
        base_sha=run.merge_base_sha,
        head_sha=head,
        changed_files=bundle.files,
        doc_paths=session.config.docs.paths,
        config_snapshot=snapshot,
        intent=run.intent or "",
        grounding_sha256=manifest.grounding_sha256,
    )


def classify(old: StageFingerprint, new: StageFingerprint) -> Classification:
    if isinstance(old, ReviewFingerprint) and isinstance(new, ReviewFingerprint):
        return classify_review(old, new)
    if isinstance(old, DocsFingerprint) and isinstance(new, DocsFingerprint):
        return classify_docs(old, new)
    if isinstance(old, ShellFingerprint) and isinstance(new, ShellFingerprint):
        return classify_shell(old, new)
    return Classification(disposition=Disposition.UNKNOWN, reasons=(ReasonCode.INPUTS_UNAVAILABLE,))


def archive(session: Session, run: RunDoc) -> RunDoc:
    """Finalize fresh origins without changing reused execution provenance."""
    all_findings = session.store.load_findings(run.run_id)
    origins = dict(run.evidence)
    for stage in Stage:
        if stage in origins and origins[stage].refreshed_at is not None:
            continue
        record = run.stages.get(stage)
        if (
            record is None
            or record.fingerprint is None
            or record.finished_at is None
            or record.status not in {"green", "skipped"}
        ):
            continue
        coverage = run.review_coverage if stage is Stage.REVIEW else None
        result = AttestedStage(
            status="green" if record.status == "green" else "skipped",
            executor=record.executor,
            command=record.command,
            exit_code=record.exit_code,
            output_sha256=record.output_sha256,
            reason=record.reason,
            coverage=coverage,
        )
        origin = OriginalExecution(
            run_id=run.run_id,
            source_worktree_id=run.source_worktree_id or session.owner_id,
            stage=stage,
            head_sha=record.head_sha or run.head_sha,
            base_sha=run.merge_base_sha,
            branch=run.branch,
            base_ref=run.base_ref,
            config_sha256=run.config_digest or "",
            config_snapshot=run.config_snapshot or {},
            finished_at=datetime.fromisoformat(record.finished_at),
            result=result,
            fingerprint=record.fingerprint,
            findings=[finding for finding in all_findings if finding.stage is stage],
        )
        origins[stage] = StageEvidence(
            origin=origin,
            origin_sha256=json_digest(origin.model_dump(mode="json")),
            fingerprint=record.fingerprint,
        )
    with session.store.transaction(run.run_id) as doc:
        doc.evidence = origins
        return doc


def discover(session: Session, run: RunDoc) -> RunDoc:
    """Consider only completed evidence from this source worktree and branch."""
    candidates = {}
    prior_runs = []
    for run_id in session.store.list_runs():
        if run_id == run.run_id:
            continue
        try:
            old = session.store.load_run(run_id)
        except (OSError, ValueError, ValidationError):
            continue
        if old.source_worktree_id != run.source_worktree_id or old.branch != run.branch:
            continue
        if old.state not in {
            State.VERIFIED,
            State.AWAITING_PUSH_CONFIRM,
            State.PUSHED,
            State.DONE,
            State.ABORTED,
            State.ORPHANED,
        }:
            continue
        prior_runs.append(old)
    prior_runs.sort(key=lambda old: (old.created_at or "", old.run_id), reverse=True)
    for old in prior_runs:
        for stage, item in old.evidence.items():
            if item.origin.source_worktree_id != run.source_worktree_id:
                continue
            try:
                verify_stage(
                    session.repo_root,
                    item,
                    head=old.head_sha,
                    base=old.merge_base_sha,
                    run_id=old.run_id,
                )
            except (ValueError, gitx.GitError):
                continue
            if stage not in candidates:
                candidates[stage] = item
            else:
                try:
                    current = fingerprint(session, run, stage)
                    if (
                        classify(candidates[stage].origin.fingerprint, current).disposition
                        != Disposition.REUSABLE
                        and classify(item.origin.fingerprint, current).disposition
                        == Disposition.REUSABLE
                    ):
                        candidates[stage] = item
                except (OSError, ValueError, gitx.GitError):
                    pass
    with session.store.transaction(run.run_id) as doc:
        doc.reuse_candidates = candidates
        doc.evidence_discovered = True
        return doc


def _ready_stage(run: RunDoc) -> Stage | None:
    return {
        State.REVIEW_AWAITING_FINDINGS: Stage.REVIEW,
        State.REVIEW_GREEN: Stage.DOCS,
        State.DOCS_AWAITING_FINDINGS: Stage.DOCS,
        State.DOCS_GREEN: Stage.LINT,
        State.LINT_GREEN: Stage.TEST,
    }.get(run.state)


def reopen_changed_inputs(session: Session, run: RunDoc) -> RunDoc:
    """Preserve completed evidence before reopening changed review inputs."""
    if run.state not in {State.REVIEW_GREEN, State.DOCS_GREEN, State.LINT_GREEN, State.TEST_GREEN}:
        return run
    changed = False
    for stage, record in run.stages.items():
        if record.status not in {"green", "skipped"} or record.fingerprint is None:
            continue
        current = fingerprint(
            session,
            run,
            stage,
            command=record.command or "" if stage in {Stage.LINT, Stage.TEST} else None,
        )
        if record.fingerprint != current:
            changed = True
            break
    if not changed:
        return run
    run = archive(session, run)
    with session.store.transaction(run.run_id) as doc:
        doc.reuse_candidates.update(doc.evidence)
        doc.evidence = {}
        doc.review_coverage = None
        doc.stages = {
            stage: StageRecord(attempts=record.attempts) for stage, record in doc.stages.items()
        }
        _apply(doc, Action.INVALIDATE_REVIEW)
        return doc


def advance(session: Session, run: RunDoc) -> RunDoc:
    """Persist decisions, then import consecutive applicable stages atomically.

    Later candidates remain durable while an earlier stage is pending. Each
    invocation recomputes inputs, so a repair cannot consume an old green result.
    """
    if not base_supports_refresh(session.repo_root, run.merge_base_sha):
        with session.store.transaction(run.run_id) as doc:
            doc.applicability = {
                stage: Classification(
                    disposition=Disposition.UNKNOWN, reasons=(ReasonCode.CONSUMER_UNAVAILABLE,)
                )
                for stage in Stage
            }
            return doc
    if not gitx.is_clean(_require_worktree(run)):
        return run
    run = reopen_changed_inputs(session, run)
    decisions: dict[Stage, Classification] = {}
    current: dict[Stage, StageFingerprint] = {}
    for stage in Stage:
        item = run.reuse_candidates.get(stage)
        if item is None:
            decisions[stage] = Classification(
                disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINGERPRINT_MISSING,)
            )
            continue
        try:
            new = fingerprint(session, run, stage)
            current[stage] = new
            decisions[stage] = classify(item.origin.fingerprint, new)
            policy = session.config.docs if stage is Stage.DOCS else session.config.review
            if findings.blocking(
                item.origin.findings, blocking_severities=policy.blocking_severities
            ) or findings.actionable(item.origin.findings):
                decisions[stage] = Classification(
                    disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINDINGS_UNRESOLVED,)
                )
        except (OSError, ValueError, gitx.GitError):
            decisions[stage] = Classification(
                disposition=Disposition.UNKNOWN, reasons=(ReasonCode.INPUTS_UNAVAILABLE,)
            )
    with session.store.transaction(run.run_id) as doc:
        doc.applicability = decisions
        run = doc
    while (ready := _ready_stage(run)) is not None:
        stage = ready
        decision = decisions[stage]
        if decision.disposition != Disposition.REUSABLE:
            break
        item = run.reuse_candidates[stage]
        policy = session.config.docs if stage is Stage.DOCS else session.config.review
        if findings.blocking(item.origin.findings, blocking_severities=policy.blocking_severities):
            break
        imported = item.model_copy(
            update={
                "fingerprint": current[stage],
                "refreshed_at": datetime.now(UTC),
                "derivation_reason": "equivalent_inputs",
            }
        )
        head = gitx.rev_parse(_require_worktree(run), "HEAD")
        try:
            verify_stage(
                session.repo_root, imported, head=head, base=run.merge_base_sha, run_id=run.run_id
            )
        except (ValueError, gitx.GitError):
            with session.store.transaction(run.run_id) as doc:
                doc.applicability[stage] = Classification(
                    disposition=Disposition.UNKNOWN, reasons=(ReasonCode.PROVENANCE_INVALID,)
                )
                run = doc
            break
        coverage = (
            rebound_coverage(session.repo_root, item.origin, head=head, base=run.merge_base_sha)
            if stage is Stage.REVIEW
            else None
        )
        stored_findings = session.store.load_findings(run.run_id)
        existing_stage = [f for f in stored_findings if f.stage is stage]
        if existing_stage and [f.model_dump(exclude={"id"}) for f in existing_stage] != [
            f.model_dump(exclude={"id"}) for f in item.origin.findings
        ]:
            break
        next_id = max((int(f.id[1:]) for f in stored_findings), default=0) + 1
        if not existing_stage:
            stored_findings.extend(
                f.model_copy(update={"id": f"F{next_id + index:03d}"})
                for index, f in enumerate(item.origin.findings)
            )
        session.store.save_findings(run.run_id, stored_findings)
        result = item.origin.result
        with session.store.transaction(run.run_id) as doc:
            doc.stages[stage] = StageRecord(
                status=result.status,
                executor=result.executor,
                command=result.command,
                reason=result.reason,
                exit_code=result.exit_code,
                output_sha256=result.output_sha256,
                finished_at=item.origin.finished_at.isoformat(),
                head_sha=head,
                fingerprint=current[stage],
            )
            doc.evidence[stage] = imported
            if stage is Stage.REVIEW:
                doc.review_coverage = coverage
                _apply(doc, Action.SUBMIT_CLEAN)
            elif stage is Stage.DOCS:
                if result.status == "skipped":
                    _apply(doc, Action.SKIP_DOCS)
                else:
                    if doc.state is State.REVIEW_GREEN:
                        _apply(doc, Action.BEGIN_DOCS)
                    _apply(doc, Action.SUBMIT_CLEAN)
            elif stage is Stage.LINT:
                _apply(doc, Action.RUN_LINT)
                _apply(doc, Action.LINT_PASSED)
            elif result.status == "skipped":
                _apply(doc, Action.SKIP_TEST)
            else:
                _apply(doc, Action.RUN_TEST)
                _apply(doc, Action.TEST_PASSED)
            doc.risk = risk.assess(
                doc.changed_files,
                stored_findings,
                policy=session.config.policy,
                review_blocking_severities=session.config.review.blocking_severities,
                docs_blocking_severities=session.config.docs.blocking_severities,
            )
            run = doc
        session.store.append_event(
            run.run_id,
            {
                "event": "stage_reused",
                "stage": stage.value,
                "origin_run_id": item.origin.run_id,
                "origin_sha": item.origin.head_sha,
                "refreshed_at": _now(),
            },
        )
    return archive(session, run)
