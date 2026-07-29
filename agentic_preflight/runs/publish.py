"""Publication, pull-request monitoring, and post-merge cleanup."""

from __future__ import annotations

import shlex
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import gitx
from ..attestation import NOTES_REF
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    GhUnavailableError,
    ManualGate,
    NeedsConfirm,
    NeedsHuman,
)
from ..machine import Action, State
from ..models import RunDoc, Stage
from ..publish import gate as gatemod
from ..publish import github as githubmod
from ..publish import provider as providermod
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _load_current,
    _now,
    _release_run_worktree,
    _require_state,
    _worktree_cleanup_action,
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
    summary = gatemod.GateSummary(
        remote="origin",
        refspec=f"{run.branch}:{run.branch} {NOTES_REF}:{NOTES_REF}",
        branch=run.branch,
        base_ref=run.base_ref,
        commits=commits,
        pr_title=_default_pr_title(session, run, commits),
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
                "Show the user this summary and ask them to run the push themselves."
            ),
            next_command=f"git push --atomic origin {run.branch} {NOTES_REF}",
        )

    summary.token = gatemod.mint_token()
    with session.store.transaction(run.run_id) as doc:
        doc.gate_token = summary.token
        if doc.state is State.VERIFIED:
            _apply(doc, Action.GATE)
        run = doc

    return _envelope_for(
        run,
        data=summary.as_dict(),
        next_instruction=(
            "Show the user the remote, branch, and commit list in plain language and "
            "ask whether to push. Never push without asking."
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
        run, data={"pushed": True, "sha": run.head_sha, "remote": "origin", "dry_run": False}
    )


def _default_pr_title(session: Session, run: RunDoc, commits: list[dict] | None = None) -> str:
    if session.config.publish.pr_title:
        return session.config.publish.pr_title
    if run.branch:
        return run.branch
    commits = commits or []
    return commits[0]["subject"] if commits else "agentic-preflight verified change"


def pull_request(
    session: Session, *, draft: bool | None = None, title: str | None = None
) -> Envelope:
    run = _load_current(session)
    _require_state(run, State.PUSHED, command="pr")

    remote = _remote_for(session, run)
    if remote.provider != "github":
        raise NeedsHuman(
            f"{remote.host or 'this remote'} is not supported in v1 (GitHub only)",
            state=run.state.value,
            run_id=run.run_id,
            data={"host": remote.host},
            next_instruction="Open the pull request manually.",
        )

    commit_shas = gitx.commits_between(session.repo_root, run.merge_base_sha, run.head_sha)
    commits = [
        {"sha": sha, "subject": gitx.commit_subject(session.repo_root, sha)} for sha in commit_shas
    ]
    title = title or _default_pr_title(session, run, commits)
    draft_pr = session.config.publish.draft_pr if draft is None else draft

    try:
        result = githubmod.create_or_update_pull_request(
            session.repo_root,
            base=run.base_ref,
            head=run.branch,
            title=title,
            body=_pr_body(session, run),
            draft=draft_pr,
        )
    except githubmod.GhUnavailable as exc:
        raise GhUnavailableError(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
            data={
                "compare_url": providermod.compare_url(remote, base=run.base_ref, head=run.branch)
            },
            next_instruction=(
                "Give the user the compare URL and let them open the PR themselves. "
                "agentic-preflight never handles credentials."
            ),
        ) from exc

    with session.store.transaction(run.run_id) as doc:
        doc.pr_url = result.url
        _apply(doc, Action.OPEN_PR)
        run = doc

    session.store.append_event(
        run.run_id,
        {"event": "pr_opened" if result.created else "pr_updated", "url": result.url},
    )
    return _envelope_for(run, data={"pr_url": result.url, "created": result.created})


def monitor_ci(
    session: Session,
    *,
    once: bool = False,
    timeout_seconds: int | None = None,
    poll_interval_seconds: int | None = None,
) -> Envelope:
    """Monitor GitHub checks; return repair work to the host, never an in-process LLM."""
    run = _load_current(session)
    _require_state(
        run,
        State.PR_OPEN,
        State.CI_MONITORING,
        State.CI_FAILED,
        State.CHECKS_PASSED,
        State.CI_TIMED_OUT,
        command="ci",
    )
    if not run.pr_url:
        raise NeedsHuman("this run has no recorded pull request URL", run_id=run.run_id)
    pr_url = run.pr_url

    timeout = timeout_seconds or session.config.ci.timeout_seconds
    poll = poll_interval_seconds or session.config.ci.poll_interval_seconds
    if timeout < 1 or poll < 1:
        raise NeedsHuman("CI timeout and polling interval must both be positive")

    with session.store.transaction(run.run_id) as doc:
        if doc.state is State.PR_OPEN:
            _apply(doc, Action.BEGIN_CI)
        elif doc.state is not State.CI_MONITORING:
            _apply(doc, Action.RETRY_CI)
        if doc.ci_started_at is None:
            doc.ci_started_at = _now()
        run = doc

    try:
        started_at = datetime.fromisoformat(run.ci_started_at or _now())
        elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
    except ValueError:
        elapsed = 0.0
    deadline = time.monotonic() + max(0.0, timeout - elapsed)
    while True:
        try:
            health = githubmod.pull_request_health(session.repo_root, pr_url)
            logs = (
                githubmod.failed_check_logs(session.repo_root, health.failed_checks)
                if health.outcome == "failed"
                else {}
            )
        except githubmod.GhUnavailable as exc:
            raise GhUnavailableError(
                str(exc),
                state=run.state.value,
                run_id=run.run_id,
                next_instruction="Restore gh authentication, then resume CI monitoring.",
                next_command="agentic-preflight ci",
            ) from exc

        if health.outcome == "pending" and not once and time.monotonic() >= deadline:
            outcome = "timed_out"
            action = Action.CI_TIMEOUT
        else:
            outcome = health.outcome
            action = {
                "pending": Action.CI_PENDING,
                "failed": Action.CI_FAILURE,
                "checks_passed": Action.CI_PASSED,
                "merged": Action.CI_MERGED,
                "closed": Action.CI_CLOSED,
            }[outcome]

        with session.store.transaction(run.run_id) as doc:
            doc.ci_last_checked_at = _now()
            doc.ci_status = outcome
            doc.ci_failures = [item.as_dict() for item in health.failed_checks]
            doc.ci_logs = logs
            _apply(doc, action)
            run = doc
        data = {
            "pr": health.as_dict(),
            "outcome": outcome,
            "failed_logs": logs,
            "intent": run.intent,
            "intent_source": run.intent_source,
            "host_driven": True,
        }
        session.store.append_event(run.run_id, {"event": "ci_checked", **data})

        if outcome == "closed":
            _release_run_worktree(session, run)
            with session.store.transaction(run.run_id) as doc:
                doc.worktree_released = True
                run = doc
            session.store.set_current(None)
            return _envelope_for(
                run,
                data=data,
                next_instruction="The pull request closed without merging; the run is complete.",
                next_command=None,
            )
        if outcome == "failed":
            restart = shlex.join(["agentic-preflight", "start", "--intent", run.intent or ""])
            return _envelope_for(
                run,
                data={**data, "restart_command": restart},
                next_instruction=(
                    "Inspect the failed logs and repair the source branch as the host agent. "
                    "Do not invoke an LLM from agentic-preflight. Preserve the recorded intent, abort "
                    "this completed validation run, commit the repair, then start the supplied "
                    "fresh validation command. Only push after that entire run is green."
                ),
                next_command="agentic-preflight abort --force",
            )
        if outcome in {"checks_passed", "merged", "timed_out"} or once:
            return _envelope_for(run, data=data)

        time.sleep(poll)


def finish(session: Session) -> Envelope:
    """Mark a pushed run with no pull request complete."""
    run = _load_current(session)
    _require_state(run, State.PUSHED, command="finish")

    _release_run_worktree(session, run)
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.FINISH)
        doc.worktree_released = True
        run = doc

    session.store.set_current(None)
    session.store.append_event(run.run_id, {"event": "finished"})
    return _envelope_for(
        run,
        data={"pushed_sha": run.pushed_sha, "pr_url": run.pr_url},
        next_instruction=_worktree_completion(_worktree_mode(run, session.config)),
        next_command="agentic-preflight gc",
    )


