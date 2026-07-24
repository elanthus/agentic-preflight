"""Run creation, synchronization, and validation-checkout setup."""

from __future__ import annotations

import hashlib
import json

from .. import dependencies as dependenciesmod
from .. import gitx, runtime, worktree
from .. import sync as syncmod
from ..config import load_config
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    EmptyDiff,
    IntentRequired,
    NeedsHuman,
    SyncConflictError,
    WrongState,
)
from ..machine import Action, State
from ..models import RunDoc
from ..store import CurrentRunExists
from ._session import Session, _apply, _envelope_for, _new_run_id, _now


def start(
    session: Session,
    *,
    base_ref: str | None = None,
    intent: str | None = None,
) -> Envelope:
    repo = session.repo_root
    # Starting is the one command that deliberately reads the working copy.
    # Every later command uses the snapshot persisted below.
    cfg = load_config(repo)
    session.config = cfg
    if cfg.worktree.mode != "in_place":
        session.store.set_worktrees_root(worktree.resolve_root(repo, cfg.worktree.root))

    current = session.store.get_current()
    if current:
        raise WrongState(
            f"run {current} is already active; the validation runner has a single lease",
            run_id=current,
            next_instruction="Finish, clean up, or abort the active run before starting another.",
            next_command="agentic-preflight status",
        )

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
    snapshot = cfg.model_dump(mode="json")
    config_digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
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
        config_snapshot=snapshot,
        config_digest=config_digest,
        created_at=_now(),
    )
    # Persist the intent *before* the git call, so a crash mid-create leaves a
    # record for `gc` to reconcile rather than an orphan nobody knows about.
    session.store.create_run(run)
    try:
        session.store.claim_current(run_id)
    except CurrentRunExists as exc:
        with session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        raise WrongState(
            f"run {exc.run_id} is already active; the validation runner has a single lease",
            run_id=exc.run_id,
            next_instruction="Finish, clean up, or abort the active run before starting another.",
            next_command="agentic-preflight status",
        ) from exc
    session.store.append_event(
        run_id,
        {
            "event": "run_created",
            "head_sha": head_sha,
            "config_digest": config_digest,
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
                session.store.runner_dependency_state_path.unlink(missing_ok=True)
            worktree.create(repo, path=wt_path, branch=wt_branch, head_sha=head_sha)
    except worktree.WorktreeError as exc:
        with session.store.transaction(run_id) as doc:
            _apply(doc, Action.ABORT)
            doc.worktree_released = True
        session.store.clear_current_if(run_id)
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

    with session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        _apply(doc, Action.CREATE_WORKTREE)
        _apply(doc, Action.BEGIN_SYNC)

    try:
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
        session.store.set_current(None)
        raise EmptyDiff(
            "the branch has no changes after synchronizing with the fresh remote base",
            next_instruction="The requested change is already present upstream.",
        )

    copied = (
        worktree.protect_in_place_files(repo, cfg.worktree.copy_files)
        if in_place
        else worktree.copy_files(repo, wt_path, cfg.worktree.copy_files)
    )

    setup_result = None
    if cfg.worktree.setup_command:
        prepared = runtime.prepare_command(
            wt_path,
            cfg.worktree.setup_command,
            manager=cfg.runtime.manager,
            strict=cfg.runtime.strict,
        )
        completed = worktree.run_setup(
            wt_path, prepared.command, timeout_seconds=cfg.stage.timeout_seconds
        )
        setup_result = {
            "kind": "custom",
            "command": cfg.worktree.setup_command,
            "exit_code": completed.returncode,
            "runtime": prepared.runtime.as_dict(),
        }
    elif cfg.worktree.dependency_setup == "auto" and in_place:
        setup_result = {
            "kind": "dependencies",
            "manager": "checkout",
            "action": "reuse",
            "command": None,
            "reason": "in-place mode uses the checkout's existing dependency environment",
            "node": None,
            "exit_code": 0,
            "fingerprint": None,
        }
    elif cfg.worktree.dependency_setup == "auto":
        setup_result = {
            "kind": "dependencies",
            **dependenciesmod.setup(
                wt_path,
                cache_state_path=(session.store.runner_dependency_state_path if reusable else None),
                runtime_manager=cfg.runtime.manager,
                runtime_strict=cfg.runtime.strict,
                timeout_seconds=cfg.stage.timeout_seconds,
            ).as_dict(),
        }

    with session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        doc.copied_files = copied
        doc.head_sha = sync_result.head_after
        doc.source_head_sha = sync_result.head_after if in_place else head_sha
        doc.merge_base_sha = sync_result.base_sha
        doc.sync_base_sha = sync_result.base_sha
        doc.sync_base_ref = sync_result.base_ref
        doc.sync_remote = sync_result.remote
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
            # Names only. Contents are never read, logged, or echoed.
            "copied_files": copied,
            "setup": setup_result,
        },
    )
