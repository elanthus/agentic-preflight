"""Install the bundled Agent Skill into supported host agents.

Python package installers own an isolated environment; host agents discover
skills in their own user or project directories.  This module bridges those
two locations explicitly, without install-time side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .envelope import ExitCode
from .errors import AgenticError

SKILL_NAME = "agentic-preflight"
INSTALL_METADATA = ".agentic-preflight-install.json"
INSTALL_SCHEMA = 1


@dataclass(frozen=True)
class IntegrationSpec:
    """Discovery roots for one supported host agent."""

    user_parts: tuple[str, ...]
    project_parts: tuple[str, ...]


SUPPORTED_INTEGRATIONS: dict[str, IntegrationSpec] = {
    "codex": IntegrationSpec((".agents", "skills"), (".agents", "skills")),
    "claude": IntegrationSpec((".claude", "skills"), (".claude", "skills")),
    "cursor": IntegrationSpec((".cursor", "skills"), (".cursor", "skills")),
    "opencode": IntegrationSpec(
        (".config", "opencode", "skills"), (".opencode", "skills")
    ),
    "amp": IntegrationSpec((".config", "agents", "skills"), (".agents", "skills")),
}


@dataclass(frozen=True)
class InstallTarget:
    integration: str
    path: Path


class IntegrationOperation(StrEnum):
    """The four public lifecycle operations over an integration target."""

    INSTALL = "install"
    STATUS = "status"
    UPDATE = "update"
    UNINSTALL = "uninstall"


@dataclass(frozen=True)
class OperationSpec:
    actions: dict[str, str]
    result_status: str | None
    conflict_verb: str


OPERATION_SPECS = {
    IntegrationOperation.INSTALL: OperationSpec(
        actions={
            "missing": "installed",
            "current": "unchanged",
            "outdated": "updated",
            "modified": "replaced",
            "unmanaged": "replaced",
        },
        result_status="current",
        conflict_verb="overwrite",
    ),
    IntegrationOperation.UPDATE: OperationSpec(
        actions={
            "missing": "skipped_missing",
            "current": "unchanged",
            "outdated": "updated",
            "modified": "replaced",
            "unmanaged": "replaced",
        },
        result_status="current",
        conflict_verb="overwrite",
    ),
    IntegrationOperation.UNINSTALL: OperationSpec(
        actions={
            "missing": "missing",
            "current": "removed",
            "outdated": "removed",
            "modified": "removed",
            "unmanaged": "removed",
        },
        result_status="missing",
        conflict_verb="remove",
    ),
}


class IntegrationError(AgenticError):
    code = "integration_error"
    exit_code = ExitCode.PRECONDITION


class IntegrationConflict(IntegrationError):
    code = "integration_conflict"


def package_version() -> str:
    try:
        return version("agentic-preflight")
    except PackageNotFoundError:
        return "0+unknown"


def bundled_skill_dir() -> Path:
    """Find the force-included wheel resource or the source-checkout fallback."""
    candidates = (
        Path(__file__).parent / "_bundled_skill",
        Path(__file__).parent.parent / "skill",
    )
    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise IntegrationError(
        "the agentic-preflight skill bundle is missing from this installation",
        next_instruction="Reinstall agentic-preflight, then retry the integration command.",
    )


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def resolve_targets(
    agents: Iterable[str],
    *,
    scope: str,
    custom_roots: Iterable[Path] = (),
    home: Path | None = None,
    project_root: Path | None = None,
) -> list[InstallTarget]:
    """Resolve agent names and custom skill roots to final skill directories."""
    if scope not in {"user", "project"}:
        raise IntegrationError("scope must be either 'user' or 'project'")

    home = _absolute(home or Path.home())
    if scope == "project":
        if project_root is None:
            raise IntegrationError("project scope requires a repository root")
        project_root = _absolute(project_root)
    targets: list[InstallTarget] = []
    seen_targets: set[tuple[str, Path]] = set()
    for agent in agents:
        spec = SUPPORTED_INTEGRATIONS.get(agent)
        if spec is None:
            valid = ", ".join(sorted(SUPPORTED_INTEGRATIONS))
            raise IntegrationError(f"unsupported integration {agent!r}; choose from {valid}")
        if scope == "user":
            root = home.joinpath(*spec.user_parts)
        else:
            if project_root is None:
                raise IntegrationError("project scope requires a repository root")
            root = project_root.joinpath(*spec.project_parts)
        destination = _absolute(root / SKILL_NAME)
        key = (agent, destination)
        if key not in seen_targets:
            targets.append(InstallTarget(agent, destination))
            seen_targets.add(key)

    for custom_root in custom_roots:
        root = _absolute(custom_root)
        destination = _absolute(root / SKILL_NAME)
        key = ("custom", destination)
        if key not in seen_targets:
            targets.append(InstallTarget("custom", destination))
            seen_targets.add(key)
    return targets


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _skill_hash(directory: Path) -> str:
    """Hash paths and contents without following user-created symlinks."""
    digest = hashlib.sha256()
    paths = sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix())
    for path in paths:
        relative = path.relative_to(directory).as_posix()
        if relative == INSTALL_METADATA:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_dir():
            digest.update(b"dir\0")
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"other\0")
        digest.update(b"\0")
    return digest.hexdigest()


def _read_install_metadata(destination: Path) -> dict | None:
    metadata_path = destination / INSTALL_METADATA
    if metadata_path.is_symlink():
        return None
    try:
        payload = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != INSTALL_SCHEMA:
        return None
    if payload.get("installed_by") != "agentic-preflight":
        return None
    if not isinstance(payload.get("content_sha256"), str):
        return None
    if not isinstance(payload.get("package_version"), str):
        return None
    return payload


def inspect_target(
    target: InstallTarget,
    *,
    source_dir: Path | None = None,
    source_version: str | None = None,
) -> dict:
    source_dir = source_dir or bundled_skill_dir()
    source_version = source_version or package_version()
    current_hash = _skill_hash(source_dir)
    base = {
        "integration": target.integration,
        "path": str(target.path),
        "source_version": source_version,
    }

    if not _path_exists(target.path):
        return {**base, "status": "missing", "managed": False, "installed_version": None}
    if target.path.is_symlink() or not target.path.is_dir():
        return {**base, "status": "unmanaged", "managed": False, "installed_version": None}

    metadata = _read_install_metadata(target.path)
    if metadata is None:
        return {**base, "status": "unmanaged", "managed": False, "installed_version": None}

    try:
        actual_hash = _skill_hash(target.path)
    except OSError as exc:
        raise IntegrationError(f"could not inspect the skill at {target.path}: {exc}") from exc
    installed_hash = metadata["content_sha256"]
    installed_version = metadata.get("package_version")
    if actual_hash != installed_hash:
        state = "modified"
    elif installed_hash != current_hash:
        state = "outdated"
    else:
        state = "current"
    return {
        **base,
        "status": state,
        "managed": True,
        "installed_version": installed_version,
    }


def integration_status(
    agents: Iterable[str],
    *,
    scope: str = "user",
    custom_roots: Iterable[Path] = (),
    home: Path | None = None,
    project_root: Path | None = None,
    source_dir: Path | None = None,
    source_version: str | None = None,
) -> list[dict]:
    source_dir = source_dir or bundled_skill_dir()
    source_version = source_version or package_version()
    targets = resolve_targets(
        agents,
        scope=scope,
        custom_roots=custom_roots,
        home=home,
        project_root=project_root,
    )
    return [
        inspect_target(target, source_dir=source_dir, source_version=source_version)
        for target in targets
    ]


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _write_install_metadata(destination: Path, source_hash: str, source_version: str) -> None:
    payload = {
        "schema": INSTALL_SCHEMA,
        "installed_by": "agentic-preflight",
        "package_version": source_version,
        "content_sha256": source_hash,
    }
    (destination / INSTALL_METADATA).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def _replace_target(
    destination: Path,
    *,
    source_dir: Path,
    source_hash: str,
    source_version: str,
) -> None:
    parent = destination.parent
    temp_root: Path | None = None
    staged: Path | None = None
    backup = parent / f".{SKILL_NAME}.backup-{uuid.uuid4().hex}"
    moved_existing = False
    try:
        parent.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.install-", dir=parent))
        staged = temp_root / SKILL_NAME
        shutil.copytree(source_dir, staged)
        _write_install_metadata(staged, source_hash, source_version)
        if _path_exists(destination):
            os.replace(destination, backup)
            moved_existing = True
        try:
            os.replace(staged, destination)
        except OSError:
            if moved_existing and _path_exists(backup) and not _path_exists(destination):
                os.replace(backup, destination)
                moved_existing = False
            raise
        if moved_existing:
            _remove_path(backup)
            moved_existing = False
    except OSError as exc:
        raise IntegrationError(f"could not install the skill at {destination}: {exc}") from exc
    finally:
        try:
            if staged is not None and _path_exists(staged):
                _remove_path(staged)
            if temp_root is not None and temp_root.exists():
                _remove_path(temp_root)
        except OSError:
            # The destination is already installed or rolled back. A leftover
            # hidden staging directory must not turn that result into a traceback.
            pass


def _conflicts(reports: list[dict], *, force: bool) -> list[dict]:
    if force:
        return []
    return [report for report in reports if report["status"] in {"modified", "unmanaged"}]


def manage_integrations(
    operation: IntegrationOperation | str,
    agents: Iterable[str],
    *,
    scope: str = "user",
    custom_roots: Iterable[Path] = (),
    force: bool = False,
    home: Path | None = None,
    project_root: Path | None = None,
    source_dir: Path | None = None,
    source_version: str | None = None,
) -> list[dict]:
    """Inspect or apply one lifecycle operation to all resolved targets."""
    operation = IntegrationOperation(operation)
    source_dir = source_dir or bundled_skill_dir()
    source_version = source_version or package_version()
    targets = resolve_targets(
        agents,
        scope=scope,
        custom_roots=custom_roots,
        home=home,
        project_root=project_root,
    )
    reports = [
        inspect_target(target, source_dir=source_dir, source_version=source_version)
        for target in targets
    ]
    if operation is IntegrationOperation.STATUS:
        return reports

    spec = OPERATION_SPECS[operation]
    conflicts = _conflicts(reports, force=force)
    if conflicts:
        paths = ", ".join(report["path"] for report in conflicts)
        raise IntegrationConflict(
            f"refusing to {spec.conflict_verb} an unmanaged or modified skill: {paths}",
            data={"conflicts": conflicts},
            next_instruction=(
                "Inspect those skill directories, or rerun with --force to "
                f"{spec.conflict_verb} them."
            ),
        )

    source_hash = _skill_hash(source_dir)
    results: list[dict] = []
    for target, report in zip(targets, reports, strict=True):
        previous = report["status"]
        action = spec.actions[previous]
        if action in {"installed", "updated", "replaced"}:
            _replace_target(
                target.path,
                source_dir=source_dir,
                source_hash=source_hash,
                source_version=source_version,
            )
        elif action == "removed":
            try:
                _remove_path(target.path)
            except OSError as exc:
                raise IntegrationError(
                    f"could not remove the skill at {target.path}: {exc}"
                ) from exc

        resulting_status = previous if action == "skipped_missing" else spec.result_status
        results.append(
            {
                **report,
                "previous_status": previous,
                "status": resulting_status,
                "managed": resulting_status == "current",
                "installed_version": (
                    source_version if resulting_status == "current" else report["installed_version"]
                ),
                "action": action,
            }
        )
    return results


def install_integrations(
    agents: Iterable[str],
    *,
    scope: str = "user",
    custom_roots: Iterable[Path] = (),
    force: bool = False,
    update_only: bool = False,
    home: Path | None = None,
    project_root: Path | None = None,
    source_dir: Path | None = None,
    source_version: str | None = None,
) -> list[dict]:
    """Compatibility API for install and update callers."""
    return manage_integrations(
        IntegrationOperation.UPDATE if update_only else IntegrationOperation.INSTALL,
        agents,
        scope=scope,
        custom_roots=custom_roots,
        force=force,
        home=home,
        project_root=project_root,
        source_dir=source_dir,
        source_version=source_version,
    )


def uninstall_integrations(
    agents: Iterable[str],
    **kwargs,
) -> list[dict]:
    """Compatibility API for uninstall callers."""
    return manage_integrations(IntegrationOperation.UNINSTALL, agents, **kwargs)
