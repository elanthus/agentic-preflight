"""Shared session state and orchestration helpers."""

from __future__ import annotations

import hashlib
import shlex
import time
import uuid
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from .. import findings as findingsmod
from .. import gitx, risk, telemetry, worktree
from ..config import Config, load_config, snapshot_run_settings
from ..envelope import Envelope
from ..errors import (
    MaxRestarts,
    NoRun,
    StaleRun,
    WrongState,
)
from ..machine import (
    CLOSING_ACTIONS,
    RESTART_ACTIONS,
    TERMINAL_STATES,
    Action,
    IllegalTransition,
    State,
    next_state,
    recovery_hint,
)
from ..models import Finding, Producer, RiskAssessment, RunDoc, Stage, StageRecord
from ..store import RunReadError, Store, UnknownRun

STATE_DIR_NAME = "agentic-preflight"

_T = TypeVar("_T")


@dataclass
class Session:
    """Everything a command needs about *where* it is running."""

    repo_root: Path
    caller_root: Path
    owner_id: str
    store: Store
    config: Config
    selected_run_id: str | None = None
    source_worktree_available: bool = True
    #: Review inputs already computed by this command, keyed on the snapshot they
    #: describe. A session lives for one CLI process, which is the scope of the
    #: "each invocation recomputes inputs" guarantee, so nothing here outlives it.
    memo: dict[tuple[Hashable, ...], Any] = field(default_factory=dict, repr=False, compare=False)

    def active_run_id(self) -> str | None:
        return self.selected_run_id or self.store.get_active(self.owner_id)

    def memoized(self, key: tuple[Hashable, ...], compute: Callable[[], _T]) -> _T:
        """Return the value computed for ``key`` in this command, computing it once."""
        if key not in self.memo:
            self.memo[key] = compute()
        return self.memo[key]

    def clear_memo(self) -> None:
        """Forget every memoized input, for code that has just changed the worktree."""
        self.memo.clear()


def worktree_identity(cwd: Path | str) -> str:
    """Return a stable, filesystem-safe identity for one linked worktree."""
    private_git_dir = gitx.git_dir(cwd).resolve()
    return hashlib.sha256(str(private_git_dir).encode()).hexdigest()


def open_session(cwd: Path | str | None = None, *, run_id: str | None = None) -> Session:
    """Open a session for the repository containing a directory."""
    cwd = Path(cwd) if cwd else Path.cwd()
    caller_root = gitx.repo_root(cwd)
    # GIT_COMMON_DIR, not GIT_DIR: these differ when the caller is already
    # inside a worktree, and run state must be one namespace per clone.
    state_root = gitx.git_common_dir(cwd) / STATE_DIR_NAME
    store = Store(state_root)
    owner_id = worktree_identity(caller_root)

    # Once a run exists, its resolved snapshot is authoritative. This also
    # keeps a malformed or edited working-copy config from stranding `status`
    # or silently reshaping an in-flight gate.
    cfg = None
    current = run_id or store.get_active(owner_id)
    active = None
    if current:
        try:
            active = store.load_run(current)
            cfg = Config.model_validate(active.config_snapshot)
        except (UnknownRun, RunReadError, ValidationError):
            # Inspection must remain available without trusting an unreadable run.
            pass
    repo_root = caller_root
    source_worktree_available = True
    if active is not None:
        source = Path(active.source_worktree_path)
        if source.exists():
            repo_root = source
        else:
            source_worktree_available = False
    cfg = cfg or load_config(repo_root)
    if cfg.worktree.mode != "in_place":
        store.set_worktrees_root(worktree.resolve_root(repo_root, cfg.worktree.root))
    session = Session(
        repo_root=repo_root,
        caller_root=caller_root,
        owner_id=owner_id,
        store=store,
        config=cfg,
        selected_run_id=run_id,
        source_worktree_available=source_worktree_available,
    )
    # Every path into DONE, ABORTED, or ORPHANED commits through a store
    # transaction, so the run summary hangs off that one commit point.
    store.on_terminal = lambda run: _summarize_ended_run(session, run)
    return session


def _summarize_ended_run(session: Session, run: RunDoc) -> None:
    """Write the local summary of a run whose terminal state was just committed."""
    telemetry.record_run_end(session.store, session.repo_root, run)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def _max_restarts(run: RunDoc) -> int:
    """Read the limit from the run's own snapshot, which an edited config cannot move."""
    return snapshot_run_settings(run.config_snapshot)[0]


RESTART_LIMIT_INSTRUCTION = (
    "This needs a person. Stop repairing. Show the user the run status and the "
    "repairs that kept reopening review, and ask how to proceed. Only the user "
    "may decide to abort this run and start a fresh one."
)


def _restart_limit_reached(run: RunDoc) -> bool:
    return run.validation_restarts >= _max_restarts(run)


