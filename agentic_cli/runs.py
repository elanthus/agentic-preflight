"""Run orchestration: the logic behind each command.

``cli.py`` is argument parsing and envelope emission only, so everything that
decides *what happens* lives here. Each entry point takes a :class:`Session` and
returns an :class:`Envelope`; none of them print, and none of them call
``sys.exit``. That keeps them directly testable and keeps the transport concerns
in one place.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from . import diff as diffmod
from . import findings as findingsmod
from . import gitx, runtime, worktree
from . import ledger as ledgermod
from . import mergeback as mergebackmod
from .publish import gate as gatemod
from .publish import github as githubmod
from .publish import provider as providermod
from .stages import detect, shellstage
from .stages import docs as docsstage
from .config import Config, load_config
from .envelope import Envelope
from .errors import (
    DirtyTree,
    DiffTooLarge,
    EmptyDiff,
    InvalidFindings,
    InvalidResponse,
    NoRun,
    StaleRun,
    MaxAttempts,
    GhUnavailableError,
    ManualGate,
    MergebackConflictError,
    NeedsConfirm,
    NeedsHuman,
    NoLog,
    StageFailed,
    UnknownFinding,
    UnmergedWork,
    WrongState,
)
from .machine import Action, IllegalTransition, State, next_state
from .models import FindingStatus, FindingSubmission, RunDoc, Stage, StageRecord
from .store import Store

STATE_DIR_NAME = "agentic-cli"


@dataclass
class Session:
    """Everything a command needs about *where* it is running."""

    repo_root: Path
    store: Store
    config: Config


def open_session(cwd: Path | str | None = None) -> Session:
    cwd = Path(cwd) if cwd else Path.cwd()
    repo_root = gitx.repo_root(cwd)
    # GIT_COMMON_DIR, not GIT_DIR: these differ when the caller is already
    # inside a worktree, and run state must be one namespace per clone.
    state_root = gitx.git_common_dir(cwd) / STATE_DIR_NAME
    store = Store(state_root)

    # Once a run exists, its resolved snapshot is authoritative. This also
    # keeps a malformed or edited working-copy config from stranding `status`
    # or silently reshaping an in-flight gate.
    cfg = None
    current = store.get_current()
    if current:
        try:
            active = store.load_run(current)
            if active.config_snapshot is not None:
                cfg = Config.model_validate(active.config_snapshot)
        except Exception:
            pass
    cfg = cfg or load_config(repo_root)
    store.set_worktrees_root(worktree.resolve_root(repo_root, cfg.worktree.root))
    return Session(repo_root=repo_root, store=store, config=cfg)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def _apply(run: RunDoc, action: Action) -> None:
    """Advance the run, converting an illegal move into a typed error."""
    try:
        run.state = next_state(run.state, action)
    except IllegalTransition as exc:
        raise WrongState(str(exc)) from exc


def _require_state(run: RunDoc, *allowed: State, command: str) -> None:
    if run.state not in allowed:
        raise WrongState(
            f"`{command}` is not legal in state {run.state.name}",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Run `status` to see where the run actually is, then obey `next`.",
            next_command="agentic-cli status",
        )


def _next_hint(state: State) -> tuple[str | None, str | None]:
    """The default next legal command for a state, or nothing when terminal.

    A default, not a rule: `next` is properly a function of *(state, what just
    happened)*. ``start`` and ``context`` both land in
    ``REVIEW_AWAITING_FINDINGS`` but call for different moves — fetch the diff
    versus judge the diff you now hold — so those commands override this.
    """
    return {
        State.CREATED: ("Create the worktree.", "agentic-cli start"),
        State.WORKTREE_READY: ("Fetch the diff to review.", "agentic-cli context"),
        State.REVIEW_AWAITING_FINDINGS: (
            "Review the diff, then submit findings (an empty list is a valid outcome).",
            "agentic-cli submit-findings --file findings.json",
        ),
        State.REVIEW_SUBMITTED: ("Check the blocking set.", "agentic-cli verify"),
        State.REVIEW_AWAITING_RESPONSES: (
            "Resolve each blocking finding with `respond`.",
            "agentic-cli respond --id F001 --action fixed --commit <sha>",
        ),
        State.REVIEW_FIXING: (
            "Keep responding until nothing blocks, then verify.",
            "agentic-cli verify",
        ),
        State.REVIEW_GREEN: ("Review is green. Move to the docs stage.", "agentic-cli context --section docs"),
        State.DOCS_GREEN: ("Docs are green. Run lint.", "agentic-cli stage run lint"),
        State.LINT_GREEN: ("Lint is green. Run tests.", "agentic-cli stage run test"),
        State.TEST_GREEN: ("Tests are green. Merge the fixes back.", "agentic-cli mergeback"),
        State.MERGEBACK_CONFLICT: (
            "Resolve the reported conflict or restore the affected paths, then retry mergeback.",
            "agentic-cli mergeback",
        ),
        State.VERIFIED: ("Everything is green. Open the gate.", "agentic-cli gate"),
        State.AWAITING_PUSH_CONFIRM: (
            "Show the user the gate summary and ask before pushing.",
            "agentic-cli push --confirm <token>",
        ),
        State.PUSHED: ("Open the pull request.", "agentic-cli pr"),
    }.get(state, (None, None))


def _envelope_for(run: RunDoc, **overrides) -> Envelope:
    instruction, command = _next_hint(run.state)
    fields = dict(
        run_id=run.run_id,
        state=run.state.value,
        next_instruction=instruction,
        next_command=command,
    )
    fields.update(overrides)
    return Envelope(**fields)


# -- run lookup and staleness -----------------------------------------------


def _load_current(session: Session) -> RunDoc:
    run_id = session.store.get_current()
    if not run_id:
        raise NoRun()
    try:
        return session.store.load_run(run_id)
    except Exception as exc:  # a dangling `current` pointer is a missing run
        raise NoRun(f"current run {run_id} is missing from the store") from exc


def _head_moved(session: Session, run: RunDoc) -> str | None:
    """Return the current tip if it differs from the reviewed one."""
    try:
        tip = gitx.rev_parse(session.repo_root, run.branch)
    except gitx.GitError:
        return None
    return None if tip == run.head_sha else tip


def _assert_fresh(session: Session, run: RunDoc) -> None:
    tip = _head_moved(session, run)
    if tip is None:
        return
    if not run.stale:
        with session.store.transaction(run.run_id) as doc:
            doc.stale = True
    raise StaleRun(
        f"branch {run.branch} has moved to {tip[:8]}; this run reviewed {run.head_sha[:8]}",
        state=run.state.value,
        run_id=run.run_id,
    )


# -- start ------------------------------------------------------------------


def start(session: Session, *, base_ref: str | None = None) -> Envelope:
    repo = session.repo_root
    # Starting is the one command that deliberately reads the working copy.
    # Every later command uses the snapshot persisted below.
    cfg = load_config(repo)
    session.config = cfg
    session.store.set_worktrees_root(worktree.resolve_root(repo, cfg.worktree.root))

    if not gitx.is_clean(repo):
        raise DirtyTree(
            "the working tree has uncommitted changes",
            next_instruction="Commit or stash your changes, then start the run.",
            next_command="git status",
        )

    base_ref = base_ref or cfg.general.base_ref
    branch = gitx.current_branch(repo)
    head_sha = gitx.rev_parse(repo, "HEAD")
    try:
        merge_base = gitx.merge_base(repo, base_ref, "HEAD")
    except gitx.GitError as exc:
        raise EmptyDiff(f"cannot find a merge base with {base_ref!r}: {exc}") from exc

    changed = gitx.changed_files(repo, merge_base, "HEAD")
    if not changed:
        raise EmptyDiff(
            f"{branch} has no changes over {base_ref}; there is nothing to review",
            next_instruction="Commit some work on a branch, then start a run.",
        )

    run_id = _new_run_id()
    snapshot = cfg.model_dump(mode="json")
    config_digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run = RunDoc(
        run_id=run_id,
        state=State.CREATED,
        branch=branch,
        base_ref=base_ref,
        merge_base_sha=merge_base,
        head_sha=head_sha,
        config_snapshot=snapshot,
        config_digest=config_digest,
        created_at=_now(),
    )
    # Persist the intent *before* the git call, so a crash mid-create leaves a
    # record for `gc` to reconcile rather than an orphan nobody knows about.
    session.store.create_run(run)
    session.store.set_current(run_id)
    session.store.append_event(
        run_id,
        {
            "event": "run_created",
            "head_sha": head_sha,
            "config_digest": config_digest,
            "config_snapshot": snapshot,
        },
    )

    wt_path = session.store.worktrees_dir / run_id
    wt_branch = f"ac/{run_id}"
    worktree.create(repo, path=wt_path, branch=wt_branch, head_sha=head_sha)

    copied = worktree.copy_files(repo, wt_path, cfg.worktree.copy_files)

    setup_result = None
    if cfg.worktree.setup_command:
        prepared = runtime.prepare_command(
            wt_path,
            cfg.worktree.setup_command,
            manager=cfg.runtime.manager,
            strict=cfg.runtime.strict,
        )
        completed = worktree.run_setup(
            wt_path, prepared.command, timeout_seconds=cfg.stage.timeout_seconds
        )
        setup_result = {
            "command": cfg.worktree.setup_command,
            "exit_code": completed.returncode,
            "runtime": prepared.runtime.as_dict(),
        }

    with session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        doc.copied_files = copied
        _apply(doc, Action.CREATE_WORKTREE)
        _apply(doc, Action.BEGIN_REVIEW)
        run = doc

    session.store.append_event(run_id, {"event": "worktree_ready", "path": str(wt_path)})

    return _envelope_for(
        run,
        next_instruction="Fetch the diff before judging it.",
        next_command="agentic-cli context",
        data={
            "worktree_path": str(wt_path),
            "worktree_branch": wt_branch,
            "branch": branch,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "merge_base_sha": merge_base,
            "changed_files": changed,
            # Names only. Contents are never read, logged, or echoed.
            "copied_files": copied,
            "setup": setup_result,
        },
    )


# -- context ----------------------------------------------------------------


def _bundle_for(session: Session, run: RunDoc) -> diffmod.DiffBundle:
    return diffmod.build_bundle(
        run.worktree_path or session.repo_root,
        run.merge_base_sha,
        "HEAD",
        exclude=session.config.diff.exclude,
    )


def _open_docs_stage(session: Session, run: RunDoc) -> RunDoc:
    """Move REVIEW_GREEN into the docs sub-machine."""
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.BEGIN_DOCS)
        return doc


def _skip_docs_if_disabled(session: Session, run: RunDoc) -> RunDoc:
    """Skip the docs stage as an explicit transition, never a silent pass.

    ``[docs] enabled = false`` exists for repos with no meaningful doc surface.
    Modelling it as a real transition to DOCS_GREEN keeps the ledger honest:
    the stage was skipped by configuration, and that is a recorded fact rather
    than an absence.
    """
    if session.config.docs.enabled or run.state is not State.REVIEW_GREEN:
        return run
    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.SKIP_DOCS)
        run = doc
    session.store.append_event(run.run_id, {"event": "docs_skipped", "reason": "disabled"})
    return run


def context(session: Session, *, section: str = "review") -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)

    if section == "docs":
        _require_state(
            run,
            State.REVIEW_GREEN,
            State.DOCS_AWAITING_FINDINGS,
            command="context --section docs",
        )
        if run.state is State.REVIEW_GREEN:
            run = _open_docs_stage(session, run)
    else:
        _require_state(
            run,
            State.WORKTREE_READY,
            State.REVIEW_AWAITING_FINDINGS,
            command="context",
        )

    bundle = _bundle_for(session, run)
    report = diffmod.check_budget(bundle, session.config.diff.max_bytes)
    if report.over_budget:
        raise DiffTooLarge(
            f"the diff is {report.total_bytes} bytes, over the {report.max_bytes} byte budget",
            state=run.state.value,
            run_id=run.run_id,
            stage=section,
            data={
                "mode": "diff_too_large",
                "total_bytes": report.total_bytes,
                "max_bytes": report.max_bytes,
                "by_file": [list(item) for item in report.by_file],
            },
            next_instruction=(
                "Narrow the diff before reviewing it. Add generated or vendored paths "
                "to `[diff] exclude` in .agentic-cli.toml, or raise `[diff] max_bytes` "
                "if the change really is this large. The diff is never truncated, so "
                "reviewing it partially is not an option."
            ),
            next_command="agentic-cli context",
        )

    data = {
        "section": section,
        "worktree_path": run.worktree_path,
        "base": run.merge_base_sha,
        "head": run.head_sha,
        "changed_files": bundle.files,
        "excluded_files": bundle.excluded,
        "diff": bundle.text,
        "diff_bytes": bundle.total_bytes,
    }

    if section == "docs":
        inventory = docsstage.build_inventory(
            run.worktree_path, bundle.files, session.config.docs.paths
        )
        data["doc_surface"] = [entry.as_dict() for entry in inventory]
        data["require_changelog"] = session.config.docs.require_changelog

    envelope = _envelope_for(run, stage=section, data=data)
    if section == "docs":
        envelope.next_instruction = (
            "Ask one question of the diff: would a reader following the current "
            "docs now be wrong? Submit findings against documentation files only. "
            "Zero findings is a normal and common outcome."
        )
        envelope.next_command = "agentic-cli submit-findings --file findings.json"
    return envelope


# -- submit-findings --------------------------------------------------------


def _parse_submissions(payload) -> list[FindingSubmission]:
    if isinstance(payload, dict):
        payload = payload.get("findings", [])
    if not isinstance(payload, list):
        raise InvalidFindings(
            "expected a JSON list of findings, or an object with a `findings` key"
        )
    try:
        return [FindingSubmission.model_validate(item) for item in payload]
    except ValidationError as exc:
        raise InvalidFindings(_describe_validation(exc)) from exc


def _describe_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        if error["type"] == "extra_forbidden":
            parts.append(
                f"{location}: not a field you may set — id and stage are assigned by "
                f"agentic-cli, never supplied by the agent"
            )
        else:
            parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def submit_findings(session: Session, payload) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_AWAITING_FINDINGS,
        State.DOCS_AWAITING_FINDINGS,
        command="submit-findings",
    )

    stage = findingsmod.stage_for_state(run.state)
    submissions = _parse_submissions(payload)
    bundle = _bundle_for(session, run)
    existing = session.store.load_findings(run.run_id)

    inventory = None
    if stage is Stage.DOCS:
        # Docs findings may target files the diff never touched — that is the
        # point of the stage — so the changed-file constraint relaxes to the
        # documentation allowlist rather than disappearing.
        inventory = docsstage.build_inventory(
            run.worktree_path, bundle.files, session.config.docs.paths
        )
        allowed = docsstage.allowlist(inventory)
    else:
        allowed = set(bundle.files)

    blocking_severities = (
        session.config.review.blocking_severities
        if stage is Stage.REVIEW
        else session.config.docs.blocking_severities
    )

    try:
        accepted = findingsmod.validate_and_assign(
            submissions,
            stage=stage,
            worktree_path=run.worktree_path,
            allowed_paths=allowed,
            existing=existing,
            max_findings=session.config.review.max_findings,
        )
    except findingsmod.FindingRejected as exc:
        raise InvalidFindings(str(exc)) from exc

    combined = existing + accepted

    if stage is Stage.DOCS and session.config.docs.require_changelog:
        injected = docsstage.changelog_finding(
            inventory, bundle.files, finding_id=findingsmod.next_id(combined)
        )
        if injected is not None:
            accepted = accepted + [injected]
            combined = combined + [injected]
    session.store.save_findings(run.run_id, combined)

    stage_findings = [f for f in combined if f.stage is stage]
    blocking = findingsmod.blocking(stage_findings, blocking_severities=blocking_severities)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.SUBMIT_FINDINGS)
        _apply(doc, Action.TRIAGE_BLOCKING if blocking else Action.TRIAGE_CLEAN)
        run = doc

    run = _skip_docs_if_disabled(session, run)

    session.store.append_event(
        run.run_id,
        {"event": "findings_submitted", "stage": stage.value, "count": len(accepted)},
    )

    return _envelope_for(
        run,
        stage=stage.value,
        data={
            "accepted": [f.model_dump(mode="json") for f in accepted],
            "total": len(combined),
        },
        blocking=[f.model_dump(mode="json") for f in blocking],
    )


# -- verify -----------------------------------------------------------------


def verify(session: Session) -> Envelope:
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_SUBMITTED,
        State.REVIEW_AWAITING_RESPONSES,
        State.REVIEW_FIXING,
        State.REVIEW_GREEN,
        State.DOCS_SUBMITTED,
        State.DOCS_AWAITING_RESPONSES,
        State.DOCS_FIXING,
        State.DOCS_GREEN,
        command="verify",
    )

    stage = findingsmod.stage_for_state(run.state)
    blocking_severities = (
        session.config.review.blocking_severities
        if stage is Stage.REVIEW
        else session.config.docs.blocking_severities
    )
    stage_findings = [f for f in session.store.load_findings(run.run_id) if f.stage is stage]
    outstanding = findingsmod.blocking(stage_findings, blocking_severities=blocking_severities)

    if outstanding:
        raise StageFailed(
            f"{len(outstanding)} finding(s) still block the {stage.value} stage",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage.value,
            data={"stage": stage.value},
            blocking=[f.model_dump(mode="json") for f in outstanding],
            next_instruction="Resolve each blocking finding with `respond`, then verify again.",
            next_command="agentic-cli respond --id <id> --action fixed --commit <sha>",
        )

    if run.state not in (State.REVIEW_GREEN, State.DOCS_GREEN):
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.RESOLVE_GREEN)
            run = doc

    run = _skip_docs_if_disabled(session, run)

    return _envelope_for(
        run,
        stage=stage.value,
        data={"stage": stage.value, "blocking_count": 0},
    )


# -- status -----------------------------------------------------------------


RESPONSE_ACTIONS = ("fixed", "dismissed", "accepted")

_STAGE_STATES = {
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


def _resolve_command(session: Session, run: RunDoc, stage_name: str, override: str | None):
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
            f"[commands] {stage_name} in .agentic-cli.toml to settle it permanently."
        ),
        next_command=(
            f"agentic-cli stage run {stage_name} "
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
    _assert_fresh(session, run)
    spec = _STAGE_STATES[stage_name]
    _require_state(run, *spec["ready"], command=f"stage run {stage_name}")

    stage = Stage(stage_name)
    record_entry = run.stages.get(stage) or StageRecord()
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
            next_command=f"agentic-cli logs --stage {stage_name}",
        )

    resolved = _resolve_command(session, run, stage_name, command)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, spec["retry"] if doc.state is spec["red"] else spec["run"])
        run = doc

    baseline_red = None
    if baseline:
        baseline_red = _baseline_is_red(session, run, resolved)

    prepared = runtime.prepare_command(
        run.worktree_path,
        resolved,
        manager=session.config.runtime.manager,
        strict=session.config.runtime.strict,
    )
    result = shellstage.run_stage(
        run.worktree_path,
        prepared.command,
        timeout_seconds=session.config.stage.timeout_seconds,
    )

    secrets = shellstage.read_secrets(run.worktree_path, run.copied_files)
    clean_output = shellstage.redact(result.output, secrets)

    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(clean_output)

    summary = shellstage.summarise(clean_output)

    with session.store.transaction(run.run_id) as doc:
        entry = doc.stages.get(stage) or StageRecord()
        entry.command = resolved
        entry.exit_code = result.exit_code
        entry.log_path = str(log_path)
        entry.status = "green" if result.passed else "red"
        entry.finished_at = _now()
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
        return _envelope_for(run, stage=stage_name, data=data)

    message = f"the {stage_name} stage failed (exit {result.exit_code})"
    instruction = (
        f"Read the log, fix the cause in the worktree, commit, then re-run the stage."
    )
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
        next_command=f"agentic-cli logs --stage {stage_name}",
    )


def _baseline_is_red(session: Session, run: RunDoc, command: str) -> bool:
    """Run the command against the base commit in a scratch worktree.

    Answers the question that otherwise sends an agent chasing phantoms: is this
    failure ours, or was the base already broken?
    """
    scratch = Path(run.worktree_path).parent / f"{run.run_id}-baseline"
    branch = f"ac/{run.run_id}-baseline"
    try:
        worktree.create(
            session.repo_root, path=scratch, branch=branch, head_sha=run.merge_base_sha
        )
        try:
            worktree.copy_files(session.repo_root, scratch, session.config.worktree.copy_files)
        except worktree.CopyRefused:
            pass
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


def mergeback(session: Session) -> Envelope:
    """Cherry-pick the verified fixes onto the user's branch, strictly."""
    run = _load_current(session)
    retrying_conflict = run.state is State.MERGEBACK_CONFLICT
    if not retrying_conflict:
        _assert_fresh(session, run)
    _require_state(
        run,
        State.TEST_GREEN,
        State.MERGEBACK_PENDING,
        State.MERGEBACK_CONFLICT,
        command="mergeback",
    )

    repo = session.repo_root
    affected_paths = gitx.changed_paths_in_commits(
        run.worktree_path or repo, run.fix_commits
    )
    overlapping = gitx.status_for_paths(repo, affected_paths)
    if overlapping:
        raise DirtyTree(
            "the working tree has changes on paths mergeback would overwrite",
            state=run.state.value,
            run_id=run.run_id,
            data={"affected_paths": affected_paths, "overlapping_status": overlapping},
            next_instruction=(
                "Commit or stash the reported overlapping paths, then merge back. "
                "Unrelated tracked and untracked files may remain."
            ),
            next_command="git status",
        )

    if run.state is State.TEST_GREEN:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.BEGIN_MERGEBACK)
            run = doc
    elif run.state is State.MERGEBACK_CONFLICT:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.MERGEBACK_RETRY)
            run = doc

    try:
        local_tree = gitx.tree_sha(repo)
        verified_tree = gitx.tree_sha(run.worktree_path)
        branch_moved = gitx.rev_parse(repo, "HEAD") != run.head_sha
        if retrying_conflict and (local_tree == verified_tree or branch_moved):
            result = mergebackmod.MergebackResult(
                pre_sha=gitx.rev_parse(repo, "HEAD"),
                post_sha=gitx.rev_parse(repo, "HEAD"),
                applied=[],
                local_tree_sha=local_tree,
                worktree_tree_sha=verified_tree,
            )
        else:
            result = mergebackmod.cherry_pick_fixes(
                repo,
                run.fix_commits,
                worktree_branch=run.worktree_branch,
                worktree_path=run.worktree_path,
            )
    except mergebackmod.MergebackConflict as exc:
        with session.store.transaction(run.run_id) as doc:
            _apply(doc, Action.MERGEBACK_FAILED)
            run = doc
        report = exc.report.as_dict()
        session.store.append_event(
            run.run_id, {"event": "mergeback_conflict", **report}
        )
        raise MergebackConflictError(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
            data=report,
            next_instruction=(
                "Show the user the resolution block verbatim. The fix commits remain "
                "intact; after the reported paths are resolved, `mergeback` can retry "
                "without rerunning the completed stages."
            ),
            next_command="agentic-cli mergeback",
        ) from exc

    # Green transfers only when the content is provably identical.
    findings = session.store.load_findings(run.run_id)
    summary: dict[str, int] = {}
    for finding in findings:
        summary[finding.status.value] = summary.get(finding.status.value, 0) + 1

    if result.tree_equivalent:
        stages_recorded = {stage: "green" for stage in run.stages}
        stages_recorded[Stage.REVIEW] = "green"
        if session.config.docs.enabled:
            stages_recorded[Stage.DOCS] = "green"
        entry = ledgermod.build_entry(
            run,
            sha=result.post_sha,
            tree_sha=result.local_tree_sha,
            stages=stages_recorded,
            findings_summary=summary,
        )
        session.store.save_ledger(ledgermod.record(session.store.load_ledger(), entry))

    with session.store.transaction(run.run_id) as doc:
        doc.head_sha = result.post_sha
        _apply(doc, Action.MERGEBACK_OK)
        run = doc

    session.store.append_event(
        run.run_id,
        {"event": "mergeback_ok", "post_sha": result.post_sha,
         "tree_equivalent": result.tree_equivalent},
    )

    envelope = _envelope_for(run, data=result.as_dict())
    if not result.tree_equivalent:
        envelope.next_instruction = (
            "The merged tree does not match what was verified, so green did not "
            "transfer. Start a fresh run against the new tip."
        )
        envelope.next_command = "agentic-cli start"
    return envelope


