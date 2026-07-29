"""Project runtime discovery and non-interactive shell activation.

Agent stages do not inherit interactive-shell version-manager shims. This
module turns committed runtime pins into an explicit command wrapper so a run
uses the same runtime in an agent, a developer shell, and CI.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeInfo:
    manager: str
    pin_file: str | None = None
    requested: str | None = None
    available: bool = True
    node_project: bool = False
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "manager": self.manager,
            "pin_file": self.pin_file,
            "requested": self.requested,
            "available": self.available,
            "node_project": self.node_project,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PreparedCommand:
    command: str
    runtime: RuntimeInfo


@dataclass(frozen=True)
class NodeProbe:
    available: bool
    version: str | None
    major: int | None
    modules_abi: str | None
    runtime: RuntimeInfo
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "version": self.version,
            "major": self.major,
            "modules_abi": self.modules_abi,
            "runtime": self.runtime.as_dict(),
            "reason": self.reason,
        }


def _package_json(repo: Path) -> dict:
    path = repo / "package.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_line(path: Path) -> str | None:
    try:
        return next(
            (line.strip() for line in path.read_text().splitlines() if line.strip()),
            None,
        )
    except OSError:
        return None


def _tool_version(path: Path, tool: str) -> str | None:
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == tool:
                return parts[1]
    except OSError:
        pass
    return None


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidates = {
        "volta": [Path(os.environ.get("VOLTA_HOME", Path.home() / ".volta")) / "bin/volta"],
        "asdf": [Path(os.environ.get("ASDF_DATA_DIR", Path.home() / ".asdf")) / "bin/asdf"],
        "mise": [Path.home() / ".local/bin/mise"],
        "fnm": [Path.home() / ".local/share/fnm/fnm"],
        "nodenv": [Path.home() / ".nodenv/bin/nodenv"],
    }.get(name, [])
    return next((str(path) for path in candidates if path.is_file()), None)


def inspect_project(repo: Path | str, manager: str = "auto") -> RuntimeInfo:
    repo = Path(repo)
    package = _package_json(repo)
    node_project = (repo / "package.json").is_file() or any(
        (repo / name).exists()
        for name in (
            ".nvmrc",
            ".node-version",
            ".tool-versions",
            ".mise.toml",
            "mise.toml",
        )
    )
    requested_manager = manager

    if requested_manager == "none":
        return RuntimeInfo("none", node_project=node_project)

    volta_node = (
        (package.get("volta") or {}).get("node") if isinstance(package.get("volta"), dict) else None
    )
    engines_node = (
        (package.get("engines") or {}).get("node")
        if isinstance(package.get("engines"), dict)
        else None
    )

    detected = "none"
    pin_file = None
    requested = None
    if volta_node:
        detected, pin_file, requested = "volta", "package.json#volta.node", str(volta_node)
    elif (repo / ".mise.toml").is_file() or (repo / "mise.toml").is_file():
        detected, pin_file = (
            "mise",
            ".mise.toml" if (repo / ".mise.toml").is_file() else "mise.toml",
        )
    elif (repo / ".tool-versions").is_file() and _tool_version(repo / ".tool-versions", "nodejs"):
        detected, pin_file, requested = (
            "asdf",
            ".tool-versions",
            _tool_version(repo / ".tool-versions", "nodejs"),
        )
    elif (repo / ".nvmrc").is_file():
        detected, pin_file, requested = "nvm", ".nvmrc", _first_line(repo / ".nvmrc")
    elif (repo / ".node-version").is_file():
        pin_file, requested = ".node-version", _first_line(repo / ".node-version")
        detected = next(
            (name for name in ("mise", "fnm", "nodenv", "asdf") if _binary(name)),
            "none",
        )

    if requested_manager != "auto":
        detected = requested_manager

    if detected == "none":
        reason = None
        if node_project and not pin_file:
            reason = (
                f"Node is not pinned (package.json engines.node is {engines_node!r})"
                if engines_node
                else "Node is not pinned"
            )
        elif pin_file:
            reason = f"{pin_file} exists, but no supported version manager was found"
        return RuntimeInfo(
            detected,
            pin_file,
            requested or engines_node,
            detected == "none" and not pin_file,
            node_project,
            reason,
        )

    available = _nvm_script() is not None if detected == "nvm" else _binary(detected) is not None
    reason = (
        None
        if available
        else f"{detected} is required by the runtime configuration but was not found"
    )
    return RuntimeInfo(detected, pin_file, requested, available, node_project, reason)


def _nvm_script() -> Path | None:
    root = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm"))
    script = root / "nvm.sh"
    return script if script.is_file() else None


def prepare_command(
    repo: Path | str,
    command: str,
    *,
    manager: str = "auto",
    strict: bool = True,
) -> PreparedCommand:
    """Wrap ``command`` with the manager implied by committed runtime pins."""
    info = inspect_project(repo, manager)
    if info.manager == "none" or not info.node_project:
        if strict and info.pin_file and not info.available:
            message = shlex.quote(f"agentic-preflight: {info.reason}")
            return PreparedCommand(f"echo {message} >&2; exit 127", info)
        return PreparedCommand(command, info)

    quoted = shlex.quote(command)
    if info.manager == "nvm":
        script = _nvm_script()
        if script:
            wrapped = f". {shlex.quote(str(script))} && nvm use --silent && bash -c {quoted}"
        else:
            wrapped = f"echo {shlex.quote('agentic-preflight: nvm was not found')} >&2; exit 127"
    else:
        binary = _binary(info.manager)
        if binary and info.manager in {"volta", "asdf"}:
            subcommand = "run --" if info.manager == "volta" else "exec"
            wrapped = f"{shlex.quote(binary)} {subcommand} bash -c {quoted}"
        elif binary and info.manager == "mise":
            wrapped = f"{shlex.quote(binary)} exec -- bash -c {quoted}"
        elif binary and info.manager == "fnm":
            version = shlex.quote(info.requested or "")
            wrapped = f"{shlex.quote(binary)} exec --using={version} -- bash -c {quoted}"
        elif binary and info.manager == "nodenv":
            wrapped = f"{shlex.quote(binary)} exec bash -c {quoted}"
        else:
            wrapped = f"echo {shlex.quote(f'agentic-preflight: {info.reason}')} >&2; exit 127"

    if not info.available and not strict:
        wrapped = command
    return PreparedCommand(wrapped, info)


def expected_node_major(info: RuntimeInfo) -> int | None:
    """Extract the first requested major from a pin or a one-major engine range."""
    if not info.requested:
        return None
    match = re.search(r"(?:^|[^0-9])(\d{1,3})(?:\.|\b)", info.requested)
    return int(match.group(1)) if match else None


def probe_node(
    repo: Path | str,
    *,
    manager: str = "auto",
    strict: bool = True,
) -> NodeProbe:
    """Read the activated Node version and module ABI without running project code."""
    command = (
        'node -p "JSON.stringify({version: process.versions.node, '
        'modules: process.versions.modules})"'
    )
    prepared = prepare_command(repo, command, manager=manager, strict=strict)
    result = subprocess.run(
        ["bash", "-lc", prepared.command],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        reason = result.stderr.strip() or result.stdout.strip() or "node is unavailable"
        return NodeProbe(False, None, None, None, prepared.runtime, reason)

    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("version"):
            continue
        version = str(payload["version"])
        try:
            major = int(version.split(".", 1)[0])
        except ValueError:
            major = None
        return NodeProbe(
            True,
            version,
            major,
            str(payload.get("modules")) if payload.get("modules") else None,
            prepared.runtime,
        )
    return NodeProbe(
        False,
        None,
        None,
        None,
        prepared.runtime,
        "node returned no version information",
    )
