"""Shared session state and orchestration helpers."""

from __future__ import annotations

import hashlib
import shlex
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import findings as findingsmod
from .. import gitx, worktree
from ..config import Config, load_config
from ..envelope import Envelope
from ..errors import (
    NoRun,
    StaleRun,
    WrongState,
)
from ..machine import Action, IllegalTransition, State, next_state, recovery_hint
from ..models import RunDoc, Stage
from ..store import Store

STATE_DIR_NAME = "agentic-preflight"


@dataclass
class Session:
    """Everything a command needs about *where* it is running."""

    repo_root: Path
    caller_root: Path
    owner_id: str
    store: Store
    config: Config
    selected_run_id: str | None = None

    def active_run_id(self) -> str | None:
        return self.selected_run_id or self.store.get_active(self.owner_id)


def worktree_identity(cwd: Path | str) -> str:
    """Return a stable, filesystem-safe identity for one linked worktree."""
    private_git_dir = gitx.git_dir(cwd).resolve()
    return hashlib.sha256(str(private_git_dir).encode()).hexdigest()


def open_session(cwd: Path | str | None = None, *, run_id: str | None = None) -> Session:
    cwd = Path(cwd) if cwd else Path.cwd()
    caller_root = gitx.repo_root(cwd)
    # GIT_COMMON_DIR, not GIT_DIR: these differ when the caller is already
    # inside a worktree, and run state must be one namespace per clone.
    state_root = gitx.git_common_dir(cwd) / STATE_DIR_NAME
    store = Store(state_root)
    owner_id = worktree_identity(caller_root)
    store.migrate_legacy_current(owner_id)

    # Once a run exists, its resolved snapshot is authoritative. This also
    # keeps a malformed or edited working-copy config from stranding `status`
    # or silently reshaping an in-flight gate.
    cfg = None
    current = run_id or store.get_active(owner_id)
    active = None
    if current:
        try:
            active = store.load_run(current)
            if active.config_snapshot is not None:
                cfg = Config.model_validate(active.config_snapshot)
        except Exception:  # noqa: BLE001,S110 - a corrupt snapshot falls back to repo config
            pass
    repo_root = caller_root
    if active is not None and active.source_worktree_path:
        source = Path(active.source_worktree_path)
        if source.exists():
            repo_root = source
    cfg = cfg or load_config(repo_root)
    if cfg.worktree.mode != "in_place":
        store.set_worktrees_root(worktree.resolve_root(repo_root, cfg.worktree.root))
    return Session(
        repo_root=repo_root,
        caller_root=caller_root,
        owner_id=owner_id,
        store=store,
        config=cfg,
        selected_run_id=run_id,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def _apply(run: RunDoc, action: Action) -> None:
    """Advance the run, converting an illegal move into a typed error."""
    try:
        run.state = next_state(run.state, action)
    except IllegalTransition as exc:
        raise WrongState(
            str(exc),
            state=run.state.value,
            run_id=run.run_id,
        ) from exc


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


def _envelope_for(run: RunDoc, **overrides) -> Envelope:
    instruction, command = _next_hint(run.state)
    fields: dict[str, Any] = {
        "run_id": run.run_id,
        "state": run.state.value,
        "next_instruction": instruction,
        "next_command": command,
    }
    fields.update(overrides)
    return Envelope(**fields)


def _load_current(session: Session) -> RunDoc:
    run_id = session.active_run_id()
    if not run_id:
        raise NoRun()
    try:
        return session.store.load_run(run_id)
    except Exception as exc:  # a dangling `current` pointer is a missing run
        raise NoRun(f"current run {run_id} is missing from the store") from exc


def _head_moved(session: Session, run: RunDoc) -> str | None:
    """Return the current tip if it differs from the reviewed one."""
    source = Path(run.source_worktree_path) if run.source_worktree_path else session.repo_root
    try:
        tip = gitx.rev_parse(source, "HEAD")
    except (gitx.GitError, OSError):
        return None
    expected = run.source_head_sha or run.head_sha
    return None if tip == expected else tip


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
        next_command=shlex.join(
            [
                "agentic-preflight",
                "start",
                "--intent",
                run.intent or "<objective and acceptance criteria>",
            ]
        ),
    )


def _worktree_mode(run: RunDoc, fallback: Config) -> str:
    """Return the snapshotted lifecycle, treating pre-feature runs as strict."""
    if run.config_snapshot is None:
        return fallback.worktree.mode
    section = run.config_snapshot.get("worktree", {})
    return section.get("mode", "strict")


def _is_in_place(run: RunDoc, fallback: Config) -> bool:
    return _worktree_mode(run, fallback) == "in_place"


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
    mode = _worktree_mode(run, session.config)
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
