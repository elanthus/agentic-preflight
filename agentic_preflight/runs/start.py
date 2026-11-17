"""Run creation, synchronization, and validation-checkout setup."""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Literal

from .. import attestation, gitx, housekeeping, timing, worktree
from .. import sync as syncmod
from ..cli_contract import StartData
from ..config import Config, config_digest, load_config_layers
from ..envelope import Envelope, ExitCode
from ..errors import (
    REMOTE_TIMEOUT_CHECKS,
    AgenticError,
    DirtyTree,
    EmptyDiff,
    IntentRequired,
    MissingMergeBase,
    NeedsHuman,
    RemoteTimeout,
    SetupFailed,
    SyncConflictError,
    SyncFailed,
    UnrelatedHistories,
    WrongState,
)
from ..errors import (
    OperationInProgress as OperationInProgressError,
)
from ..gitx import redact_remote_credentials
from ..machine import TERMINAL_STATES, Action, State
from ..models import Producer, RiskAssessment, RunDoc, SetupFailure
from ..repository import require_supported_object_format
from ..stages import shellstage
from ..stages.shellstage import OutputProtection
from ..store import CurrentRunExists, RunReadError, UnknownRun
from . import evidence
from ._session import (
    Session,
    apply_action,
    assess_risk,
    envelope_for,
    new_run_id,
    next_hint,
    release_run_worktree,
    start_command,
    utc_now,
    worktree_identity,
    worktree_mode,
)
from .fix_commits import UnreadableFixHistory, _branch_only_commits

# Lower-case harness names. Integration ids are accepted as aliases, so
# `--client claude` (the `integrations install` name) records `claude-code`.
_CLIENT_ALIASES = {"claude": "claude-code"}
_CLIENT_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_MODEL_MAX_LENGTH = 128


def normalize_client(value: str) -> str:
    """Return the canonical lower-case client name, or raise ``ValueError``."""
    name = re.sub(r"[ _]+", "-", value.strip().lower())
    name = _CLIENT_ALIASES.get(name, name)
    if not _CLIENT_NAME.fullmatch(name):
        raise ValueError(
            "a client name is 1-64 ASCII letters, digits, '.', or '-' (spaces and '_' "
            "become '-'), for example claude-code, codex, cursor, opencode, or amp"
        )
    return name


def normalize_model(value: str) -> str:
    """Return the agent's reported model id unchanged but trimmed, or raise ``ValueError``."""
    model = value.strip()
    if not model or len(model) > _MODEL_MAX_LENGTH or not model.isprintable():
        raise ValueError(
            f"a model id is 1-{_MODEL_MAX_LENGTH} printable characters, for example claude-opus-4-5"
        )
    return model


def detect_client(environ: Mapping[str, str]) -> str | None:
    """Name the coding agent from a documented environment marker, else ``None``.

    Only markers that the harness documents as set for the commands it runs are
    used. Claude Code sets ``CLAUDECODE=1`` in the subprocesses it spawns. Other
    harnesses document no such marker, so they are recorded only from ``--client``.
    """
    if environ.get("CLAUDECODE") == "1":
        return "claude-code"
    return None


def _tool_version() -> str:
    try:
        return distribution_version("agentic-preflight")
    except PackageNotFoundError:
        return "unknown"