def _remote_for(session: Session, run: RunDoc):
    url = gitx.remote_url(session.repo_root, "origin")
    if not url:
        raise NeedsHuman(
            "no `origin` remote is configured, so there is nowhere to push",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction="Add a remote, then run the gate again.",
            next_command="git remote add origin <url>",
        )
    return providermod.parse_remote(url)


def gate(session: Session) -> Envelope:
    """Summarise what would be pushed and mint a confirmation token."""
    run = _load_current(session)
    _assert_fresh(session, run)
    _require_state(run, State.VERIFIED, State.AWAITING_PUSH_CONFIRM, command="gate")

    remote = _remote_for(session, run)
    commits = [
        {"sha": sha, "subject": gitx.commit_subject(session.repo_root, sha)}
        for sha in gitx.commits_between(session.repo_root, run.merge_base_sha, run.head_sha)
    ]
    summary = gatemod.GateSummary(
        remote="origin",
        refspec=f"{run.branch}:{run.branch}",
        branch=run.branch,
        base_ref=run.base_ref,
        commits=commits,
        pr_title=_default_pr_title(session, run, commits),
    )

    if session.config.gate.mode == "manual":
        raise ManualGate(
            "gate.mode is 'manual', so agentic-cli will not push on your behalf",
            state=run.state.value,
            run_id=run.run_id,
            data={**summary.as_dict(), "manual_command": f"git push origin {run.branch}"},
            next_instruction=(
                "Show the user this summary and ask them to run the push themselves."
            ),
            next_command=f"git push origin {run.branch}",
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
        next_command=f"agentic-cli push --confirm {summary.token}",
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
            next_command="agentic-cli gate",
        )

    if dry_run:
        return _envelope_for(
            run,
            data={"dry_run": True, "would_push": f"origin {run.branch}", "pushed": False},
            next_instruction="Dry run only; nothing was pushed.",
            next_command=f"agentic-cli push --confirm {run.gate_token}",
        )

    gitx.run(session.repo_root, "push", "origin", f"{run.branch}:{run.branch}")

    with session.store.transaction(run.run_id) as doc:
        doc.pushed_sha = run.head_sha
        _apply(doc, Action.PUSH)
        run = doc

    session.store.append_event(run.run_id, {"event": "pushed", "sha": run.head_sha})
    return _envelope_for(
        run, data={"pushed": True, "sha": run.head_sha, "remote": "origin", "dry_run": False}
    )


