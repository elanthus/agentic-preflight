"""Click adapters for local run telemetry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click

from . import gitx
from . import hosted as hostedmod
from . import telemetry as telemetrymod
from .cli_support import as_error, command, finish, selected_run_id
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
    """Local run summaries. Only sync-hosted contacts GitHub, and it only reads."""


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


SYNC_COMMAND = "agentic-preflight telemetry sync-hosted"
REPORT_COMMAND = "agentic-preflight report"
_RUN_ID = re.compile(r"r_[0-9a-f]{10}")
_RETRY = "Run the sync again after that. It skips the runs that it already synced."

#: The error code and instruction for each reason a sync stops.
_STOPS: dict[str, tuple[str, str]] = {
    "gh_unavailable": (
        "gh_unavailable",
        "Install the GitHub CLI from https://cli.github.com, then ask the user to run "
        f"`gh auth login` for github.com. {_RETRY}",
    ),
    "unsupported_remote": (
        "unsupported_remote",
        "Only clones whose origin is on github.com can be synced. Nothing was fetched.",
    ),
    "rate_limited": (
        "hosted_rate_limited",
        f"GitHub's rate limit stopped the sync. Wait until the limit resets. {_RETRY}",
    ),
    "network_error": (
        "hosted_network_error",
        f"gh could not reach GitHub. Check the network connection. {_RETRY}",
    ),
    "write_failed": (
        "telemetry_write_failed",
        f"Make the run directories writable. {_RETRY}",
    ),
}
_REQUEST_FAILED = (
    "hosted_request_failed",
    "GitHub did not answer a request. Check that the gh account can read the repository "
    f"named in `data.repo.remote`. {_RETRY}",
)


def _run_id_option(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is not None and _RUN_ID.fullmatch(value) is None:
        raise click.BadParameter(
            f"must look like r_ followed by 10 lowercase hex digits: {value!r}"
        )
    return value


def _sync_error(data: dict[str, Any]) -> AgenticError | None:
    """The error envelope for a sync that stopped or could not write a summary."""
    stopped = data["stopped"]
    if stopped is not None:
        code, instruction = _STOPS.get(stopped["reason"], _REQUEST_FAILED)
        error = as_error(code, stopped["message"], ExitCode.USAGE, instruction)
        error.data = data
        return error
    summaries = data["summaries"] or {}
    if summaries.get("write_failures") or (summaries.get("mirror") or {}).get("error"):
        error = as_error(
            "telemetry_write_failed",
            "hosted.json was written, but not every summary or mirror row",
            ExitCode.USAGE,
            "Make the run directories and the telemetry directory writable, then run the rebuild.",
            "agentic-preflight telemetry rebuild",
        )
        error.data = data
        return error
    return None


def _sync_next(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """What to tell the agent after a sync. Another network call is never ``next.command``."""
    parts: list[str] = []
    command_text: str | None = None
    run = data["run"]
    if data["dry_run"]:
        if data["selected"]:
            parts.append(
                f"This was a dry run. `{SYNC_COMMAND}` without `--dry-run` reads these runs "
                "from GitHub."
            )
    elif data["synced"]:
        parts.append("Run the report to see the hosted outcomes.")
        command_text = REPORT_COMMAND
    if run is not None and not data["eligible"]:
        parts.append(f"Run {run} is not a DONE run with a pushed commit. Nothing to fetch.")
    elif run is not None and data["already_synced"]:
        parts.append(f"Run {run} is already synced. Pass `--refresh` to fetch it again.")
    if data["deferred"]:
        parts.append("More runs wait beyond `--limit`. Run the sync again for the next ones.")
    if data["unreadable"]:
        parts.append(
            "Some run records could not be read and were not synced. Inspect each record "
            "in `data.unreadable_runs`."
        )
    return (" ".join(parts) or None), command_text


@telemetry.command("sync-hosted")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=hostedmod.DEFAULT_LIMIT,
    show_default=True,
    help="Fetch at most this many runs, newest first.",
)
@click.option(
    "--run",
    "run_id",
    default=None,
    metavar="RUN_ID",
    callback=_run_id_option,
    help="Fetch only this run.",
)
@click.option("--refresh", is_flag=True, help="Fetch runs that already have hosted.json again.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="List the runs to fetch. Contacts nothing and writes nothing.",
)
@command
def telemetry_sync_hosted(limit: int, run_id: str | None, refresh: bool, dry_run: bool) -> None:
    """Join GitHub pull-request and check outcomes onto pushed runs. Reads GitHub only."""
    from .runs import STATE_DIR_NAME

    cwd = Path.cwd()
    repo_root = gitx.repo_root(cwd)
    store = Store(gitx.git_common_dir(cwd) / STATE_DIR_NAME)
    run_id = run_id or selected_run_id()
    try:
        data = hostedmod.sync_hosted(
            store,
            repo_root,
            load_config(repo_root),
            limit=limit,
            run_id=run_id,
            refresh=refresh,
            dry_run=dry_run,
        )
    except hostedmod.UnknownRun as exc:
        raise as_error(
            "no_run",
            f"no stored run {run_id} in this clone",
            ExitCode.PRECONDITION,
            "List the stored runs, then pass one of their run IDs.",
            "agentic-preflight status --all",
        ) from exc
    error = _sync_error(data)
    if error is not None:
        raise error
    instruction, next_command = _sync_next(data)
    finish(Envelope(data=data, next_instruction=instruction, next_command=next_command))


COMMANDS = (telemetry,)


def register(group: click.Group) -> None:
    """Add the telemetry commands to a CLI group."""
    for cli_command in COMMANDS:
        group.add_command(cli_command)