def build_producer(
    *,
    client: str | None = None,
    model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Producer:
    """Record who produced a run. Flags win; the model is never inferred."""
    resolved_client: str | None
    client_source: Literal["flag", "environment"] | None
    if client is not None:
        resolved_client, client_source = normalize_client(client), "flag"
    else:
        resolved_client = detect_client(os.environ if environ is None else environ)
        client_source = "environment" if resolved_client is not None else None
    reported_model = normalize_model(model) if model is not None else None
    return Producer(
        tool_version=_tool_version(),
        python_version=platform.python_version(),
        platform=sys.platform,
        client=resolved_client,
        client_source=client_source,
        reported_model=reported_model,
        reported_model_source="flag" if reported_model is not None else None,
    )


def _release_idle_runner(session: Session, run: RunDoc) -> None:
    """Free an orphaned run's reusable runner when it provably holds no work.

    Releasing resets and cleans the checkout and deletes its branch, so it
    happens only when no fix commits were recorded, the runner is clean, and
    both its HEAD and its validation branch are still the reviewed head.
    Otherwise, or on any failure, the runner stays leased for ``gc`` and a
    later start reports it as before. A runner held because its branch holds
    commits, or a branch history that cannot be read, is detached; it is put
    back on that branch so the next run cannot lease it.
    """
    if run.fix_commits or run.worktree_released or worktree_mode(run) != "reusable":
        return
    path = run.worktree_path
    if not path or not Path(path).exists():
        return
    try:
        if not gitx.is_clean(path) or gitx.rev_parse(path, "HEAD") != run.head_sha:
            return
        # Release deletes the branch, which a detached checkout does not show.
        # Like any other hold, a branch history that cannot be read keeps the
        # runner leased without an event; `gc` reports it.
        try:
            hidden = bool(_branch_only_commits(run, path, [], head=run.head_sha))
        except (UnreadableFixHistory, gitx.GitError, OSError):
            hidden = True
        if hidden:
            _reattach_runner(path, run)
            return
        release_run_worktree(session, run)
        with session.store.transaction(run.run_id) as doc:
            doc.worktree_released = True
    except Exception as exc:  # noqa: BLE001 - orphaning must complete regardless
        session.store.append_event(
            run.run_id, {"event": "runner_release_failed", "error": str(exc)[:500]}
        )


def _reattach_runner(path: str, run: RunDoc) -> None:
    """Put a held, detached runner back on its run's branch, so it stays leased.

    A detached runner counts as released, and the next run would lease it and
    reset it. The runner is clean at the reviewed head, which the source branch
    still reaches, so switching to the validation branch loses nothing.
    """
    if run.worktree_branch and gitx.current_branch(path) == "HEAD":
        gitx.run(path, "switch", "--quiet", run.worktree_branch)


def _interrupted_checkouts(session: Session, run: RunDoc) -> list[Path]:
    """Return the validation checkouts an interrupted ``start`` left for ``run``.

    The run record may not name its checkout yet, so ownership comes from Git:
    a checkout on the run's own ``ap/<run_id>`` branch, or a detached strict
    checkout at a path named for the run, which `git worktree add` leaves if
    the process dies before the branch is created. Only checkouts under the
    store's worktrees directory count, so a user's own worktree is never taken.
    """
    if worktree_mode(run) == "in_place":
        return []
    root = session.store.worktrees_dir.resolve()
    branch_ref = f"refs/heads/ap/{run.run_id}"
    owned: list[Path] = []
    for record in gitx.list_worktrees(session.repo_root):
        if "worktree" not in record:
            continue
        path = Path(record["worktree"])
        if not path.resolve().is_relative_to(root):
            continue
        if record.get("branch") == branch_ref or ("detached" in record and path.name == run.run_id):
            owned.append(path)
    return owned


def _is_disposable(run: RunDoc, path: Path) -> bool:
    """Whether ``path`` is gone, or clean at the head ``run`` started from."""
    return not path.exists() or (
        gitx.is_clean(path) and gitx.rev_parse(path, "HEAD") == run.head_sha
    )


def _require_disposable(run: RunDoc, checkouts: list[Path]) -> None:
    """Refuse to reclaim a checkout that holds anything beyond the run's start."""
    for path in checkouts:
        if _is_disposable(run, path):
            continue
        raise NeedsHuman(
            f"run {run.run_id} was interrupted during start and left validation checkout "
            f"{path} with changes or commits beyond {run.head_sha}; refusing to reclaim it",
            state=run.state.value,
            run_id=run.run_id,
            data={
                "worktree_path": str(path),
                "worktree_branch": f"ap/{run.run_id}",
                "head_sha": run.head_sha,
            },
            next_instruction=(
                f"Show the checkout to the user. Once they have kept what they need, it must "
                f"have no local changes and its HEAD must be {run.head_sha}. Then start "
                "again: it reclaims the checkout."
            ),
            next_command=f"git -C {shlex.quote(str(path))} status",
        )


def _reclaim_interrupted_checkouts(
    session: Session, run: RunDoc, checkouts: list[Path]
) -> list[Path]:
    """Release the ``checkouts`` an interrupted ``start`` provisioned for ``run``.

    The caller has checked that each one is disposable. A reusable runner is
    reset and detached so the next run can lease it; a strict checkout is
    removed. The run's ``ap/`` branch is deleted afterwards, atomically and only
    while it still points at the head the run started from and no checkout has
    it checked out, so commits made on it are never lost. A failure raises
    ``needs_human`` so the caller leaves the run as it was for a retry.
    """
    ref = f"refs/heads/ap/{run.run_id}"
    reclaimed: list[Path] = []
    try:
        for path in checkouts:
            if path.name == run.run_id:
                worktree.remove(session.repo_root, path)
            else:
                worktree.release_reusable(
                    session.repo_root, path, branch=None, copied_files=run.copied_files
                )
            reclaimed.append(path)
        if not any(
            record.get("branch") == ref for record in gitx.list_worktrees(session.repo_root)
        ):
            gitx.run(session.repo_root, "update-ref", "-d", ref, run.head_sha, check=False)
    except (worktree.WorktreeError, gitx.GitError, OSError, subprocess.SubprocessError) as exc:
        session.store.append_event(
            run.run_id, {"event": "interrupted_checkout_release_failed", "error": str(exc)[:500]}
        )
        raise NeedsHuman(
            f"cannot release the validation checkout of interrupted run {run.run_id}: {exc}",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=(
                "Fix the problem named in the error, then start again; the interrupted "
                "run is left as it was so the release can be retried."
            ),
            next_command="agentic-preflight status --all",
        ) from exc
    session.store.append_event(
        run.run_id,
        {"event": "interrupted_checkout_reclaimed", "paths": [str(path) for path in reclaimed]},
    )
    return reclaimed


_RUN_ID = re.compile(r"r_[0-9a-f]{10}")


def _reclaim_checkouts_from_ended_runs(session: Session) -> None:
    """Free checkouts still held by ended runs whose ``start`` was interrupted.

    An interrupted run that was aborted, or orphaned by an earlier version,
    never recorded its checkout, so neither ``abort`` nor ``gc`` releases it. A
    leased reusable runner then blocks every later lease, and a strict checkout
    leaks. Each checkout under the worktrees directory names its run by its
    ``ap/<run_id>`` branch or, detached, by its directory. It is reclaimed only
    when that run is terminal, recorded no fix commits, holds no unreleased
    recorded checkout, is idle, and the checkout is clean at the run's head.
    Only the checked checkout is reclaimed. A runner that fails those checks
    is refused with ``needs_human``: a reusable run must lease it, and a strict
    run must remove it first, so a new run in either mode cannot proceed past
    it. A strict checkout that fails them, or cannot be removed (for example
    because it is locked), is left in place, since it blocks nothing.
    """
    root = session.store.worktrees_dir.resolve()
    runner = (session.store.worktrees_dir / "runner").resolve()
    for record in gitx.list_worktrees(session.repo_root):
        if "worktree" not in record:
            continue
        path = Path(record["worktree"])
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        branch = record.get("branch", "")
        if branch.startswith("refs/heads/ap/"):
            run_id = branch.removeprefix("refs/heads/ap/")
        elif "detached" in record:
            run_id = path.name
        else:
            continue
        if not _RUN_ID.fullmatch(run_id):
            continue
        try:
            run = session.store.load_run(run_id)
        except (UnknownRun, RunReadError):
            continue
        if (
            run.state not in TERMINAL_STATES
            or run.fix_commits
            or (run.worktree_path and not run.worktree_released)
            or resolved not in {owned.resolve() for owned in _interrupted_checkouts(session, run)}
        ):
            continue
        with session.store.try_operation(run.run_id) as idle:
            if not idle:
                continue
            if resolved == runner:
                _require_disposable(run, [path])
                _reclaim_interrupted_checkouts(session, run, [path])
            elif _is_disposable(run, path):
                try:
                    _reclaim_interrupted_checkouts(session, run, [path])
                except NeedsHuman:
                    continue  # recorded as an event; a strict leftover blocks nothing


def _orphan(
    session: Session,
    run: RunDoc,
    *,
    reason: str,
    interrupted_start: bool = False,
) -> None:
    """Detach an idle run without destroying its evidence or validation work.

    With ``interrupted_start``, the run's ``start`` died before the run was
    synchronized. The checkout it provisioned is reclaimed *before* the run is
    orphaned, so a refusal or failed release leaves the run for a retry.
    """
    with session.store.try_operation(run.run_id) as idle:
        if not idle:
            raise WrongState(
                f"run {run.run_id} is executing a command and cannot be replaced",
                state=run.state.value,
                run_id=run.run_id,
                next_instruction="Wait for the active command to finish, then retry.",
                next_command="agentic-preflight status",
            )
        released = False
        if interrupted_start:
            checkouts = _interrupted_checkouts(session, run)
            _require_disposable(run, checkouts)
            reclaimed = _reclaim_interrupted_checkouts(session, run, checkouts)
            # Only a recorded checkout that was reclaimed, or is gone, is released;
            # one outside the current worktrees root stays leased for `gc`.
            if run.worktree_path:
                recorded = Path(run.worktree_path)
                released = not recorded.exists() or recorded.resolve() in {
                    path.resolve() for path in reclaimed
                }
        with session.store.transaction(run.run_id) as doc:
            if doc.state not in TERMINAL_STATES:
                apply_action(doc, Action.ORPHAN)
            doc.orphaned_reason = reason
            if released:
                doc.worktree_released = True
        session.store.clear_run(run.run_id)
        session.store.append_event(run.run_id, {"event": "orphaned", "reason": reason})
        if not interrupted_start:
            _release_idle_runner(session, run)


def _resume_existing(session: Session, run: RunDoc) -> Envelope:
    from .lifecycle import status

    envelope = status(session)
    data = StartData.model_validate(envelope.data.model_dump(exclude_unset=True))
    data.resumed = True
    data.resume_reason = "matching active run"
    envelope.data = data
    return envelope


def _claim_alias(session: Session, owner_id: str, run_id: str) -> None:
    current = session.store.get_active(owner_id)
    if current and current != run_id:
        try:
            existing = session.store.load_run(current)
        except UnknownRun:
            session.store.clear_active_if(owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_active_if(owner_id, current)
            else:
                raise CurrentRunExists(current)
    if session.store.get_active(owner_id) != run_id:
        session.store.claim_active(owner_id, run_id)


@dataclass(frozen=True)
class _StartContext:
    session: Session
    cfg: Config
    repo: Path
    intent: str
    base_ref: str
    branch: str
    head_sha: str
    snapshot: dict[str, Any]
    config_digest: str
    user_config_sections: list[str]
    producer: Producer


class NoBranch(AgenticError):
    """HEAD is detached or unborn, so there is no branch to validate and publish."""

    code = "no_branch"
    exit_code = ExitCode.PRECONDITION


class UnsupportedBaseRef(AgenticError):
    """The base is not a branch that verification can resolve from the note."""

    code = "unsupported_base_ref"
    exit_code = ExitCode.PRECONDITION


def _require_branch(repo: Path) -> None:
    try:
        detached = gitx.current_branch(repo) == "HEAD"
    except gitx.NotOnBranch as exc:
        raise NoBranch(
            f"{exc}; start validates and publishes a named branch",
            next_instruction="Check out the branch you want to ship, then start the run.",
            next_command="git switch <branch>",
        ) from exc
    if detached:
        raise NoBranch(
            "HEAD is detached; start validates and publishes a named branch",
            next_instruction="Check out the branch you want to ship, then start the run.",
            next_command="git switch <branch>",
        )
    if gitx.run(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        raise NoBranch(
            "the current branch has no commits yet",
            next_instruction="Commit your work on the branch, then start the run.",
            next_command="git status",
        )


def _check_preconditions(
    session: Session, *, base_ref: str | None, intent: str | None, producer: Producer
) -> _StartContext:
    repo = session.caller_root
    require_supported_object_format(repo)
    # Starting is the one command that deliberately reads the working copy.
    # Every later command uses the snapshot persisted below.
    cfg, user_config_sections = load_config_layers(repo)
    session.config = cfg
    if cfg.worktree.mode != "in_place":
        session.store.set_worktrees_root(worktree.resolve_root(repo, cfg.worktree.root))

    intent = (intent or "").strip()
    if not intent:
        raise IntentRequired(
            "start requires the user's objective and acceptance criteria",
            next_instruction=(
                "Pass what the user asked for in their own terms, including important "
                "constraints and deliberate tradeoffs. Do not substitute a diff summary."
            ),
            next_command='agentic-preflight start --intent "<user objective and acceptance criteria>"',
        )

    operation_path: Path | None = None
    if cfg.worktree.mode == "in_place":
        operation_path = repo
    elif cfg.worktree.mode == "reusable":
        reusable_path = session.store.worktrees_dir / "runner"
        if (reusable_path / ".git").is_file():
            operation_path = reusable_path
    if operation_path is not None:
        operation = gitx.operation_in_progress(operation_path)
        if operation is not None:
            raise OperationInProgressError(operation, str(operation_path.resolve()))

    # After the operation check: a paused rebase also detaches HEAD, and that
    # deserves the more specific error.
    _require_branch(repo)

    if not gitx.is_clean(repo):
        raise DirtyTree(
            "the working tree has uncommitted changes",
            next_instruction="Commit or stash your changes, then start the run.",
            next_command="git status",
        )

    base_ref = base_ref or cfg.general.base_ref
    # Fail before any stage runs rather than at push, where the hook verifies
    # without --base and resolves the note's base_ref to a branch.
    if attestation.resolve_base_ref(repo, base_ref) is None:
        raise UnsupportedBaseRef(
            f"base {base_ref!r} does not name an existing branch as <branch>, origin/<branch>, "
            "refs/heads/<branch>, or refs/remotes/origin/<branch>",
            next_instruction=(
                "Name the branch this change merges into. Fetch it from origin first "
                "if it exists only there, then start the run."
            ),
            next_command='agentic-preflight start --base-ref <branch> --intent "<objective>"',
        )
    branch = gitx.current_branch(repo)
    head_sha = gitx.rev_parse(repo, "HEAD")
    snapshot = cfg.model_dump(mode="json")
    resolved_config_digest = config_digest(snapshot)

    return _StartContext(
        session=session,
        cfg=cfg,
        repo=repo,
        intent=intent,
        base_ref=base_ref,
        branch=branch,
        head_sha=head_sha,
        snapshot=snapshot,
        config_digest=resolved_config_digest,
        user_config_sections=user_config_sections,
        producer=producer,
    )


#: States only an interrupted `start` leaves persisted, with the orphaning reason.
_INTERRUPTED_START: dict[State, str] = {
    State.CREATED: "start was interrupted before its validation checkout existed",
    State.WORKTREE_READY: "start was interrupted before synchronization began",
}


def _resolve_existing_run(ctx: _StartContext, *, replace: bool) -> Envelope | None:
    session = ctx.session

    current = session.store.get_active(session.owner_id)
    if current:
        try:
            existing = session.store.load_run(current)
        except UnknownRun:
            session.store.clear_active_if(session.owner_id, current)
        else:
            if existing.state in TERMINAL_STATES:
                session.store.clear_run(existing.run_id)
            else:
                if existing.source_worktree_id != session.owner_id:
                    raise WrongState(
                        f"run {existing.run_id} belongs to another source worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={"source_worktree_path": existing.source_worktree_path},
                        next_instruction="Run `start` from the recorded source worktree.",
                        next_command="agentic-preflight status",
                    )
                interrupted = _INTERRUPTED_START.get(existing.state)
                if interrupted is not None:
                    # `start` holds the run's operation lock from before the
                    # record exists until it returns, so an idle run in one of
                    # these states belongs to a start that died. `_orphan`
                    # refuses a run whose start is still running. Replace it
                    # whatever the request, since there is nothing to resume.
                    _orphan(session, existing, reason=interrupted, interrupted_start=True)
                    return None
                stale = (
                    existing.stale
                    or ctx.head_sha != existing.source_head_sha
                    or ctx.branch != existing.branch
                )
                matches = (
                    not stale
                    and existing.intent == ctx.intent
                    and existing.base_ref == ctx.base_ref
                    and existing.config_digest == ctx.config_digest
                )
                if matches:
                    return _resume_existing(session, existing)
                if stale:
                    _orphan(session, existing, reason="source worktree moved")
                elif replace:
                    _orphan(session, existing, reason="replaced by a new start")
                else:
                    command = start_command(
                        ctx.intent,
                        base_ref=ctx.base_ref,
                        default_base_ref=ctx.cfg.general.base_ref,
                        producer=ctx.producer,
                        replace=True,
                    )
                    raise WrongState(
                        f"run {existing.run_id} is already active in this worktree",
                        state=existing.state.value,
                        run_id=existing.run_id,
                        data={
                            "existing_intent": existing.intent,
                            "requested_intent": ctx.intent,
                            "source_worktree_path": existing.source_worktree_path,
                        },
                        next_instruction=(
                            "Resume the matching run, or explicitly replace it. Replacement "
                            "orphans the old run without deleting its evidence or fixes."
                        ),
                        next_command=command,
                    )
    return None


def _require_changes(ctx: _StartContext) -> str:
    try:
        merge_base = gitx.merge_base(ctx.repo, ctx.base_ref, "HEAD")
    except gitx.GitError as exc:
        # Git uses status 1 for no common ancestor. Operational failures (for
        # example unreadable objects) must keep their original Git diagnostic.
        if exc.returncode != 1:
            raise
        if gitx.out(ctx.repo, "rev-parse", "--is-shallow-repository") == "true":
            raise MissingMergeBase(
                f"repository is shallow; the available history has no merge base "
                f"with {ctx.base_ref!r}",
                next_instruction=(
                    "Fetch the missing history with git fetch --unshallow, or deepen "
                    "it incrementally with git fetch --deepen=<n>, then start again. "
                    "If the complete histories still have no common ancestor, check the base branch."
                ),
                next_command="git fetch --unshallow",
            ) from exc
        raise UnrelatedHistories(
            f"HEAD and {ctx.base_ref!r} have unrelated histories: no merge base exists "
            "in this non-shallow repository",
            next_instruction=(
                "Check that the base branch and feature branch belong to the same history. "
                "Choose the correct base or resolve the unrelated histories before starting again."
            ),
            next_command="git log --oneline --graph --all",
        ) from exc

    # Criss-cross history can hide a change from the single merge base, such as an
    # ``-s ours`` merge that reverts a branch already in the base; synchronization keeps it.
    changed = gitx.classified_files(ctx.repo, ctx.base_ref, "HEAD")
    if not changed:
        raise EmptyDiff(
            f"{ctx.branch} has no changes over {ctx.base_ref}; there is nothing to review",
            next_instruction="Commit some work on a branch, then start a run.",
        )
    return merge_base


def _create_run_record(ctx: _StartContext, run_id: str, merge_base: str) -> tuple[Path, str]:
    """Persist the new run and claim its lease. The caller holds the run's operation lock."""
    run = RunDoc(
        schema_version=2,
        run_id=run_id,
        state=State.CREATED,
        branch=ctx.branch,
        base_ref=ctx.base_ref,
        merge_base_sha=merge_base,
        head_sha=ctx.head_sha,
        source_head_sha=ctx.head_sha,
        intent=ctx.intent,
        intent_source="user",
        source_worktree_id=ctx.session.owner_id,
        source_worktree_path=str(ctx.repo.resolve()),
        owner_ids=[ctx.session.owner_id],
        config_snapshot=ctx.snapshot,
        config_digest=ctx.config_digest,
        user_config_sections=ctx.user_config_sections,
        producer=ctx.producer,
        created_at=utc_now(),
    )
    # Persist the intent *before* the git call, so a crash mid-create leaves a
    # visible CREATED record rather than an orphan nobody knows about. The next
    # `start` in this worktree orphans that record and creates a fresh run;
    # `gc` reclaims it only once the source worktree or its lease disappears.
    ctx.session.store.create_run(run)
    try:
        ctx.session.store.claim_active(ctx.session.owner_id, run_id)
    except CurrentRunExists as exc:
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.ABORT)
            doc.worktree_released = True
        raise WrongState(
            f"run {exc.run_id} is already active in this worktree",
            run_id=exc.run_id,
            next_instruction="Finish, clean up, or abort the active run before starting another.",
            next_command="agentic-preflight status",
        ) from exc
    ctx.session.store.append_event(
        run_id,
        {
            "event": "run_created",
            "head_sha": ctx.head_sha,
            "config_digest": ctx.config_digest,
            "config_snapshot": ctx.snapshot,
            "intent": ctx.intent,
            "intent_source": "user",
            "producer": ctx.producer.model_dump(mode="json"),
            # This run times the commands the tool runs (see timing.timed_command).
            timing.COMMAND_TIMING_MARKER: timing.COMMAND_TIMING_VERSION,
        },
    )

    in_place = ctx.cfg.worktree.mode == "in_place"
    reusable = ctx.cfg.worktree.mode == "reusable"
    wt_path = (
        ctx.repo
        if in_place
        else ctx.session.store.worktrees_dir / ("runner" if reusable else run_id)
    )
    wt_branch = ctx.branch if in_place else f"ap/{run_id}"
    return wt_path, wt_branch


def _provision_validation_checkout(
    ctx: _StartContext, run_id: str, wt_path: Path, wt_branch: str
) -> None:
    try:
        if ctx.cfg.worktree.mode == "in_place":
            pass
        elif ctx.cfg.worktree.mode == "reusable":
            worktree.acquire_reusable(
                ctx.repo,
                path=wt_path,
                branch=wt_branch,
                head_sha=ctx.head_sha,
            )
        else:  # strict
            retained_runner = ctx.session.store.worktrees_dir / "runner"
            if retained_runner.exists():
                if gitx.current_branch_or_none(retained_runner) != "HEAD":
                    raise worktree.WorktreeError(
                        f"cannot enter strict mode: reusable runner {retained_runner} "
                        "is still leased; recover or release its run first"
                    )
                worktree.remove(ctx.repo, retained_runner)
            worktree.create(
                ctx.repo,
                path=wt_path,
                branch=wt_branch,
                head_sha=ctx.head_sha,
            )
    except worktree.WorktreeError as exc:
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.ABORT)
            doc.worktree_released = True
        ctx.session.store.clear_run(run_id)
        raise NeedsHuman(
            str(exc),
            run_id=run_id,
            data={"worktree_path": str(wt_path)},
            next_instruction=(
                "Inspect the existing validation checkout and run records before "
                "reclaiming anything; an interrupted run may still own commits."
            ),
            next_command="agentic-preflight gc",
        ) from exc

    validation_owner = worktree_identity(wt_path)
    with ctx.session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
    try:
        _claim_alias(ctx.session, validation_owner, run_id)
    except CurrentRunExists as exc:
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.ABORT)
            doc.worktree_released = False
        ctx.session.store.clear_run(run_id)
        raise NeedsHuman(
            f"validation worktree is still owned by run {exc.run_id}",
            run_id=run_id,
            data={"worktree_path": str(wt_path), "conflicting_run_id": exc.run_id},
            next_instruction="Inspect both runs before reclaiming the validation checkout.",
            next_command="agentic-preflight status --all",
        ) from exc

    with ctx.session.store.transaction(run_id) as doc:
        if validation_owner not in doc.owner_ids:
            doc.owner_ids.append(validation_owner)
        apply_action(doc, Action.CREATE_WORKTREE)
        apply_action(doc, Action.BEGIN_SYNC)


