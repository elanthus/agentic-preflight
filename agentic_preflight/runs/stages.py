"""Deterministic test and lint stage execution."""

from __future__ import annotations

import shlex
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .. import gitx, worktree
from ..attestation import output_digest
from ..envelope import Envelope
from ..errors import (
    DirtyTree,
    MaxAttempts,
    NoLog,
    SetupFailed,
    StageFailed,
    StaleRun,
)
from ..machine import Action, State, legal_actions
from ..models import RunDoc, SetupFailure, Stage, StageRecord
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
    _start_command,
)
from .review import _skip_test_if_not_applicable
from .review_coverage import invalidate_stage_result, reopen_if_stale


@dataclass(frozen=True)
class _BaselineSetupFailure(Exception):
    command: str
    exit_code: int
    worktree_path: str


class _StageSpec(TypedDict):
    running: State
    run: Action
    retry: Action
    passed: Action
    failed: Action
    red: State


_STAGE_STATES: dict[str, _StageSpec] = {
    "lint": {
        "running": State.LINT_RUNNING,
        "run": Action.RUN_LINT,
        "retry": Action.RETRY_LINT,
        "passed": Action.LINT_PASSED,
        "failed": Action.LINT_FAILED,
        "red": State.LINT_RED,
    },
    "test": {
        "running": State.TEST_RUNNING,
        "run": Action.RUN_TEST,
        "retry": Action.RETRY_TEST,
        "passed": Action.TEST_PASSED,
        "failed": Action.TEST_FAILED,
        "red": State.TEST_RED,
    },
}


_STAGE_READY_STATES: dict[str, tuple[State, ...]] = {
    stage_name: tuple(
        state
        for state in State
        if {spec["run"], spec["retry"], spec["failed"]}.intersection(legal_actions(state))
    )
    for stage_name, spec in _STAGE_STATES.items()
}


def _recover_interrupted_stage(
    session: Session, run: RunDoc, stage: Stage, spec: _StageSpec
) -> RunDoc:
    """Turn a persisted running state into an explicit retryable failure."""
    if run.state is not spec["running"]:
        return run
    with session.store.transaction(run.run_id) as doc:
        entry = doc.stages.get(stage) or StageRecord()
        entry.status = "red"
        entry.attempts += 1
        entry.command = None
        entry.exit_code = 125
        entry.reason = "interrupted"
        entry.output_sha256 = None
        entry.log_path = None
        entry.finished_at = _now()
        entry.head_sha = gitx.rev_parse(_require_worktree(doc), "HEAD")
        doc.stages[stage] = entry
        _apply(doc, spec["failed"])
        run = doc
    session.store.append_event(
        run.run_id,
        {"event": f"{stage.value}_interrupted", "attempts": entry.attempts},
    )
    return run


