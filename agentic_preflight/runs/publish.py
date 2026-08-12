"""Quality-gated publication of a verified branch and its attestation."""

from __future__ import annotations

from .. import attestation as attestationmod
from .. import gitx
from .. import risk as riskmod
from ..attestation import NOTES_REF
from ..envelope import Envelope
from ..errors import ManualGate, NeedsConfirm
from ..machine import Action, State
from ..publish import gate as gatemod
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _load_current,
    _release_run_worktree,
    _require_state,
    _worktree_completion,
    _worktree_mode,
)
from .mergeback import _remote_for


def gate(session: Session) -> Envelope:
    """Summarise what would be pushed and mint a confirmation token."""
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(run, State.VERIFIED, State.AWAITING_PUSH_CONFIRM, command="gate")

    _remote_for(session, run)
    commits = [
        {"sha": sha, "subject": gitx.commit_subject(session.repo_root, sha)}
        for sha in gitx.commits_between(session.repo_root, run.merge_base_sha, run.head_sha)
    ]
    changed_files = gitx.changed_files(
        session.repo_root,
        run.merge_base_sha,
        run.head_sha,
    )
    assessment = riskmod.assess(
        changed_files,
        session.store.load_findings(run.run_id),
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )
    try:
        portable = attestationmod.verify(session.repo_root, run.head_sha)
    except (attestationmod.InvalidAttestation, gitx.GitError):
        portable = None
    if portable is not None:
        assessment = riskmod.include_attested_findings(assessment, portable.findings_summary)
    if run.risk != assessment or run.changed_files != changed_files:
        with session.store.transaction(run.run_id) as doc:
            doc.changed_files = changed_files
            doc.risk = assessment
            run = doc
    summary = gatemod.GateSummary(
        remote="origin",
        refspec=f"{run.branch}:{run.branch} {NOTES_REF}:{NOTES_REF}",
        branch=run.branch,
        base_ref=run.base_ref,
        pr_mode=session.config.pr.mode,
        approval_mode=session.config.approval.mode,
        commits=commits,
        risk=assessment.model_dump(mode="json"),
    )

    if session.config.gate.mode == "manual":
        raise ManualGate(
            "gate.mode is 'manual', so agentic-preflight will not push on your behalf",
            state=run.state.value,
            run_id=run.run_id,
            data={
                **summary.as_dict(),
                "manual_command": f"git push --atomic origin {run.branch} {NOTES_REF}",
            },
            next_instruction=(
                "Show the user this summary and ask them to review the change and run "
                "the push themselves."
            ),
            next_command=f"git push --atomic origin {run.branch} {NOTES_REF}",
        )

    summary.token = gatemod.mint_token()
    with session.store.transaction(run.run_id) as doc:
        doc.gate_token = summary.token
        if doc.state is State.VERIFIED:
            _apply(doc, Action.GATE)
        run = doc

    opens_pr = session.config.pr.mode == "auto"
    if not assessment.requires_human_review:
        risk_instruction = ""
    elif session.config.approval.mode == "manual_merge":
        risk_instruction = (
            " Explain that this high-risk pull request must be merged manually by the user; "
            "the agent must not merge it or enable auto-merge."
        )
    elif session.config.approval.mode == "environment":
        risk_instruction = (
            " Explain that merge requires approval through GitHub Environment "
            f"{session.config.approval.environment!r} for the exact workflow run."
        )
    else:
        risk_instruction = (
            " Explain that merge still requires eligible peer approval of the exact head."
        )
    manual_pr_instruction = (
        " PR mode is manual, so do not open the pull request; give the user the compare URL "
        "after the push."
        if not opens_pr
        else " After the confirmed push and preflight finish, automatically open or reuse "
        "the pull request; auto mode is standing authorization, so do not ask again."
    )
    return _envelope_for(
        run,
        data=summary.as_dict(),
        next_instruction=(
            "Show the user the remote, branch, and commit list in plain language. If the "
            "user explicitly requested a push, publish, or asked to create or open a pull "
            "request in this task, that request authorizes this push when the summary "
            "matches the requested work; proceed without asking again. Otherwise, ask "
            "whether to push and wait for their answer."
            f"{risk_instruction}{manual_pr_instruction}"
        ),
        next_command=f"agentic-preflight push --confirm {summary.token}",
    )


def push(session: Session, *, confirm: str | None = None, dry_run: bool = False) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(run, State.AWAITING_PUSH_CONFIRM, command="push")

    if not gatemod.token_matches(run.gate_token, confirm):
        raise NeedsConfirm(
            "push requires the confirmation token from `gate`",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=(
                "Run `gate`, show the user what would be pushed, ask for their "
                "agreement, then push with the token."
            ),
            next_command="agentic-preflight gate",
        )

    if dry_run:
        return _envelope_for(
            run,
            data={
                "dry_run": True,
                "would_push": f"origin {run.branch} {NOTES_REF}",
                "branch": run.branch,
                "base_ref": run.base_ref,
                "pr_mode": session.config.pr.mode,
                "pushed": False,
            },
            next_instruction="Dry run only; nothing was pushed.",
            next_command=f"agentic-preflight push --confirm {run.gate_token}",
        )

    gitx.run(
        session.repo_root,
        "push",
        "--atomic",
        "origin",
        f"{run.branch}:{run.branch}",
        f"{NOTES_REF}:{NOTES_REF}",
    )

    with session.store.transaction(run.run_id) as doc:
        doc.pushed_sha = run.head_sha
        _apply(doc, Action.PUSH)
        run = doc

    session.store.append_event(run.run_id, {"event": "pushed", "sha": run.head_sha})
    return _envelope_for(
        run,
        data={
            "pushed": True,
            "sha": run.head_sha,
            "remote": "origin",
            "branch": run.branch,
            "base_ref": run.base_ref,
            "pr_mode": session.config.pr.mode,
            "dry_run": False,
        },
    )


def finish(session: Session) -> Envelope:
    """Mark a pushed validation run complete and release its runner."""
    run = _load_current(session)
    _require_state(run, State.PUSHED, command="finish")

    _release_run_worktree(session, run)
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.FINISH)
        doc.worktree_released = True
        run = doc

    session.store.set_current(None)
    session.store.append_event(run.run_id, {"event": "finished"})
    pr_instruction = (
        " Run gc, then open or reuse the pull request with gh; auto PR mode is standing "
        "authorization, so do not ask again."
        if session.config.pr.mode == "auto"
        else " Run gc, then give the user the compare URL; do not open the pull request."
    )
    return _envelope_for(
        run,
        data={
            "pushed_sha": run.pushed_sha,
            "branch": run.branch,
            "base_ref": run.base_ref,
            "pr_mode": session.config.pr.mode,
        },
        next_instruction=(
            _worktree_completion(_worktree_mode(run, session.config)) + pr_instruction
        ),
        next_command="agentic-preflight gc",
    )