def _cleanup_base_branch(repo: Path, base_ref: str) -> str | None:
    candidates = [base_ref]
    if "/" in base_ref:
        candidates.append(base_ref.split("/", 1)[1])
    return next(
        (branch for branch in candidates if gitx.local_branch_exists(repo, branch)),
        None,
    )


def cleanup(session: Session, *, confirm: str | None = None) -> Envelope:
    """Reclaim one merged PR's worktree and local/remote branches after consent."""
    run = _load_current(session)
    _require_state(
        run,
        State.PR_OPEN,
        State.CI_MONITORING,
        State.CI_FAILED,
        State.CHECKS_PASSED,
        State.CI_TIMED_OUT,
        State.PR_MERGED,
        command="cleanup",
    )
    if not run.pr_url:
        raise NeedsHuman(
            "this run has no recorded pull request URL",
            state=run.state.value,
            run_id=run.run_id,
        )

    try:
        pr = githubmod.pull_request_status(session.repo_root, run.pr_url)
    except githubmod.GhUnavailable as exc:
        raise GhUnavailableError(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Check the pull request with gh, then retry cleanup.",
            next_command="agentic-preflight cleanup",
        ) from exc

    if not pr.merged:
        raise NeedsHuman(
            f"pull request is {pr.state.lower()}, not merged",
            state=run.state.value,
            run_id=run.run_id,
            data={"pr_url": pr.url, "pr_state": pr.state},
            next_instruction="Wait until the pull request is merged, then retry cleanup.",
            next_command="agentic-preflight cleanup",
        )
    if pr.head != run.branch:
        raise NeedsHuman(
            f"pull request head {pr.head!r} does not match run branch {run.branch!r}",
            state=run.state.value,
            run_id=run.run_id,
        )
    expected_base = run.base_ref.rsplit("/", 1)[-1]
    if pr.base != expected_base:
        raise NeedsHuman(
            f"pull request base {pr.base!r} does not match run base {run.base_ref!r}",
            state=run.state.value,
            run_id=run.run_id,
        )

    base_branch = _cleanup_base_branch(session.repo_root, run.base_ref)
    current_branch = gitx.current_branch(session.repo_root)
    preview = {
        "pr_url": pr.url,
        "merged_at": pr.merged_at,
        "worktree_path": run.worktree_path,
        "worktree_branch": run.worktree_branch,
        "worktree_action": _worktree_cleanup_action(_worktree_mode(run, session.config)),
        "local_branch": run.branch,
        "remote_branch": f"origin/{run.branch}",
        "switch_to": base_branch if current_branch == run.branch else None,
    }

    checked_out_elsewhere = [
        record["worktree"]
        for record in gitx.list_worktrees(session.repo_root)
        if record.get("branch") == f"refs/heads/{run.branch}"
        and Path(record["worktree"]).resolve() != session.repo_root.resolve()
    ]
    if checked_out_elsewhere:
        raise NeedsHuman(
            f"local branch {run.branch!r} is checked out in another worktree",
            state=run.state.value,
            run_id=run.run_id,
            data={**preview, "checked_out_elsewhere": checked_out_elsewhere},
        )

    if confirm is not None and not gatemod.token_matches(run.cleanup_token, confirm):
        raise NeedsConfirm(
            "cleanup requires the confirmation token from the latest preview",
            state=run.state.value,
            run_id=run.run_id,
            data=preview,
            next_instruction=(
                "Preview cleanup again, show it to the user, and use its token only "
                "after they agree."
            ),
            next_command="agentic-preflight cleanup",
        )
    if confirm is not None and run.cleanup_preview != preview:
        raise NeedsConfirm(
            "cleanup targets changed after the latest preview",
            state=run.state.value,
            run_id=run.run_id,
            data=preview,
            next_instruction=(
                "Preview cleanup again and ask the user to approve the updated targets."
            ),
            next_command="agentic-preflight cleanup",
        )

    if confirm is None:
        token = gatemod.mint_token()
        with session.store.transaction(run.run_id) as doc:
            doc.cleanup_token = token
            doc.cleanup_preview = preview
            run = doc
        return _envelope_for(
            run,
            data={**preview, "token": token},
            next_instruction=(
                "Show the user every worktree and branch in this cleanup preview. "
                "Only run the confirmation command after they agree."
            ),
            next_command=f"agentic-preflight cleanup --confirm {token}",
        )

    if current_branch == run.branch:
        if not gitx.is_clean(session.repo_root):
            raise DirtyTree(
                "the PR branch checkout has uncommitted changes; cleanup would switch branches",
                state=run.state.value,
                run_id=run.run_id,
                data=preview,
                next_instruction="Commit or stash the changes, then preview cleanup again.",
                next_command="git status",
            )
        if base_branch is None:
            raise NeedsHuman(
                f"no local branch exists for base ref {run.base_ref!r}",
                state=run.state.value,
                run_id=run.run_id,
                data=preview,
            )

    remote_deleted = gitx.delete_remote_branch(session.repo_root, run.branch)
    if current_branch == run.branch:
        if base_branch is None:
            raise NeedsHuman(
                f"no local branch exists for base ref {run.base_ref!r}",
                state=run.state.value,
                run_id=run.run_id,
                data=preview,
            )
        gitx.run(session.repo_root, "switch", base_branch)
    if gitx.local_branch_exists(session.repo_root, run.branch):
        gitx.run(session.repo_root, "branch", "-D", run.branch)
    _release_run_worktree(session, run)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.CLEANUP)
        doc.worktree_released = True
        doc.cleanup_token = None
        doc.cleanup_preview = None
        run = doc
    session.store.set_current(None)
    session.store.append_event(
        run.run_id,
        {"event": "merged_pr_cleaned", "remote_deleted": remote_deleted},
    )
    return _envelope_for(
        run,
        data={**preview, "remote_deleted": remote_deleted, "cleaned": True},
        next_instruction=_worktree_completion(_worktree_mode(run, session.config), merged_pr=True),
        next_command=None,
    )


def _pr_body(session: Session, run: RunDoc) -> str:
    findings = session.store.load_findings(run.run_id)
    lines = [
        "Verified by agentic-preflight.",
        "",
        "## Intent and acceptance criteria",
        "",
        run.intent or "(not recorded by this legacy run)",
        "",
        "## Validation",
        "",
        f"- review: {len([f for f in findings if f.stage is Stage.REVIEW])} finding(s)",
        f"- docs: {len([f for f in findings if f.stage is Stage.DOCS])} finding(s)",
    ]
    for stage_name, record in run.stages.items():
        if record.status == "skipped":
            lines.append(f"- {stage_name.value}: skipped ({record.reason})")
        else:
            lines.append(f"- {stage_name.value}: {record.status} (`{record.command}`)")
    return "\n".join(lines)
