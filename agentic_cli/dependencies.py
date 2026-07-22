"""Dependency setup for disposable Node worktrees.

pnpm already shares package contents through its content-addressable store, so
each worktree gets its own lockfile-specific layout. npm needs a narrower
optimization: reuse the main checkout's ``node_modules`` only when the
dependency inputs still match the run's base and the activated Node ABI matches
the committed Node requirement. Otherwise ``npm ci`` tests the branch's real
dependency graph in isolation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitx, runtime, worktree

NPM_INPUTS = ("package.json", "package-lock.json", ".npmrc")


@dataclass(frozen=True)
class DependencySetup:
    manager: str
    action: str
    command: str | None = None
    reason: str | None = None
    source: str | None = None
    node: dict | None = None
    exit_code: int | None = None

    def as_dict(self) -> dict:
        return {
            "manager": self.manager,
            "action": self.action,
            "command": self.command,
            "reason": self.reason,
            "source": self.source,
            "node": self.node,
            "exit_code": self.exit_code,
        }


def _inputs_match(source: Path, target: Path, names: tuple[str, ...]) -> bool:
    """Treat missing files as inputs too: present-vs-absent is a mismatch."""
    for name in names:
        left = source / name
        right = target / name
        if left.is_file() != right.is_file():
            return False
        if left.is_file() and left.read_bytes() != right.read_bytes():
            return False
    return True


def _changed_from_base(target: Path, base_sha: str, names: tuple[str, ...]) -> bool:
    return bool(
        gitx.out(target, "diff", "--name-only", base_sha, "HEAD", "--", *names)
    )


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
    source_repo: Path | str,
    target_worktree: Path | str,
    *,
    base_sha: str,
    runtime_manager: str = "auto",
    runtime_strict: bool = True,
    timeout_seconds: int = 600,
) -> DependencySetup:
    """Prepare dependencies for one worktree without changing lockfiles."""
    source = Path(source_repo)
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

    source_modules = source / "node_modules"
    inputs_match = _inputs_match(source, target, NPM_INPUTS)
    changed_from_base = _changed_from_base(target, base_sha, NPM_INPUTS)
    probe = runtime.probe_node(
        target, manager=runtime_manager, strict=runtime_strict
    )
    expected_major = runtime.expected_node_major(probe.runtime)
    abi_matches = (
        probe.available
        and expected_major is not None
        and probe.major == expected_major
    )

    if (
        source_modules.is_dir()
        and inputs_match
        and not changed_from_base
        and abi_matches
    ):
        destination = target / "node_modules"
        if destination.exists() or destination.is_symlink():
            raise worktree.WorktreeError(
                f"cannot share dependencies: {destination} already exists"
            )
        os.symlink(source_modules.resolve(), destination, target_is_directory=True)
        return DependencySetup(
            manager="npm",
            action="share",
            reason="dependency inputs match the base and activated Node ABI",
            source=str(source_modules.resolve()),
            node=probe.as_dict(),
            exit_code=0,
        )

    reasons = []
    if not source_modules.is_dir():
        reasons.append("main checkout has no node_modules")
    if not inputs_match:
        reasons.append("main checkout dependency inputs differ")
    if changed_from_base:
        reasons.append("dependency inputs changed from the run base")
    if not abi_matches:
        reasons.append("activated Node version does not match the committed requirement")

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
        reason="; ".join(reasons),
        node={**probe.as_dict(), "runtime": runtime_info},
        exit_code=code,
    )
