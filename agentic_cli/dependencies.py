"""Dependency setup for isolated Node validation worktrees.

Strict worktrees install from the lockfile every time. A reusable runner retains
``node_modules`` only while dependency inputs, runtime, platform, architecture,
package-manager version, and install command have the same fingerprint. Nothing
is linked from the user's main checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import runtime, worktree

DEPENDENCY_INPUTS = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    ".npmrc",
    ".nvmrc",
    ".node-version",
    ".tool-versions",
    ".mise.toml",
    "mise.toml",
)


@dataclass(frozen=True)
class DependencySetup:
    manager: str
    action: str
    command: str | None = None
    reason: str | None = None
    node: dict | None = None
    exit_code: int | None = None
    fingerprint: str | None = None

    def as_dict(self) -> dict:
        return {
            "manager": self.manager,
            "action": self.action,
            "command": self.command,
            "reason": self.reason,
            "node": self.node,
            "exit_code": self.exit_code,
            "fingerprint": self.fingerprint,
        }


def _manager_version(
    target: Path, manager: str, *, runtime_manager: str, runtime_strict: bool
) -> str | None:
    prepared = runtime.prepare_command(
        target, f"{manager} --version", manager=runtime_manager, strict=runtime_strict
    )
    completed = subprocess.run(
        ["bash", "-lc", prepared.command],
        cwd=str(target),
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip().splitlines()[-1] if completed.returncode == 0 else None


def _dependency_fingerprint(
    target: Path,
    manager: str,
    command: str,
    *,
    runtime_manager: str,
    runtime_strict: bool,
) -> str:
    inputs = {}
    for name in DEPENDENCY_INPUTS:
        path = target / name
        inputs[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
    probe = runtime.probe_node(target, manager=runtime_manager, strict=runtime_strict)
    payload = {
        "schema": 1,
        "manager": manager,
        "manager_version": _manager_version(
            target,
            manager,
            runtime_manager=runtime_manager,
            runtime_strict=runtime_strict,
        ),
        "command": command,
        "configuration_environment": {
            name: value
            for name, value in os.environ.items()
            if name == "NODE_OPTIONS"
            or name.startswith("NPM_CONFIG_")
            or name.startswith("PNPM_")
        },
        "inputs": inputs,
        "node": probe.as_dict(),
        "platform": platform.system(),
        "architecture": platform.machine(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_cache_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cache_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    cache_state_path: Path | str | None = None,
    runtime_manager: str = "auto",
    runtime_strict: bool = True,
    timeout_seconds: int = 600,
) -> DependencySetup:
    """Prepare dependencies for one worktree without changing lockfiles."""
    target = Path(target_worktree)

    if (target / "pnpm-lock.yaml").is_file():
        manager = "pnpm"
        command = "pnpm install --frozen-lockfile"
        reason = "pnpm uses its shared content-addressable store"
    elif (target / "package-lock.json").is_file():
        manager = "npm"
        command = "npm ci"
        reason = "npm installs the worktree's frozen dependency graph in isolation"
    else:
        return DependencySetup(
            manager="none",
            action="skip",
            reason="no pnpm-lock.yaml or package-lock.json",
        )

    cache_path = Path(cache_state_path) if cache_state_path else None
    fingerprint = None
    if cache_path is not None:
        fingerprint = _dependency_fingerprint(
            target,
            manager,
            command,
            runtime_manager=runtime_manager,
            runtime_strict=runtime_strict,
        )
        cached = _load_cache_state(cache_path)
        modules = target / "node_modules"
        if (
            cached.get("fingerprint") == fingerprint
            and cached.get("manager") == manager
            and modules.is_dir()
            and not modules.is_symlink()
        ):
            return DependencySetup(
                manager=manager,
                action="reuse",
                reason="dependency inputs and runtime fingerprint match the reusable runner",
                node=cached.get("node"),
                exit_code=0,
                fingerprint=fingerprint,
            )
        cache_path.unlink(missing_ok=True)

    code, runtime_info = _run_install(
        target,
        command,
        runtime_manager=runtime_manager,
        runtime_strict=runtime_strict,
        timeout_seconds=timeout_seconds,
    )
    if cache_path is not None and code == 0:
        _write_cache_state(
            cache_path,
            {
                "manager": manager,
                "fingerprint": fingerprint,
                "node": runtime_info,
            },
        )
    return DependencySetup(
        manager=manager,
        action="install",
        command=command,
        reason=reason,
        node=runtime_info,
        exit_code=code,
        fingerprint=fingerprint,
    )
