"""Merge verified fixes back to the source branch."""

from __future__ import annotations

from .. import attestation as attestationmod
from .. import gitx, public_config, timing, worktree
from .. import mergeback as mergebackmod
from .. import sync as syncmod
from ..cli_contract import MergebackData
from ..envelope import Envelope, ExitCode
from ..errors import (
    AgenticError,
    DirtyTree,
    MergebackConflictError,
    NeedsHuman,
    StaleRun,
)
from ..errors import (
    OperationInProgress as OperationInProgressError,
)
from ..machine import Action, State
from ..models import MergebackAttempt, RunDoc, Stage
from . import evidence
from ._session import (
    Session,
    apply_action,
    assert_fresh,
    assess_risk,
    check_restart_limit,
    envelope_for,
    is_in_place,
    load_current,
    require_state,
    require_worktree,
    worktree_mode,
)
from .fix_commits import derive_fix_commits
from .review_coverage import invalidate_stage_result, reopen_if_stale


def _reject_pending(session: Session, run: RunDoc, message: str) -> None:
    with session.store.transaction(run.run_id) as doc:
        doc.stale = True
    raise StaleRun(
        message,
        state=run.state.value,
        run_id=run.run_id,
        next_command="agentic-preflight status",
    )


def _reconcile_attempt(
    session: Session, run: RunDoc
) -> tuple[bool, mergebackmod.MergebackResult | None]:
    """Reconcile a pending attempt only against its recorded validation snapshot."""
    attempt = run.mergeback_attempt
    if run.state is not State.MERGEBACK_PENDING or attempt is None:
        return False, None
    wt = require_worktree(run)
    repo = session.repo_root
    if (
        run.stale
        or gitx.current_branch(repo) != run.branch
        or gitx.rev_parse(wt, "HEAD") != attempt.validation_sha
        or gitx.tree_sha(wt) != attempt.validation_tree
        or not gitx.is_clean(wt)
    ):
        _reject_pending(
            session,
            run,
            "pending mergeback no longer matches its recorded branch or validation snapshot",
        )
    current = gitx.rev_parse(repo, "HEAD")
    if current == attempt.source_sha:
        return True, None
    current_tree = gitx.tree_sha(repo)
    on_base = gitx.is_ancestor(repo, run.sync_base_sha or run.merge_base_sha, current)
    if current_tree != attempt.validation_tree or not on_base:
        _reject_pending(
            session,
            run,
            "source moved during pending mergeback without reaching the recorded verified tree",
        )
    return True, mergebackmod.MergebackResult(
        pre_sha=attempt.source_sha,
        post_sha=current,
        applied=list(run.fix_commits),
        local_tree_sha=attempt.validation_tree,
        worktree_tree_sha=attempt.validation_tree,
    )


def _reset_non_equivalent_merge_to_review(
    session: Session,
    run: RunDoc,
    result: mergebackmod.MergebackResult,
) -> RunDoc:
    """Make a human-resolved source tree the new isolated review snapshot."""
    worktree_path = require_worktree(run)
    worktree.assert_diff_is_clean_of(
        session.repo_root,
        run.merge_base_sha,
        result.post_sha,
        run.copied_files,
    )
    worktree.assert_paths_are_ignored(session.repo_root, run.copied_files)
    gitx.run(worktree_path, "reset", "--hard", result.post_sha)
    changed = gitx.changed_files(worktree_path, run.merge_base_sha, "HEAD")
    assessment = assess_risk(session, changed, session.store.load_findings(run.run_id))
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
        apply_action(doc, Action.INVALIDATE_REVIEW)
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
    check_restart_limit(run)
    return run


def _register_validated_history(session: Session, run: RunDoc) -> RunDoc:
    """Make the replay list the history review, lint, and test validated.

    At ``TEST_GREEN`` the review coverage names the validation checkout's current
    head, so ``head_sha..HEAD`` is exactly what was validated, including commits
    made after the last ``respond`` or never cited by one.
    """
    derived = derive_fix_commits(session, run)
    if derived == run.fix_commits:
        return run
    with session.store.transaction(run.run_id) as doc:
        recorded = list(doc.fix_commits)
        doc.fix_commits = derived
        run = doc
    session.store.append_event(
        run.run_id,
        {"event": "validated_fix_commits_registered", "recorded": recorded, "commits": derived},
    )
    return run


