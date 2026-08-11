"""Deterministic test and lint stage execution."""

from __future__ import annotations

import shlex
from contextlib import suppress
from pathlib import Path
from typing import TypedDict

from .. import dependencies as dependenciesmod
from .. import gitx, runtime, worktree
from ..attestation import output_digest
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    MaxAttempts,
    NoLog,
    StageFailed,
    StaleRun,
)
from ..machine import Action, State
from ..models import RunDoc, Stage, StageRecord
from ..stages import detect, shellstage
from ._session import (
    Session,
    _apply,
    _assert_fresh,
    _envelope_for,
    _is_in_place,
    _load_current,
    _now,
    _require_state,
    _require_worktree,
)
from .review import _advance_after_review, _skip_test_if_not_applicable


class _StageSpec(TypedDict):
    ready: tuple[State, State]
    run: Action
    retry: Action
    passed: Action
    failed: Action
    red: State


_STAGE_STATES: dict[str, _StageSpec] = {
    "lint": {
        "ready": (State.DOCS_GREEN, State.LINT_RED),
        "run": Action.RUN_LINT,
        "retry": Action.RETRY_LINT,
        "passed": Action.LINT_PASSED,
        "failed": Action.LINT_FAILED,
        "red": State.LINT_RED,
    },
    "test": {
        "ready": (State.LINT_GREEN, State.TEST_RED),
        "run": Action.RUN_TEST,
        "retry": Action.RETRY_TEST,
        "passed": Action.TEST_PASSED,
        "failed": Action.TEST_FAILED,
        "red": State.TEST_RED,
    },
}


