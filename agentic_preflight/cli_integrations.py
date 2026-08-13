"""Click adapters for skill integrations and project initialization."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import click

from .cli_support import as_error, command, fail, finish
from .envelope import Envelope, ExitCode
from .integrations import SUPPORTED_INTEGRATIONS, IntegrationOperation

INTEGRATION_NAMES = tuple(SUPPORTED_INTEGRATIONS)


@click.group()
def integrations() -> None:
    """Install the bundled skill into supported coding agents."""


def _integration_options(*, target_help: str, force_help: str | None = None):
    """Shared Click surface for every integration lifecycle command."""

    def decorate(fn):
        if force_help is not None:
            fn = click.option("--force", is_flag=True, help=force_help)(fn)
        fn = click.option(
            "--target",
            "targets",
            multiple=True,
            type=click.Path(path_type=Path, file_okay=False),
            help=target_help,
        )(fn)
        fn = click.option(
            "--scope",
            type=click.Choice(["user", "project"]),
            default="user",
            show_default=True,
            help="Use this user's skills directory or the current repository.",
        )(fn)
        return click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))(fn)

    return decorate


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


class _IntegrationCommandSpec(TypedDict):
    help: str
    target_help: str
    force_help: str | None
    targets_required: bool


_INTEGRATION_COMMANDS: dict[IntegrationOperation, _IntegrationCommandSpec] = {
    IntegrationOperation.INSTALL: {
        "help": "Install or refresh the skill for AGENTS.",
        "target_help": "Also install under this custom skills directory.",
        "force_help": "Replace unmanaged or locally modified copies.",
        "targets_required": True,
    },
    IntegrationOperation.STATUS: {
        "help": "Report whether installed skills are current or modified.",
        "target_help": "Also inspect this custom skills directory.",
        "force_help": None,
        "targets_required": False,
    },
    IntegrationOperation.UPDATE: {
        "help": "Update installed skills, skipping integrations that are absent.",
        "target_help": "Also update under this custom skills directory.",
        "force_help": "Replace unmanaged or locally modified copies.",
        "targets_required": False,
    },
    IntegrationOperation.UNINSTALL: {
        "help": "Remove agentic-preflight-managed skill copies for AGENTS.",
        "target_help": "Also remove from this custom skills directory.",
        "force_help": "Remove unmanaged or locally modified copies.",
        "targets_required": True,
    },
}


def _integration_lifecycle_command(
    operation: IntegrationOperation, spec: _IntegrationCommandSpec
) -> click.Command:
    """Build the identical Click/result-shaping shell around one operation."""

    def invoke(
        agents: tuple[str, ...],
        scope: str,
        targets: tuple[Path, ...],
        force: bool = False,
    ) -> None:
        from . import integrations as integrations_module

        if spec["targets_required"]:
            _require_integration_targets(agents, targets)
        selected = agents or (() if targets else INTEGRATION_NAMES)
        results = integrations_module.manage_integrations(
            operation,
            selected,
            scope=scope,
            custom_roots=targets,
            force=force,
            project_root=_integration_project_root(scope),
        )
        finish(_integration_envelope(scope, results))

    invoke.__name__ = f"integrations_{operation.value}"
    invoke.__doc__ = spec["help"]
    decorated = command(invoke)
    decorated = _integration_options(
        target_help=spec["target_help"], force_help=spec["force_help"]
    )(decorated)
    return click.command(operation.value)(decorated)


for _operation, _spec in _INTEGRATION_COMMANDS.items():
    integrations.add_command(_integration_lifecycle_command(_operation, _spec))
del _operation, _spec


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
        fail(
            as_error(
                "hook_exists",
                f"a pre-push hook already exists at {exc} and was not written by "
                f"agentic-preflight; refusing to replace it",
                ExitCode.PRECONDITION,
                "Inspect the existing hook. Re-run with --force to replace it, or "
                "merge the `agentic-preflight hook-check` call into it by hand.",
                "agentic-preflight init --force",
            )
        )


COMMANDS = (integrations, init_command)


def register(group: click.Group) -> None:
    for cli_command in COMMANDS:
        group.add_command(cli_command)
