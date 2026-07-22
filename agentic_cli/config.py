"""``.agentic-cli.toml`` loading.

Repo config (committed, at the repo root) layers over user config
(``~/.config/agentic-cli/config.toml``). Merging is per *section*, one level
deep: a section present in the repo file replaces the user's section wholesale
rather than merging key-by-key, so a reader of the committed file can tell what
is in force without knowing the reader's home directory.

Unknown keys are errors that name the key. A silently ignored typo in a config
that governs a *safety gate* is exactly the kind of quiet failure this tool
exists to prevent.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_CONFIG_NAME = ".agentic-cli.toml"
USER_CONFIG_NAME = "config.toml"

from .diff import DEFAULT_EXCLUDE  # noqa: E402  (kept next to its one consumer)


class ConfigError(Exception):
    """Configuration is malformed, unknown, or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralSection(_Section):
    base_ref: str = "main"


class CommandsSection(_Section):
    lint: str | None = None
    test: str | None = None


class StageSection(_Section):
    timeout_seconds: int = Field(default=600, ge=1)
    max_attempts: int = Field(default=5, ge=1)


class ReviewSection(_Section):
    blocking_severities: list[str] = Field(default_factory=lambda: ["critical", "high"])
    max_findings: int = Field(default=50, ge=1)
    require_fix_commits: bool = True


class DocsSection(_Section):
    enabled: bool = True
    paths: list[str] = Field(default_factory=list)
    require_changelog: bool = False
    blocking_severities: list[str] = Field(default_factory=lambda: ["critical", "high"])


class DiffSection(_Section):
    """The budget tripwire. Over ``max_bytes``, `context` refuses rather than
    truncating; ``exclude`` is the intended remedy and ships pre-loaded with the
    usual generated-file noise."""

    max_bytes: int = Field(default=200_000, ge=1)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))


class WorktreeSection(_Section):
    ttl_hours: int = Field(default=48, ge=1)
    root: str | None = None
    copy_files: list[str] = Field(default_factory=lambda: [".env"])
    setup_command: str | None = None


class RuntimeSection(_Section):
    manager: str = "auto"
    strict: bool = True


class GateSection(_Section):
    mode: str = "token"


class HookSection(_Section):
    enabled: bool = True
    allow_force_push: bool = False


class PublishSection(_Section):
    provider: str = "auto"
    draft_pr: bool = False
    pr_title: str | None = None


VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_GATE_MODES = {"token", "manual"}
VALID_RUNTIME_MANAGERS = {
    "auto",
    "none",
    "nvm",
    "volta",
    "asdf",
    "mise",
    "fnm",
    "nodenv",
}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralSection = Field(default_factory=GeneralSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
    stage: StageSection = Field(default_factory=StageSection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    docs: DocsSection = Field(default_factory=DocsSection)
    diff: DiffSection = Field(default_factory=DiffSection)
    worktree: WorktreeSection = Field(default_factory=WorktreeSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    gate: GateSection = Field(default_factory=GateSection)
    hook: HookSection = Field(default_factory=HookSection)
    publish: PublishSection = Field(default_factory=PublishSection)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _validate_enums(cfg: Config) -> None:
    """Checks pydantic cannot express as cleanly, phrased to name the offender."""
    for section, values in (
        ("review", cfg.review.blocking_severities),
        ("docs", cfg.docs.blocking_severities),
    ):
        for severity in values:
            if severity not in VALID_SEVERITIES:
                raise ConfigError(
                    f"[{section}] blocking_severities: unknown severity {severity!r}; "
                    f"valid values are {sorted(VALID_SEVERITIES)}"
                )
    if cfg.gate.mode not in VALID_GATE_MODES:
        raise ConfigError(
            f"[gate] mode: unknown mode {cfg.gate.mode!r}; "
            f"valid values are {sorted(VALID_GATE_MODES)}"
        )
    if cfg.runtime.manager not in VALID_RUNTIME_MANAGERS:
        raise ConfigError(
            f"[runtime] manager: unknown manager {cfg.runtime.manager!r}; "
            f"valid values are {sorted(VALID_RUNTIME_MANAGERS)}"
        )


def _describe(exc: ValidationError, source: Path) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        if error["type"] == "extra_forbidden":
            parts.append(f"unknown key {location!r}")
        else:
            parts.append(f"{location}: {error['msg']}")
    return f"invalid configuration in {source}: " + "; ".join(parts)


def load_config(
    repo_root: Path | str,
    *,
    user_config_dir: Path | str | None = None,
) -> Config:
    repo_root = Path(repo_root)
    if user_config_dir is None:
        user_config_dir = Path.home() / ".config" / "agentic-cli"
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
        top = str(exc.errors()[0]["loc"][0]) if exc.errors() else ""
        raise ConfigError(_describe(exc, sources.get(top, repo_file))) from exc

    _validate_enums(cfg)
    return cfg
