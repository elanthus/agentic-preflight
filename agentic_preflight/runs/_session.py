"""Shared session state and orchestration helpers."""

from __future__ import annotations

import hashlib
import shlex
import time
import uuid
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from .. import attestation as attestationmod
from .. import findings as findingsmod
from .. import gitx, public_config, risk, telemetry, worktree
from ..cli_contract import RunDataBase
from ..config import (
    Config,
    ConfigError,
    load_config,
    snapshot_config,
    snapshot_run_settings,
)
from ..envelope import Envelope
from ..errors import (
    FindingsUnresolved,
    MaxRestarts,
    NeedsHuman,
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
from ..run_summary import utc_now
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
    unreadable: RunReadError | None = None
    if current:
        try:
            active = store.load_run(current)
        except UnknownRun:
            pass
        except RunReadError as exc:
            # Inspection must remain available without trusting an unreadable run.
            # The files below only shape that inspection: every command that
            # changes the run loads it again and fails with run_record_unreadable,
            # and status reports it as unreadable rather than quoting this config.
            unreadable = exc
        else:
            # The same tolerant loader the store just validated this snapshot with,
            # so a key a later release removed cannot hand the run to the files.
            cfg = snapshot_config(active.config_snapshot)
    repo_root = caller_root
    source_worktree_available = True
    if active is not None:
        source = Path(active.source_worktree_path)
        if source.exists():
            repo_root = source
        else:
            source_worktree_available = False
    if cfg is None:
        try:
            cfg = load_config(repo_root)
        except ConfigError as exc:
            if unreadable is None:
                raise
            # The unreadable run comes first: a configuration error here would hide
            # its recovery instruction, and the run's own snapshot cannot be used.
            raise unreadable from exc
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


def new_run_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def max_restarts(run: RunDoc) -> int:
    """Read the limit from the run's own snapshot, which an edited config cannot move."""
    return snapshot_run_settings(run.config_snapshot)[0]


RESTART_LIMIT_INSTRUCTION = (
    "This needs a person. Stop repairing. Show the user the run status and the "
    "repairs that kept reopening review, and ask how to proceed. Only the user "
    "may decide to abort this run and start a fresh one."
)


def restart_limit_reached(run: RunDoc) -> bool:
    return run.validation_restarts >= max_restarts(run)


def check_restart_limit(run: RunDoc) -> None:
    """Refuse to continue a run whose validation has restarted too many times."""
    limit = max_restarts(run)
    if not restart_limit_reached(run):
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


def apply_action(run: RunDoc, action: Action) -> None:
    """Advance the run, converting an illegal move into a typed error.

    Restarts are counted here because every path back to review passes through
    this function. Once the limit is reached, only closing the run stays legal.
    Stage start times are recorded here for the same reason, with the time of
    the transition event.
    """
    if action not in CLOSING_ACTIONS:
        check_restart_limit(run)
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
    at = utc_now()
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


def require_state(run: RunDoc, *allowed: State, command: str) -> None:
    if run.state not in allowed:
        raise WrongState(
            f"`{command}` is not legal in state {run.state.name}",
            state=run.state.value,
            run_id=run.run_id,
        )


def require_worktree(run: RunDoc) -> str:
    """Return the validation worktree or report a corrupt run document."""
    if run.worktree_path is None:
        raise WrongState(
            "the active run has no validation worktree",
            state=run.state.value,
            run_id=run.run_id,
        )
    return run.worktree_path


def require_finding_stage(run: RunDoc) -> Stage:
    """Return the finding stage implied by the current review/docs state."""
    stage = findingsmod.stage_for_state(run.state)
    if stage is None:
        raise WrongState(
            f"state {run.state.value} does not belong to a findings stage",
            state=run.state.value,
            run_id=run.run_id,
        )
    return stage


def next_hint(state: State) -> tuple[str | None, str | None]:
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


def start_command(
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


#: States in which an earlier release has already written the run's note.
_ATTESTED_STATES = frozenset({State.VERIFIED, State.AWAITING_PUSH_CONFIRM})

_EARLIER_RELEASE = (
    "run was started by an earlier release that did not record which "
    "configuration came from the user file"
)
_CANNOT_PUBLISH = "this release cannot merge it back, gate it, or push it"
_ABORT_AND_RESTART = "Abort this run and start a fresh one with the current release."


def unpublishable(run: RunDoc) -> bool:
    """Whether this release will refuse to merge back, gate, or push ``run``.

    True for a run of unknown configuration provenance that has not been pushed.
    ``PUSHED`` is left out because ``finish`` publishes nothing, so a run that an
    earlier release pushed can still be finished.
    """
    return (
        not public_config.provenance_known(run)
        and run.state not in TERMINAL_STATES
        and run.state is not State.PUSHED
    )


def _stale_note(run: RunDoc) -> str:
    return (
        f"the note on {run.head_sha} may hold user-level configuration; "
        f"remove it with `git notes --ref={attestationmod.NOTES_REF} remove "
        f"{run.head_sha}` instead of pushing it"
    )


def _keep_fix_commits(run: RunDoc) -> str:
    """Advice to save recorded fixes, which only an isolated abort can discard."""
    if not run.fix_commits or is_in_place(run):
        return ""
    return (
        " Keep the recorded fix commits in data.fix_commits by cherry-picking them onto "
        "your branch before you abort, because `abort --force` discards them."
    )


def unknown_provenance_hint(run: RunDoc) -> tuple[str, str]:
    """The next move for a run that :func:`unpublishable` names: abort it."""
    note = ""
    if run.state in _ATTESTED_STATES:
        stale_note = _stale_note(run)
        note = f" {stale_note[0].upper()}{stale_note[1:]}."
    return (
        f"This {_EARLIER_RELEASE}, so {_CANNOT_PUBLISH}.{note} "
        f"{_ABORT_AND_RESTART}{_keep_fix_commits(run)}",
        "agentic-preflight abort",
    )


def _unknown_provenance(run: RunDoc, *, command: str, situation: str) -> NeedsHuman:
    # At the restart limit even the abort is left to a person, as in `envelope_for`.
    stopped = restart_limit_reached(run)
    recovery = RESTART_LIMIT_INSTRUCTION if stopped else _ABORT_AND_RESTART
    return NeedsHuman(
        f"this {_EARLIER_RELEASE}, so {situation}",
        state=run.state.value,
        run_id=run.run_id,
        data={"reason": "unknown_config_provenance", "fix_commits": list(run.fix_commits)},
        next_instruction=f"Do not retry `{command}`. {recovery}{_keep_fix_commits(run)}",
        next_command=None if stopped else "agentic-preflight abort",
    )


def require_known_provenance(run: RunDoc, *, command: str) -> None:
    """Refuse to attest or publish a run whose user-level configuration is unknown.

    A run that an earlier release started did not record which snapshot sections
    came from the user file, so its note could publish them. ``mergeback`` and the
    publication commands check this before they change or push anything.
    """
    if public_config.provenance_known(run):
        return
    if command == "mergeback":
        situation = "its attestation could publish user-level configuration"
    else:
        situation = _stale_note(run)
    raise _unknown_provenance(run, command=command, situation=situation)


def require_publishable(run: RunDoc, *, command: str) -> None:
    """Refuse to spend review or stage work on a run that cannot publish.

    ``mergeback``, ``gate``, and ``push`` refuse a run of unknown provenance, so
    every command that moves such a run toward them stops here first, with the
    same envelope. Inspection, ``abort``, ``gc``, and ``finish`` do not call this.
    """
    if not unpublishable(run):
        return
    raise _unknown_provenance(
        run,
        command=command,
        situation=f"{_CANNOT_PUBLISH}, and `{command}` would spend work on a run that cannot publish",
    )


def envelope_for(run: RunDoc, *, data: RunDataBase | None = None, **overrides) -> Envelope:
    """The envelope for ``run``, with the shared run keys added to ``data``."""
    instruction, command = next_hint(run.state)
    stopped = run.state not in TERMINAL_STATES and restart_limit_reached(run)
    if stopped:
        # A stopped run must never advertise the state's ordinary next move.
        instruction, command = RESTART_LIMIT_INSTRUCTION, None
    elif unpublishable(run):
        # Nor may a run that merge-back and publication will always refuse. The
        # restart limit stays ahead: it leaves even the abort to a person.
        instruction, command = unknown_provenance_hint(run)
    fields: dict[str, Any] = {
        "run_id": run.run_id,
        "state": run.state.value,
        "next_instruction": instruction,
        "next_command": command,
    }
    fields.update(overrides)
    data = data if data is not None else RunDataBase()
    report = telemetry.take_report(run.run_id) if run.state in TERMINAL_STATES else None
    if report is not None:
        # The summary written when this command ended the run.
        data.telemetry = report
    if stopped:
        # Machine-readable on every envelope, not only in the wording.
        data.needs_human = True
    return Envelope(data=data, **fields)


def load_current(session: Session) -> RunDoc:
    run_id = session.active_run_id()
    if not run_id:
        raise NoRun()
    try:
        run = session.store.load_run(run_id)
    except UnknownRun as exc:
        raise NoRun(f"current run {run_id} is missing from the store") from exc
    return run


def head_moved(session: Session, run: RunDoc) -> str | None:
    """Return the current tip if it differs from the reviewed one."""
    source = Path(run.source_worktree_path)
    try:
        tip = gitx.rev_parse(source, "HEAD")
    except (gitx.GitError, OSError):
        return None
    return None if tip == run.source_head_sha else tip


def assert_fresh(session: Session, run: RunDoc) -> None:
    tip = head_moved(session, run)
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
        next_command=start_command(
            run.intent,
            base_ref=run.base_ref,
            default_base_ref=session.config.general.base_ref,
            producer=run.producer,
        ),
    )


def worktree_mode(run: RunDoc) -> str:
    """The run's validation mode, ignoring keys a later release removed."""
    return snapshot_run_settings(run.config_snapshot)[1]


def is_in_place(run: RunDoc) -> bool:
    return worktree_mode(run) == "in_place"


def worktree_completion(mode: str) -> str:
    prefix = "Run complete. "
    if mode == "in_place":
        return prefix + "the in-place checkout was left intact."
    if mode == "reusable":
        return prefix + "the reusable runner is ready for the next run."
    return prefix + "the strict worktree was removed."


def release_run_worktree(session: Session, run: RunDoc) -> None:
    if not run.worktree_path or run.worktree_released:
        return
    mode = worktree_mode(run)
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


def assess_risk(
    session: Session, changed_files: list[str], findings: list[Finding]
) -> RiskAssessment:
    """Apply this run's path and finding policy consistently."""
    return risk.assess(
        changed_files,
        findings,
        policy=session.config.policy,
        review=session.config.review,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )


def stage_blocking_severities(session: Session, stage: Stage) -> Sequence[str]:
    return findingsmod.stage_blocking_severities(session.config, stage)


def findings_next_hint(
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
            "or record why it is not worth fixing with `--action accepted --reason <reason> --note <note>`."
        )
        envelope.next_command = _respond_command(actionable[0].id)


def unresolved_findings(session: Session, items: list[Finding]) -> list[Finding]:
    """Review and docs findings that still need ``respond``, in id order.

    This is the test the attestation applies to each stage's evidence, so a run
    must not leave review or documentation while one remains: ``respond`` is not
    legal after documentation, and merge-back would refuse the evidence.
    """
    unresolved: list[Finding] = []
    for stage in (Stage.REVIEW, Stage.DOCS):
        unresolved.extend(
            findingsmod.unresolved(
                [finding for finding in items if finding.stage is stage],
                blocking_severities=stage_blocking_severities(session, stage),
            )
        )
    return sorted(unresolved, key=lambda finding: finding.id)


def unresolved_next_hint(session: Session, envelope: Envelope, unresolved: list[Finding]) -> None:
    """Point a green stage's envelope at the first finding that still needs ``respond``."""
    blocking = [
        finding
        for finding in unresolved
        if finding
        in findingsmod.blocking(
            [finding],
            blocking_severities=stage_blocking_severities(session, finding.stage),
        )
    ]
    actionable = [finding for finding in unresolved if finding not in blocking]
    findings_next_hint(envelope, blocking, actionable)


def findings_unresolved(
    run: RunDoc,
    unresolved: list[Finding],
    *,
    step: str,
    next_instruction: str | None = None,
    next_command: str | None = None,
) -> FindingsUnresolved:
    """Name the findings that keep the run from taking ``step``."""
    ids = ", ".join(finding.id for finding in unresolved)
    return FindingsUnresolved(
        f"{step} is not allowed while finding(s) {ids} are open; answer each with `respond` first",
        state=run.state.value,
        run_id=run.run_id,
        blocking=[finding.model_dump(mode="json") for finding in unresolved],
        next_instruction=next_instruction
        or (
            "Fix each named finding and cite the commit, or record why it is not worth "
            "fixing with `--action accepted --reason <reason> --note <note>`. Then follow `next.command` again."
        ),
        next_command=next_command or _respond_command(unresolved[0].id),
    )
