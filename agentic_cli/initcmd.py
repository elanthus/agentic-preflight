"""``agentic-cli init``: install the hook and seed a config file."""

from __future__ import annotations

from pathlib import Path

from . import gitx, hook, runtime, worktree
from .config import REPO_CONFIG_NAME, load_config
from .envelope import Envelope

DEFAULT_CONFIG = """# agentic-cli configuration. Committed, so the gate is the same for everyone.
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

[worktree]
# Worktrees default to a hidden sibling directory, outside .git, so Jest can
# discover tests.
# root = "/absolute/path/to/agentic-cli-worktrees"
# Copied into the disposable worktree. Must already be gitignored.
copy_files = [".env"]
# Auto-detect pnpm/npm lockfiles. pnpm gets a frozen install backed by its
# shared store; npm runs npm ci in each worktree for an isolated install.
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

[ci]
timeout_seconds = 3600
poll_interval_seconds = 30
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
        "Set [commands] lint and test in .agentic-cli.toml so stages do not have "
        "to be detected on every run, then start a run."
    )
    if runtime_info.node_project and not runtime_info.pin_file:
        instruction = (
            "Pin Node with .nvmrc, .node-version, Volta, asdf, or mise so agentic-cli "
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
            "worktree_root": str(worktree.resolve_root(repo_root, cfg.worktree.root)),
            "runtime": runtime_info.as_dict(),
            "warnings": warnings,
        },
        next_instruction=instruction,
        next_command='agentic-cli start --intent "<objective and acceptance criteria>"',
    )