def _check_restart_limit(run: RunDoc) -> None:
    """Refuse to continue a run whose validation has restarted too many times."""
    limit = _max_restarts(run)
    if not _restart_limit_reached(run):
        return
    raise MaxRestarts(
        f"validation has restarted {run.validation_restarts} times "
        f"(max_restarts={limit}); stopping rather than looping",
        state=run.state.value,
        run_id=run.run_id,
        data={
            "validation_restarts": run.validation_restarts,
            "max_restarts": limit,
            "needs_human": True,
        },
        next_instruction=RESTART_LIMIT_INSTRUCTION,
        next_command="agentic-preflight status",
    )


#: The stage each action begins. A restart back to review begins review again.
_STAGE_BEGINS: dict[Action, Stage] = {
    Action.BEGIN_REVIEW: Stage.REVIEW,
    Action.INVALIDATE_REVIEW: Stage.REVIEW,
    Action.LINT_FIX_RESTART: Stage.REVIEW,
    Action.TEST_FIX_RESTART: Stage.REVIEW,
    Action.BEGIN_DOCS: Stage.DOCS,
    Action.RUN_LINT: Stage.LINT,
    Action.RUN_TEST: Stage.TEST,
}


def _apply(run: RunDoc, action: Action) -> None:
    """Advance the run, converting an illegal move into a typed error.

    Restarts are counted here because every path back to review passes through
    this function. Once the limit is reached, only closing the run stays legal.
    Stage start times are recorded here for the same reason, with the time of
    the transition event.
    """
    if action not in CLOSING_ACTIONS:
        _check_restart_limit(run)
    previous = run.state
    try:
        run.state = next_state(run.state, action)
    except IllegalTransition as exc:
        raise WrongState(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
        ) from exc
    moved = run.state is not previous
    at = _now()
    if moved:
        run._state_events.append(
            {
                "event": "state_transition",
                "from": previous.value,
                "to": run.state.value,
                "action": action.value,
                "at": at,
                "monotonic_ns": time.monotonic_ns(),
            }
        )
    stage = _STAGE_BEGINS.get(action)
    if stage is not None:
        record = run.stages.get(stage) or StageRecord()
        # A reopen while review is already awaiting findings keeps its start,
        # unless the caller has just discarded the review result with it.
        if moved or record.started_at is None:
            record.started_at = at
            run.stages[stage] = record
    # A reopen while review is already awaiting findings discards no progress.
    if action in RESTART_ACTIONS and moved:
        run.validation_restarts += 1


def _require_state(run: RunDoc, *allowed: State, command: str) -> None:
    if run.state not in allowed:
        raise WrongState(
            f"`{command}` is not legal in state {run.state.name}",
            state=run.state.value,
            run_id=run.run_id,
        )


def _require_worktree(run: RunDoc) -> str:
    """Return the validation worktree or report a corrupt run document."""
    if run.worktree_path is None:
        raise WrongState(
            "the active run has no validation worktree",
            state=run.state.value,
            run_id=run.run_id,
        )
    return run.worktree_path


def _require_finding_stage(run: RunDoc) -> Stage:
    """Return the finding stage implied by the current review/docs state."""
    stage = findingsmod.stage_for_state(run.state)
    if stage is None:
        raise WrongState(
            f"state {run.state.value} does not belong to a findings stage",
            state=run.state.value,
            run_id=run.run_id,
        )
    return stage


def _next_hint(state: State) -> tuple[str | None, str | None]:
    """The default next move declared beside the state's legal transitions."""
    hint = recovery_hint(state)
    return hint.instruction, hint.command


def _respond_command(finding_id: str) -> str:
    """Return the concrete command for resolving the next blocking finding."""
    return f"agentic-preflight respond --id {finding_id} --action fixed --commit <sha>"


def _producer_flags(producer: Producer | None) -> list[str]:
    """Repeat the producer values that came from flags, and only those.

    A client detected from the environment is left out: the next invocation
    detects it again from the same marker, and turning it into ``--client``
    would promote a detection into a self-report.
    """
    if producer is None:
        return []
    flags: list[str] = []
    if producer.client is not None and producer.client_source == "flag":
        flags.extend(["--client", producer.client])
    if producer.reported_model is not None and producer.reported_model_source == "flag":
        flags.extend(["--model", producer.reported_model])
    return flags


def _start_command(
    intent: str | None,
    *,
    base_ref: str,
    default_base_ref: str,
    producer: Producer | None,
    replace: bool = False,
) -> str:
    """Build a restart command without silently changing a run's base ref.

    ``producer`` is required so that no caller can drop the recorded client and
    model: the next run should record what this one did, or what the refused
    invocation asked for. Every value is shell-quoted by ``shlex.join``.
    """
    args = ["agentic-preflight", "start"]
    if replace:
        args.append("--replace")
    if base_ref != default_base_ref:
        args.extend(["--base-ref", base_ref])
    args.extend(["--intent", intent or "<objective and acceptance criteria>"])
    args.extend(_producer_flags(producer))
    return shlex.join(args)


