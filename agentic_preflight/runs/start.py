"""Run creation, synchronization, and validation-checkout setup."""

from __future__ import annotations

from .. import attestation as attestationmod
from .. import gitx, risk, worktree
from .. import sync as syncmod
from ..config import config_digest, load_config
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    EmptyDiff,
    IntentRequired,
    NeedsHuman,
    SetupFailed,
    SyncConflictError,
    WrongState,
)
from ..machine import TERMINAL_STATES, Action, State
from ..models import Attestation, RunDoc, SetupFailure, Stage, StageRecord
from ..store import CurrentRunExists
from ._session import (
    Session,
    _apply,
    _envelope_for,
    _new_run_id,
    _now,
    _start_command,
    worktree_identity,
)


def _import_evidence_through_machine(doc: RunDoc, evidence: Attestation) -> None:
    """Replay imported evidence through every load-bearing green transition."""
    _apply(doc, Action.SYNC_PASSED)
    _apply(doc, Action.BEGIN_REVIEW)
    _apply(doc, Action.SUBMIT_CLEAN)
    if evidence.stages[Stage.DOCS].status == "skipped":
        _apply(doc, Action.SKIP_DOCS)
    else:
        _apply(doc, Action.BEGIN_DOCS)
        _apply(doc, Action.SUBMIT_CLEAN)
    _apply(doc, Action.RUN_LINT)
    _apply(doc, Action.LINT_PASSED)
    if evidence.stages[Stage.TEST].status == "skipped":
        _apply(doc, Action.SKIP_TEST)
    else:
        _apply(doc, Action.RUN_TEST)
        _apply(doc, Action.TEST_PASSED)
    _apply(doc, Action.BEGIN_MERGEBACK)
    _apply(doc, Action.MERGEBACK_OK)


def _orphan(session: Session, run: RunDoc, *, reason: str) -> None:
    """Detach an idle run without destroying its evidence or validation work."""
    with session.store.try_operation(run.run_id) as idle:
        if not idle:
            raise WrongState(
                f"run {run.run_id} is executing a command and cannot be replaced",
                state=run.state.value,
                run_id=run.run_id,
                next_instruction="Wait for the active command to finish, then retry.",
                next_command="agentic-preflight status",
            )
        with session.store.transaction(run.run_id) as doc:
            if doc.state not in TERMINAL_STATES:
                _apply(doc, Action.ORPHAN)
            doc.orphaned_reason = reason
        session.store.clear_run(run.run_id)
        session.store.append_event(run.run_id, {"event": "orphaned", "reason": reason})


def _resume_existing(session: Session, run: RunDoc) -> Envelope:
    from .lifecycle import status

    envelope = status(session)
    envelope.data["resumed"] = True
    envelope.data["resume_reason"] = "matching active run"
    return envelope