def _check_dirty_paths(session: Session, run: RunDoc, *, in_place: bool) -> None:
    """Reject only dirt that the selected mergeback mode cannot preserve."""
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
        return
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


def _record_attempt(session: Session, run: RunDoc, *, retrying_conflict: bool) -> RunDoc:
    """Record the exact source and validation snapshots before mutation."""
    if run.state not in {State.TEST_GREEN, State.MERGEBACK_CONFLICT}:
        return run
    repo = session.repo_root
    if gitx.current_branch(repo) != run.branch:
        raise StaleRun("mergeback requires the recorded source branch")
    source_sha = gitx.rev_parse(repo, "HEAD")
    attempt = MergebackAttempt(
        source_sha=source_sha,
        validation_sha=gitx.rev_parse(require_worktree(run), "HEAD"),
        validation_tree=gitx.tree_sha(require_worktree(run)),
        retrying_conflict=retrying_conflict,
    )
    with session.store.transaction(run.run_id) as doc:
        doc.mergeback_attempt = attempt
        apply_action(doc, Action.MERGEBACK_RETRY if retrying_conflict else Action.BEGIN_MERGEBACK)
        return doc


class FixesNotLanded(AgenticError):
    """A conflict retry found recorded fix commits that never reached the branch."""

    code = "fixes_not_landed"
    exit_code = ExitCode.NEEDS_HUMAN


def _perform_merge(
    session: Session,
    run: RunDoc,
    *,
    in_place: bool,
    retrying_conflict: bool,
    recovered: mergebackmod.MergebackResult | None,
) -> mergebackmod.MergebackResult:
    """Produce the source result from recovery, in-place validation, or isolated fixes."""
    repo = session.repo_root
    if recovered is not None:
        return recovered
    if in_place:
        current = gitx.rev_parse(repo, "HEAD")
        tree = gitx.tree_sha(repo, "HEAD")
        return mergebackmod.MergebackResult(
            pre_sha=current,
            post_sha=current,
            applied=[],
            local_tree_sha=tree,
            worktree_tree_sha=tree,
        )
    worktree_path = require_worktree(run)
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
                "the source worktree must be clean before synchronizing with the recorded base",
                state=run.state.value,
                run_id=run.run_id,
                next_instruction="Commit or stash the changes, then retry mergeback.",
                next_command="git status",
            )
        syncmod.sync_onto(repo, sync_base)
    local_tree = gitx.tree_sha(repo)
    verified_tree = gitx.tree_sha(worktree_path)
    branch_moved = gitx.rev_parse(repo, "HEAD") != run.source_head_sha
    # An unchanged branch whose tree already matches is only acceptable when no
    # fix commits were recorded; otherwise the recorded fixes never landed.
    if retrying_conflict and local_tree == verified_tree and run.fix_commits and not branch_moved:
        raise FixesNotLanded(
            "the recorded fix commits never landed on the source branch, yet its tree "
            "already matches the validation worktree; the fixes change nothing and the "
            "findings they claim to fix were not repaired",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=("Abort this run and start a new one so the branch is revalidated."),
            next_command="agentic-preflight abort",
        )
    tree_equal = local_tree == verified_tree and not run.fix_commits
    if retrying_conflict and (tree_equal or branch_moved):
        return mergebackmod.MergebackResult(
            pre_sha=gitx.rev_parse(repo, "HEAD"),
            post_sha=gitx.rev_parse(repo, "HEAD"),
            applied=[],
            local_tree_sha=local_tree,
            worktree_tree_sha=verified_tree,
        )
    return mergebackmod.cherry_pick_fixes(
        repo,
        run.fix_commits,
        worktree_branch=worktree_branch,
        worktree_path=worktree_path,
    )


