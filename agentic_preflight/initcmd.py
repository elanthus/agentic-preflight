"""``agentic-preflight init``: install the hook and seed a config file."""

from __future__ import annotations

from pathlib import Path

from . import gitx, hook, worktree
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
# human-review or high-risk path invokes the configured approval mode before merge,
# while the normal confirmation-token push remains available.
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
# gitignored and are forbidden from repair commits. Dotenv values that appear
# verbatim in captured output are redacted; other formats are not guaranteed.
copy_files = [".env"]
# Runs before review in every mode and before a --baseline stage in its scratch
# worktree. Use it to install dependencies or prepare ignored build inputs.
# setup_command = "uv sync"

[gate]
# "token" mints a confirmation token; "manual" refuses to push at all, so a
# person must run the command themselves. Risk policy does not change this mode.
mode = "token"

[pr]
# "auto" opens a pull request after the user approves the push gate. "manual"
# leaves pull-request creation to the user and reports a compare URL instead.
mode = "auto"
# After an automatically opened PR is green, poll for its merge and perform the
# disclosed run-scoped cleanup. Set false to require a later explicit request.
automatedCleanup = true

[approval]
# High-risk changes default to a successful hosted check that requires a person
# to perform the merge. "environment" waits for the named GitHub Environment;
# "peer_review" requires an eligible reviewer other than the pull-request author.
mode = "manual_merge"
environment = "high-risk-review"

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
        config_path.write_text(DEFAULT_CONFIG, encoding="utf-8", newline="\n")
        config_written = True

    hook_path = None
    hook_installed = False
    if install_hook:
        path, written = hook.install(git_dir, force=force)
        hook_path = str(path)
        hook_installed = True
        _ = written

    cfg = load_config(repo_root)
    instruction = (
        "Set [commands] lint and test in .agentic-preflight.toml so stages do not have "
        "to be detected on every run, then start a run."
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
        },
        next_instruction=instruction,
        next_command='agentic-preflight start --intent "<objective and acceptance criteria>"',
    )