def _synchronize(
    ctx: _StartContext, run_id: str, wt_path: Path, wt_branch: str
) -> tuple[syncmod.SyncResult, list[str]]:
    try:
        with (
            ctx.session.store.resource("sync", holder=run_id),
            ctx.session.store.resource("notes", holder=run_id),
            timing.timed_command(ctx.session.store, run_id, "sync"),
        ):
            sync_result = syncmod.synchronize(
                ctx.repo,
                wt_path,
                base_ref=ctx.base_ref,
                network_timeout=ctx.cfg.general.network_timeout_seconds,
            )
    except syncmod.OperationInProgress as exc:
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.SYNC_FAILED)
            run = doc
        report: dict[str, object] = {"operation": exc.operation, "path": exc.path}
        ctx.session.store.append_event(run_id, {"event": "sync_refused", **report})
        raise OperationInProgressError(
            exc.operation,
            exc.path,
            state=run.state.value,
            run_id=run_id,
        ) from exc
    except syncmod.SyncConflict as exc:
        with ctx.session.store.transaction(run_id) as doc:
            doc.sync_base_sha = exc.base_sha
            doc.sync_base_ref = exc.base_ref
            apply_action(doc, Action.SYNC_FAILED)
            run = doc
        report = {
            "base_ref": exc.base_ref,
            "base_sha": exc.base_sha,
            "head_before": exc.head_before,
            "conflicting_files": exc.conflicting_files,
            "worktree_path": str(wt_path),
        }
        ctx.session.store.append_event(run_id, {"event": "sync_conflict", **report})
        raise SyncConflictError(
            str(exc),
            state=run.state.value,
            run_id=run_id,
            data=report,
            next_instruction=(
                "The validation-checkout synchronization was aborted cleanly. Show the conflict "
                "report to the user, resolve or rebase the source branch deliberately, "
                "then abort this run and start again with the same intent."
            ),
            next_command="agentic-preflight abort --force",
        ) from exc
    except gitx.RemoteTimeout as exc:
        # The locks were released when the ``with`` above unwound.
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.SYNC_FAILED)
            run = doc
        error = RemoteTimeout.from_git(
            exc,
            data={"base_ref": ctx.base_ref, "worktree_path": str(wt_path)},
            state=run.state.value,
            run_id=run_id,
            next_instruction=(
                f"{REMOTE_TIMEOUT_CHECKS} Then abort this run and start again with the same intent."
            ),
            next_command="agentic-preflight abort --force",
        )
        ctx.session.store.append_event(run_id, {"event": "remote_timeout", **error.data})
        raise error from exc
    except (gitx.GitError, subprocess.TimeoutExpired, OSError) as exc:
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.SYNC_FAILED)
            run = doc
        report = {
            "base_ref": ctx.base_ref,
            "error_type": type(exc).__name__,
            "detail": redact_remote_credentials(str(exc)),
            "worktree_path": str(wt_path),
        }
        ctx.session.store.append_event(run_id, {"event": "sync_failed", **report})
        raise SyncFailed(
            f"synchronizing with the fresh remote base failed: {report['detail']}",
            state=run.state.value,
            run_id=run_id,
            data=report,
            next_instruction=(
                "Fix the remote, network, or base ref named in the error, then abort "
                "this run and start again with the same intent."
            ),
            next_command="agentic-preflight abort --force",
        ) from exc

    changed = gitx.changed_files(wt_path, sync_result.base_sha, "HEAD")
    if not changed:
        if ctx.cfg.worktree.mode == "in_place":
            pass
        elif ctx.cfg.worktree.mode == "reusable":
            worktree.release_reusable(ctx.repo, wt_path, branch=wt_branch, copied_files=[])
        else:
            worktree.remove(ctx.repo, wt_path, branch=wt_branch)
        with ctx.session.store.transaction(run_id) as doc:
            apply_action(doc, Action.ABORT)
            doc.worktree_released = True
        ctx.session.store.clear_run(run_id)
        raise EmptyDiff(
            "the branch has no changes after synchronizing with the fresh remote base",
            next_instruction="The requested change is already present upstream.",
        )
    with ctx.session.store.transaction(run_id) as doc:
        doc.head_sha = sync_result.head_after
        doc.source_head_sha = (
            sync_result.head_after if ctx.cfg.worktree.mode == "in_place" else ctx.head_sha
        )
        doc.merge_base_sha = sync_result.base_sha
        doc.sync_base_sha = sync_result.base_sha
        doc.sync_base_ref = sync_result.base_ref
        doc.sync_remote = sync_result.remote
        doc.changed_files = changed
    return sync_result, changed