def require_known_provenance(run: RunDoc, *, command: str) -> None:
    """Refuse to attest or publish a run whose user-level configuration is unknown.

    A run that an earlier release started did not record which snapshot sections
    came from the user file, so its note could publish them. ``mergeback`` and the
    publication commands check this before they change or push anything.
    """
    if public_config.provenance_known(run):
        return
    if command == "mergeback":
        situation = "its attestation could publish user-level configuration"
    else:
        situation = (
            f"the note on {run.head_sha} may hold user-level configuration; "
            f"remove it with `git notes --ref={attestationmod.NOTES_REF} remove "
            f"{run.head_sha}` instead of pushing it"
        )
    keep = (
        " Keep the recorded fix commits in data.fix_commits by cherry-picking them onto "
        "your branch before you abort, because `abort --force` discards them."
        if run.fix_commits
        else ""
    )
    raise NeedsHuman(
        "this run was started by an earlier release that did not record which "
        f"configuration came from the user file, so {situation}",
        state=run.state.value,
        run_id=run.run_id,
        data={"reason": "unknown_config_provenance", "fix_commits": list(run.fix_commits)},
        next_instruction=(
            f"Do not retry `{command}`. Abort this run and start a fresh one with the "
            f"current release.{keep}"
        ),
        next_command="agentic-preflight abort",
    )


def mergeback(session: Session) -> Envelope:
    """Attest in-place validation or merge isolated fixes onto the source branch."""
    run = load_current(session)
    in_place = is_in_place(run)
    repo = session.repo_root
    operation = gitx.operation_in_progress(repo)
    if operation is not None:
        raise OperationInProgressError(
            operation,
            str(repo),
            state=run.state.value,
            run_id=run.run_id,
        )
    require_known_provenance(run, command="mergeback")

    retrying_conflict = run.state is State.MERGEBACK_CONFLICT or (
        run.state is State.MERGEBACK_PENDING
        and run.mergeback_attempt is not None
        and run.mergeback_attempt.retrying_conflict
    )
    reconciled, recovered = _reconcile_attempt(session, run)
    if not retrying_conflict and not reconciled:
        assert_fresh(session, run)
    if run.state is State.TEST_GREEN:
        run = evidence.advance(session, run)
        if run.state is not State.TEST_GREEN:
            return envelope_for(run, data=MergebackData(inputs_invalidated=True))
        run, reopened = reopen_if_stale(session, run)
        if reopened:
            return envelope_for(
                run,
                stage="review",
                data=MergebackData(coverage_invalidated=True),
                next_command="agentic-preflight context",
            )
        if not in_place:
            run = _register_validated_history(session, run)
    require_state(
        run,
        State.TEST_GREEN,
        State.MERGEBACK_PENDING,
        State.MERGEBACK_CONFLICT,
        command="mergeback",
    )

    # Validate saved origins before recording an attempt or changing the source branch.
    run = evidence.archive(session, run)
    _check_dirty_paths(session, run, in_place=in_place)
    run = _record_attempt(session, run, retrying_conflict=retrying_conflict)

    try:
        with timing.timed_command(session.store, run.run_id, "mergeback"):
            result = _perform_merge(
                session,
                run,
                in_place=in_place,
                retrying_conflict=retrying_conflict,
                recovered=recovered,
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
            apply_action(doc, Action.MERGEBACK_FAILED)
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
        # The schema models this as an open string-to-count summary.
        summary[finding.severity.value] = summary.get(finding.severity.value, 0) + 1

    if not result.tree_equivalent:
        run = _reset_non_equivalent_merge_to_review(session, run, result)
        return envelope_for(
            run,
            stage="review",
            data=MergebackData.model_validate(
                {
                    **result.as_dict(),
                    "worktree_mode": worktree_mode(run),
                    "validation_restarted": True,
                }
            ),
            next_instruction=(
                "The human-resolved merge tree differs from the verified tree. The "
                "validation checkout now matches that resolution; review the complete "
                "current diff and rerun every applicable stage."
            ),
            next_command="agentic-preflight context",
        )

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
        apply_action(doc, Action.MERGEBACK_OK)
        run = doc

    session.store.append_event(
        run.run_id,
        {
            "event": "mergeback_ok",
            "post_sha": result.post_sha,
            "tree_equivalent": result.tree_equivalent,
        },
    )

    return envelope_for(
        run,
        data=MergebackData.model_validate(
            {**result.as_dict(), "worktree_mode": worktree_mode(run)}
        ),
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
