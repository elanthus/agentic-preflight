"""Click adapters for skill integrations and project initialization."""

from __future__ import annotations

from pathlib import Path

import click

from .cli_support import as_error, command, fail, finish
from .envelope import Envelope, ExitCode
from .integrations import SUPPORTED_INTEGRATIONS

INTEGRATION_NAMES = tuple(SUPPORTED_INTEGRATIONS)


@click.group()
def integrations() -> None:
    """Install the bundled skill into supported coding agents."""


def _integration_project_root(scope: str) -> Path | None:
    if scope != "project":
        return None
    from . import gitx

    return gitx.repo_root(Path.cwd())


def _require_integration_targets(agents: tuple[str, ...], targets: tuple[Path, ...]) -> None:
    if agents or targets:
        return
    fail(
        as_error(
            "integration_target_required",
            "name at least one integration, or pass --target with a skills directory",
            ExitCode.USAGE,
        )
    )


def _integration_envelope(scope: str, results: list[dict]) -> Envelope:
    lines = [
        f"{item['integration']}: {item.get('action', item['status'])} ({item['path']})"
        for item in results
    ]
    if any(item.get("action") in {"installed", "updated", "replaced"} for item in results):
        lines.append("Restart a running agent if the skill does not appear automatically.")
    return Envelope(
        data={"scope": scope, "integrations": results},
        human="\n".join(lines),
    )


def _run_integration(
    operation: str,
    agents: tuple[str, ...],
    scope: str,
    targets: tuple[Path, ...],
    force: bool = False,
) -> None:
    from . import integrations as integration_module

    if operation in {"install", "uninstall"}:
        _require_integration_targets(agents, targets)
    selected = agents or (() if targets else INTEGRATION_NAMES)
    resolved = integration_module.resolve_targets(
        selected, scope=scope, custom_roots=targets, project_root=_integration_project_root(scope)
    )
    if operation == "status":
        results = [integration_module.inspect_target(target) for target in resolved]
    elif operation == "uninstall":
        results = integration_module.uninstall(resolved, force=force)
    else:
        results = integration_module.install(
            resolved, force=force, skip_missing=operation == "update"
        )
    finish(_integration_envelope(scope, results))


@integrations.command("install")
@click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="user",
    show_default=True,
    help="Use this user's skills directory or the current repository.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Also install under this custom skills directory.",
)
@click.option("--force", is_flag=True, help="Replace unmanaged or locally modified copies.")
@command
def integrations_install(
    agents: tuple[str, ...], scope: str, targets: tuple[Path, ...], force: bool
) -> None:
    """Install or refresh the skill for AGENTS."""
    _run_integration("install", agents, scope, targets, force)


@integrations.command("status")
@click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="user",
    show_default=True,
    help="Use this user's skills directory or the current repository.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Also inspect this custom skills directory.",
)
@command
def integrations_status(agents: tuple[str, ...], scope: str, targets: tuple[Path, ...]) -> None:
    """Report whether installed skills are current or modified."""
    _run_integration("status", agents, scope, targets)


@integrations.command("update")
@click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="user",
    show_default=True,
    help="Use this user's skills directory or the current repository.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Also update under this custom skills directory.",
)
@click.option("--force", is_flag=True, help="Replace unmanaged or locally modified copies.")
@command
def integrations_update(
    agents: tuple[str, ...], scope: str, targets: tuple[Path, ...], force: bool
) -> None:
    """Update installed skills, skipping integrations that are absent."""
    _run_integration("update", agents, scope, targets, force)


@integrations.command("uninstall")
@click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))
@click.option(
    "--scope",
    type=click.Choice(["user", "project"]),
    default="user",
    show_default=True,
    help="Use this user's skills directory or the current repository.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Also remove from this custom skills directory.",
)
@click.option("--force", is_flag=True, help="Remove unmanaged or locally modified copies.")
@command
def integrations_uninstall(
    agents: tuple[str, ...], scope: str, targets: tuple[Path, ...], force: bool
) -> None:
    """Remove agentic-preflight-managed skill copies for AGENTS."""
    _run_integration("uninstall", agents, scope, targets, force)


@click.command("init")
@click.option("--force", is_flag=True, help="Replace an existing pre-push hook.")
@click.option("--no-hook", is_flag=True, help="Write config only, skip the hook.")
@command
def init_command(force: bool, no_hook: bool) -> None:
    """Install the pre-push hook and seed .agentic-preflight.toml."""
    from . import gitx, initcmd

    repo_root = gitx.repo_root(Path.cwd())
    try:
        finish(initcmd.init(repo_root, force=force, install_hook=not no_hook))
    except FileExistsError as exc:
        hook_path = str(exc)
        error = as_error(
            "hook_exists",
            f"a pre-push hook already exists at {hook_path} and was not written by "
            f"agentic-preflight; refusing to replace it",
            ExitCode.PRECONDITION,
            f"Add `agentic-preflight hook-check` to the existing hook at {hook_path}, "
            "or re-run with --force to replace it.",
            "agentic-preflight init --force",
        )
        error.data["hook_path"] = hook_path
        fail(error)


COMMANDS = (integrations, init_command)


def register(group: click.Group) -> None:
    """Add the integration commands to a CLI group."""
    for cli_command in COMMANDS:
        group.add_command(cli_command)
