"""Run creation, synchronization, and validation-checkout setup."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from .. import gitx, risk, worktree
from .. import sync as syncmod
from ..config import Config, config_digest, load_config
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
from ..errors import (
    OperationInProgress as OperationInProgressError,
)
from ..machine import TERMINAL_STATES, Action, State
from ..models import RiskAssessment, RunDoc, SetupFailure
from ..store import CurrentRunExists, UnknownRun
from . import evidence
from ._session import (
    Session,
    _apply,
    _envelope_for,
    _new_run_id,
    _next_hint,
    _now,
    _start_command,
    worktree_identity,
)


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
        except UnknownRun:
            session.store.clear_active_if(owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_active_if(owner_id, current)
            else:
                raise CurrentRunExists(current)
    if session.store.get_active(owner_id) != run_id:
        session.store.claim_active(owner_id, run_id)


@dataclass(frozen=True)
class _StartContext:
    session: Session
    cfg: Config
    repo: Path
    intent: str
    base_ref: str
    branch: str
    head_sha: str
    snapshot: dict[str, Any]
    config_digest: str
    merge_base: str | None = None
    changed: list[str] | None = None
    run_id: str | None = None
    wt_path: Path | None = None
    wt_branch: str | None = None
    in_place: bool = False
    reusable: bool = False
    sync_result: syncmod.SyncResult | None = None
    copied: list[str] | None = None
    assessment: RiskAssessment | None = None
    setup_result: dict[str, object] | None = None


def _check_preconditions(
    session: Session, *, base_ref: str | None, intent: str | None
) -> _StartContext:
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

    operation_path: Path | None = None
    if cfg.worktree.mode == "in_place":
        operation_path = repo
    elif cfg.worktree.mode == "reusable":
        reusable_path = session.store.worktrees_dir / "runner"
        if (reusable_path / ".git").is_file():
            operation_path = reusable_path
    if operation_path is not None:
        operation = gitx.operation_in_progress(operation_path)
        if operation is not None:
            raise OperationInProgressError(operation, str(operation_path.resolve()))

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

    return _StartContext(
        session=session,
        cfg=cfg,
        repo=repo,
        intent=intent,
        base_ref=base_ref,
        branch=branch,
        head_sha=head_sha,
        snapshot=snapshot,
        config_digest=resolved_config_digest,
    )


def _resolve_existing_run(ctx: _StartContext, *, replace: bool) -> Envelope | None:
    session = ctx.session

    current = session.store.get_active(session.owner_id)
    if current:
        try:
            existing = session.store.load_run(current)
        except UnknownRun:
            session.store.clear_active_if(session.owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_run(existing.run_id)
            else:
                if existing.source_worktree_id != session.owner_id:
                    raise WrongState(
                        f"run {existing.run_id} belongs to another source worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={"source_worktree_path": existing.source_worktree_path},
                        next_instruction="Run `start` from the recorded source worktree.",
                        next_command="agentic-preflight status",
                    )
                stale = (
                    existing.stale
                    or ctx.head_sha != existing.source_head_sha
                    or ctx.branch != existing.branch
                )
                matches = (
                    not stale
                    and existing.intent == ctx.intent
                    and existing.base_ref == ctx.base_ref
                    and existing.config_digest == ctx.config_digest
                )
                if matches:
                    return _resume_existing(session, existing)
                if stale:
                    _orphan(session, existing, reason="source worktree moved")
                elif replace:
                    _orphan(session, existing, reason="replaced by a new start")
                else:
                    command = _start_command(
                        ctx.intent,
                        base_ref=ctx.base_ref,
                        default_base_ref=ctx.cfg.general.base_ref,
                        replace=True,
                    )
                    raise WrongState(
                        f"run {existing.run_id} is already active in this worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={
                            "existing_intent": existing.intent,
                            "requested_intent": ctx.intent,
                            "source_worktree_path": existing.source_worktree_path,
                        },
                        next_instruction=(
                            "Resume the matching run, or explicitly replace it. Replacement "
                            "orphans the old run without deleting its evidence or fixes."
                        ),
                        next_command=command,
                    )
    return None


def _require_changes(ctx: _StartContext) -> _StartContext:
    try:
        merge_base = gitx.merge_base(ctx.repo, ctx.base_ref, "HEAD")
    except gitx.GitError as exc:
        raise EmptyDiff(f"cannot find a merge base with {ctx.base_ref!r}: {exc}") from exc

    changed = gitx.changed_files(ctx.repo, merge_base, "HEAD")
    if not changed:
        raise EmptyDiff(
            f"{ctx.branch} has no changes over {ctx.base_ref}; there is nothing to review",
            next_instruction="Commit some work on a branch, then start a run.",
        )
    return replace(ctx, merge_base=merge_base, changed=changed)


def _create_run_record(ctx: _StartContext) -> _StartContext:
    merge_base = cast(str, ctx.merge_base)

    run_id = _new_run_id()
    run = RunDoc(
        schema_version=2,
        run_id=run_id,
        state=State.CREATED,
        branch=ctx.branch,
        base_ref=ctx.base_ref,
        merge_base_sha=merge_base,
        head_sha=ctx.head_sha,
        source_head_sha=ctx.head_sha,
        intent=ctx.intent,
        intent_source="user",
        source_worktree_id=ctx.session.owner_id,
        source_worktree_path=str(ctx.repo.resolve()),
        owner_ids=[ctx.session.owner_id],
        config_snapshot=ctx.snapshot,
        config_digest=ctx.config_digest,
        created_at=_now(),
    )
    # Persist the intent *before* the git call, so a crash mid-create leaves a
    # record for `gc` to reconcile rather than an orphan nobody knows about.
    with ctx.session.store.operation(run_id):
        ctx.session.store.create_run(run)
        try:
            ctx.session.store.claim_active(ctx.session.owner_id, run_id)
        except CurrentRunExists as exc:
            with ctx.session.store.transaction(run_id) as doc:
                _apply(doc, Action.ABORT)
                doc.worktree_released = True
            raise WrongState(
                f"run {exc.run_id} is already active in this worktree",
                run_id=exc.run_id,
                next_instruction="Finish, clean up, or abort the active run before starting another.",
                next_command="agentic-preflight status",
            ) from exc
    ctx.session.store.append_event(
        run_id,
        {
            "event": "run_created",
            "head_sha": ctx.head_sha,
            "config_digest": ctx.config_digest,
            "config_snapshot": ctx.snapshot,
            "intent": ctx.intent,
            "intent_source": "user",
        },
    )

    in_place = ctx.cfg.worktree.mode == "in_place"
    reusable = ctx.cfg.worktree.mode == "reusable"
    wt_path = (
        ctx.repo
        if in_place
        else ctx.session.store.worktrees_dir / ("runner" if reusable else run_id)
    )
    wt_branch = ctx.branch if in_place else f"ap/{run_id}"
    return replace(
        ctx,
        run_id=run_id,
        wt_path=wt_path,
        wt_branch=wt_branch,
        in_place=in_place,
        reusable=reusable,
    )


def _provision_validation_checkout(ctx: _StartContext) -> _StartContext:
    run_id = cast(str, ctx.run_id)
    wt_path = cast(Path, ctx.wt_path)
    wt_branch = cast(str, ctx.wt_branch)
    try:
        if ctx.in_place:
            pass
        elif ctx.reusable:
            worktree.acquire_reusable(
                ctx.repo,
                path=wt_path,
                branch=wt_branch,
                head_sha=ctx.head_sha,
            )
        else:  # strict
            retained_runner = ctx.session.store.worktrees_dir / "runner"
            if retained_runner.exists():
                if gitx.current_branch(retained_runner) != "HEAD":
                    raise worktree.WorktreeError(
                        f"cannot enter strict mode: reusable runner {retained_runner} "
                        "is still leased; recover or release its run first"
                    )
                worktree.remove(ctx.repo, retained_runner)
            worktree.create(
                ctx.repo,
                path=wt_path,
                branch=wt_branch,
                head_sha=ctx.head_sha,
            )
    except worktree.WorktreeError as exc:
        with ctx.session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        ctx.session.store.clear_run(run_id)
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
    with ctx.session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
    try:
        _claim_alias(ctx.session, validation_owner, run_id)
    except CurrentRunExists as exc:
        with ctx.session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = False
        ctx.session.store.clear_run(run_id)
        raise NeedsHuman(
            f"validation worktree is still owned by run {exc.run_id}",
            run_id=run_id,
            data={"worktree_path": str(wt_path), "conflicting_run_id": exc.run_id},
            next_instruction="Inspect both runs before reclaiming the validation checkout.",
            next_command="agentic-preflight status --all",
        ) from exc

    with ctx.session.store.transaction(run_id) as doc:
        if validation_owner not in doc.owner_ids:
            doc.owner_ids.append(validation_owner)
        _apply(doc, Action.CREATE_WORKTREE)
        _apply(doc, Action.BEGIN_SYNC)
    return ctx


def _synchronize(ctx: _StartContext) -> _StartContext:
    run_id = cast(str, ctx.run_id)
    wt_path = cast(Path, ctx.wt_path)
    wt_branch = cast(str, ctx.wt_branch)
    try:
        with ctx.session.store.resource("sync"), ctx.session.store.resource("notes"):
            sync_result = syncmod.synchronize(ctx.repo, wt_path, base_ref=ctx.base_ref)
    except syncmod.OperationInProgress as exc:
        with ctx.session.store.transaction(run_id) as doc:
            _apply(doc, Action.SYNC_FAILED)
            run = doc
        report: dict[str, object] = {"operation": exc.operation, "path": exc.path}
        ctx.session.store.append_event(run_id, {"event": "sync_refused", **report})
        raise OperationInProgressError(
            exc.operation,
            exc.path,
            state=run.state.value,
            run_id=run_id,
        ) from exc
    except syncmod.SyncConflict as exc:
        with ctx.session.store.transaction(run_id) as doc:
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
        ctx.session.store.append_event(run_id, {"event": "sync_conflict", **report})
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
        if ctx.in_place:
            pass
        elif ctx.reusable:
            worktree.release_reusable(ctx.repo, wt_path, branch=wt_branch, copied_files=[])
        else:
            worktree.remove(ctx.repo, wt_path, branch=wt_branch)
        with ctx.session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        ctx.session.store.clear_run(run_id)
        raise EmptyDiff(
            "the branch has no changes after synchronizing with the fresh remote base",
            next_instruction="The requested change is already present upstream.",
        )
    return replace(ctx, changed=changed, sync_result=sync_result)


def _run_setup_command(ctx: _StartContext) -> _StartContext:
    run_id = cast(str, ctx.run_id)
    wt_path = cast(Path, ctx.wt_path)
    sync_result = cast(syncmod.SyncResult, ctx.sync_result)
    changed = cast(list[str], ctx.changed)
    copied = (
        worktree.protect_in_place_files(ctx.repo, ctx.cfg.worktree.copy_files)
        if ctx.in_place
        else worktree.copy_files(ctx.repo, wt_path, ctx.cfg.worktree.copy_files)
    )
    assessment = risk.assess(
        changed,
        [],
        policy=ctx.cfg.policy,
        review_blocking_severities=ctx.cfg.review.blocking_severities,
        docs_blocking_severities=ctx.cfg.docs.blocking_severities,
    )

    # Persist copied paths before setup so an abort after a failed command still
    # removes secret-bearing copies from a reusable runner.
    with ctx.session.store.transaction(run_id) as doc:
        doc.copied_files = copied

    setup_result = None
    if ctx.cfg.worktree.setup_command:
        completed = worktree.run_setup(
            wt_path,
            ctx.cfg.worktree.setup_command,
            timeout_seconds=ctx.cfg.stage.timeout_seconds,
        )
        setup_result = {
            "kind": "custom",
            "command": ctx.cfg.worktree.setup_command,
            "exit_code": completed.returncode,
        }
        if completed.returncode != 0:
            failure = SetupFailure(
                scope="initial",
                command=ctx.cfg.worktree.setup_command,
                exit_code=completed.returncode,
                worktree_path=str(wt_path),
                next_instruction=(
                    "Fix the setup command or its environment, then abort this run and "
                    "start a fresh one. The active run keeps its configuration snapshot."
                ),
                next_command="agentic-preflight abort --force",
            )
            with ctx.session.store.transaction(run_id) as doc:
                doc.head_sha = sync_result.head_after
                doc.source_head_sha = sync_result.head_after if ctx.in_place else ctx.head_sha
                doc.merge_base_sha = sync_result.base_sha
                doc.sync_base_sha = sync_result.base_sha
                doc.sync_base_ref = sync_result.base_ref
                doc.sync_remote = sync_result.remote
                doc.changed_files = changed
                doc.risk = assessment
                doc.setup_failure = failure
                _apply(doc, Action.SETUP_FAILED)
            ctx.session.store.append_event(
                run_id,
                {"event": "setup_failed", **failure.model_dump(mode="json")},
            )
            raise SetupFailed(
                f"the setup command failed (exit {completed.returncode})",
                state=State.SETUP_FAILED.value,
                run_id=run_id,
                stage="setup",
                data={"worktree_path": str(wt_path), "setup": setup_result},
                next_instruction=failure.next_instruction,
                next_command=failure.next_command,
            )
    return replace(
        ctx,
        copied=copied,
        assessment=assessment,
        setup_result=setup_result,
    )


def _prime_review(ctx: _StartContext) -> Envelope:
    run_id = cast(str, ctx.run_id)
    wt_path = cast(Path, ctx.wt_path)
    wt_branch = cast(str, ctx.wt_branch)
    sync_result = cast(syncmod.SyncResult, ctx.sync_result)
    changed = cast(list[str], ctx.changed)
    copied = cast(list[str], ctx.copied)
    assessment = cast(RiskAssessment, ctx.assessment)
    with ctx.session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        doc.head_sha = sync_result.head_after
        doc.source_head_sha = sync_result.head_after if ctx.in_place else ctx.head_sha
        doc.merge_base_sha = sync_result.base_sha
        doc.sync_base_sha = sync_result.base_sha
        doc.sync_base_ref = sync_result.base_ref
        doc.sync_remote = sync_result.remote
        doc.changed_files = changed
        doc.risk = assessment
        _apply(doc, Action.SYNC_PASSED)
        _apply(doc, Action.BEGIN_REVIEW)
        run = doc

    ctx.session.store.append_event(
        run_id,
        {
            "event": "worktree_ready",
            "path": str(wt_path),
            "mode": ctx.cfg.worktree.mode,
            "sync": sync_result.as_dict(),
            "risk": assessment.model_dump(mode="json"),
        },
    )

    run = evidence.advance(ctx.session, evidence.discover(ctx.session, run))

    return _envelope_for(
        run,
        next_instruction="Fetch the diff before judging it."
        if run.state is State.REVIEW_AWAITING_FINDINGS
        else _next_hint(run.state)[0],
        next_command="agentic-preflight context"
        if run.state is State.REVIEW_AWAITING_FINDINGS
        else _next_hint(run.state)[1],
        data={
            "worktree_path": str(wt_path),
            "worktree_branch": wt_branch,
            "worktree_mode": ctx.cfg.worktree.mode,
            "branch": ctx.branch,
            "base_ref": ctx.base_ref,
            "head_sha": sync_result.head_after,
            "source_head_sha": ctx.head_sha,
            "merge_base_sha": sync_result.base_sha,
            "intent": ctx.intent,
            "intent_source": "user",
            "sync": sync_result.as_dict(),
            "changed_files": changed,
            "risk": assessment.model_dump(mode="json"),
            # Names only. Contents are never read, logged, or echoed.
            "copied_files": copied,
            "setup": ctx.setup_result,
            "applicability": {
                stage.value: value.model_dump(mode="json")
                for stage, value in run.applicability.items()
            },
        },
    )


def start(
    session: Session,
    *,
    base_ref: str | None = None,
    intent: str | None = None,
    replace: bool = False,
) -> Envelope:
    ctx = _check_preconditions(session, base_ref=base_ref, intent=intent)
    existing = _resolve_existing_run(ctx, replace=replace)
    if existing is not None:
        return existing
    ctx = _require_changes(ctx)
    ctx = _create_run_record(ctx)
    ctx = _provision_validation_checkout(ctx)
    ctx = _synchronize(ctx)
    ctx = _run_setup_command(ctx)
    return _prime_review(ctx)