def _default_pr_title(
    session: Session, run: RunDoc, commits: list[dict] | None = None
) -> str:
    if session.config.publish.pr_title:
        return session.config.publish.pr_title
    if run.branch:
        return run.branch
    commits = commits or []
    return commits[0]["subject"] if commits else "agentic-cli verified change"


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
        {"sha": sha, "subject": gitx.commit_subject(session.repo_root, sha)}
        for sha in commit_shas
    ]
    title = title or _default_pr_title(session, run, commits)
    draft_pr = session.config.publish.draft_pr if draft is None else draft

    try:
        result = githubmod.create_pull_request(
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
                "compare_url": providermod.compare_url(
                    remote, base=run.base_ref, head=run.branch
                )
            },
            next_instruction=(
                "Give the user the compare URL and let them open the PR themselves. "
                "agentic-cli never handles credentials."
            ),
        ) from exc

    with session.store.transaction(run.run_id) as doc:
        doc.pr_url = result.url
        _apply(doc, Action.OPEN_PR)
        run = doc

    session.store.append_event(run.run_id, {"event": "pr_opened", "url": result.url})
    return _envelope_for(run, data={"pr_url": result.url})


def _pr_body(session: Session, run: RunDoc) -> str:
    findings = session.store.load_findings(run.run_id)
    lines = [
        "Verified by agentic-cli.",
        "",
        f"- review: {len([f for f in findings if f.stage is Stage.REVIEW])} finding(s)",
        f"- docs: {len([f for f in findings if f.stage is Stage.DOCS])} finding(s)",
    ]
    for stage_name, record in run.stages.items():
        lines.append(f"- {stage_name.value}: {record.status} (`{record.command}`)")
    return "\n".join(lines)


