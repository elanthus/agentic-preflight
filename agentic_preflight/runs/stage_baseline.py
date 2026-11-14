"""Baseline execution and setup-failure recovery for local shell stages."""

from __future__ import annotations

import shlex
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .. import gitx, worktree
from ..errors import SetupFailed
from ..machine import Action
from ..models import RunDoc, SetupFailure, Stage, StageRecord, StageStatus
from ..stages import shellstage
from ._session import Session, _apply, _now, _require_worktree


@dataclass(frozen=True)
class _BaselineSetupFailure(Exception):
    command: str
    exit_code: int
    worktree_path: str
    kind: str = "custom"
    reason: str = "baseline setup command failed"
    detail: str = ""


def check_baseline(
    session: Session, run: RunDoc, stage: Stage, command: str, *, failure_action: Action
) -> bool:
    """Return whether the base fails; persist and raise if its setup never completed."""
    stage_name = stage.value
    wt = _require_worktree(run)
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
                "The stage was not evaluated against the base commit. Fix the setup "
                "environment, then retry the same stage with its baseline check."
                + (f" Worktree error: {exc.detail}" if exc.detail else "")
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
                finished_at=_now(),
                head_sha=gitx.rev_parse(wt, "HEAD"),
            )
            doc.stages[stage] = entry
            doc.setup_failure = failure
            _apply(doc, failure_action)
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
            },
            next_instruction=failure.next_instruction,
            next_command=failure.next_command,
        ) from exc


def _baseline_is_red(session: Session, run: RunDoc, command: str) -> bool:
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
        raise _BaselineSetupFailure(
            command=shlex.join(["git", "worktree", "add", "-b", branch, intended]),
            exit_code=1,
            worktree_path=intended,
            kind="worktree",
            reason="baseline worktree could not be prepared",
            detail=str(exc),
        ) from exc
    scratch = root / f"{run.run_id}-baseline"
    try:
        worktree.create(session.repo_root, path=scratch, branch=branch, head_sha=run.merge_base_sha)
        with suppress(worktree.CopyRefused):
            worktree.copy_files(session.repo_root, scratch, session.config.worktree.copy_files)
        if session.config.worktree.setup_command:
            completed = worktree.run_setup(
                scratch,
                session.config.worktree.setup_command,
                timeout_seconds=session.config.stage.timeout_seconds,
            )
            if completed.exit_code != 0:
                raise _BaselineSetupFailure(
                    command=session.config.worktree.setup_command,
                    exit_code=completed.exit_code,
                    worktree_path=str(scratch),
                )
        result = shellstage.run_stage(
            scratch, command, timeout_seconds=session.config.stage.timeout_seconds
        )
        return not result.passed
    except worktree.WorktreeError as exc:
        raise _BaselineSetupFailure(
            command=shlex.join(["git", "worktree", "add", "-b", branch, str(scratch)]),
            exit_code=1,
            worktree_path=str(scratch),
            kind="worktree",
            reason="baseline worktree could not be prepared",
            detail=str(exc),
        ) from exc
    finally:
        _remove_scratch(session, run, scratch, branch)


def _remove_scratch(session: Session, run: RunDoc, scratch: Path, branch: str) -> None:
    """Tear down the baseline checkout without letting cleanup replace the outcome.

    The baseline result, or the setup failure being raised, is the answer the agent
    asked for; a cleanup refusal must not overwrite it. A worktree someone locked
    stays in place as :func:`worktree.remove` promises, but it is detached first so
    the throwaway ``ap/<run>-baseline`` branch can still be deleted, and the
    refusal is recorded in the run's event log.
    """
    try:
        worktree.remove(session.repo_root, scratch, branch=branch)
        return
    except (worktree.WorktreeError, gitx.GitError, OSError) as exc:
        detail = str(exc)
    with suppress(gitx.GitError, OSError):
        gitx.run(scratch, "switch", "--detach", check=False)
    with suppress(gitx.GitError, OSError):
        gitx.run(session.repo_root, "branch", "-D", branch, check=False)
    with suppress(Exception):
        session.store.append_event(
            run.run_id,
            {
                "event": "baseline_cleanup_failed",
                "worktree_path": str(scratch),
                "branch": branch,
                "detail": detail,
            },
        )
