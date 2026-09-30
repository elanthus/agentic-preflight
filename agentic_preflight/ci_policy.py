"""Read CI and mandatory local policy from committed protected-base data."""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import gitx
from .ci_models import TestDelegation
from .config import REPO_CONFIG_NAME, Config, snapshot_config
from .models import Attestation


def _validate_policy(contents: str) -> Config:
    try:
        return Config.model_validate(tomllib.loads(contents))
    except ValueError as exc:
        raise ValueError(f"invalid protected policy: {exc}") from exc


def _has_committed_config(repo: Path | str, revision: str) -> bool:
    return bool(gitx.out(repo, "ls-tree", "--name-only", revision, "--", REPO_CONFIG_NAME))


def committed_config(repo: Path | str, revision: str) -> Config:
    """Read the configuration committed at a revision, using defaults when it has none."""
    if not _has_committed_config(repo, revision):
        return Config()
    return _validate_policy(gitx.out(repo, "show", f"{revision}:{REPO_CONFIG_NAME}"))


def _require_ci_authority(cfg: Config) -> Config:
    if cfg.ci.test_authority != "github_actions":
        raise ValueError("protected base has not enabled delegated-test CI authority")
    return cfg


def parse_policy(contents: str) -> Config:
    """Parse protected policy text that must enable delegated-test CI authority."""
    return _require_ci_authority(_validate_policy(contents))


def committed_policy(repo: Path | str, revision: str) -> Config:
    """Read the committed protected policy, which must exist and enable CI authority."""
    if not _has_committed_config(repo, revision):
        raise gitx.GitError(
            ["show", f"{revision}:{REPO_CONFIG_NAME}"],
            128,
            f"{REPO_CONFIG_NAME} is not committed at {revision}",
        )
    return _require_ci_authority(committed_config(repo, revision))


def base_enabled(repo: Path | str, revision: str) -> bool:
    """A producer-only rollout must still perform complete local validation."""
    try:
        committed_policy(repo, revision)
    except (ValueError, gitx.GitError):
        return False
    return True


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
        effective=snapshot_config(value.config_snapshot),
    )
    if requested != actual:
        raise ValueError("CI declaration does not match protected policy")
