"""Quality-gated publication of a verified branch and its attestation."""

from __future__ import annotations

import secrets
import shlex
from dataclasses import asdict, dataclass, field

from .. import attestation as attestationmod
from .. import evidence_transport, gitx
from .. import risk as riskmod
from ..attestation import NOTES_REF
from ..cli_contract import FinishData, GateData, PushData
from ..envelope import Envelope
from ..errors import AttestationFailed, ManualGate, NeedsConfirm
from ..machine import Action, State
from ..models import Attestation, RunDoc
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _assess,
    _envelope_for,
    _load_current,
    _release_run_worktree,
    _require_state,
    _worktree_completion,
    _worktree_mode,
)
from .mergeback import _remote_for

#: Stands for the notes commit that ``push`` builds when it publishes. It is not
#: a ref, so a summary that shows it cannot be run as a literal command.
NOTES_COMMIT_PLACEHOLDER = "<notes-commit>"

#: How many times ``push`` builds its notes commit when another clone updates
#: the remote notes ref between the fetch and the push.
_NOTES_PUBLISH_ATTEMPTS = 3


def _push_refspecs(
    head_sha: str, branch: str, notes_source: str | None = NOTES_COMMIT_PLACEHOLDER
) -> list[str]:
    """Push the attested head and, when there is one, the notes commit to publish.

    The notes source is never the local notes ref: that ref also holds notes for
    commits that were never pushed.
    """
    refspecs = [f"{head_sha}:refs/heads/{branch}"]
    if notes_source is not None:
        refspecs.append(f"{notes_source}:{NOTES_REF}")
    return refspecs


def _pushed_commits(session: Session, run: RunDoc) -> list[str]:
    """The commits this run publishes, oldest first; the gate lists the same range."""
    return gitx.commits_between(session.repo_root, run.merge_base_sha, run.head_sha)


def _published_notes(session: Session, commits: list[str]) -> list[str]:
    """The commits among ``commits`` whose local notes publication would carry."""
    local = gitx.note_blobs(session.repo_root, NOTES_REF)
    return [sha for sha in commits if sha in local]


def _push_commands(evidence_refspecs: list[str], refspecs: list[str]) -> list[str]:
    """Describe the same dependency-first atomic pushes used for publication."""
    return [
        shlex.join(["git", "push", "--atomic", "origin", *group])
        for group in (evidence_refspecs, refspecs)
        if group
    ]