_SETUP_OUTPUT_TAIL = 4000


def _relative_to(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` when it lies inside it."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


def _execute_setup(
    ctx: _StartContext, run_id: str, wt_path: Path, copied: list[str], command: str
) -> tuple[shellstage.StageResult, Path]:
    """Run the setup command with copied-file redaction and write its log."""
    log_path = ctx.session.store.logs_dir(run_id) / "setup.txt"
    try:
        protection = OutputProtection.capture(wt_path, copied)
    except shellstage.SecretRedactionError as exc:
        result = shellstage.StageResult(
            command=command,
            exit_code=shellstage.EXIT_UNRUNNABLE,
            output=(
                f"cannot protect setup output: copied file {_relative_to(exc.path, wt_path)!r} "
                "could not be read"
            ),
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.output, encoding="utf-8", newline="\n")
        return result, log_path
    with timing.timed_command(ctx.session.store, run_id, "setup"):
        completed = worktree.run_setup(
            wt_path,
            command,
            timeout_seconds=ctx.cfg.stage.timeout_seconds,
            guarded_files=copied,
        )
    protected = protection.finish(completed, log_path)
    result = replace(protected.result, output=protected.clean_output)
    return result, protected.log_path


def _run_setup_command(
    ctx: _StartContext, run_id: str, wt_path: Path, changed: list[str]
) -> tuple[list[str], RiskAssessment, dict[str, object] | None]:
    copied = (
        worktree.protect_in_place_files(ctx.repo, ctx.cfg.worktree.copy_files)
        if ctx.cfg.worktree.mode == "in_place"
        else worktree.copy_files(ctx.repo, wt_path, ctx.cfg.worktree.copy_files)
    )
    assessment = assess_risk(ctx.session, changed, [])

    # Persist copied paths before setup so an abort after a failed command still
    # removes secret-bearing copies from a reusable runner.
    with ctx.session.store.transaction(run_id) as doc:
        doc.copied_files = copied

    setup_result = None
    if ctx.cfg.worktree.setup_command:
        completed, log_path = _execute_setup(
            ctx, run_id, wt_path, copied, ctx.cfg.worktree.setup_command
        )
        setup_result = {
            "kind": "custom",
            "command": ctx.cfg.worktree.setup_command,
            "exit_code": completed.exit_code,
            "timed_out": completed.timed_out,
            "log_path": str(log_path),
        }
        if completed.exit_code != 0:
            setup_result["output_tail"] = completed.output[-_SETUP_OUTPUT_TAIL:]
            failure = SetupFailure(
                scope="initial",
                command=ctx.cfg.worktree.setup_command,
                exit_code=completed.exit_code,
                worktree_path=str(wt_path),
                next_instruction=(
                    "Fix the setup command or its environment, then abort this run and "
                    "start a fresh one. The active run keeps its configuration snapshot."
                ),
                next_command="agentic-preflight abort --force",
            )
            with ctx.session.store.transaction(run_id) as doc:
                doc.risk = assessment
                doc.setup_failure = failure
                apply_action(doc, Action.SETUP_FAILED)
            ctx.session.store.append_event(
                run_id,
                {
                    "event": "setup_failed",
                    **failure.model_dump(mode="json"),
                    "timed_out": completed.timed_out,
                    "log_path": str(log_path),
                },
            )
            reason = (
                f"timed out after {ctx.cfg.stage.timeout_seconds}s"
                if completed.timed_out
                else f"exit {completed.exit_code}"
            )
            raise SetupFailed(
                f"the setup command failed ({reason}); output is in {log_path}",
                state=State.SETUP_FAILED.value,
                run_id=run_id,
                stage="setup",
                data={"worktree_path": str(wt_path), "setup": setup_result},
                next_instruction=failure.next_instruction,
                next_command=failure.next_command,
            )
    return copied, assessment, setup_result


def _prime_review(
    ctx: _StartContext,
    run_id: str,
    wt_path: Path,
    wt_branch: str,
    sync_result: syncmod.SyncResult,
    changed: list[str],
    copied: list[str],
    assessment: RiskAssessment,
    setup_result: dict[str, object] | None,
) -> Envelope:
    with ctx.session.store.transaction(run_id) as doc:
        doc.worktree_path = str(wt_path)
        doc.worktree_branch = wt_branch
        doc.risk = assessment
        apply_action(doc, Action.SYNC_PASSED)
        apply_action(doc, Action.BEGIN_REVIEW)
        run = doc

    ctx.session.store.append_event(
        run_id,
        {
            "event": "worktree_ready",
            "path": str(wt_path),
            "mode": ctx.cfg.worktree.mode,
            "sync": sync_result.as_dict(),
            "risk": assessment.model_dump(mode="json"),
        },
    )

    run = evidence.advance(ctx.session, evidence.discover(ctx.session, run))

    return envelope_for(
        run,
        next_instruction="Fetch the diff before judging it."
        if run.state is State.REVIEW_AWAITING_FINDINGS
        else next_hint(run.state)[0],
        next_command="agentic-preflight context"
        if run.state is State.REVIEW_AWAITING_FINDINGS
        else next_hint(run.state)[1],
        data=StartData(
            worktree_path=str(wt_path),
            worktree_branch=wt_branch,
            worktree_mode=ctx.cfg.worktree.mode,
            branch=ctx.branch,
            base_ref=ctx.base_ref,
            head_sha=sync_result.head_after,
            source_head_sha=ctx.head_sha,
            merge_base_sha=sync_result.base_sha,
            intent=ctx.intent,
            intent_source="user",
            producer=ctx.producer,
            sync=sync_result.as_dict(),
            changed_files=changed,
            risk=assessment.model_dump(mode="json"),
            # Names only. Contents are never read, logged, or echoed.
            copied_files=copied,
            setup=setup_result,
            applicability={
                stage.value: value.model_dump(mode="json")
                for stage, value in run.applicability.items()
            },
        ),
    )


def start(
    session: Session,
    *,
    base_ref: str | None = None,
    intent: str | None = None,
    replace: bool = False,
    client: str | None = None,
    model: str | None = None,
) -> Envelope:
    """Start a run, or resume or replace an existing one.

    ``client`` and ``model`` are the agent's self-reports for the new run's
    producer record. A resumed run keeps the producer recorded when it was created.
    """
    producer = build_producer(client=client, model=model)
    envelope = _start(session, base_ref=base_ref, intent=intent, replace=replace, producer=producer)
    # Both successful outcomes, a new run and a resumed one, carry StartData.
    if envelope.ok and isinstance(envelope.data, StartData):
        envelope.data.housekeeping = housekeeping.report(session.store, session.repo_root)
    return envelope


def _start(
    session: Session,
    *,
    base_ref: str | None,
    intent: str | None,
    replace: bool,
    producer: Producer,
) -> Envelope:
    ctx = _check_preconditions(session, base_ref=base_ref, intent=intent, producer=producer)
    existing = _resolve_existing_run(ctx, replace=replace)
    if existing is not None:
        return existing
    if ctx.cfg.worktree.mode != "in_place":
        _reclaim_checkouts_from_ended_runs(session)
    merge_base = _require_changes(ctx)
    run_id = new_run_id()
    # Hold the new run's operation lock until the run is ready for review, as
    # every other mutating command does. Released earlier, a concurrent
    # `start --replace`, `gc`, or `abort` sees an idle run and can orphan it
    # while its checkout is still being provisioned.
    with session.store.operation(run_id):
        wt_path, wt_branch = _create_run_record(ctx, run_id, merge_base)
        _provision_validation_checkout(ctx, run_id, wt_path, wt_branch)
        sync_result, changed = _synchronize(ctx, run_id, wt_path, wt_branch)
        copied, assessment, setup_result = _run_setup_command(ctx, run_id, wt_path, changed)
        return _prime_review(
            ctx, run_id, wt_path, wt_branch, sync_result, changed, copied, assessment, setup_result
        )
