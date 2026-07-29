"""Merge verified fixes back to the source branch."""

from __future__ import annotations

import shlex

from .. import gitx
from .. import ledger as ledgermod
from .. import mergeback as mergebackmod
from .. import sync as syncmod
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    MergebackConflictError,
    NeedsHuman,
)
from ..machine import Action, State
from ..models import RunDoc, Stage
from ..publish import provider as providermod
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


def mergeback(session: Session) -> Envelope:
    """Attest in-place validation or merge isolated fixes onto the source branch."""
    run = _load_current(session)
    in_place = _is_in_place(run, session.config)
    retrying_conflict = run.state is State.MERGEBACK_CONFLICT
    if not retrying_conflict:
        _assert_fresh(session, run)
    _require_state(
        run,
        State.LINT_GREEN,
        State.MERGEBACK_PENDING,
        State.MERGEBACK_CONFLICT,
        command="mergeback",
    )

    repo = session.repo_root
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

    if run.state is State.LINT_GREEN:
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

    if result.tree_equivalent:
        stages_recorded = {stage: record.status for stage, record in run.stages.items()}
        stages_recorded[Stage.REVIEW] = "green"
        if session.config.docs.enabled:
            stages_recorded[Stage.DOCS] = "green"
        entry = ledgermod.build_entry(
            run,
            sha=result.post_sha,
            tree_sha=result.local_tree_sha,
            stages=stages_recorded,
            findings_summary=summary,
        )
        session.store.save_ledger(ledgermod.record(session.store.load_ledger(), entry))

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

    envelope = _envelope_for(
        run, data={**result.as_dict(), "worktree_mode": _worktree_mode(run, session.config)}
    )
    if not result.tree_equivalent:
        envelope.next_instruction = (
            "The merged tree does not match what was verified, so green did not "
            "transfer. Start a fresh run against the new tip."
        )
        envelope.next_command = shlex.join(
            [
                "agentic-preflight",
                "start",
                "--intent",
                run.intent or "<objective and acceptance criteria>",
            ]
        )
    return envelope


def _remote_for(session: Session, run: RunDoc):
    url = gitx.remote_url(session.repo_root, "origin")
    if not url:
        raise NeedsHuman(
            "no `origin` remote is configured, so there is nowhere to push",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Add a remote, then run the gate again.",
            next_command="git remote add origin <url>",
        )
    return providermod.parse_remote(url)
