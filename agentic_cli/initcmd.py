"""``agentic-cli init``: install the hook and seed a config file."""

from __future__ import annotations

from pathlib import Path

from . import gitx, hook
from .config import REPO_CONFIG_NAME
from .envelope import Envelope

DEFAULT_CONFIG = """# agentic-cli configuration. Committed, so the gate is the same for everyone.
[general]
base_ref = "main"

[commands]
# lint = "ruff check ."
# test = "pytest"

[docs]
enabled = true

[worktree]
# Copied into the disposable worktree. Must already be gitignored.
copy_files = [".env"]
# setup_command = "uv sync"

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

    return Envelope(
        data={
            "repo_root": str(repo_root),
            "config_path": str(config_path),
            "config_written": config_written,
            "hook_path": hook_path,
            "hook_installed": hook_installed,
        },
        next_instruction=(
            "Set [commands] lint and test in .agentic-cli.toml so stages do not have "
            "to be detected on every run, then start a run."
        ),
        next_command="agentic-cli start",
    )
