"""Capture, discover and advance independently applicable stage evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from .. import findings, gitx
from ..digests import json_digest, sha256_text
from ..errors import STAGE_EVIDENCE_RECOVERY, StageEvidenceInvalid
from ..fingerprints import (
    Classification,
    Disposition,
    DocsFingerprint,
    ReasonCode,
    ReviewFingerprint,
    ShellFingerprint,
    classify_docs,
    classify_review,
    compute_docs_fingerprint,
    compute_review_fingerprint,
    shell_config_digest,
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
from ..refresh_validation import verify_stage
from ..store import RunReadError, UnknownRun
from . import review_protocol
from ._session import (
    Session,
    _apply,
    _assess,
    _blocking_severities,
    _check_restart_limit,
    _now,
    _require_worktree,
)


def fingerprint(
    session: Session,
    run: RunDoc,
    stage: Stage,
    *,
    command: str | None = None,
    clean: bool | None = None,
) -> StageFingerprint:
    """Compute the current fingerprint for a stage of a run.

    ``clean`` is a cleanliness check of the validation worktree the caller has
    just made; no additional cleanliness check is needed for shell fingerprints.
    """
    wt = _require_worktree(run)
    head = gitx.rev_parse(wt, "HEAD")
    snapshot = run.config_snapshot
    if stage in {Stage.LINT, Stage.TEST}:
        configured = getattr(session.config.commands, stage.value)
        return ShellFingerprint(
            base_tree_sha=gitx.tree_sha(wt, run.merge_base_sha),
            head_tree_sha=gitx.tree_sha(wt, head),
            command_sha256=sha256_text(command if command is not None else configured or ""),
            config_sha256=shell_config_digest(snapshot, stage.value),
        )
    bundle = review_protocol.bundle_for(session, run, head=head)
    manifest = review_protocol.grounded_manifest(session, run, bundle, head=head)
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
        surface=review_protocol.docs_surface(session, run, bundle.files, head=head),
    )


def classify(old: StageFingerprint, new: StageFingerprint) -> Classification:
    """Classify how a stage's evidence changed between two fingerprints."""
    if isinstance(old, ReviewFingerprint) and isinstance(new, ReviewFingerprint):
        return classify_review(old, new)
    if isinstance(old, DocsFingerprint) and isinstance(new, DocsFingerprint):
        return classify_docs(old, new)
    if isinstance(old, ShellFingerprint) and isinstance(new, ShellFingerprint):
        return Classification(
            disposition=Disposition.INVALID, reasons=(ReasonCode.SHELL_REUSE_DISABLED,)
        )
    return Classification(disposition=Disposition.UNKNOWN, reasons=(ReasonCode.INPUTS_UNAVAILABLE,))


def verify_origins(session: Session, run: RunDoc, origins: dict[Stage, StageEvidence]) -> None:
    """Check saved evidence without confusing validity with record loadability."""
    for stage, item in origins.items():
        try:
            item.verify_origin()
        except ValueError as exc:
            raise StageEvidenceInvalid(
                str(exc),
                state=run.state.value,
                run_id=run.run_id,
                stage=stage.value,
                data={
                    "path": str(session.store.run_path(run.run_id)),
                    "stage": stage.value,
                    "reason": "invalid_origin_digest",
                },
                next_instruction=STAGE_EVIDENCE_RECOVERY,
                next_command="agentic-preflight abort",
            ) from exc


def archive(session: Session, run: RunDoc) -> RunDoc:
    """Finalize fresh origins without changing reused execution provenance."""
    verify_origins(session, run, run.evidence)
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
            source_worktree_id=run.source_worktree_id,
            stage=stage,
            head_sha=record.head_sha or run.head_sha,
            base_sha=run.merge_base_sha,
            branch=run.branch,
            base_ref=run.base_ref,
            config_sha256=run.config_digest,
            config_snapshot=run.config_snapshot,
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
    verify_origins(session, run, origins)
    with session.store.transaction(run.run_id) as doc:
        doc.evidence = origins
        return doc


def _prior_runs(session: Session, run: RunDoc) -> list[RunDoc]:
    """Return finished runs from this source worktree and branch, newest first."""
    prior_runs: list[RunDoc] = []
    for run_id in session.store.list_runs():
        if run_id == run.run_id:
            continue
        # Skip runs that a raw read already shows belong elsewhere. A record that
        # cannot be read that way reaches load_run and is handled as before.
        peeked = session.store.peek(run_id, "branch", "source_worktree_id")
        if peeked is not None and any(
            isinstance(peeked[field], str) and peeked[field] != expected
            for field, expected in (
                ("branch", run.branch),
                ("source_worktree_id", run.source_worktree_id),
            )
        ):
            continue
        try:
            old = session.store.load_run(run_id)
        except (OSError, ValueError, ValidationError, RunReadError, UnknownRun):
            continue
        # Keep the audit record, but never reuse validation discarded after an
        # interrupted legacy update, even after its pending journal is removed.
        if (
            old.orphaned_reason == "interrupted legacy update"
            or (session.store.run_dir(old.run_id) / "pending-update.json").exists()
        ):
            continue
        if old.source_worktree_id != run.source_worktree_id:
            continue
        if old.branch != run.branch:
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
    prior_runs.sort(key=lambda old: (old.created_at, old.run_id), reverse=True)
    return prior_runs


