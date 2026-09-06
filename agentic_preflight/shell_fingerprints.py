"""Declared content inputs for cross-commit shell evidence.

This captures a repository author's bounded dependency assumption; it cannot
discover arbitrary shell dependencies. No input contents appear in diagnostics.
"""

from __future__ import annotations

import hashlib
import os
import platform
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import gitx
from .digests import json_digest as config_digest
from .fingerprints import (
    FINGERPRINT_VERSION,
    Classification,
    Disposition,
    ReasonCode,
)
from .stages import command as command_plan


class ShellInputContract(BaseModel):
    """Opt-in declaration that history, time and external services are not inputs.

    Files name exact repository-relative dependency/toolchain inputs, including
    ignored or copied files. Environment names identify every environment input.
    Toolchain paths name additional exact executable/library files. The primary
    executable is captured automatically. Shell profiles are unsupported.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["content"]
    files: list[str]
    environment: list[str]
    toolchain: list[str]

    @field_validator("toolchain")
    @classmethod
    def safe_toolchain(cls, values: list[str]) -> list[str]:
        if any(not Path(value).is_absolute() for value in values):
            raise ValueError("toolchain files must use absolute paths")
        if len(values) != len(set(values)):
            raise ValueError("toolchain paths must be unique")
        return sorted(values)

    @field_validator("files")
    @classmethod
    def safe_files(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or value != path.as_posix()
                or path.is_absolute()
                or ".." in path.parts
                or ".git" in path.parts
                or "\\" in value
                or ":" in value
                or any(char in value for char in "*?[]")
                or path == PurePosixPath(".")
            ):
                raise ValueError("input files must be exact repository-relative paths")
        if len(values) != len(set(values)):
            raise ValueError("input file paths must be unique")
        return sorted(values)

    @field_validator("environment")
    @classmethod
    def safe_environment(cls, values: list[str]) -> list[str]:
        if any(not value or "=" in value or "\x00" in value for value in values):
            raise ValueError("invalid environment name")
        if len(values) != len(set(values)):
            raise ValueError("environment names must be unique")
        return sorted(values)


class ShellFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = FINGERPRINT_VERSION
    base_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unavailable: ReasonCode | None = None


def _file_input(root: Path, name: str) -> dict[str, str | int | bool]:
    path = root / name
    # A symlink can escape the checkout or change without its referent changing.
    # Refuse all symlink components, including dangling links, rather than follow.
    for component in [path, *path.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("symlink input")
    if not path.exists():
        return {"exists": False}
    before = path.stat()
    if not path.is_file():
        raise ValueError("non-file input")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    after = path.stat()
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ValueError("input changed during capture")
    return {"exists": True, "mode": before.st_mode, "sha256": digest}


def compute_shell_fingerprint(
    repo: Path | str,
    *,
    base_sha: str,
    head_sha: str,
    command: str,
    contract: ShellInputContract | None,
    execution_config: dict,
    copied_files: list[str],
    environment: Mapping[str, str] | None = None,
) -> ShellFingerprint:
    """Capture inputs without executing the command or exposing input values.

    Call before and after execution. Only matching, available captures can be
    recorded as reusable evidence. ``execution_config`` is the stage's resolved
    timeout, retry, setup and worktree policy, not unrelated stage commands.
    """
    root = Path(repo).resolve()
    record = ShellFingerprint(
        base_tree_sha=gitx.tree_sha(root, base_sha),
        head_tree_sha=gitx.tree_sha(root, head_sha),
        command_sha256=hashlib.sha256(command.encode()).hexdigest(),
        config_sha256=config_digest(
            {
                "execution": execution_config,
                "contract": contract.model_dump(mode="json") if contract else None,
            }
        ),
    )
    if contract is None:
        return record.model_copy(update={"unavailable": ReasonCode.CONTRACT_UNDECLARED})
    if not set(copied_files) <= set(contract.files):
        return record.model_copy(update={"unavailable": ReasonCode.INPUTS_UNAVAILABLE})
    try:
        if gitx.rev_parse(root, "HEAD") != gitx.rev_parse(root, head_sha) or not gitx.is_clean(
            root
        ):
            raise ValueError("checkout does not match the execution subject")
        files = {name: _file_input(root, name) for name in contract.files}
        plan = command_plan.plan(command, cwd=root)
        if plan.uses_shell:
            raise ValueError("login shell inputs are outside the supported contract")
        toolchain = {}
        for name in sorted({plan.argv[0], *contract.toolchain}):
            resolved = Path(name).resolve(strict=True)
            toolchain[name] = {
                "resolved": str(resolved),
                **_file_input(resolved.parent, resolved.name),
            }
    except (OSError, ValueError):
        return record.model_copy(update={"unavailable": ReasonCode.INPUTS_UNAVAILABLE})
    env = os.environ if environment is None else environment
    inputs = {
        "files": files,
        "toolchain": toolchain,
        # Distinguish absent from present-but-empty. Publish only the combined
        # digest, never individual environment values or their individual hashes.
        "environment": {name: env.get(name) for name in contract.environment},
        "platform": [platform.system(), platform.release(), platform.machine()],
    }
    return record.model_copy(update={"inputs_sha256": config_digest(inputs)})


def classify_shell(old: ShellFingerprint | None, new: ShellFingerprint) -> Classification:
    if old is None:
        return Classification(
            disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINGERPRINT_MISSING,)
        )
    if old.version != FINGERPRINT_VERSION or new.version != FINGERPRINT_VERSION:
        return Classification(
            disposition=Disposition.UNKNOWN,
            reasons=(ReasonCode.FINGERPRINT_VERSION_MISMATCH,),
        )
    unavailable = tuple(
        dict.fromkeys(reason for reason in (old.unavailable, new.unavailable) if reason)
    )
    if unavailable or old.inputs_sha256 is None or new.inputs_sha256 is None:
        return Classification(
            disposition=Disposition.UNKNOWN,
            reasons=unavailable or (ReasonCode.INPUTS_UNAVAILABLE,),
        )
    reasons = tuple(
        reason
        for name, reason in (
            ("base_tree_sha", ReasonCode.BASE_TREE_CHANGED),
            ("head_tree_sha", ReasonCode.HEAD_TREE_CHANGED),
            ("command_sha256", ReasonCode.COMMAND_CHANGED),
            ("config_sha256", ReasonCode.CONFIG_CHANGED),
            ("inputs_sha256", ReasonCode.INPUTS_CHANGED),
        )
        if getattr(old, name) != getattr(new, name)
    )
    return Classification(
        disposition=Disposition.INVALID if reasons else Disposition.REUSABLE, reasons=reasons
    )
