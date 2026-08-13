"""Run inspection, abortion, and garbage collection."""

from __future__ import annotations

import shlex
from pathlib import Path

from .. import gitx
from .. import risk as riskmod
from ..envelope import Envelope
from ..errors import (
    START_COMMAND,
    UnmergedWork,
)
from ..machine import Action, State
from ..models import FindingStatus, RunDoc
from ._session import (
    Session,
    _apply,
    _envelope_for,
    _head_moved,
    _is_in_place,
    _load_current,
    _release_run_worktree,
    _worktree_mode,
)


def events(session: Session, *, limit: int | None = None) -> Envelope:
    run = _load_current(session)
    history = session.store.load_events(run.run_id)
    if limit:
        history = history[-limit:]
    return _envelope_for(run, data={"events": history, "count": len(history)})


def abort(session: Session, *, force: bool = False) -> Envelope:
    """End the run and reclaim its worktree.

    Unmerged fix commits are reported rather than discarded: the agent may have
    done real work in there, and silently deleting it is the one outcome nobody
    can undo.
    """
    run = _load_current(session)

    isolated = not _is_in_place(run, session.config)
    if isolated and run.fix_commits and not force:
        raise UnmergedWork(
            f"this run has {len(run.fix_commits)} fix commit(s) that were never merged "
            f"back: {', '.join(run.fix_commits)}",
            state=run.state.value,
            run_id=run.run_id,
            data={"fix_commits": run.fix_commits, "worktree_path": run.worktree_path},
            next_instruction=(
                "Those commits exist only in the worktree. Cherry-pick anything worth "
                "keeping, then abort again with --force to discard the rest."
            ),
            next_command="agentic-preflight abort --force",
        )

    _release_run_worktree(session, run)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.ABORT)
        doc.worktree_released = True
        run = doc

    session.store.set_current(None)
    session.store.append_event(run.run_id, {"event": "aborted", "forced": force})

    return _envelope_for(
        run,
        data={
            "discarded_fix_commits": run.fix_commits if isolated and force else [],
            "preserved_in_place_commits": run.fix_commits if not isolated else [],
        },
        next_instruction="Run aborted. Start a fresh one when ready.",
        next_command=shlex.join(
            [
                "agentic-preflight",
                "start",
                "--intent",
                run.intent or "<objective and acceptance criteria>",
            ]
        ),
    )


def gc(session: Session, *, force: bool = False) -> Envelope:
    """Reconcile three sources of truth: run dirs, git worktrees, and ap/* branches.

    Anything still holding unmerged fix commits is *reported*, never removed
    without ``--force``. Reclaiming disk is not worth destroying work.
    """
    store = session.store
    repo = session.repo_root

    known_runs = set(store.list_runs())
    live_worktrees = {
        record["branch"].removeprefix("refs/heads/ap/"): record["worktree"]
        for record in gitx.list_worktrees(repo)
        if "worktree" in record and record.get("branch", "").startswith("refs/heads/ap/")
    }
    ac_branches = {
        line.strip().lstrip("* ").strip()
        for line in gitx.out(repo, "branch", "--list", "ap/*").splitlines()
        if line.strip()
    }

    removed: list[str] = []
    retained: list[dict] = []
    orphans: list[str] = []

    for run_id in sorted(known_runs):
        run = store.load_run(run_id)
        terminal = run.state in (State.ABORTED, State.DONE, State.ORPHANED)
        if not terminal:
            # An active run is never a reclamation candidate, but one holding
            # fix commits is worth surfacing so it is not forgotten about.
            if run.fix_commits:
                retained.append(
                    {
                        "run_id": run_id,
                        "reason": "run still active with unmerged fix commits",
                        "fix_commits": run.fix_commits,
                    }
                )
            continue
        if run.fix_commits and not force and not _is_in_place(run, session.config):
            # Only DONE proves mergeback and publication completed. Aborted or
            # orphaned runs must retain every fix even if an unrelated commit
            # in branch history happens to share its patch ID.
            unlanded = (
                _unlanded_fix_commits(repo, run)
                if run.state is State.DONE
                else list(run.fix_commits)
            )
            if unlanded:
                retained.append(
                    {
                        "run_id": run_id,
                        "reason": "unmerged fix commits",
                        "fix_commits": unlanded,
                    }
                )
                continue
        if not run.worktree_released and run.worktree_path and Path(run.worktree_path).exists():
            _release_run_worktree(session, run)
            with store.transaction(run_id) as doc:
                doc.worktree_released = True
        removed.append(run_id)

    # A worktree or branch git knows about but the store does not: reconcile by
    # reporting, so a half-created run is visible rather than silently leaked.
    orphans.extend(name for name in live_worktrees if name not in known_runs)
    for branch in ac_branches:
        run_id = branch.removeprefix("ap/")
        if run_id not in known_runs and run_id not in orphans:
            orphans.append(run_id)

    current = store.get_current()
    if current and current not in known_runs:
        store.set_current(None)

    return Envelope(
        data={
            "removed": removed,
            "retained": retained,
            "orphans": orphans,
            "runs_known": sorted(known_runs),
        },
        next_instruction=("Orphans were found; inspect them before removing." if orphans else None),
        next_command="agentic-preflight gc --force" if orphans and not force else None,
    )


