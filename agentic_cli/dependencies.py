"""Dependency setup for disposable Node worktrees.

pnpm shares package contents through its content-addressable store while giving
each worktree its own lockfile-specific layout. npm always runs ``npm ci`` so
the worktree tests its frozen dependency graph in isolation; a ``node_modules``
symlink to the source checkout would make the verified tree mutable and visible
to Git when a repository ignores directories with ``node_modules/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import runtime, worktree


@dataclass(frozen=True)
class DependencySetup:
    manager: str
    action: str
    command: str | None = None
    reason: str | None = None
    node: dict | None = None
    exit_code: int | None = None

    def as_dict(self) -> dict:
        return {
            "manager": self.manager,
            "action": self.action,
            "command": self.command,
            "reason": self.reason,
            "node": self.node,
            "exit_code": self.exit_code,
        }


def _run_install(
    target: Path,
    command: str,
    *,
    runtime_manager: str,
    runtime_strict: bool,
    timeout_seconds: int,
) -> tuple[int, dict]:
    prepared = runtime.prepare_command(
        target, command, manager=runtime_manager, strict=runtime_strict
    )
    completed = worktree.run_setup(
        target, prepared.command, timeout_seconds=timeout_seconds
    )
    return completed.returncode, prepared.runtime.as_dict()


def setup(
    target_worktree: Path | str,
    *,
    runtime_manager: str = "auto",
    runtime_strict: bool = True,
    timeout_seconds: int = 600,
) -> DependencySetup:
    """Prepare dependencies for one worktree without changing lockfiles."""
    target = Path(target_worktree)

    if (target / "pnpm-lock.yaml").is_file():
        command = "pnpm install --frozen-lockfile"
        code, runtime_info = _run_install(
            target,
            command,
            runtime_manager=runtime_manager,
            runtime_strict=runtime_strict,
            timeout_seconds=timeout_seconds,
        )
        return DependencySetup(
            manager="pnpm",
            action="install",
            command=command,
            reason="pnpm uses its shared content-addressable store",
            node=runtime_info,
            exit_code=code,
        )

    if not (target / "package-lock.json").is_file():
        return DependencySetup(
            manager="none",
            action="skip",
            reason="no pnpm-lock.yaml or package-lock.json",
        )

    command = "npm ci"
    code, runtime_info = _run_install(
        target,
        command,
        runtime_manager=runtime_manager,
        runtime_strict=runtime_strict,
        timeout_seconds=timeout_seconds,
    )
    return DependencySetup(
        manager="npm",
        action="install",
        command=command,
        reason="npm installs the worktree's frozen dependency graph in isolation",
        node=runtime_info,
        exit_code=code,
    )