def logs(session: Session, *, stage_name: str) -> Envelope:
    run = _load_current(session)
    log_path = session.store.logs_dir(run.run_id) / f"{stage_name}.txt"
    if not log_path.exists():
        raise NoLog(
            f"the {stage_name} stage has not run in this run, so there is no log",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=f"Run the stage first.",
            next_command=f"agentic-cli stage run {stage_name}",
        )
    return _envelope_for(
        run,
        stage=stage_name,
        data={"stage": stage_name, "log_path": str(log_path), "output": log_path.read_text()},
    )


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
    _assert_fresh(session, run)
    _require_state(
        run,
        State.REVIEW_AWAITING_RESPONSES,
        State.REVIEW_FIXING,
        State.DOCS_AWAITING_RESPONSES,
        State.DOCS_FIXING,
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
            f"{finding_id} is already {target.status.value}; each finding is "
            f"resolved once",
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

    with session.store.transaction(run.run_id) as doc:
        if commit and commit not in doc.fix_commits:
            doc.fix_commits.append(commit)
        if doc.state in (State.REVIEW_AWAITING_RESPONSES, State.DOCS_AWAITING_RESPONSES):
            _apply(doc, Action.RESPOND)
        run = doc

    target.status = FindingStatus(action)
    target.fix_commit = commit
    target.response_note = note
    session.store.save_findings(run.run_id, stored)
    session.store.append_event(
        run.run_id,
        {"event": "finding_resolved", "id": finding_id, "action": action, "commit": commit},
    )

    stage = findingsmod.stage_for_state(run.state)
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
        data={"finding": target.model_dump(mode="json"), "remaining_blocking": len(remaining)},
        blocking=[f.model_dump(mode="json") for f in remaining],
    )
    if not remaining:
        envelope.next_instruction = "Nothing blocks this stage any more. Verify it."
        envelope.next_command = "agentic-cli verify"
    return envelope


