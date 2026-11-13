"""Finding response and fix-commit verification."""

from __future__ import annotations

from .. import findings as findingsmod
from .. import gitx, worktree
from ..cli_contract import RespondData
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    InvalidResponse,
    UnknownFinding,
)
from ..machine import Action, State
from ..models import RESPONSE_REASONS, FindingStatus, ResponseReason, RunDoc, Stage
from ..stages import shellstage
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _assess,
    _blocking_severities,
    _envelope_for,
    _findings_next_hint,
    _is_in_place,
    _load_current,
    _require_finding_stage,
    _require_state,
    _require_worktree,
)
from .fix_commits import derive_fix_commits, record_fix_commits
from .review import _skip_docs_if_disabled
from .review_coverage import reopen_if_stale

RESPONSE_ACTIONS = ("fixed", "dismissed", "accepted")


def _validate_response(
    session: Session,
    run: RunDoc,
    target,
    finding_id: str,
    action: str,
    commit: str | None,
    note: str | None,
    reason: str | None = None,
) -> str | None:
    """Validate action-specific evidence and normalize the fix commit."""
    if reason is not None and action == "fixed":
        raise InvalidResponse(
            f"--reason is only valid when resolving {finding_id} as dismissed or accepted, "
            "not fixed",
            state=run.state.value,
            run_id=run.run_id,
        )
    if reason is not None and reason not in RESPONSE_REASONS:
        raise InvalidResponse(
            f"--reason must be one of {', '.join(RESPONSE_REASONS)}, not {reason!r}",
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
        return _verify_fix_commit(session, run, target, commit)
    if commit is not None:
        raise InvalidResponse(
            f"--commit is only valid when resolving {finding_id} as fixed, not {action}",
            state=run.state.value,
            run_id=run.run_id,
        )
    if not note:
        raise InvalidResponse(
            f"resolving {finding_id} as {action} requires --note explaining why; "
            f"an unexplained dismissal is indistinguishable from an oversight",
            state=run.state.value,
            run_id=run.run_id,
        )
    return None


def _redacted_note(run: RunDoc, note: str | None) -> str | None:
    """Apply stage-log redaction to a response note before it is persisted."""
    if note is None:
        return None
    try:
        secrets = shellstage.read_secrets(_require_worktree(run), run.copied_files)
    except shellstage.SecretRedactionError as exc:
        raise InvalidResponse(
            "the response cannot be recorded because copied-file redaction is unavailable",
            state=run.state.value,
            run_id=run.run_id,
            data={"copied_file": str(exc.path)},
            next_instruction=(
                "Restore the reported copied file as readable text, or remove it from "
                "[worktree] copy_files, then respond again."
            ),
        ) from exc
    return findingsmod.redact_text(note, secrets, limit=findingsmod.RESPONSE_NOTE_LIMIT)


def respond(
    session: Session,
    *,
    finding_id: str,
    action: str,
    commit: str | None = None,
    note: str | None = None,
    reason: ResponseReason | None = None,
) -> Envelope:
    """Record the agent's resolution of one finding, verifying what it claims.

    "I fixed F003 in abc123" is an assertion, and three things can be wrong with
    it: the commit may not exist, it may not touch the file the finding was
    about, or it may carry a copied environment file. All three are cheap to
    check and expensive to miss, so none of them are taken on trust.

    ``reason`` is the agent's label for a dismissal or acceptance. It is kept
    for telemetry only, so it is checked against the vocabulary and nothing more.
    """
    run = _load_current(session)
    accepting_in_place_fix = _is_in_place(run) and action == "fixed" and commit is not None
    if not accepting_in_place_fix:
        _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_BLOCKED,
        State.REVIEW_GREEN,
        State.DOCS_BLOCKED,
        State.DOCS_GREEN,
        command="respond",
    )
    active_stage = _require_finding_stage(run)

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

    commit = _validate_response(session, run, target, finding_id, action, commit, note, reason)
    note = _redacted_note(run, note)

    # The cited commit is the audit record; the fix-commit list is the
    # validated history, so uncited or out-of-order repairs still merge back.
    new_commits: list[str] = []
    if accepting_in_place_fix and commit:
        new_commits = _in_place_fix_commits(session, run, commit)
    elif commit:
        new_commits = derive_fix_commits(session, run)

    target.status = FindingStatus(action)
    target.fix_commit = commit
    target.response_note = note
    target.response_reason = reason
    changed_files = gitx.changed_files(
        _require_worktree(run),
        run.merge_base_sha,
        "HEAD",
    )
    assessment = _assess(session, changed_files, stored)

    responded_from_green = False
    with session.store.transaction(run.run_id, findings=stored) as doc:
        record_fix_commits(doc, new_commits)
        if accepting_in_place_fix:
            doc.head_sha = new_commits[-1]
            doc.source_head_sha = new_commits[-1]
        responded_from_green = doc.state in (State.REVIEW_GREEN, State.DOCS_GREEN)
        _apply(doc, Action.RESPOND)
        doc.changed_files = changed_files
        doc.risk = assessment
        run = doc

    session.store.append_event(
        run.run_id,
        {
            "event": "finding_resolved",
            "id": finding_id,
            "action": action,
            "commit": commit,
            "reason": reason,
            "risk": assessment.model_dump(mode="json"),
        },
    )

    if action == "fixed" and responded_from_green:
        run, reopened = reopen_if_stale(session, run)
        if reopened:
            return _envelope_for(
                run,
                stage=Stage.REVIEW.value,
                data=RespondData(
                    finding=target.model_dump(mode="json"),
                    coverage_invalidated=True,
                    risk=assessment.model_dump(mode="json"),
                ),
                next_instruction=(
                    "The repair changed the reviewed snapshot. Review the complete "
                    "current diff and rerun every applicable stage."
                ),
                next_command="agentic-preflight context",
            )

    stage = active_stage
    severities = _blocking_severities(session, stage)
    stage_findings = [finding for finding in stored if finding.stage is stage]
    remaining = findingsmod.blocking(stage_findings, blocking_severities=severities)
    actionable = findingsmod.actionable(stage_findings)
    if not remaining and not actionable:
        run = _skip_docs_if_disabled(session, run)

    envelope = _envelope_for(
        run,
        stage=stage.value,
        data=RespondData(
            finding=target.model_dump(mode="json"),
            remaining_blocking=len(remaining),
            risk=assessment.model_dump(mode="json"),
        ),
        blocking=[f.model_dump(mode="json") for f in remaining],
    )
    _findings_next_hint(
        envelope,
        remaining,
        actionable,
        blocking_instruction="Keep responding until nothing blocks, then verify.",
    )
    if not remaining and not actionable and run.state in (State.REVIEW_BLOCKED, State.DOCS_BLOCKED):
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

    if not _is_in_place(run) and not gitx.is_ancestor(wt, full_sha, "HEAD"):
        raise InvalidResponse(
            f"commit {commit[:8]} is not part of the validation checkout's current "
            "history; apply the repair in the validation worktree before responding",
            state=run.state.value,
            run_id=run.run_id,
        )

    if not _is_in_place(run) and (
        full_sha == run.head_sha or not gitx.is_ancestor(wt, run.head_sha, full_sha)
    ):
        raise InvalidResponse(
            f"commit {commit[:8]} is not a new commit on top of the reviewed head "
            f"{run.head_sha[:8]}; commit the repair in the validation worktree and cite "
            "that commit",
            state=run.state.value,
            run_id=run.run_id,
        )

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
    commits = derive_fix_commits(session, run)
    if not commits:
        raise InvalidResponse(
            "in-place resolution requires a new repair commit after findings were submitted",
            state=run.state.value,
            run_id=run.run_id,
        )
    return commits
