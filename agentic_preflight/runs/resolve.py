"""Finding response and fix-commit verification."""

from __future__ import annotations

import shlex

from .. import findings as findingsmod
from .. import gitx, worktree
from .. import risk as riskmod
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    InvalidResponse,
    StaleRun,
    UnknownFinding,
)
from ..machine import Action, State
from ..models import FindingStatus, RunDoc, Stage
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _is_in_place,
    _load_current,
    _require_finding_stage,
    _require_state,
    _require_worktree,
)

RESPONSE_ACTIONS = ("fixed", "dismissed", "accepted")


def respond(
    session: Session,
    *,
    finding_id: str,
    action: str,
    commit: str | None = None,
    note: str | None = None,
) -> Envelope:
    """Record the agent's resolution of one finding, verifying what it claims.

    "I fixed F003 in abc123" is an assertion, and three things can be wrong with
    it: the commit may not exist, it may not touch the file the finding was
    about, or it may carry a copied environment file. All three are cheap to
    check and expensive to miss, so none of them are taken on trust.
    """
    run = _load_current(session)
    accepting_in_place_fix = (
        _is_in_place(run, session.config) and action == "fixed" and commit is not None
    )
    if not accepting_in_place_fix:
        _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_BLOCKED,
        State.DOCS_BLOCKED,
        command="respond",
    )

    stored = session.store.load_findings(run.run_id)
    target = next((f for f in stored if f.id == finding_id), None)
    if target is None:
        raise UnknownFinding(
            f"no finding {finding_id!r} in this run; valid ids: "
            f"{[f.id for f in stored] or '(none)'}",
            state=run.state.value,
            run_id=run.run_id,
        )

    if target.status is not FindingStatus.OPEN:
        raise InvalidResponse(
            f"{finding_id} is already {target.status.value}; each finding is resolved once",
            state=run.state.value,
            run_id=run.run_id,
        )

    if action == "fixed":
        if not commit:
            raise InvalidResponse(
                f"resolving {finding_id} as fixed requires --commit <sha> naming the "
                f"commit that fixes it",
                state=run.state.value,
                run_id=run.run_id,
            )
        commit = _verify_fix_commit(session, run, target, commit)
    elif not note:
        raise InvalidResponse(
            f"resolving {finding_id} as {action} requires --note explaining why; "
            f"an unexplained dismissal is indistinguishable from an oversight",
            state=run.state.value,
            run_id=run.run_id,
        )

    new_commits = (
        _in_place_fix_commits(session, run, commit)
        if accepting_in_place_fix and commit
        else ([commit] if commit else [])
    )

    target.status = FindingStatus(action)
    target.fix_commit = commit
    target.response_note = note
    changed_files = gitx.changed_files(
        _require_worktree(run),
        run.merge_base_sha,
        "HEAD",
    )
    assessment = riskmod.assess(
        changed_files,
        stored,
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )

    with session.store.transaction(run.run_id) as doc:
        for fix_commit in new_commits:
            if fix_commit not in doc.fix_commits:
                doc.fix_commits.append(fix_commit)
        if accepting_in_place_fix:
            doc.head_sha = new_commits[-1]
            doc.source_head_sha = new_commits[-1]
        _apply(doc, Action.RESPOND)
        doc.changed_files = changed_files
        doc.risk = assessment
        run = doc

    session.store.save_findings(run.run_id, stored)
    session.store.append_event(
        run.run_id,
        {
            "event": "finding_resolved",
            "id": finding_id,
            "action": action,
            "commit": commit,
            "risk": assessment.model_dump(mode="json"),
        },
    )

    stage = _require_finding_stage(run)
    severities = (
        session.config.review.blocking_severities
        if stage is Stage.REVIEW
        else session.config.docs.blocking_severities
    )
    remaining = findingsmod.blocking(
        [f for f in stored if f.stage is stage], blocking_severities=severities
    )

    envelope = _envelope_for(
        run,
        stage=stage.value,
        data={
            "finding": target.model_dump(mode="json"),
            "remaining_blocking": len(remaining),
            "risk": assessment.model_dump(mode="json"),
        },
        blocking=[f.model_dump(mode="json") for f in remaining],
    )
    if remaining:
        next_finding = remaining[0]
        envelope.next_instruction = "Keep responding until nothing blocks, then verify."
        envelope.next_command = (
            f"agentic-preflight respond --id {next_finding.id} --action fixed --commit <sha>"
        )
    else:
        envelope.next_instruction = "Nothing blocks this stage any more. Verify it."
        envelope.next_command = "agentic-preflight verify"
    return envelope


def _verify_fix_commit(session: Session, run: RunDoc, target, commit: str) -> str:
    wt = _require_worktree(run)
    if not gitx.commit_exists(wt, commit):
        raise InvalidResponse(
            f"commit {commit} does not exist in the validation checkout",
            state=run.state.value,
            run_id=run.run_id,
        )

    full_sha = gitx.rev_parse(wt, commit)

    if not gitx.commit_touches(wt, full_sha, target.path):
        raise InvalidResponse(
            f"commit {commit[:8]} does not touch {target.path}, the file {target.id} "
            f"is about; it changes {gitx.commit_files(wt, full_sha)}",
            state=run.state.value,
            run_id=run.run_id,
        )

    # Independent of the preflight copy refusal: a .gitignore edited mid-run
    # must not be able to open this hole.
    worktree.assert_commit_is_clean_of(wt, full_sha, run.copied_files)
    return full_sha


def _in_place_fix_commits(session: Session, run: RunDoc, commit: str) -> list[str]:
    """Account for every direct repair commit before advancing the exact SHA."""
    repo = session.repo_root
    if not gitx.is_clean(repo):
        raise DirtyTree(
            "the in-place validation checkout has uncommitted changes",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Commit the intended repair, then respond again.",
            next_command="git status",
        )
    current = gitx.rev_parse(repo, "HEAD")
    if commit != current:
        raise InvalidResponse(
            f"in-place repair commit {commit[:8]} is not the current branch tip "
            f"{current[:8]}; every intervening commit must be accounted for",
            state=run.state.value,
            run_id=run.run_id,
        )
    if not gitx.is_ancestor(repo, run.head_sha, current):
        raise StaleRun(
            "the in-place validation branch was rewritten instead of advanced by repair commits",
            state=run.state.value,
            run_id=run.run_id,
            next_command=shlex.join(
                [
                    "agentic-preflight",
                    "start",
                    "--intent",
                    run.intent or "<objective and acceptance criteria>",
                ]
            ),
        )
    commits = gitx.commits_between(repo, run.head_sha, current)
    if not commits:
        raise InvalidResponse(
            "in-place resolution requires a new repair commit after findings were submitted",
            state=run.state.value,
            run_id=run.run_id,
        )
    for sha in commits:
        worktree.assert_commit_is_clean_of(repo, sha, run.copied_files)
    return commits