def _verify_fix_commit(session: Session, run: RunDoc, target, commit: str) -> str:
    wt = run.worktree_path
    if not gitx.commit_exists(wt, commit):
        raise InvalidResponse(
            f"commit {commit} does not exist on the worktree branch",
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


def events(session: Session, *, limit: int | None = None) -> Envelope:
    run = _load_current(session)
    history = session.store.load_events(run.run_id)
    if limit:
        history = history[-limit:]
    return _envelope_for(run, data={"events": history, "count": len(history)})


def abort(session: Session, *, force: bool = False) -> Envelope:
    """End the run and reclaim its worktree.

    Unmerged fix commits are reported rather than discarded: the agent may have
    done real work in there, and silently deleting it is the one outcome nobody
    can undo.
    """
    run = _load_current(session)

    if run.fix_commits and not force:
        raise UnmergedWork(
            f"this run has {len(run.fix_commits)} fix commit(s) that were never merged "
            f"back: {', '.join(run.fix_commits)}",
            state=run.state.value,
            run_id=run.run_id,
            data={"fix_commits": run.fix_commits, "worktree_path": run.worktree_path},
            next_instruction=(
                "Those commits exist only in the worktree. Cherry-pick anything worth "
                "keeping, then abort again with --force to discard the rest."
            ),
            next_command="agentic-cli abort --force",
        )

    if run.worktree_path:
        worktree.remove(session.repo_root, run.worktree_path, branch=run.worktree_branch)

    with session.store.transaction(run.run_id) as doc:
        _apply(doc, Action.ABORT)
        run = doc

    session.store.set_current(None)
    session.store.append_event(run.run_id, {"event": "aborted", "forced": force})

    return _envelope_for(
        run,
        data={"discarded_fix_commits": run.fix_commits if force else []},
        next_instruction="Run aborted. Start a fresh one when ready.",
        next_command="agentic-cli start",
    )


def gc(session: Session, *, force: bool = False) -> Envelope:
    """Reconcile three sources of truth: run dirs, git worktrees, and ac/* branches.

    Anything still holding unmerged fix commits is *reported*, never removed
    without ``--force``. Reclaiming disk is not worth destroying work.
    """
    store = session.store
    repo = session.repo_root

    known_runs = set(store.list_runs())
    live_worktrees = {
        record["branch"].removeprefix("refs/heads/ac/"): record["worktree"]
        for record in gitx.list_worktrees(repo)
        if "worktree" in record
        and record.get("branch", "").startswith("refs/heads/ac/")
    }
    ac_branches = {
        line.strip().lstrip("* ").strip()
        for line in gitx.out(repo, "branch", "--list", "ac/*").splitlines()
        if line.strip()
    }

    removed: list[str] = []
    retained: list[dict] = []
    orphans: list[str] = []

    for run_id in sorted(known_runs):
        run = store.load_run(run_id)
        terminal = run.state in (State.ABORTED, State.DONE, State.ORPHANED)
        if not terminal:
            # An active run is never a reclamation candidate, but one holding
            # fix commits is worth surfacing so it is not forgotten about.
            if run.fix_commits:
                retained.append(
                    {
                        "run_id": run_id,
                        "reason": "run still active with unmerged fix commits",
                        "fix_commits": run.fix_commits,
                    }
                )
            continue
        if run.fix_commits and not force:
            retained.append({"run_id": run_id, "reason": "unmerged fix commits"})
            continue
        if run.worktree_path and Path(run.worktree_path).exists():
            worktree.remove(repo, run.worktree_path, branch=run.worktree_branch)
        removed.append(run_id)

    # A worktree or branch git knows about but the store does not: reconcile by
    # reporting, so a half-created run is visible rather than silently leaked.
    for name, path in live_worktrees.items():
        if name not in known_runs:
            orphans.append(name)
    for branch in ac_branches:
        run_id = branch.removeprefix("ac/")
        if run_id not in known_runs and run_id not in orphans:
            orphans.append(run_id)

    current = store.get_current()
    if current and current not in known_runs:
        store.set_current(None)

    return Envelope(
        data={
            "removed": removed,
            "retained": retained,
            "orphans": orphans,
            "runs_known": sorted(known_runs),
        },
        next_instruction=(
            "Orphans were found; inspect them before removing." if orphans else None
        ),
        next_command="agentic-cli gc --force" if orphans and not force else None,
    )


def status(session: Session) -> Envelope:
    """Legal in every state, and the universal recovery entry point.

    Deliberately never raises for a stale or wedged run: if `status` could fail,
    an agent that had wandered off the path would have nowhere to go.
    """
    run_id = session.store.get_current()
    if not run_id:
        return Envelope(
            data={"has_run": False},
            next_instruction="No run is active. Start one.",
            next_command="agentic-cli start",
        )

    try:
        run = session.store.load_run(run_id)
    except Exception:
        return Envelope(
            data={"has_run": False, "dangling_run_id": run_id},
            next_instruction="The recorded run is missing. Start a fresh one.",
            next_command="agentic-cli start",
        )

    findings = session.store.load_findings(run_id)
    summary = {status.value: 0 for status in FindingStatus}
    for finding in findings:
        summary[finding.status.value] += 1

    tip = _head_moved(session, run)
    stale = run.state is not State.MERGEBACK_CONFLICT and (run.stale or tip is not None)

    envelope = _envelope_for(
        run,
        data={
            "has_run": True,
            "seq": run.seq,
            "branch": run.branch,
            "base_ref": run.base_ref,
            "head_sha": run.head_sha,
            "current_tip": tip or run.head_sha,
            "stale": stale,
            "worktree_path": run.worktree_path,
            "worktree_branch": run.worktree_branch,
            "config_digest": run.config_digest,
            "gate_token": run.gate_token,
            "pushed_sha": run.pushed_sha,
            "pr_url": run.pr_url,
            "fix_commits": run.fix_commits,
            # Names only — contents are never read, logged, or echoed anywhere.
            "copied_files": run.copied_files,
            "findings": [f.model_dump(mode="json") for f in findings],
            "findings_summary": summary,
        },
    )
    if stale:
        envelope.next_instruction = (
            "This run is stale: the branch moved after review began. Start a fresh run."
        )
        envelope.next_command = "agentic-cli start"
    elif run.state is State.MERGEBACK_CONFLICT:
        conflict = next(
            (
                event
                for event in reversed(session.store.load_events(run_id))
                if event.get("event") == "mergeback_conflict"
            ),
            None,
        )
        envelope.data["mergeback_conflict"] = conflict
        envelope.next_instruction = (
            "Use the durable conflict report below, resolve the affected paths, "
            "then retry mergeback; completed verification stages are retained."
        )
        envelope.next_command = "agentic-cli mergeback"
    return envelope
