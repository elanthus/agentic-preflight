"""Baseline execution and setup-failure recovery for local shell stages."""

from __future__ import annotations

import os
import shlex
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from .. import gitx, worktree
from ..errors import SetupFailed
from ..machine import Action
from ..models import RunDoc, SetupFailure, Stage, StageRecord, StageStatus
from ..stages import shellstage
from ._session import Session, apply_action, require_worktree, utc_now


@dataclass(frozen=True)
class _BaselineSetupFailure(Exception):
    command: str
    exit_code: int
    worktree_path: str
    kind: str = "custom"
    reason: str = "baseline setup command failed"
    detail: str = ""
    instruction: str = ""
    cleanup_failure: dict[str, str] | None = None


@dataclass(frozen=True)
class BaselineOutcome:
    """Whether the base commit fails, and any scratch checkout that was left behind."""

    red: bool
    cleanup_failure: dict[str, str] | None = None


def check_baseline(
    session: Session, run: RunDoc, stage: Stage, command: str, *, failure_action: Action
) -> BaselineOutcome:
    """Return whether the base fails; persist and raise if its setup never completed."""
    stage_name = stage.value
    wt = require_worktree(run)
    try:
        return _baseline_is_red(session, run, command)
    except _BaselineSetupFailure as exc:
        retry_command = shlex.join(
            [
                "agentic-preflight",
                "stage",
                "run",
                stage_name,
                "--command",
                command,
                "--record",
                "--baseline",
            ]
        )
        failure = SetupFailure(
            scope="baseline",
            stage=stage,
            command=exc.command,
            exit_code=exc.exit_code,
            worktree_path=exc.worktree_path,
            next_instruction=(
                "The stage was not evaluated against the base commit. "
                + (
                    exc.instruction
                    or "Fix the setup environment, then retry the same stage with its "
                    "baseline check."
                    + (f" Worktree error: {exc.detail}" if exc.detail else "")
                )
                + (" " + cleanup_instruction(exc.cleanup_failure) if exc.cleanup_failure else "")
            ),
            next_command=retry_command,
        )
        with session.store.transaction(run.run_id) as doc:
            previous = doc.stages.get(stage) or StageRecord()
            entry = StageRecord(
                status=StageStatus.RED,
                attempts=previous.attempts + 1,
                command=command,
                reason=exc.reason,
                started_at=previous.started_at,
                finished_at=utc_now(),
                head_sha=gitx.rev_parse(wt, "HEAD"),
            )
            doc.stages[stage] = entry
            doc.setup_failure = failure
            apply_action(doc, failure_action)
            run = doc
        session.store.append_event(
            run.run_id,
            {"event": "setup_failed", **failure.model_dump(mode="json")},
        )
        message = (
            f"the baseline worktree could not be prepared: {exc.detail}"
            if exc.kind == "worktree"
            else f"the baseline setup command failed (exit {exc.exit_code})"
        )
        raise SetupFailed(
            message,
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            data={
                "scope": "baseline",
                "worktree_path": exc.worktree_path,
                "setup": {
                    "kind": exc.kind,
                    "command": exc.command,
                    "exit_code": exc.exit_code,
                },
                **({"baseline_cleanup_failed": exc.cleanup_failure} if exc.cleanup_failure else {}),
            },
            next_instruction=failure.next_instruction,
            next_command=failure.next_command,
        ) from exc


def cleanup_instruction(failure: dict[str, str]) -> str:
    """Tell the agent about a scratch checkout the baseline could not remove."""
    return (
        f"The baseline scratch worktree {failure['worktree_path']} could not be removed: "
        f"{failure['detail']} Until it is gone, a later --baseline check fails."
    )


def _baseline_is_red(session: Session, run: RunDoc, command: str) -> BaselineOutcome:
    """Run the command against the base commit in a scratch worktree.

    Answers the question that otherwise sends an agent chasing phantoms: is this
    failure ours, or was the base already broken? The scratch checkout lives under
    the external validation-worktree root in every mode, in-place included, so
    runners that skip VCS directories (Jest among them) see an ordinary checkout
    rather than one inside the Git directory.
    """
    branch = f"ap/{run.run_id}-baseline"
    configured = session.config.worktree.root
    try:
        root = worktree.resolve_root(session.repo_root, configured)
    except worktree.WorktreeError as exc:
        intended = str(Path(configured or "") / f"{run.run_id}-baseline")
        raise _worktree_failure(branch, intended, str(exc)) from exc
    scratch = root / f"{run.run_id}-baseline"
    _reclaim_leftovers(session, run, scratch, branch)

    created = False
    red = False  # set by the stage run; every other path leaves ``pending`` set
    pending: _BaselineSetupFailure | None = None
    pending_cause: BaseException | None = None
    try:
        worktree.create(session.repo_root, path=scratch, branch=branch, head_sha=run.merge_base_sha)
        created = True
        with suppress(worktree.CopyRefused):
            worktree.copy_files(session.repo_root, scratch, session.config.worktree.copy_files)
        if session.config.worktree.setup_command:
            completed = worktree.run_setup(
                scratch,
                session.config.worktree.setup_command,
                timeout_seconds=session.config.stage.timeout_seconds,
            )
            if completed.exit_code != 0:
                pending = _BaselineSetupFailure(
                    command=session.config.worktree.setup_command,
                    exit_code=completed.exit_code,
                    worktree_path=str(scratch),
                )
        if pending is None:
            result = shellstage.run_stage(
                scratch, command, timeout_seconds=session.config.stage.timeout_seconds
            )
            red = not result.passed
    except worktree.WorktreeError as exc:
        pending, pending_cause = _worktree_failure(branch, str(scratch), str(exc)), exc
    finally:
        # Only what this call created is ours to remove: a refusal to reuse an
        # existing branch must not be followed by deleting that branch.
        cleanup = _remove_scratch(session, run, scratch, branch) if created else None
    if pending is not None:
        raise replace(pending, cleanup_failure=cleanup) from pending_cause
    return BaselineOutcome(red=red, cleanup_failure=cleanup)