def _register_stage_fix_commits(
    session: Session, run: RunDoc, stage: Stage, record_entry: StageRecord
) -> tuple[RunDoc, bool]:
    """Register committed repairs made after a red stage attempt."""
    wt = run.worktree_path or session.repo_root
    current_head = gitx.rev_parse(wt, "HEAD")
    if record_entry.head_sha and record_entry.head_sha != current_head:
        if not gitx.is_ancestor(wt, record_entry.head_sha, current_head):
            raise StaleRun(
                f"the validation branch no longer descends from the {stage.value} "
                "stage's recorded head",
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
        commits = gitx.commits_between(wt, record_entry.head_sha, current_head)
        for commit in commits:
            worktree.assert_commit_is_clean_of(wt, commit, run.copied_files)
        with session.store.transaction(run.run_id) as doc:
            for commit in commits:
                if commit not in doc.fix_commits:
                    doc.fix_commits.append(commit)
            entry = doc.stages.get(stage) or StageRecord()
            entry.head_sha = current_head
            doc.stages[stage] = entry
            if _is_in_place(doc, session.config):
                doc.head_sha = current_head
                doc.source_head_sha = current_head
            if stage is Stage.LINT:
                # A lint repair changes the tree a future test must describe. This
                # also clears a prior red test when lint is being revalidated after
                # a committed test repair, so the lint commit is not later mistaken
                # for another test repair.
                doc.stages.pop(Stage.TEST, None)
                _apply(doc, Action.LINT_FIX_RESTART)
            elif stage is Stage.TEST:
                # The previously green lint result names the pre-repair tree. Drop
                # it before returning to docs/lint so the next lint run starts clean
                # rather than treating this test commit as a lint repair.
                doc.stages.pop(Stage.LINT, None)
                _apply(doc, Action.TEST_FIX_RESTART)
            run = doc
        session.store.append_event(
            run.run_id,
            {"event": "stage_fix_commits_registered", "stage": stage.value, "commits": commits},
        )
        return run, stage in {Stage.LINT, Stage.TEST}
    return run, False


def _resolve_command(session: Session, run: RunDoc, stage_name: str, override: str | None) -> str:
    """--command flag, then config, then detection. Detection never auto-runs."""
    if override:
        return override
    configured = getattr(session.config.commands, stage_name)
    if configured:
        return configured

    candidates = detect.candidates_for(run.worktree_path or session.repo_root, stage_name)
    raise StageFailed(
        f"no command configured for the {stage_name} stage",
        state=run.state.value,
        run_id=run.run_id,
        stage=stage_name,
        data={
            "mode": "needs_command",
            "stage": stage_name,
            "candidates": [c.as_dict() for c in candidates],
        },
        next_instruction=(
            f"Pick the command that runs {stage_name} in this repo and re-invoke with "
            f"--command. Detection will not guess on your behalf. Add it to "
            f"[commands] {stage_name} in .agentic-preflight.toml to settle it permanently."
        ),
        next_command=(
            f"agentic-preflight stage run {stage_name} "
            f"--command '{candidates[0].command if candidates else '<command>'}' --record"
        ),
    )


def run_stage(
    session: Session,
    stage_name: str,
    *,
    command: str | None = None,
    record: bool = False,
    baseline: bool = False,
) -> Envelope:
    """Run a deterministic shell stage. Pass/fail is the exit code, nothing else."""
    run = _load_current(session)
    spec = _STAGE_STATES[stage_name]
    stage = Stage(stage_name)
    record_entry = run.stages.get(stage) or StageRecord()
    accepting_repair = (
        _is_in_place(run, session.config)
        and run.state is spec["red"]
        and record_entry.head_sha is not None
    )
    if not accepting_repair:
        _assert_fresh(session, run)
    _require_state(run, *spec["ready"], command=f"stage run {stage_name}")
    worktree_path = _require_worktree(run)

    if record_entry.attempts >= session.config.stage.max_attempts:
        raise MaxAttempts(
            f"the {stage_name} stage has failed {record_entry.attempts} times "
            f"(max_attempts={session.config.stage.max_attempts}); stopping rather than "
            f"looping",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            data={"attempts": record_entry.attempts, "stage": stage_name},
            next_instruction=(
                "This needs a person. Show the user the stage log and the last failure, "
                "and ask how to proceed."
            ),
            next_command=f"agentic-preflight logs --stage {stage_name}",
        )

    if not gitx.is_clean(worktree_path):
        raise DirtyTree(
            "the validation worktree has uncommitted changes",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            next_instruction="Commit the intended repair, or discard incidental output, then retry.",
            next_command="git status",
        )

    run, restarted = _register_stage_fix_commits(session, run, stage, record_entry)
    if restarted:
        run = _advance_after_review(session, run)
        return _envelope_for(
            run,
            stage=stage_name,
            data={"stage": stage_name, "validation_restarted": True},
            next_instruction=(
                f"The {stage_name} repair changed the verified tree. Re-run every applicable "
                "stage so each result describes the repaired commit."
            ),
        )
    resolved = _resolve_command(session, run, stage_name, command)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, spec["retry"] if doc.state is spec["red"] else spec["run"])
        run = doc

    wt = _require_worktree(run)
    baseline_red = None
    if baseline:
        baseline_red = _baseline_is_red(session, run, resolved)

    prepared = runtime.prepare_command(
        wt,
        resolved,
        manager=session.config.runtime.manager,
        strict=session.config.runtime.strict,
    )
    result = shellstage.run_stage(
        wt,
        prepared.command,
        timeout_seconds=session.config.stage.timeout_seconds,
    )
    if not gitx.is_clean(wt):
        result = shellstage.StageResult(
            command=result.command,
            exit_code=result.exit_code or 1,
            output=(
                result.output
                + "\n[agentic-preflight] stage changed the worktree; validation results are stale"
            ),
            timed_out=result.timed_out,
        )

    secrets = shellstage.read_secrets(wt, run.copied_files)
    clean_output = shellstage.redact(result.output, secrets)

    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(clean_output)

    summary = shellstage.summarise(clean_output)

    with session.store.transaction(run.run_id) as doc:
        entry = doc.stages.get(stage) or StageRecord()
        entry.command = resolved
        entry.exit_code = result.exit_code
        entry.output_sha256 = output_digest(clean_output)
        entry.log_path = str(log_path)
        entry.status = "green" if result.passed else "red"
        entry.finished_at = _now()
        entry.head_sha = gitx.rev_parse(wt, "HEAD")
        if not result.passed:
            entry.attempts += 1
        doc.stages[stage] = entry
        _apply(doc, spec["passed"] if result.passed else spec["failed"])
        run = doc

    session.store.append_event(
        run.run_id,
        {"event": f"{stage_name}_finished", "exit_code": result.exit_code},
    )

    data = {
        "stage": stage_name,
        "command": resolved,
        "exit_code": result.exit_code,
        "log_path": str(log_path),
        "timed_out": result.timed_out,
        "runtime": prepared.runtime.as_dict(),
        **summary,
    }
    if baseline_red is not None:
        data["baseline_red"] = baseline_red

    if result.passed:
        if stage is Stage.LINT:
            run = _skip_test_if_not_applicable(session, run)
        return _envelope_for(run, stage=stage_name, data=data)

    message = f"the {stage_name} stage failed (exit {result.exit_code})"
    instruction = "Read the log, fix the cause in the worktree, commit, then re-run the stage."
    if baseline_red:
        message = (
            f"the {stage_name} stage failed, but the base commit fails it too — "
            f"this is pre-existing, not caused by the diff"
        )
        instruction = (
            "The base commit already fails this stage, so the change under review is "
            "not responsible. Tell the user rather than trying to fix it here."
        )

    raise StageFailed(
        message,
        state=run.state.value,
        run_id=run.run_id,
        stage=stage_name,
        data=data,
        next_instruction=instruction,
        next_command=f"agentic-preflight logs --stage {stage_name}",
    )