def _unlanded_fix_commits(repo: Path, run: RunDoc) -> list[str]:
    """Return fixes with no patch-equivalent commit in merged run history.

    Mergeback cherry-picks, so source SHA ancestry cannot prove that a fix
    landed. The run's post-mergeback ``head_sha`` is durable even after its
    branch moves; compare stable patch IDs within that reviewed history.
    """
    try:
        candidates = gitx.commits_between(repo, run.merge_base_sha, run.head_sha)
        landed_patch_ids = {
            patch_id
            for sha in candidates
            if (patch_id := gitx.commit_patch_id(repo, sha)) is not None
        }
        return [
            sha
            for sha in run.fix_commits
            if (patch_id := gitx.commit_patch_id(repo, sha)) is None
            or patch_id not in landed_patch_ids
        ]
    except gitx.GitError:
        # Missing or corrupt history is not permission to destroy the only
        # remaining ref to a fix. Report it as unmerged and require --force.
        return list(run.fix_commits)


def status(session: Session) -> Envelope:
    """Legal in every state, and the universal recovery entry point.

    Deliberately never raises for a stale or wedged run: if `status` could fail,
    an agent that had wandered off the path would have nowhere to go.
    """
    run_id = session.store.get_current()
    if not run_id:
        return Envelope(
            data={"has_run": False},
            next_instruction="No run is active. Start one.",
            next_command=START_COMMAND,
        )

    try:
        run = session.store.load_run(run_id)
    except Exception:  # noqa: BLE001 - status is the recovery path for corrupt state
        return Envelope(
            data={"has_run": False, "dangling_run_id": run_id},
            next_instruction="The recorded run is missing. Start a fresh one.",
            next_command=START_COMMAND,
        )

    findings = session.store.load_findings(run_id)
    summary = {status.value: 0 for status in FindingStatus}
    for finding in findings:
        summary[finding.status.value] += 1
    assessment = riskmod.assess(
        run.changed_files,
        findings,
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )

    tip = _head_moved(session, run)
    stale = run.state is not State.MERGEBACK_CONFLICT and (run.stale or tip is not None)

    envelope = _envelope_for(
        run,
        data={
            "has_run": True,
            "seq": run.seq,
            "branch": run.branch,
            "base_ref": run.base_ref,
            "head_sha": run.head_sha,
            "source_head_sha": run.source_head_sha,
            "intent": run.intent,
            "intent_source": run.intent_source,
            "sync": {
                "remote": run.sync_remote,
                "base_ref": run.sync_base_ref,
                "base_sha": run.sync_base_sha,
            },
            "current_tip": tip or run.head_sha,
            "stale": stale,
            "worktree_path": run.worktree_path,
            "worktree_branch": run.worktree_branch,
            "worktree_mode": _worktree_mode(run, session.config),
            "worktree_released": run.worktree_released,
            "config_digest": run.config_digest,
            "pr_mode": session.config.pr.mode,
            "approval_mode": session.config.approval.mode,
            "gate_token": run.gate_token,
            "pushed_sha": run.pushed_sha,
            "fix_commits": run.fix_commits,
            "review_coverage": (
                run.review_coverage.summary() if run.review_coverage is not None else None
            ),
            "stages": {
                stage.value: record.model_dump(mode="json") for stage, record in run.stages.items()
            },
            "setup_failure": (
                run.setup_failure.model_dump(mode="json")
                if run.setup_failure is not None
                else None
            ),
            # Names only — contents are never read, logged, or echoed anywhere.
            "copied_files": run.copied_files,
            "findings": [f.model_dump(mode="json") for f in findings],
            "findings_summary": summary,
            "risk": assessment.model_dump(mode="json"),
        },
    )
    if stale:
        envelope.next_instruction = (
            "This run is stale: the branch moved after review began. Abort it to release "
            "the active-run lease; the abort response preserves the intent and returns "
            "the legal fresh-start command."
        )
        envelope.next_command = "agentic-preflight abort --force"
    elif run.setup_failure is not None:
        envelope.next_instruction = run.setup_failure.next_instruction
        envelope.next_command = run.setup_failure.next_command
    elif run.state is State.MERGEBACK_CONFLICT:
        conflict = next(
            (
                event
                for event in reversed(session.store.load_events(run_id))
                if event.get("event") == "mergeback_conflict"
            ),
            None,
        )
        envelope.data["mergeback_conflict"] = conflict
        envelope.next_instruction = (
            "Use the durable conflict report below, resolve the affected paths, "
            "then retry mergeback; completed verification stages are retained."
        )
        envelope.next_command = "agentic-preflight mergeback"
    return envelope