def _envelope_for(run: RunDoc, **overrides) -> Envelope:
    instruction, command = _next_hint(run.state)
    stopped = run.state not in TERMINAL_STATES and _restart_limit_reached(run)
    if stopped:
        # A stopped run must never advertise the state's ordinary next move.
        instruction, command = RESTART_LIMIT_INSTRUCTION, None
    fields: dict[str, Any] = {
        "run_id": run.run_id,
        "state": run.state.value,
        "next_instruction": instruction,
        "next_command": command,
    }
    fields.update(overrides)
    report = telemetry.take_report(run.run_id) if run.state in TERMINAL_STATES else None
    if report is not None:
        # The summary written when this command ended the run.
        fields["data"] = {**fields.get("data", {}), "telemetry": report}
    if stopped:
        # Machine-readable on every envelope, not only in the wording.
        fields["data"] = {**fields.get("data", {}), "needs_human": True}
    return Envelope(**fields)


def _interrupted_legacy_update(session: Session, run: RunDoc) -> bool:
    return (
        run.state not in TERMINAL_STATES
        and (session.store.run_dir(run.run_id) / "pending-update.json").exists()
    )


def _load_current(session: Session, *, allow_interrupted: bool = False) -> RunDoc:
    run_id = session.active_run_id()
    if not run_id:
        raise NoRun()
    try:
        run = session.store.load_run(run_id)
    except UnknownRun as exc:
        raise NoRun(f"current run {run_id} is missing from the store") from exc
    if not allow_interrupted and _interrupted_legacy_update(session, run):
        stopped = _restart_limit_reached(run)
        raise WrongState(
            "This run has an interrupted update from an older version; validation cannot continue.",
            state=run.state.value,
            run_id=run.run_id,
            data={"source_worktree_path": run.source_worktree_path, "needs_human": stopped},
            next_instruction=(
                RESTART_LIMIT_INSTRUCTION
                if stopped
                else f"Run start from the source worktree {run.source_worktree_path} "
                "to discard the interrupted validation and restart."
            ),
            next_command=None
            if stopped
            else _start_command(
                run.intent,
                base_ref=run.base_ref,
                default_base_ref=session.config.general.base_ref,
                producer=run.producer,
            ),
        )
    return run


def _head_moved(session: Session, run: RunDoc) -> str | None:
    """Return the current tip if it differs from the reviewed one."""
    source = Path(run.source_worktree_path)
    try:
        tip = gitx.rev_parse(source, "HEAD")
    except (gitx.GitError, OSError):
        return None
    return None if tip == run.source_head_sha else tip


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
        next_instruction=(
            "Start again from the source worktree with the same intent. The stale run "
            "will be preserved as ORPHANED before the fresh run begins."
        ),
        next_command=_start_command(
            run.intent,
            base_ref=run.base_ref,
            default_base_ref=session.config.general.base_ref,
            producer=run.producer,
        ),
    )


def _worktree_mode(run: RunDoc) -> str:
    """The run's validation mode, ignoring keys a later release removed."""
    return snapshot_run_settings(run.config_snapshot)[1]


def _is_in_place(run: RunDoc) -> bool:
    return _worktree_mode(run) == "in_place"


def _worktree_completion(mode: str) -> str:
    prefix = "Run complete. "
    if mode == "in_place":
        return prefix + "the in-place checkout was left intact."
    if mode == "reusable":
        return prefix + "the reusable runner is ready for the next run."
    return prefix + "the strict worktree was removed."


def _release_run_worktree(session: Session, run: RunDoc) -> None:
    if not run.worktree_path or run.worktree_released:
        return
    mode = _worktree_mode(run)
    if mode == "in_place":
        return
    if mode == "reusable":
        worktree.release_reusable(
            session.repo_root,
            run.worktree_path,
            branch=run.worktree_branch,
            copied_files=run.copied_files,
        )
    else:
        worktree.remove(session.repo_root, run.worktree_path, branch=run.worktree_branch)


def _assess(session: Session, changed_files: list[str], findings: list[Finding]) -> RiskAssessment:
    """Apply this run's path and finding policy consistently."""
    return risk.assess(
        changed_files,
        findings,
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )


def _blocking_severities(session: Session, stage: Stage) -> Sequence[str]:
    return (
        session.config.docs.blocking_severities
        if stage is Stage.DOCS
        else session.config.review.blocking_severities
    )


def _findings_next_hint(
    envelope: Envelope,
    blocking: list[Finding],
    actionable: list[Finding],
    *,
    blocking_instruction: str = "Resolve each blocking finding with `respond`.",
) -> None:
    """Select a blocking or open auto-fix finding without changing green-stage hints."""
    if blocking:
        envelope.next_instruction = blocking_instruction
        envelope.next_command = _respond_command(blocking[0].id)
    elif actionable:
        envelope.next_instruction = (
            "This stage is green, but an auto-fix finding is still open. Fix it, "
            "or record why it is not worth fixing with `--action accepted --note`."
        )
        envelope.next_command = _respond_command(actionable[0].id)