def _baseline_is_red(session: Session, run: RunDoc, command: str) -> bool:
    """Run the command against the base commit in a scratch worktree.

    Answers the question that otherwise sends an agent chasing phantoms: is this
    failure ours, or was the base already broken?
    """
    worktree_path = _require_worktree(run)
    scratch = Path(worktree_path).parent / f"{run.run_id}-baseline"
    branch = f"ap/{run.run_id}-baseline"
    try:
        worktree.create(session.repo_root, path=scratch, branch=branch, head_sha=run.merge_base_sha)
        with suppress(worktree.CopyRefused):
            worktree.copy_files(session.repo_root, scratch, session.config.worktree.copy_files)
        if session.config.worktree.setup_command:
            setup = runtime.prepare_command(
                scratch,
                session.config.worktree.setup_command,
                manager=session.config.runtime.manager,
                strict=session.config.runtime.strict,
            )
            worktree.run_setup(
                scratch,
                setup.command,
                timeout_seconds=session.config.stage.timeout_seconds,
            )
        elif session.config.worktree.dependency_setup == "auto":
            dependenciesmod.setup(
                scratch,
                runtime_manager=session.config.runtime.manager,
                runtime_strict=session.config.runtime.strict,
                timeout_seconds=session.config.stage.timeout_seconds,
            )
        prepared = runtime.prepare_command(
            scratch,
            command,
            manager=session.config.runtime.manager,
            strict=session.config.runtime.strict,
        )
        result = shellstage.run_stage(
            scratch, prepared.command, timeout_seconds=session.config.stage.timeout_seconds
        )
        return not result.passed
    except worktree.WorktreeError:
        return False
    finally:
        worktree.remove(session.repo_root, scratch, branch=branch)


def logs(session: Session, *, stage_name: str) -> Envelope:
    run = _load_current(session)
    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    if not log_path.exists():
        raise NoLog(
            f"the {stage_name} stage has not run in this run, so there is no log",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Run the stage first.",
            next_command=f"agentic-preflight stage run {stage_name}",
        )
    return _envelope_for(
        run,
        stage=stage_name,
        data={"stage": stage_name, "log_path": str(log_path), "output": log_path.read_text()},
    )
