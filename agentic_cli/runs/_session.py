"""Shared session state and orchestration helpers."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import gitx, worktree
from ..config import Config, load_config
from ..envelope import Envelope
from ..errors import (
    START_COMMAND,
    NoRun,
    StaleRun,
    WrongState,
)
from ..machine import Action, IllegalTransition, State, next_state
from ..models import RunDoc
from ..store import Store

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
    if cfg.worktree.mode != "in_place":
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
        State.CREATED: ("Create the worktree.", START_COMMAND),
        State.WORKTREE_READY: ("Synchronize with the fresh remote base.", None),
        State.SYNC_RUNNING: ("Remote synchronization is running.", None),
        State.SYNC_CONFLICT: (
            "The fresh-base rebase conflicted. Preserve the report and restart after resolution.",
            "agentic-cli abort --force",
        ),
        State.SYNC_GREEN: ("Begin review of the synchronized diff.", "agentic-cli context"),
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
        State.REVIEW_GREEN: ("Review is green. Run targeted tests.", "agentic-cli stage run test"),
        State.TEST_GREEN: (
            "Tests are green. Check whether documentation is now stale.",
            "agentic-cli context --section docs",
        ),
        State.DOCS_GREEN: ("Docs are green. Run lint.", "agentic-cli stage run lint"),
        State.LINT_GREEN: ("Lint is green. Merge the fixes back.", "agentic-cli mergeback"),
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
        State.PR_OPEN: ("Monitor pull-request checks and mergeability.", "agentic-cli ci"),
        State.CI_MONITORING: ("Continue monitoring pull-request checks.", "agentic-cli ci"),
        State.CI_FAILED: (
            "Use the failed logs to fix the branch, then run a fresh full validation.",
            None,
        ),
        State.CHECKS_PASSED: (
            "Checks passed. Ask the user to review and merge; check again later.",
            "agentic-cli ci --once",
        ),
        State.CI_TIMED_OUT: (
            "CI monitoring timed out. Check again when ready.",
            "agentic-cli ci",
        ),
        State.PR_MERGED: (
            "The pull request merged. Preview cleanup and ask the user.",
            "agentic-cli cleanup",
        ),
    }.get(state, (None, None))


def _envelope_for(run: RunDoc, **overrides) -> Envelope:
    instruction, command = _next_hint(run.state)
    fields: dict[str, Any] = dict(
        run_id=run.run_id,
        state=run.state.value,
        next_instruction=instruction,
        next_command=command,
    )
    fields.update(overrides)
    return Envelope(**fields)


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
        next_command=shlex.join(
            [
                "agentic-cli",
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


def _worktree_completion(mode: str, *, merged_pr: bool = False) -> str:
    prefix = "Merged pull request cleanup is complete; " if merged_pr else "Run complete. "
    if mode == "in_place":
        return prefix + "the in-place checkout was left intact."
    if mode == "reusable":
        return prefix + "the reusable runner is ready for the next run."
    return prefix + "the strict worktree was removed."


def _worktree_cleanup_action(mode: str) -> str:
    if mode == "in_place":
        return "leave in-place checkout intact"
    if mode == "reusable":
        return "release reusable runner"
    return "remove strict worktree"


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
