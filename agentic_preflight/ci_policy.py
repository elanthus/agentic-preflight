"""Read CI and mandatory local policy from committed protected-base data."""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import gitx
from .ci_models import TestDelegation
from .config import Config, ConfigError, _validate_enums
from .models import Attestation


def parse_policy(contents: str) -> Config:
    try:
        cfg = Config.model_validate(tomllib.loads(contents))
        _validate_enums(cfg)
    except (ValueError, ConfigError) as exc:
        raise ValueError(f"invalid protected policy: {exc}") from exc
    if cfg.ci.test_authority != "github_actions":
        raise ValueError("protected base has not enabled a schema-6 CI consumer")
    return cfg


def committed_policy(repo: Path | str, revision: str) -> Config:
    return parse_policy(gitx.out(repo, "show", f"{revision}:.agentic-preflight.toml"))


def base_enabled(repo: Path | str, revision: str) -> bool:
    """A producer-only rollout must still perform complete local validation."""
    try:
        committed_policy(repo, revision)
    except (ValueError, gitx.GitError):
        return False
    return True


def consumer_installed(repo: Path | str, revision: str) -> bool:
    result = gitx.run(repo, "show", f"{revision}:agentic_preflight/ci_models.py", check=False)
    if result.returncode == 0 and "\nclass TestDelegation(BaseModel):\n" in result.stdout:
        return True
    policy = gitx.run(repo, "show", f"{revision}:.agentic-preflight.toml", check=False)
    if policy.returncode:
        return False
    try:
        return tomllib.loads(policy.stdout).get("ci", {}).get("consumer_schema") == 6
    except (ValueError, AttributeError):
        return False


def enforce_local_policy(effective: Config, protected: Config, *, include_ci: bool = True) -> None:
    # Exact agreement is deliberately conservative in this initial opt-in path.
    # A feature branch cannot silently weaken any mandatory local stage.
    for section in ("ci", "review", "policy", "docs", "diff", "context", "approval"):
        if section == "ci" and not include_ci:
            continue
        if getattr(effective, section) != getattr(protected, section):
            raise ValueError(f"effective [{section}] differs from protected-base policy")
    if effective.commands.lint != protected.commands.lint:
        raise ValueError("effective lint command differs from protected-base policy")


def declaration(
    repo: Path | str, *, base: str, head: str, base_ref: str, effective: Config
) -> TestDelegation:
    protected = committed_policy(repo, base)
    enforce_local_policy(effective, protected)
    if base_ref not in {protected.ci.base_branch, f"origin/{protected.ci.base_branch}"}:
        raise ValueError("delegation base does not match the protected CI branch")
    # An uncommitted user override cannot opt in or supply a different authority.
    proposed = committed_policy(repo, head)
    if proposed.ci != protected.ci:
        raise ValueError("proposed CI declaration differs from the protected base")
    return TestDelegation(policy_revision=gitx.rev_parse(repo, base), policy=protected.ci)


def verify_declaration(repo: Path | str, value: Attestation) -> None:
    requested = value.test_delegation
    if requested is None or value.config_snapshot is None:
        raise ValueError("missing CI declaration or effective local configuration")
    if requested.policy_revision != value.merge_base_sha:
        raise ValueError("CI declaration does not identify the attested protected base")
    actual = declaration(
        repo,
        base=requested.policy_revision,
        head=value.sha,
        base_ref=value.base_ref,
        effective=Config.model_validate(value.config_snapshot),
    )
    if requested != actual:
        raise ValueError("CI declaration does not match protected policy")