def gate(session: Session) -> Envelope:
    """Summarise what would be pushed and mint a confirmation token."""
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(run, State.VERIFIED, State.AWAITING_PUSH_CONFIRM, command="gate")

    _remote_for(session, run)
    pushed_commits = _pushed_commits(session, run)
    commits = [
        {"sha": sha, "subject": gitx.commit_subject(session.repo_root, sha)}
        for sha in pushed_commits
    ]
    changed_files = gitx.changed_files(
        session.repo_root,
        run.merge_base_sha,
        run.head_sha,
    )
    assessment = _assess(session, changed_files, session.store.load_findings(run.run_id))
    try:
        portable = attestationmod.verify(session.repo_root, run.head_sha, purpose="publish")
    except (attestationmod.InvalidAttestation, gitx.GitError):
        portable = None
    if portable is not None:
        assessment = riskmod.include_attested_findings(assessment, portable.findings_summary)
    evidence_refspecs = []
    if portable is not None:
        evidence_refspecs = evidence_transport.refspecs(portable)
    notes_source: str | None
    if session.config.gate.mode == "manual":
        # A person runs the manual command, so it must name a real notes commit.
        # Build it now on the remote notes tip. No ref keeps it afterwards. If the
        # remote notes move first, or Git prunes the unreferenced commit, the
        # atomic push is rejected and publishes nothing; the person reruns gate.
        with session.store.resource("notes"):
            remote_tip = gitx.fetch_notes(session.repo_root, "origin", NOTES_REF)
            with gitx.notes_publication(
                session.repo_root, NOTES_REF, remote_tip, pushed_commits
            ) as (staging, published_notes):
                notes_source = (
                    None if staging is None else gitx.rev_parse(session.repo_root, staging)
                )
    else:
        published_notes = _published_notes(session, pushed_commits)
        notes_source = NOTES_COMMIT_PLACEHOLDER if published_notes else None
    refspecs = _push_refspecs(run.head_sha, run.branch, notes_source)
    if run.risk != assessment or run.changed_files != changed_files:
        with session.store.transaction(run.run_id) as doc:
            doc.changed_files = changed_files
            doc.risk = assessment
            run = doc
    summary = GateSummary(
        remote="origin",
        refspec=" ".join([*evidence_refspecs, *refspecs]),
        branch=run.branch,
        base_ref=run.base_ref,
        pr_mode=session.config.pr.mode,
        automated_cleanup=session.config.pr.automated_cleanup,
        approval_mode=session.config.approval.mode,
        commits=commits,
        published_notes=published_notes,
        risk=assessment.model_dump(mode="json"),
    )

    if session.config.gate.mode == "manual":
        raise ManualGate(
            "gate.mode is 'manual', so agentic-preflight will not push on your behalf",
            state=run.state.value,
            run_id=run.run_id,
            data={
                **summary.as_dict(),
                "manual_command": " && ".join(_push_commands(evidence_refspecs, refspecs)),
            },
            next_instruction=(
                "Show the user this summary and data.manual_command, then stop; the user "
                "must review the change and run the push themselves."
            ),
            next_command="agentic-preflight status",
        )

    summary.token = mint_token()
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
    elif session.config.approval.mode == "owner_review":
        risk_instruction = (
            " Explain that merge requires the configured reviewer to approve the exact head, "
            "or submit a review containing only approved on their own pull request."
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
    if opens_pr and session.config.pr.automated_cleanup:
        cleanup_instruction = (
            " Automated cleanup is enabled: after opening or reusing the pull request, "
            "disclose the exact cleanup scope, poll that PR every 5 minutes, and clean up "
            "only after the merge is verified."
        )
    elif opens_pr:
        cleanup_instruction = (
            " Automated cleanup is disabled: do not enter the merge-state polling or "
            "cleanup flow; report hosted check results and leave cleanup for a later "
            "explicit user request."
        )
    else:
        cleanup_instruction = ""
    return _envelope_for(
        run,
        data=GateData.model_validate(
            {
                **summary.as_dict(),
                "push_commands": _push_commands(evidence_refspecs, refspecs),
            }
        ),
        next_instruction=(
            "Apply https://github.com/elanthus/agentic-preflight/blob/main/skill/SKILL.md#escalation-etiquette and its authorization rule to this "
            "gate summary; the agent substitutes data.token for <token> only after authorization."
            f"{risk_instruction}{manual_pr_instruction}{cleanup_instruction}"
        ),
        next_command="agentic-preflight push --confirm <token>",
    )


def push(session: Session, *, confirm: str | None = None, dry_run: bool = False) -> Envelope:
    """Push the run's branch after the confirmation token is checked."""
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(run, State.AWAITING_PUSH_CONFIRM, command="push")

    if not token_matches(run.gate_token, confirm):
        raise NeedsConfirm(
            "push requires the confirmation token from `gate`",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=(
                "Run `gate` and show the user what would be pushed. When the summary "
                "matches the user's explicit request or applicable standing instructions, "
                "proceed without asking again. Ask whether to push only if authorization "
                "is missing or the scope materially differs. After authorization, push "
                "with the token."
            ),
            next_command="agentic-preflight gate",
        )

    try:
        portable = attestationmod.verify(session.repo_root, run.head_sha, purpose="publish")
    except (attestationmod.InvalidAttestation, gitx.GitError) as exc:
        raise AttestationFailed(str(exc), next_command="agentic-preflight status") from exc
    evidence_refspecs = evidence_transport.refspecs(portable)
    pushed_commits = _pushed_commits(session, run)

    if dry_run:
        refspecs = _push_refspecs(run.head_sha, run.branch)
        return _envelope_for(
            run,
            data=PushData(
                dry_run=True,
                would_push=" && ".join(_push_commands(evidence_refspecs, refspecs)),
                published_notes=_published_notes(session, pushed_commits),
                branch=run.branch,
                base_ref=run.base_ref,
                pr_mode=session.config.pr.mode,
                automated_cleanup=session.config.pr.automated_cleanup,
                pushed=False,
            ),
            next_instruction="Dry run only; nothing was pushed.",
            next_command="agentic-preflight push --confirm <token>",
        )

    with session.store.resource("notes"):
        published_notes = _publish(session, run, portable, evidence_refspecs, pushed_commits)

    with session.store.transaction(run.run_id) as doc:
        doc.pushed_sha = run.head_sha
        _apply(doc, Action.PUSH)
        run = doc

    session.store.append_event(run.run_id, {"event": "pushed", "sha": run.head_sha})
    return _envelope_for(
        run,
        data=PushData(
            pushed=True,
            sha=run.head_sha,
            remote="origin",
            branch=run.branch,
            base_ref=run.base_ref,
            pr_mode=session.config.pr.mode,
            automated_cleanup=session.config.pr.automated_cleanup,
            dry_run=False,
            published_notes=published_notes,
        ),
    )


def _synchronize_notes(session: Session, run: RunDoc, portable: Attestation) -> str | None:
    """Fetch the remote notes, confirm the head's attestation survived, return the tip."""
    remote_tip = gitx.fetch_notes(session.repo_root, "origin", NOTES_REF)
    try:
        synchronized = attestationmod.read(session.repo_root, run.head_sha)
    except attestationmod.InvalidAttestation as exc:
        raise AttestationFailed(
            "Attestation changed during notes synchronization; refresh before publication",
            next_command="agentic-preflight status",
        ) from exc
    if synchronized != portable:
        raise AttestationFailed(
            "Attestation changed during notes synchronization; refresh before publication",
            next_command="agentic-preflight status",
        )
    return remote_tip


def _publish(
    session: Session,
    run: RunDoc,
    portable: Attestation,
    evidence_refspecs: list[str],
    pushed_commits: list[str],
) -> list[str]:
    """Push the branch with a notes commit that holds only the pushed commits' notes.

    The notes commit is the remote notes tip plus the local notes of
    ``pushed_commits``, so notes on commits that stay local are never sent. If
    another clone moves the remote notes between the fetch and the push, the
    atomic push is rejected as a non-fast-forward and nothing lands; the notes
    commit is then rebuilt on the new tip, which keeps the other clone's notes.
    Return the commits whose notes were published.
    """
    remote_tip = _synchronize_notes(session, run, portable)
    # Publish dependencies before exposing a note that requires them. Some
    # servers reject hidden refs before processing the atomic ref transaction;
    # a combined push can otherwise expose the branch despite that rejection.
    if evidence_refspecs:
        gitx.run(session.repo_root, "push", "--atomic", "origin", *evidence_refspecs)
    attempt = 1
    while True:
        with gitx.notes_publication(session.repo_root, NOTES_REF, remote_tip, pushed_commits) as (
            staging,
            published_notes,
        ):
            args = [
                "push",
                "--atomic",
                "origin",
                *_push_refspecs(run.head_sha, run.branch, staging),
            ]
            result = gitx.run(session.repo_root, *args, check=False)
        if result.returncode == 0:
            return published_notes
        if attempt >= _NOTES_PUBLISH_ATTEMPTS or (
            gitx.remote_ref_tip(session.repo_root, "origin", NOTES_REF) == remote_tip
        ):
            raise gitx.GitError(args, result.returncode, result.stderr)
        attempt += 1
        remote_tip = _synchronize_notes(session, run, portable)


def finish(session: Session) -> Envelope:
    """Mark a pushed validation run complete and release its runner."""
    run = _load_current(session)
    _require_state(run, State.PUSHED, command="finish")

    _release_run_worktree(session, run)
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.FINISH)
        doc.worktree_released = True
        run = doc

    session.store.clear_run(run.run_id)
    session.store.append_event(run.run_id, {"event": "finished"})
    if session.config.pr.mode == "manual":
        pr_instruction = (
            " Run gc, then give the user the compare URL; do not open the pull request."
        )
    elif session.config.pr.automated_cleanup:
        pr_instruction = (
            " Run gc, then open or reuse the pull request with gh; auto PR mode is standing "
            "authorization, so do not ask again. After hosted checks, follow the disclosed "
            "automatic cleanup lifecycle."
        )
    else:
        pr_instruction = (
            " Run gc, then open or reuse the pull request with gh; auto PR mode is standing "
            "authorization, so do not ask again. Automated cleanup is disabled: stop after "
            "hosted checks and wait for an explicit cleanup request."
        )
    return _envelope_for(
        run,
        data=FinishData(
            pushed_sha=run.pushed_sha,
            branch=run.branch,
            base_ref=run.base_ref,
            pr_mode=session.config.pr.mode,
            automated_cleanup=session.config.pr.automated_cleanup,
        ),
        next_instruction=(_worktree_completion(_worktree_mode(run)) + pr_instruction),
        next_command="agentic-preflight gc",
    )


@dataclass
class GateSummary:
    remote: str
    refspec: str
    branch: str
    base_ref: str
    pr_mode: str
    automated_cleanup: bool
    approval_mode: str
    commits: list[dict] = field(default_factory=list)
    published_notes: list[str] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    token: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def mint_token() -> str:
    """Return a new random confirmation token."""
    return secrets.token_hex(8)


def token_matches(expected: str | None, supplied: str | None) -> bool:
    """Return whether a supplied token matches the expected one."""
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)
