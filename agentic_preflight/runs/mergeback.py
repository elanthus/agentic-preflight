"""Merge verified fixes back to the source branch."""

from __future__ import annotations

from .. import attestation as attestationmod
from .. import gitx, worktree
from .. import mergeback as mergebackmod
from .. import risk as riskmod
from .. import sync as syncmod
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    MergebackConflictError,
    NeedsHuman,
)
from ..errors import (
    OperationInProgress as OperationInProgressError,
)
from ..machine import Action, State
from ..models import RunDoc, Stage
from . import evidence
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _is_in_place,
    _load_current,
    _require_state,
    _require_worktree,
    _worktree_mode,
)
from .review_coverage import invalidate_stage_result, reopen_if_stale


def _reset_non_equivalent_merge_to_review(
    session: Session,
    run: RunDoc,
    result: mergebackmod.MergebackResult,
) -> RunDoc:
    """Make a human-resolved source tree the new isolated review snapshot."""
    worktree_path = _require_worktree(run)
    worktree.assert_diff_is_clean_of(
        session.repo_root,
        run.merge_base_sha,
        result.post_sha,
        run.copied_files,
    )
    worktree.assert_paths_are_ignored(session.repo_root, run.copied_files)
    gitx.run(worktree_path, "reset", "--hard", result.post_sha)
    changed = gitx.changed_files(worktree_path, run.merge_base_sha, "HEAD")
    assessment = riskmod.assess(
        changed,
        session.store.load_findings(run.run_id),
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )
    with session.store.transaction(run.run_id) as doc:
        doc.head_sha = result.post_sha
        doc.source_head_sha = result.post_sha
        doc.changed_files = changed
        doc.risk = assessment
        doc.fix_commits = []
        doc.review_coverage = None
        invalidate_stage_result(doc, Stage.REVIEW)
        invalidate_stage_result(doc, Stage.LINT)
        invalidate_stage_result(doc, Stage.TEST)
        _apply(doc, Action.INVALIDATE_REVIEW)
        run = doc
    session.store.append_event(
        run.run_id,
        {
            "event": "mergeback_review_reset",
            "post_sha": result.post_sha,
            "verified_tree_sha": result.worktree_tree_sha,
            "resolved_tree_sha": result.local_tree_sha,
        },
    )
    return run


