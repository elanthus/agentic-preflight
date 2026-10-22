"""Click adapters for local run telemetry."""

from __future__ import annotations

from pathlib import Path

import click

from . import gitx
from . import telemetry as telemetrymod
from .cli_support import command, finish
from .config import load_config
from .envelope import Envelope, ExitCode
from .errors import AgenticError
from .store import Store

UNREADABLE_INSTRUCTION = (
    "Some run records could not be read. Their earlier summaries and mirror rows were "
    "kept. Inspect each listed record with a compatible tool version."
)


@click.group()
def telemetry() -> None:
    """Local run summaries. Nothing leaves this machine."""


@telemetry.command("rebuild")
@command
def telemetry_rebuild() -> None:
    """Regenerate run summaries and this repository's mirror rows."""
    from .runs import STATE_DIR_NAME

    cwd = Path.cwd()
    repo_root = gitx.repo_root(cwd)
    store = Store(gitx.git_common_dir(cwd) / STATE_DIR_NAME)
    result = telemetrymod.rebuild(store, repo_root, load_config(repo_root))
    if result["write_failures"] or result["mirror"]["error"]:
        error = AgenticError(
            "telemetry rebuild could not write every summary",
            data=result,
            next_instruction=(
                "Make the run directories and the telemetry directory writable, then "
                "run the rebuild again."
            ),
            next_command="agentic-preflight telemetry rebuild",
        )
        error.code = "telemetry_write_failed"
        error.exit_code = ExitCode.USAGE
        raise error
    finish(
        Envelope(
            data=result,
            next_instruction=UNREADABLE_INSTRUCTION if result["unreadable"] else None,
        )
    )


COMMANDS = (telemetry,)


def register(group: click.Group) -> None:
    """Add the telemetry commands to a CLI group."""
    for cli_command in COMMANDS:
        group.add_command(cli_command)
