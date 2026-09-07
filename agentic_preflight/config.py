"""``.agentic-preflight.toml`` loading.

Repo config (committed, at the repo root) layers over user config
(``~/.config/agentic-preflight/config.toml``). Merging is per *section*, one level
deep: a section present in the repo file replaces the user's section wholesale
rather than merging key-by-key, so a reader of the committed file can tell what
is in force without knowing the reader's home directory.

Unknown keys are errors that name the key. A silently ignored typo in a config
that governs a *safety gate* is exactly the kind of quiet failure this tool
exists to prevent.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from .ci_models import CISection
from .config_compatibility import compatible_snapshot
from .digests import json_digest
from .shell_fingerprints import ShellInputContract

REPO_CONFIG_NAME = ".agentic-preflight.toml"
USER_CONFIG_NAME = "config.toml"

from .diff import DEFAULT_EXCLUDE  # noqa: E402  (kept next to its one consumer)


class ConfigError(Exception):
    """Configuration is malformed, unknown, or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


SeverityName = Literal["critical", "high", "medium", "low"]
RiskName = Literal["low", "medium", "high"]


def _default_blocking_severities() -> list[SeverityName]:
    return ["critical", "high"]


def _repo_relative_pattern(value: str) -> str:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or PureWindowsPath(value).drive
        or ".." in normalized.split("/")
    ):
        raise ValueError("patterns must be non-empty, repo-relative, and may not contain '..'")
    # Normalize only for validation; preserve accepted snapshot bytes and glob semantics.
    return value


RepoRelativePattern = Annotated[str, AfterValidator(_repo_relative_pattern)]


class GeneralSection(_Section):
    base_ref: str = "main"


class CommandsSection(_Section):
    lint: str | None = None
    test: str | None = None


class StageSection(_Section):
    timeout_seconds: int = Field(default=600, ge=1)
    max_attempts: int = Field(default=5, ge=1)


class ReuseSection(_Section):
    attestation_schema: Literal[4, 5] = 4
    lint: ShellInputContract | None = None
    test: ShellInputContract | None = None


class ReviewSection(_Section):
    blocking_severities: list[SeverityName] = Field(default_factory=_default_blocking_severities)
    max_findings: int = Field(default=50, ge=1)
    require_fix_commits: bool = True
    executor: Literal["in_harness", "command"] = "in_harness"
    command: str | None = None
    require_command_for: list[RiskName] = Field(default_factory=list)


class PolicySection(_Section):
    """Deterministic risk rules layered underneath the agent's findings."""

    human_review_paths: list[RepoRelativePattern] = Field(default_factory=list)
    high_risk_paths: list[RepoRelativePattern] = Field(default_factory=list)
    medium_risk_paths: list[RepoRelativePattern] = Field(default_factory=list)


class DocsSection(_Section):
    enabled: bool = True
    paths: list[str] = Field(default_factory=list)
    require_changelog: bool = False
    blocking_severities: list[SeverityName] = Field(default_factory=_default_blocking_severities)


class ContextSection(_Section):
    """Deterministic repository context delivered beside the review diff."""

    enabled: bool = True
    max_bytes: int = Field(default=24_000, ge=1)
    entry_max_bytes: int = Field(default=4_000, ge=1)
    extra_paths: list[RepoRelativePattern] = Field(default_factory=list)


class DiffSection(_Section):
    """The budget tripwire. Over ``max_bytes``, `context` refuses rather than
    truncating; ``exclude`` is the intended remedy and ships pre-loaded with the
    usual generated-file noise."""

    max_bytes: int = Field(default=200_000, ge=1)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))


class WorktreeSection(_Section):
    ttl_hours: int = Field(default=48, ge=1)
    root: str | None = None
    mode: Literal["in_place", "reusable", "strict"] = "in_place"
    copy_files: list[str] = Field(default_factory=lambda: [".env"])
    setup_command: str | None = None


class GateSection(_Section):
    mode: Literal["token", "manual"] = "token"


class PRSection(_Section):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["auto", "manual"] = "auto"
    automated_cleanup: bool = Field(default=False, alias="automatedCleanup")


class ApprovalSection(_Section):
    mode: Literal["manual_merge", "environment", "peer_review"] = "manual_merge"
    environment: str = "high-risk-review"

    @model_validator(mode="after")
    def nonempty_environment(self) -> ApprovalSection:
        if self.mode == "environment" and not self.environment.strip():
            raise ValueError("environment must not be empty")
        return self


class HookSection(_Section):
    enabled: bool = True
    allow_force_push: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralSection = Field(default_factory=GeneralSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
    stage: StageSection = Field(default_factory=StageSection)
    reuse: ReuseSection = Field(default_factory=ReuseSection)
    ci: CISection = Field(default_factory=CISection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    policy: PolicySection = Field(default_factory=PolicySection)
    docs: DocsSection = Field(default_factory=DocsSection)
    context: ContextSection = Field(default_factory=ContextSection)
    diff: DiffSection = Field(default_factory=DiffSection)
    worktree: WorktreeSection = Field(default_factory=WorktreeSection)
    gate: GateSection = Field(default_factory=GateSection)
    pr: PRSection = Field(default_factory=PRSection)
    approval: ApprovalSection = Field(default_factory=ApprovalSection)
    hook: HookSection = Field(default_factory=HookSection)

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler):
        return compatible_snapshot(handler(self), self.ci)


def config_digest(snapshot: dict[str, Any]) -> str:
    """Return the stable digest used to bind validation evidence to config."""
    return json_digest(snapshot)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _describe(exc: ValidationError, sources: dict[str, Path], default_source: Path) -> str:
    by_source: dict[Path, list[str]] = {}
    for error in exc.errors():
        top = str(error["loc"][0]) if error["loc"] else ""
        parts = by_source.setdefault(sources.get(top, default_source), [])
        location = ".".join(str(item) for item in error["loc"])
        if error["type"] == "extra_forbidden":
            parts.append(f"unknown key {location!r}")
        else:
            keys = error["loc"][1:]
            label = f"[{top}] {'.'.join(map(str, keys))}" if keys else top or "<root>"
            detail = error["msg"]
            if error["type"] == "literal_error":
                detail += f"; got {error['input']!r}"
            parts.append(f"{label}: {detail}")
    if not by_source:
        return f"invalid configuration in {default_source}: validation failed"
    return "\n".join(
        f"invalid configuration in {source}: " + "; ".join(parts)
        for source, parts in by_source.items()
    )


def load_config(
    repo_root: Path | str,
    *,
    user_config_dir: Path | str | None = None,
) -> Config:
    repo_root = Path(repo_root)
    if user_config_dir is None:
        user_config_dir = Path.home() / ".config" / "agentic-preflight"
    user_config_dir = Path(user_config_dir)

    merged: dict[str, Any] = {}
    sources: dict[str, Path] = {}

    user_file = user_config_dir / USER_CONFIG_NAME
    repo_file = repo_root / REPO_CONFIG_NAME
    for path in (user_file, repo_file):
        if not path.exists():
            continue
        for section, values in _read_toml(path).items():
            merged[section] = values
            sources[section] = path

    try:
        cfg = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, sources, repo_file)) from exc

    return cfg