def mergeback(session: Session) -> Envelope:
    """Attest in-place validation or merge isolated fixes onto the source branch."""
    run = _load_current(session)
    in_place = _is_in_place(run, session.config)
    repo = session.repo_root
    operation = gitx.operation_in_progress(repo)
    if operation is not None:
        raise OperationInProgressError(
            operation,
            str(repo),
            state=run.state.value,
            run_id=run.run_id,
        )

    retrying_conflict = run.state is State.MERGEBACK_CONFLICT
    if not retrying_conflict:
        _assert_fresh(session, run)
    if run.state is State.TEST_GREEN:
        run = evidence.advance(session, run)
        if run.state is not State.TEST_GREEN:
            return _envelope_for(run, data={"inputs_invalidated": True})
        run, reopened = reopen_if_stale(session, run)
        if reopened:
            return _envelope_for(
                run,
                stage="review",
                data={"coverage_invalidated": True},
                next_command="agentic-preflight context",
            )
    _require_state(
        run,
        State.TEST_GREEN,
        State.MERGEBACK_PENDING,
        State.MERGEBACK_CONFLICT,
        command="mergeback",
    )

    if in_place:
        if not gitx.is_clean(repo):
            raise DirtyTree(
                "the in-place validation checkout has uncommitted changes",
                state=run.state.value,
                run_id=run.run_id,
                next_instruction="Commit an intended repair and re-run the affected stages.",
                next_command="git status",
            )
    else:
        affected_paths = gitx.changed_paths_in_commits(run.worktree_path or repo, run.fix_commits)
        overlapping = gitx.status_for_paths(repo, affected_paths)
        if overlapping:
            raise DirtyTree(
                "the working tree has changes on paths mergeback would overwrite",
                state=run.state.value,
                run_id=run.run_id,
                data={"affected_paths": affected_paths, "overlapping_status": overlapping},
                next_instruction=(
                    "Commit or stash the reported overlapping paths, then merge back. "
                    "Unrelated tracked and untracked files may remain."
                ),
                next_command="git status",
            )

    if run.state is State.TEST_GREEN:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.BEGIN_MERGEBACK)
            run = doc
    elif run.state is State.MERGEBACK_CONFLICT:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.MERGEBACK_RETRY)
            run = doc

    try:
        if in_place:
            current = gitx.rev_parse(repo, "HEAD")
            tree = gitx.tree_sha(repo, "HEAD")
            result = mergebackmod.MergebackResult(
                pre_sha=current,
                post_sha=current,
                applied=[],
                local_tree_sha=tree,
                worktree_tree_sha=tree,
            )
        else:
            worktree_path = _require_worktree(run)
            worktree_branch = run.worktree_branch
            if worktree_branch is None:
                raise NeedsHuman(
                    "the active run has no validation branch",
                    state=run.state.value,
                    run_id=run.run_id,
                )
            sync_base = run.sync_base_sha or run.merge_base_sha
            if not gitx.is_ancestor(repo, sync_base, "HEAD"):
                if not gitx.is_clean(repo):
                    raise DirtyTree(
                        "the source worktree must be clean before rebasing onto the synchronized base",
                        state=run.state.value,
                        run_id=run.run_id,
                        next_instruction="Commit or stash the changes, then retry mergeback.",
                        next_command="git status",
                    )
                syncmod.rebase_onto(repo, sync_base)
            local_tree = gitx.tree_sha(repo)
            verified_tree = gitx.tree_sha(worktree_path)
            branch_moved = gitx.rev_parse(repo, "HEAD") != (run.source_head_sha or run.head_sha)
            if retrying_conflict and (local_tree == verified_tree or branch_moved):
                result = mergebackmod.MergebackResult(
                    pre_sha=gitx.rev_parse(repo, "HEAD"),
                    post_sha=gitx.rev_parse(repo, "HEAD"),
                    applied=[],
                    local_tree_sha=local_tree,
                    worktree_tree_sha=verified_tree,
                )
            else:
                result = mergebackmod.cherry_pick_fixes(
                    repo,
                    run.fix_commits,
                    worktree_branch=worktree_branch,
                    worktree_path=worktree_path,
                )
    except gitx.OperationInProgress as exc:
        raise OperationInProgressError(
            exc.operation,
            exc.path,
            state=run.state.value,
            run_id=run.run_id,
        ) from exc
    except (mergebackmod.MergebackConflict, syncmod.SyncConflict) as exc:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.MERGEBACK_FAILED)
            run = doc
        report = (
            exc.report.as_dict()
            if isinstance(exc, mergebackmod.MergebackConflict)
            else {
                "mode": "fresh_base_rebase_conflict",
                "base_ref": exc.base_ref,
                "base_sha": exc.base_sha,
                "conflicting_files": exc.conflicting_files,
            }
        )
        session.store.append_event(run.run_id, {"event": "mergeback_conflict", **report})
        raise MergebackConflictError(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
            data=report,
            next_instruction=(
                "Show the user the resolution block verbatim. The fix commits remain "
                "intact; after the reported paths are resolved, `mergeback` can retry "
                "without rerunning the completed stages."
            ),
            next_command="agentic-preflight mergeback",
        ) from exc

    # Green transfers only when the content is provably identical.
    findings = session.store.load_findings(run.run_id)
    summary: dict[str, int] = {}
    for finding in findings:
        summary[finding.status.value] = summary.get(finding.status.value, 0) + 1
        # Severity totals make the attestation sufficient for a trusted merge
        # policy to reconstruct whether review findings raised the final risk.
        # Adding keys is backwards-compatible because the schema already models
        # this as an open string-to-count summary.
        summary[finding.severity.value] = summary.get(finding.severity.value, 0) + 1

    if not result.tree_equivalent:
        run = _reset_non_equivalent_merge_to_review(session, run, result)
        return _envelope_for(
            run,
            stage="review",
            data={
                **result.as_dict(),
                "worktree_mode": _worktree_mode(run, session.config),
                "validation_restarted": True,
            },
            next_instruction=(
                "The human-resolved merge tree differs from the verified tree. The "
                "validation checkout now matches that resolution; review the complete "
                "current diff and rerun every applicable stage."
            ),
            next_command="agentic-preflight context",
        )

    run = evidence.archive(session, run)
    entry = attestationmod.build(
        run,
        sha=result.post_sha,
        tree_sha=result.local_tree_sha,
        docs_enabled=session.config.docs.enabled,
        findings_summary=summary,
    )
    with session.store.resource("notes"):
        attestationmod.write(session.repo_root, entry)

    with session.store.transaction(run.run_id) as doc:
        doc.head_sha = result.post_sha
        doc.source_head_sha = result.post_sha
        _apply(doc, Action.MERGEBACK_OK)
        run = doc

    session.store.append_event(
        run.run_id,
        {
            "event": "mergeback_ok",
            "post_sha": result.post_sha,
            "tree_equivalent": result.tree_equivalent,
        },
    )

    return _envelope_for(
        run, data={**result.as_dict(), "worktree_mode": _worktree_mode(run, session.config)}
    )


def _remote_for(session: Session, run: RunDoc) -> str:
    url = gitx.remote_url(session.repo_root, "origin")
    if not url:
        raise NeedsHuman(
            "no `origin` remote is configured, so there is nowhere to push",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Add a remote, then run the gate again.",
            next_command="git remote add origin <url>",
        )
    return url
