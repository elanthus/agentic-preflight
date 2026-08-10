"""``agentic-preflight init``: install the hook and seed a config file."""

from __future__ import annotations

from pathlib import Path

from . import gitx, hook, runtime, worktree
from .config import REPO_CONFIG_NAME, load_config
from .envelope import Envelope

DEFAULT_CONFIG = """# agentic-preflight configuration. Committed, so the gate is the same for everyone.
[general]
base_ref = "main"

[commands]
# lint = "ruff check ."
# test = "pytest"

[docs]
enabled = true
# Add repository-specific documentation surfaces here. Common agent rule files,
# PRODUCT.md, and DESIGN.md are included automatically.
# paths = ["architecture/**"]

[policy]
# These rules are evaluated by code, not by the reviewing model. Matching a
# human-review path forces a manual final gate even when every stage is green.
human_review_paths = [
  ".agentic-preflight.toml",
  ".github/workflows/**",
  ".github/CODEOWNERS",
  "CODEOWNERS",
]
# high_risk_paths = ["db/migrations/**", "infra/**"]
# medium_risk_paths = ["dependencies/**"]

[worktree]
# The default validates directly in this checkout. It requires a clean tree and
# records each accepted repair commit before allowing the workflow to continue.
# Use "reusable" for one serial isolated runner with retained ignored caches, or
# "strict" for a fresh isolated worktree on every run.
mode = "in_place"
# Isolated modes put worktrees in a hidden sibling directory, outside .git, so
# Jest can discover tests without touching this checkout.
# root = "/absolute/path/to/agentic-preflight-worktrees"
# In isolated modes these files are copied into the validation worktree. In
# in-place mode they stay where they are. In every mode they must already be
# gitignored, are redacted from logs, and are forbidden from repair commits.
copy_files = [".env"]
# Auto-detect pnpm/npm lockfiles. pnpm gets a frozen install backed by its
# shared store; in-place mode uses this checkout's existing dependencies,
# reusable mode retains a fingerprint-matched install, and strict mode runs a
# clean install in every new worktree.
dependency_setup = "auto"
# setup_command = "uv sync"

# Detect committed pins for nvm, Volta, asdf, mise, fnm, and nodenv. Strict mode
# fails clearly instead of silently running a different system runtime.
# [runtime]
# manager = "auto"
# strict = true

[gate]
# "token" mints a confirmation token; "manual" refuses to push at all, so a
# person must run the command themselves.
mode = "token"

[hook]
enabled = true
allow_force_push = false
"""


def init(repo_root: Path | str, *, force: bool = False, install_hook: bool = True) -> Envelope:
    repo_root = Path(repo_root)
    git_dir = gitx.git_common_dir(repo_root)

    config_path = repo_root / REPO_CONFIG_NAME
    config_written = False
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG)
        config_written = True

    hook_path = None
    hook_installed = False
    if install_hook:
        path, written = hook.install(git_dir, force=force)
        hook_path = str(path)
        hook_installed = True
        _ = written

    cfg = load_config(repo_root)
    runtime_info = runtime.inspect_project(repo_root, cfg.runtime.manager)
    warnings: list[str] = []
    if runtime_info.reason:
        warnings.append(runtime_info.reason)

    instruction = (
        "Set [commands] lint and test in .agentic-preflight.toml so stages do not have "
        "to be detected on every run, then start a run."
    )
    if runtime_info.node_project and not runtime_info.pin_file:
        instruction = (
            "Pin Node with .nvmrc, .node-version, Volta, asdf, or mise so agentic-preflight "
            "can reproduce the runtime in non-interactive stages. Then set [commands] "
            "lint and test and start a run."
        )

    return Envelope(
        data={
            "repo_root": str(repo_root),
            "config_path": str(config_path),
            "config_written": config_written,
            "hook_path": hook_path,
            "hook_installed": hook_installed,
            "worktree_root": (
                None
                if cfg.worktree.mode == "in_place"
                else str(worktree.resolve_root(repo_root, cfg.worktree.root))
            ),
            "worktree_mode": cfg.worktree.mode,
            "runtime": runtime_info.as_dict(),
            "warnings": warnings,
        },
        next_instruction=instruction,
        next_command='agentic-preflight start --intent "<objective and acceptance criteria>"',
    )