def _select_candidates(
    session: Session, run: RunDoc, prior_runs: list[RunDoc]
) -> dict[Stage, StageEvidence]:
    """Pick, per stage, the newest verified item unless an older one alone is reusable."""
    candidates: dict[Stage, StageEvidence] = {}
    # An older item replaces a stage's candidate only when the candidate is not
    # reusable against the current fingerprint and the older item is. Once the
    # candidate is reusable, or the current fingerprint cannot be computed, no
    # later item can win, so verifying it would only repeat Git work.
    current: dict[Stage, StageFingerprint | None] = {}
    settled: set[Stage] = set()

    def current_fingerprint(stage: Stage) -> StageFingerprint | None:
        if stage not in current:
            try:
                current[stage] = fingerprint(session, run, stage)
            except (OSError, ValueError, gitx.GitError):
                current[stage] = None
        return current[stage]

    for old in prior_runs:
        for stage, item in old.evidence.items():
            if stage in {Stage.LINT, Stage.TEST}:
                continue
            if item.origin.source_worktree_id != run.source_worktree_id:
                continue
            if stage in settled:
                continue
            if stage in candidates:
                now = current_fingerprint(stage)
                if (
                    now is None
                    or classify(candidates[stage].origin.fingerprint, now).disposition
                    == Disposition.REUSABLE
                ):
                    settled.add(stage)
                    continue
            try:
                item.verify_origin()
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
                continue
            now = current_fingerprint(stage)
            if now is not None and (
                classify(item.origin.fingerprint, now).disposition == Disposition.REUSABLE
            ):
                candidates[stage] = item
                settled.add(stage)
    return candidates


def discover(session: Session, run: RunDoc) -> RunDoc:
    """Consider only completed evidence from this source worktree and branch."""
    candidates = _select_candidates(session, run, _prior_runs(session, run))
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


def reopen_changed_inputs(session: Session, run: RunDoc, *, clean: bool | None = None) -> RunDoc:
    """Preserve completed evidence before reopening changed review inputs."""
    if run.state not in {
        State.REVIEW_GREEN,
        State.DOCS_GREEN,
        State.LINT_GREEN,
        State.TEST_GREEN,
    }:
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
            clean=clean,
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
        run = doc
    _check_restart_limit(run)
    return run


def advance(session: Session, run: RunDoc, *, clean: bool | None = None) -> RunDoc:
    """Persist decisions, then import consecutive applicable stages atomically.

    Later candidates remain durable while an earlier stage is pending. Each
    invocation recomputes inputs, so a repair cannot consume an old green result.
    A caller that has just checked the worktree may pass that result as ``clean``.
    """
    if clean is None:
        clean = gitx.is_clean(_require_worktree(run))
    if not clean:
        return run
    run = reopen_changed_inputs(session, run, clean=True)
    decisions: dict[Stage, Classification] = {}
    current: dict[Stage, StageFingerprint] = {}
    for stage in Stage:
        if stage in {Stage.LINT, Stage.TEST}:
            decisions[stage] = Classification(
                disposition=Disposition.INVALID, reasons=(ReasonCode.SHELL_REUSE_DISABLED,)
            )
            continue
        item = run.reuse_candidates.get(stage)
        if item is None:
            decisions[stage] = Classification(
                disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINGERPRINT_MISSING,)
            )
            continue
        try:
            new = fingerprint(session, run, stage, clean=True)
            current[stage] = new
            decisions[stage] = classify(item.origin.fingerprint, new)
            if findings.blocking(
                item.origin.findings, blocking_severities=_blocking_severities(session, stage)
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
        if findings.blocking(
            item.origin.findings, blocking_severities=_blocking_severities(session, stage)
        ):
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
            # ``imported`` is refreshed, so verification returns the review coverage
            # it has already rebound to this head and base.
            imported.verify_origin()
            coverage = verify_stage(
                session.repo_root, imported, head=head, base=run.merge_base_sha, run_id=run.run_id
            )
        except (ValueError, gitx.GitError):
            with session.store.transaction(run.run_id) as doc:
                doc.applicability[stage] = Classification(
                    disposition=Disposition.UNKNOWN, reasons=(ReasonCode.PROVENANCE_INVALID,)
                )
                run = doc
            break
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
        result = item.origin.result
        with session.store.transaction(run.run_id, findings=stored_findings) as doc:
            record = stage_record(
                result,
                finished_at=item.origin.finished_at.isoformat(),
                head_sha=head,
            )
            record.executor = result.executor
            record.fingerprint = current[stage]
            doc.evidence[stage] = imported
            if stage is Stage.REVIEW:
                doc.review_coverage = coverage
            install_stage(doc, stage, record)
            doc.risk = _assess(session, doc.changed_files, stored_findings)
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


def stage_record(result: AttestedStage, *, finished_at: str | None, head_sha: str) -> StageRecord:
    """Reconstruct result fields from fingerprint-reused evidence."""
    return StageRecord(
        status=result.status,
        command=result.command,
        reason=result.reason,
        exit_code=result.exit_code,
        output_sha256=result.output_sha256,
        finished_at=finished_at,
        head_sha=head_sha,
    )


def install_stage(doc: RunDoc, stage: Stage, record: StageRecord) -> None:
    """Install a result and replay its load-bearing stage transitions.

    Synchronization and mergeback remain the caller's responsibility. Refresh
    may reach docs either before or after its context has been delivered.
    """
    if stage is Stage.REVIEW:
        _apply(doc, Action.SUBMIT_CLEAN)
    elif stage is Stage.DOCS:
        if record.status == "skipped":
            _apply(doc, Action.SKIP_DOCS)
        else:
            if doc.state is State.REVIEW_GREEN:
                _apply(doc, Action.BEGIN_DOCS)
            _apply(doc, Action.SUBMIT_CLEAN)
    elif stage is Stage.LINT:
        _apply(doc, Action.RUN_LINT)
        _apply(doc, Action.LINT_PASSED)
    elif record.status == "skipped":
        _apply(doc, Action.SKIP_TEST)
    else:
        _apply(doc, Action.RUN_TEST)
        _apply(doc, Action.TEST_PASSED)
    doc.stages[stage] = record
