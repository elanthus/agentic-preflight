"""Baseline execution and setup-failure recovery for local shell stages."""

from __future__ import annotations

import shlex
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .. import gitx, worktree
from ..errors import SetupFailed
from ..machine import Action
from ..models import RunDoc, SetupFailure, Stage, StageRecord
from ..stages import shellstage
from ._session import Session, _apply, _now, _require_worktree


@dataclass(frozen=True)
class _BaselineSetupFailure(Exception):
    command: str
    exit_code: int
    worktree_path: str


def check_baseline(
    session: Session, run: RunDoc, stage: Stage, command: str, *, failure_action: Action
) -> bool:
    """Return whether the base fails; persist and raise if its setup never completed."""
    stage_name = stage.value
    wt = _require_worktree(run)
    try:
        return _baseline_is_red(session, run, command, worktree_path=wt)
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
            ),
            next_command=retry_command,
        )
        with session.store.transaction(run.run_id) as doc:
            previous = doc.stages.get(stage) or StageRecord()
            entry = StageRecord(
                status="red",
                attempts=previous.attempts + 1,
                command=command,
                reason="baseline setup command failed",
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
        raise SetupFailed(
            f"the baseline setup command failed (exit {exc.exit_code})",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            data={
                "scope": "baseline",
                "worktree_path": exc.worktree_path,
                "setup": {
                    "kind": "custom",
                    "command": exc.command,
                    "exit_code": exc.exit_code,
                },
            },
            next_instruction=failure.next_instruction,
            next_command=failure.next_command,
        ) from exc


def _baseline_is_red(session: Session, run: RunDoc, command: str, *, worktree_path: str) -> bool:
    """Run the command against the base commit in a scratch worktree.

    Answers the question that otherwise sends an agent chasing phantoms: is this
    failure ours, or was the base already broken?
    """
    scratch = Path(worktree_path).parent / f"{run.run_id}-baseline"
    branch = f"ap/{run.run_id}-baseline"
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
            if completed.returncode != 0:
                raise _BaselineSetupFailure(
                    command=session.config.worktree.setup_command,
                    exit_code=completed.returncode,
                    worktree_path=str(scratch),
                )
        result = shellstage.run_stage(
            scratch, command, timeout_seconds=session.config.stage.timeout_seconds
        )
        return not result.passed
    except worktree.WorktreeError:
        return False
    finally:
        worktree.remove(session.repo_root, scratch, branch=branch)