def _claim_alias(session: Session, owner_id: str, run_id: str) -> None:
    current = session.store.get_active(owner_id)
    if current and current != run_id:
        try:
            existing = session.store.load_run(current)
        except Exception:  # noqa: BLE001 - a dangling alias is safe to reclaim
            session.store.clear_active_if(owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_active_if(owner_id, current)
            else:
                raise CurrentRunExists(current)
    if session.store.get_active(owner_id) != run_id:
        session.store.claim_active(owner_id, run_id)


def start(
    session: Session,
    *,
    base_ref: str | None = None,
    intent: str | None = None,
    replace: bool = False,
) -> Envelope:
    repo = session.caller_root
    # Starting is the one command that deliberately reads the working copy.
    # Every later command uses the snapshot persisted below.
    cfg = load_config(repo)
    session.config = cfg
    if cfg.worktree.mode != "in_place":
        session.store.set_worktrees_root(worktree.resolve_root(repo, cfg.worktree.root))

    intent = (intent or "").strip()
    if not intent:
        raise IntentRequired(
            "start requires the user's objective and acceptance criteria",
            next_instruction=(
                "Pass what the user asked for in their own terms, including important "
                "constraints and deliberate tradeoffs. Do not substitute a diff summary."
            ),
            next_command='agentic-preflight start --intent "<user objective and acceptance criteria>"',
        )

    if not gitx.is_clean(repo):
        raise DirtyTree(
            "the working tree has uncommitted changes",
            next_instruction="Commit or stash your changes, then start the run.",
            next_command="git status",
        )

    base_ref = base_ref or cfg.general.base_ref
    branch = gitx.current_branch(repo)
    head_sha = gitx.rev_parse(repo, "HEAD")
    snapshot = cfg.model_dump(mode="json")
    resolved_config_digest = config_digest(snapshot)

    current = session.store.get_active(session.owner_id)
    if current:
        try:
            existing = session.store.load_run(current)
        except Exception:  # noqa: BLE001 - a dangling pointer has no work to preserve
            session.store.clear_active_if(session.owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_run(existing.run_id)
            else:
                if existing.source_worktree_id and existing.source_worktree_id != session.owner_id:
                    raise WrongState(
                        f"run {existing.run_id} belongs to another source worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={"source_worktree_path": existing.source_worktree_path},
                        next_instruction="Run `start` from the recorded source worktree.",
                        next_command="agentic-preflight status",
                    )
                expected = existing.source_head_sha or existing.head_sha
                stale = head_sha != expected or branch != existing.branch
                matches = (
                    not stale
                    and existing.intent == intent
                    and existing.base_ref == base_ref
                    and existing.config_digest == resolved_config_digest
                )
                if matches:
                    return _resume_existing(session, existing)
                if stale:
                    _orphan(session, existing, reason="source worktree moved")
                elif replace:
                    _orphan(session, existing, reason="replaced by a new start")
                else:
                    command = _start_command(
                        intent,
                        base_ref=base_ref,
                        default_base_ref=cfg.general.base_ref,
                        replace=True,
                    )
                    raise WrongState(
                        f"run {existing.run_id} is already active in this worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={
                            "existing_intent": existing.intent,
                            "requested_intent": intent,
                            "source_worktree_path": existing.source_worktree_path,
                        },
                        next_instruction=(
                            "Resume the matching run, or explicitly replace it. Replacement "
                            "orphans the old run without deleting its evidence or fixes."
                        ),
                        next_command=command,
                    )
    try:
        merge_base = gitx.merge_base(repo, base_ref, "HEAD")
    except gitx.GitError as exc:
        raise EmptyDiff(f"cannot find a merge base with {base_ref!r}: {exc}") from exc

    changed = gitx.changed_files(repo, merge_base, "HEAD")
    if not changed:
        raise EmptyDiff(
            f"{branch} has no changes over {base_ref}; there is nothing to review",
            next_instruction="Commit some work on a branch, then start a run.",
        )

    run_id = _new_run_id()
    run = RunDoc(
        run_id=run_id,
        state=State.CREATED,
        branch=branch,
        base_ref=base_ref,
        merge_base_sha=merge_base,
        head_sha=head_sha,
        source_head_sha=head_sha,
        intent=intent,
        intent_source="user",
        source_worktree_id=session.owner_id,
        source_worktree_path=str(repo.resolve()),
        owner_ids=[session.owner_id],
        config_snapshot=snapshot,
        config_digest=resolved_config_digest,
        created_at=_now(),
    )
    # Persist the intent *before* the git call, so a crash mid-create leaves a
    # record for `gc` to reconcile rather than an orphan nobody knows about.
    with session.store.operation(run_id):
        session.store.create_run(run)
        try:
            session.store.claim_active(session.owner_id, run_id)
        except CurrentRunExists as exc:
            with session.store.transaction(run_id) as doc:
                _apply(doc, Action.ABORT)
                doc.worktree_released = True
            raise WrongState(
                f"run {exc.run_id} is already active in this worktree",
                run_id=exc.run_id,
                next_instruction="Finish, clean up, or abort the active run before starting another.",
                next_command="agentic-preflight status",
            ) from exc
    session.store.append_event(
        run_id,
        {
            "event": "run_created",
            "head_sha": head_sha,
            "config_digest": resolved_config_digest,
            "config_snapshot": snapshot,
            "intent": intent,
            "intent_source": "user",
        },
    )

    in_place = cfg.worktree.mode == "in_place"
    reusable = cfg.worktree.mode == "reusable"
    wt_path = repo if in_place else session.store.worktrees_dir / ("runner" if reusable else run_id)
    wt_branch = branch if in_place else f"ap/{run_id}"
    try:
        if in_place:
            pass
        elif reusable:
            worktree.acquire_reusable(repo, path=wt_path, branch=wt_branch, head_sha=head_sha)
        else:  # strict
            retained_runner = session.store.worktrees_dir / "runner"
            if retained_runner.exists():
                if gitx.current_branch(retained_runner) != "HEAD":
                    raise worktree.WorktreeError(
                        f"cannot enter strict mode: reusable runner {retained_runner} "
                        "is still leased; recover or release its run first"
                    )
                worktree.remove(repo, retained_runner)
            worktree.create(repo, path=wt_path, branch=wt_branch, head_sha=head_sha)
    except worktree.WorktreeError as exc:
        with session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        session.store.clear_run(run_id)
        raise NeedsHuman(
            str(exc),
            run_id=run_id,
            data={"worktree_path": str(wt_path)},
            next_instruction=(
                "Inspect the existing validation checkout and run records before "
                "reclaiming anything; an interrupted run may still own commits."
            ),
            next_command="agentic-preflight gc",
        ) from exc

    validation_owner = worktree_identity(wt_path)
    with session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
    try:
        _claim_alias(session, validation_owner, run_id)
    except CurrentRunExists as exc:
        with session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = False
        session.store.clear_run(run_id)
        raise NeedsHuman(
            f"validation worktree is still owned by run {exc.run_id}",
            run_id=run_id,
            data={"worktree_path": str(wt_path), "conflicting_run_id": exc.run_id},
            next_instruction="Inspect both runs before reclaiming the validation checkout.",
            next_command="agentic-preflight status --all",
        ) from exc

    with session.store.transaction(run_id) as doc:
        if validation_owner not in doc.owner_ids:
            doc.owner_ids.append(validation_owner)
        _apply(doc, Action.CREATE_WORKTREE)
        _apply(doc, Action.BEGIN_SYNC)

    try:
        with session.store.resource("sync"), session.store.resource("notes"):
            sync_result = syncmod.synchronize(repo, wt_path, base_ref=base_ref)
    except syncmod.SyncConflict as exc:
        with session.store.transaction(run_id) as doc:
            doc.sync_base_sha = exc.base_sha
            doc.sync_base_ref = exc.base_ref
            _apply(doc, Action.SYNC_FAILED)
            run = doc
        report = {
            "base_ref": exc.base_ref,
            "base_sha": exc.base_sha,
            "head_before": exc.head_before,
            "conflicting_files": exc.conflicting_files,
            "worktree_path": str(wt_path),
        }
        session.store.append_event(run_id, {"event": "sync_conflict", **report})
        raise SyncConflictError(
            str(exc),
            state=run.state.value,
            run_id=run_id,
            data=report,
            next_instruction=(
                "The validation-checkout rebase was aborted cleanly. Show the conflict "
                "report to the user, resolve or rebase the source branch deliberately, "
                "then abort this run and start again with the same intent."
            ),
            next_command="agentic-preflight abort --force",
        ) from exc

    changed = gitx.changed_files(wt_path, sync_result.base_sha, "HEAD")
    if not changed:
        if in_place:
            pass
        elif reusable:
            worktree.release_reusable(repo, wt_path, branch=wt_branch, copied_files=[])
        else:
            worktree.remove(repo, wt_path, branch=wt_branch)
        with session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        session.store.clear_run(run_id)
        raise EmptyDiff(
            "the branch has no changes after synchronizing with the fresh remote base",
            next_instruction="The requested change is already present upstream.",
        )

    reused_attestation = None
    if in_place:
        reused_attestation = attestationmod.reuse_exact(
            repo,
            sha=sync_result.head_after,
            base_sha=sync_result.base_sha,
            branch=branch,
            base_ref=base_ref,
            intent=intent,
            config_digest=resolved_config_digest,
        )
    if reused_attestation is not None:
        assessment = risk.assess(
            changed,
            [],
            policy=cfg.policy,
            review_blocking_severities=cfg.review.blocking_severities,
            docs_blocking_severities=cfg.docs.blocking_severities,
        )
        assessment = risk.include_attested_findings(assessment, reused_attestation.findings_summary)
        with session.store.transaction(run_id) as doc:
            doc.head_sha = sync_result.head_after
            doc.source_head_sha = sync_result.head_after
            doc.merge_base_sha = sync_result.base_sha
            doc.sync_base_sha = sync_result.base_sha
            doc.sync_base_ref = sync_result.base_ref
            doc.sync_remote = sync_result.remote
            doc.changed_files = changed
            doc.risk = assessment
            doc.stages = {
                stage: StageRecord(
                    status=evidence.status,
                    command=evidence.command,
                    reason=evidence.reason,
                    exit_code=evidence.exit_code,
                    output_sha256=evidence.output_sha256,
                    finished_at=reused_attestation.green_at,
                    head_sha=sync_result.head_after,
                )
                for stage, evidence in reused_attestation.stages.items()
            }
            _import_evidence_through_machine(doc, reused_attestation)
            run = doc
        session.store.append_event(
            run_id,
            {
                "event": "attestation_reused",
                "sha": sync_result.head_after,
                "base_sha": sync_result.base_sha,
                "tree_sha": reused_attestation.tree_sha,
            },
        )
        return _envelope_for(
            run,
            data={
                "worktree_path": str(wt_path),
                "worktree_branch": wt_branch,
                "worktree_mode": cfg.worktree.mode,
                "branch": branch,
                "base_ref": base_ref,
                "head_sha": sync_result.head_after,
                "merge_base_sha": sync_result.base_sha,
                "sync": sync_result.as_dict(),
                "changed_files": changed,
                "risk": assessment.model_dump(mode="json"),
                "attestation_reused": True,
            },
            next_instruction=(
                "The synchronized head still has its green attestation and contains the "
                "fresh base. Green was preserved; open the gate."
            ),
            next_command="agentic-preflight gate",
        )

    copied = (
        worktree.protect_in_place_files(repo, cfg.worktree.copy_files)
        if in_place
        else worktree.copy_files(repo, wt_path, cfg.worktree.copy_files)
    )
    assessment = risk.assess(
        changed,
        [],
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )

    # Persist copied paths before setup so an abort after a failed command still
    # removes secret-bearing copies from a reusable runner.
    with session.store.transaction(run_id) as doc:
        doc.copied_files = copied

    setup_result = None
    if cfg.worktree.setup_command:
        completed = worktree.run_setup(
            wt_path, cfg.worktree.setup_command, timeout_seconds=cfg.stage.timeout_seconds
        )
        setup_result = {
            "kind": "custom",
            "command": cfg.worktree.setup_command,
            "exit_code": completed.returncode,
        }
        if completed.returncode != 0:
            failure = SetupFailure(
                scope="initial",
                command=cfg.worktree.setup_command,
                exit_code=completed.returncode,
                worktree_path=str(wt_path),
                next_instruction=(
                    "Fix the setup command or its environment, then abort this run and "
                    "start a fresh one. The active run keeps its configuration snapshot."
                ),
                next_command="agentic-preflight abort --force",
            )
            with session.store.transaction(run_id) as doc:
                doc.head_sha = sync_result.head_after
                doc.source_head_sha = sync_result.head_after if in_place else head_sha
                doc.merge_base_sha = sync_result.base_sha
                doc.sync_base_sha = sync_result.base_sha
                doc.sync_base_ref = sync_result.base_ref
                doc.sync_remote = sync_result.remote
                doc.changed_files = changed
                doc.risk = assessment
                doc.setup_failure = failure
            session.store.append_event(
                run_id,
                {"event": "setup_failed", **failure.model_dump(mode="json")},
            )
            raise SetupFailed(
                f"the setup command failed (exit {completed.returncode})",
                state=State.SYNC_RUNNING.value,
                run_id=run_id,
                stage="setup",
                data={"worktree_path": str(wt_path), "setup": setup_result},
                next_instruction=failure.next_instruction,
                next_command=failure.next_command,
            )

    with session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        doc.head_sha = sync_result.head_after
        doc.source_head_sha = sync_result.head_after if in_place else head_sha
        doc.merge_base_sha = sync_result.base_sha
        doc.sync_base_sha = sync_result.base_sha
        doc.sync_base_ref = sync_result.base_ref
        doc.sync_remote = sync_result.remote
        doc.changed_files = changed
        doc.risk = assessment
        _apply(doc, Action.SYNC_PASSED)
        _apply(doc, Action.BEGIN_REVIEW)
        run = doc

    session.store.append_event(
        run_id,
        {
            "event": "worktree_ready",
            "path": str(wt_path),
            "mode": cfg.worktree.mode,
            "sync": sync_result.as_dict(),
            "risk": assessment.model_dump(mode="json"),
        },
    )

    return _envelope_for(
        run,
        next_instruction="Fetch the diff before judging it.",
        next_command="agentic-preflight context",
        data={
            "worktree_path": str(wt_path),
            "worktree_branch": wt_branch,
            "worktree_mode": cfg.worktree.mode,
            "branch": branch,
            "base_ref": base_ref,
            "head_sha": sync_result.head_after,
            "source_head_sha": head_sha,
            "merge_base_sha": sync_result.base_sha,
            "intent": intent,
            "intent_source": "user",
            "sync": sync_result.as_dict(),
            "changed_files": changed,
            "risk": assessment.model_dump(mode="json"),
            # Names only. Contents are never read, logged, or echoed.
            "copied_files": copied,
            "setup": setup_result,
        },
    )