def _register_stage_fix_commits(
    session: Session,
    run: RunDoc,
    stage: Stage,
    spec: _StageSpec,
    record_entry: StageRecord,
) -> tuple[RunDoc, bool]:
    """Register committed repairs made after a red stage attempt."""
    wt = run.worktree_path or session.repo_root
    current_head = gitx.rev_parse(wt, "HEAD")
    if record_entry.head_sha and record_entry.head_sha != current_head:
        if run.state is not spec["red"]:
            with session.store.transaction(run.run_id) as doc:
                invalidate_stage_result(doc, stage)
                run = doc
            return run, False
        if not gitx.is_ancestor(wt, record_entry.head_sha, current_head):
            raise StaleRun(
                f"the validation branch no longer descends from the {stage.value} "
                "stage's recorded head",
                state=run.state.value,
                run_id=run.run_id,
                next_command=_start_command(
                    run.intent,
                    base_ref=run.base_ref,
                    default_base_ref=session.config.general.base_ref,
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
            entry.head_sha = None
            doc.stages[stage] = entry
            if _is_in_place(doc, session.config):
                doc.head_sha = current_head
                doc.source_head_sha = current_head
            doc.review_coverage = None
            invalidate_stage_result(doc, Stage.REVIEW)
            if stage is Stage.LINT:
                # A lint repair changes the tree a future test must describe. This
                # also clears a prior red test when lint is being revalidated after
                # a committed test repair, so the lint commit is not later mistaken
                # for another test repair.
                invalidate_stage_result(doc, Stage.TEST)
                _apply(doc, Action.LINT_FIX_RESTART)
            elif stage is Stage.TEST:
                # The previously green lint result names the pre-repair tree. Drop
                # it before returning to docs/lint so the next lint run starts clean
                # rather than treating this test commit as a lint repair.
                invalidate_stage_result(doc, Stage.LINT)
                _apply(doc, Action.TEST_FIX_RESTART)
            run = doc
        session.store.append_event(
            run.run_id,
            {"event": "stage_fix_commits_registered", "stage": stage.value, "commits": commits},
        )
        return run, True
    return run, False


def _resolve_command(session: Session, run: RunDoc, stage_name: str, override: str | None) -> str:
    """--command flag, then config, then detection. Detection never auto-runs."""
    if override:
        return override
    configured = getattr(session.config.commands, stage_name)
    if configured:
        return configured

    candidates = detect.candidates_for(run.worktree_path or session.repo_root, stage_name)
    selected = candidates[0] if candidates and candidates[0].trust != "untrusted" else None
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
            f"[commands] {stage_name} in .agentic-preflight.toml to settle it permanently. "
            "Workflow-derived candidates must be shown to the user verbatim and approved "
            "before first use."
        ),
        next_command=(
            f"agentic-preflight stage run {stage_name} "
            f"--command '{selected.command if selected else '<command>'}' --record"
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
    if run.state in {State.DOCS_GREEN, State.TEST_GREEN}:
        run, reopened = reopen_if_stale(session, run)
        if reopened:
            return _envelope_for(
                run,
                stage=Stage.REVIEW.value,
                data={"coverage_invalidated": True},
                next_command="agentic-preflight context",
            )
    _require_state(run, *_STAGE_READY_STATES[stage_name], command=f"stage run {stage_name}")
    run = _recover_interrupted_stage(session, run, stage, spec)
    record_entry = run.stages.get(stage) or StageRecord()
    worktree_path = _require_worktree(run)

    if not gitx.is_clean(worktree_path):
        raise DirtyTree(
            "the validation worktree has uncommitted changes",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            next_instruction="Commit the intended repair, or discard incidental output, then retry.",
            next_command="git status",
        )

    run, restarted = _register_stage_fix_commits(session, run, stage, spec, record_entry)
    if restarted:
        return _envelope_for(
            run,
            stage=stage_name,
            data={"stage": stage_name, "validation_restarted": True},
            next_instruction=(
                f"The {stage_name} repair changed the verified tree. Re-run every applicable "
                "stage so each result describes the repaired commit."
            ),
            next_command="agentic-preflight context",
        )
    if record_entry.attempts >= session.config.stage.max_attempts:
        setup_failure = run.setup_failure
        if (
            setup_failure is not None
            and setup_failure.scope == "baseline"
            and setup_failure.stage is stage
        ):
            raise MaxAttempts(
                f"the {stage_name} baseline setup has failed {record_entry.attempts} times "
                f"(max_attempts={session.config.stage.max_attempts}); stopping rather than "
                "looping",
                state=run.state.value,
                run_id=run.run_id,
                stage=stage_name,
                data={
                    "attempts": record_entry.attempts,
                    "stage": stage_name,
                    "setup_failure": setup_failure.model_dump(mode="json"),
                },
                next_instruction=(
                    "The baseline setup never reached the stage, so there is no stage log. "
                    "Abort this run, fix the setup environment, then start a fresh run."
                ),
                next_command="agentic-preflight abort --force",
            )
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
    resolved = _resolve_command(session, run, stage_name, command)
    try:
        secrets = shellstage.read_secrets(worktree_path, run.copied_files)
    except shellstage.SecretRedactionError as exc:
        retry = ["agentic-preflight", "stage", "run", stage_name]
        if command is not None:
            retry.extend(("--command", command))
        if record:
            retry.append("--record")
        if baseline:
            retry.append("--baseline")
        raise StageFailed(
            f"the {stage_name} stage cannot run because copied-file redaction is unavailable",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage_name,
            data={"copied_file": str(exc.path)},
            next_instruction=(
                "Restore the reported copied file as readable text, or remove it from "
                "[worktree] copy_files, then retry the stage."
            ),
            next_command=shlex.join(retry),
        ) from exc

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, spec["retry"] if doc.state is spec["red"] else spec["run"])
        doc.setup_failure = None
        run = doc

    wt = _require_worktree(run)
    baseline_red = None
    if baseline:
        try:
            baseline_red = _baseline_is_red(session, run, resolved)
        except _BaselineSetupFailure as exc:
            retry_command = shlex.join(
                [
                    "agentic-preflight",
                    "stage",
                    "run",
                    stage_name,
                    "--command",
                    resolved,
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
                    command=resolved,
                    reason="baseline setup command failed",
                    finished_at=_now(),
                    head_sha=gitx.rev_parse(wt, "HEAD"),
                )
                doc.stages[stage] = entry
                doc.setup_failure = failure
                _apply(doc, spec["failed"])
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

    result = shellstage.run_stage(
        wt,
        resolved,
        timeout_seconds=session.config.stage.timeout_seconds,
        guarded_files=run.copied_files,
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
            copied_files_changed=result.copied_files_changed,
        )

    redaction_error = None
    try:
        post_run_secrets = shellstage.read_secrets(wt, run.copied_files)
    except shellstage.SecretRedactionError as exc:
        redaction_error = exc
        post_run_secrets = []
    redaction_failure_reason = None
    if redaction_error is not None:
        redaction_failure_reason = "copied-file redaction became unavailable"
    elif result.copied_files_changed:
        redaction_failure_reason = "copied file changed during command execution"
    if redaction_failure_reason is not None:
        result = shellstage.StageResult(
            command=result.command,
            exit_code=result.exit_code if result.exit_code != 0 else 1,
            output=shellstage.REDACTION_FAILURE_OUTPUT,
            timed_out=result.timed_out,
            copied_files_changed=result.copied_files_changed,
        )
        clean_output = result.output
    else:
        clean_output = shellstage.redact(
            result.output,
            shellstage.combine_secrets(secrets, post_run_secrets),
        )

    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(clean_output, encoding="utf-8", newline="\n")

    summary = shellstage.summarise(clean_output)

    with session.store.transaction(run.run_id) as doc:
        entry = doc.stages.get(stage) or StageRecord()
        entry.command = resolved
        entry.reason = redaction_failure_reason
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
        **summary,
    }
    if redaction_error is not None:
        data["copied_file"] = str(redaction_error.path)
    if result.copied_files_changed:
        data["copied_files"] = run.copied_files
    if baseline_red is not None:
        data["baseline_red"] = baseline_red

    if result.passed:
        if stage is Stage.LINT:
            run = _skip_test_if_not_applicable(session, run)
        return _envelope_for(run, stage=stage_name, data=data)

    message = f"the {stage_name} stage failed (exit {result.exit_code})"
    instruction = "Read the log, fix the cause in the worktree, commit, then re-run the stage."
    if redaction_failure_reason is not None:
        message = (
            f"the {stage_name} stage output was withheld because a copied file changed "
            "during execution"
            if redaction_error is None
            else f"the {stage_name} stage output was withheld because redaction became unavailable"
        )
        instruction = (
            "Restore every copied file to its intended contents, then retry the stage. "
            "Commands must not rewrite [worktree] copy_files."
        )
    elif baseline_red:
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


def logs(session: Session, *, stage_name: str) -> Envelope:
    run = _load_current(session)
    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    if not log_path.exists():
        next_command = (
            "agentic-preflight review run"
            if stage_name == "review"
            else f"agentic-preflight stage run {stage_name}"
        )
        raise NoLog(
            f"the {stage_name} stage has not run in this run, so there is no log",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Run the stage first.",
            next_command=next_command,
        )
    return _envelope_for(
        run,
        stage=stage_name,
        data={
            "stage": stage_name,
            "log_path": str(log_path),
            "output": log_path.read_text(encoding="utf-8"),
        },
    )