def _reclaim_leftovers(session: Session, run: RunDoc, scratch: Path, branch: str) -> None:
    """Clear what an earlier check of this run left behind, or refuse by name.

    A baseline killed by a signal skips its own cleanup and leaves the scratch
    worktree and its branch. Only a path Git has registered as a worktree is
    removed, never an arbitrary directory, and never a locked one. The branch is
    this run's leftover only while it still points at the run's base commit and no
    worktree has it checked out; anything else may be someone's work.
    """
    repo = session.repo_root
    target = os.path.realpath(scratch)
    for record in gitx.list_worktrees(repo):
        listed = record.get("worktree")
        if listed is None or os.path.realpath(listed) != target:
            continue
        if "locked" in record:
            message = worktree.locked_message(
                scratch, record["locked"], "so the base cannot be checked out"
            )
            raise _worktree_failure(branch, str(scratch), message, instruction=message)
        try:
            # Also prunes a registration whose directory is already gone.
            worktree.remove(repo, scratch)
        except worktree.WorktreeError as exc:
            raise _worktree_failure(branch, str(scratch), str(exc)) from exc
        break

    ref = f"refs/heads/{branch}"
    if gitx.run(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
        return
    holders = [r["worktree"] for r in gitx.list_worktrees(repo) if r.get("branch") == ref]
    tip = gitx.rev_parse(repo, ref)
    if holders:
        problem = f"branch {branch} already exists and is checked out in {holders[0]}"
    elif tip != run.merge_base_sha:
        problem = (
            f"branch {branch} already exists at {tip[:12]}, not at this run's base commit "
            f"{run.merge_base_sha[:12]}, so it may hold commits beyond the base"
        )
    else:
        gitx.run(repo, "branch", "-D", branch)
        return
    message = f"{problem}; the baseline check neither reuses nor deletes it."
    instruction = (
        f"Show the user branch {branch}; do not delete or rename it yourself. Retry the "
        "same stage with its baseline check only after the user removes or renames it."
    )
    raise _worktree_failure(branch, str(scratch), message, instruction=instruction)


def _worktree_failure(
    branch: str, path: str, detail: str, *, instruction: str = ""
) -> _BaselineSetupFailure:
    return _BaselineSetupFailure(
        command=shlex.join(["git", "worktree", "add", "-b", branch, path]),
        exit_code=1,
        worktree_path=path,
        kind="worktree",
        reason="baseline worktree could not be prepared",
        detail=detail,
        instruction=instruction,
    )


def _remove_scratch(
    session: Session, run: RunDoc, scratch: Path, branch: str
) -> dict[str, str] | None:
    """Tear down the baseline checkout without letting cleanup replace the outcome.

    The baseline result, or the setup failure being raised, is the answer the agent
    asked for; a cleanup refusal must not overwrite it. A worktree someone locked
    stays in place as :func:`worktree.remove` promises, but it is detached first so
    the throwaway ``ap/<run>-baseline`` branch can still be deleted. The refusal is
    recorded in the run's event log and returned so the envelope can report it.
    """
    try:
        worktree.remove(session.repo_root, scratch, branch=branch)
        return None
    except (worktree.WorktreeError, gitx.GitError, OSError) as exc:
        detail = str(exc)
    # Without its own ``.git`` file, git would walk up and detach an enclosing repository.
    if (scratch / ".git").is_file():
        with suppress(gitx.GitError, OSError):
            gitx.run(scratch, "switch", "--detach", check=False)
    with suppress(gitx.GitError, OSError):
        gitx.run(session.repo_root, "branch", "-D", branch, check=False)
    failure = {"worktree_path": str(scratch), "branch": branch, "detail": detail}
    with suppress(Exception):
        session.store.append_event(run.run_id, {"event": "baseline_cleanup_failed", **failure})
    return failure
