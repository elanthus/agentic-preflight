"""Click adapter for the local usage report."""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from . import gitx, hooklog, telemetry
from . import report as reportmod
from .cli_support import as_error, command, finish
from .config import load_config
from .envelope import Envelope, ExitCode
from .gitx import GitError
from .store import Store

REBUILD_COMMAND = "agentic-preflight telemetry rebuild"


def _timestamp_option(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> datetime | None:
    if value is None:
        return None
    try:
        return reportmod.parse_timestamp(value)
    except ValueError as exc:
        raise click.BadParameter(
            f"{value!r} is not an ISO 8601 date or time, for example 2026-10-01 or "
            "2026-10-01T12:00:00+00:00"
        ) from exc


def _worktree_paths(repo_root: Path) -> frozenset[str]:
    paths = {str(repo_root)}
    with contextlib.suppress(GitError):
        paths.update(
            record["worktree"] for record in gitx.list_worktrees(repo_root) if "worktree" in record
        )
    return frozenset(paths | {str(Path(path).resolve()) for path in paths})


def _open_clone(cwd: Path) -> tuple[Path | None, reportmod.Clone | None]:
    """The clone that contains ``cwd``, or ``None`` outside a Git working tree."""
    from .runs import STATE_DIR_NAME

    try:
        repo_root = gitx.repo_root(cwd)
        common = gitx.git_common_dir(cwd)
    except GitError:
        return None, None
    clone = reportmod.Clone(
        store=Store(common / STATE_DIR_NAME),
        identity=telemetry.repo_identity(repo_root),
        worktree_paths=_worktree_paths(repo_root),
    )
    return repo_root, clone


def _guidance(source: dict[str, Any], mirror_enabled: bool) -> tuple[str | None, str | None]:
    """Tell the agent about every gap in the data, and when a rebuild closes it."""
    clone = source["clone"] or {}
    parts: list[str] = []
    command_text = None
    # Suggest a rebuild only when it adds rows; otherwise following `next` would loop.
    if source["used"] == "clone_records" and clone.get("summarized"):
        parts.append(
            "The mirror has no rows for this repository, so the report summarized this "
            "clone's run records in memory."
        )
    elif clone.get("unmirrored_terminal_runs"):
        parts.append("Some finished runs in this clone are not in the mirror.")
    if parts and mirror_enabled:
        parts.append(f"Run `{REBUILD_COMMAND}` to add them, then run the report again.")
        command_text = REBUILD_COMMAND
    elif parts:
        parts.append("The mirror is off, so `telemetry rebuild` does not add them.")
    if clone.get("unreadable"):
        parts.append(
            "Some run records could not be read and are not in the report. Inspect each "
            "record in `data.source.clone.unreadable_runs`."
        )
    if source["mirror"]["malformed"]:
        parts.append(
            "Some mirror lines are not readable summaries and are not in the report. "
            "`data.source.mirror.malformed_lines` lists up to 20 of them by line number."
        )
    return (" ".join(parts) or None), command_text


@click.command("report")
@click.option(
    "--since",
    default=None,
    callback=_timestamp_option,
    help="Only runs that finished at or after this ISO 8601 date or time (UTC if no zone).",
)
@click.option(
    "--until",
    default=None,
    callback=_timestamp_option,
    help="Only runs that finished before this ISO 8601 date or time (UTC if no zone).",
)
@click.option(
    "--repo",
    default=None,
    metavar="NAME_OR_REMOTE",
    help="Only this repository, by name or remote. Reads every repository's mirror rows.",
)
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice(reportmod.REPORT_STATES, case_sensitive=False),
    help="Only runs in this terminal state. Repeat to select more than one.",
)
@click.option("--all-repos", is_flag=True, help="Read every repository's mirror rows.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(reportmod.FORMATS),
    default="json",
    show_default=True,
    help="markdown also renders the report as Markdown in data.markdown.",
)
@click.option("--rows", "include_rows", is_flag=True, help="Include the summary rows in data.rows.")
@command
def report(
    since: datetime | None,
    until: datetime | None,
    repo: str | None,
    states: tuple[str, ...],
    all_repos: bool,
    output_format: str,
    include_rows: bool,
) -> None:
    """Count finished runs from the local telemetry. Nothing leaves this machine."""
    cwd = Path.cwd()
    all_repos = all_repos or repo is not None
    repo_root, clone = _open_clone(cwd)
    if clone is None and not all_repos:
        raise as_error(
            "not_a_repository",
            "report without --all-repos or --repo must run inside a Git working tree",
            ExitCode.USAGE,
            "Run the report inside a clone, or read every repository's mirror rows.",
            "agentic-preflight report --all-repos",
        )
    config = load_config(repo_root or cwd)
    path = telemetry.mirror_path(config)
    try:
        mirror = reportmod.read_mirror(path)
    except OSError as exc:
        error = as_error(
            "telemetry_mirror_unreadable",
            "the telemetry mirror exists but cannot be read",
            ExitCode.USAGE,
            "Make the mirror file readable, then run the report again.",
        )
        error.data = {"path": str(path), "error": telemetry._describe(exc)}
        raise error from exc
    rows, source = reportmod.collect(mirror, clone, all_repos=all_repos)
    source["mirror"]["enabled"] = config.telemetry.mirror
    filters = reportmod.Filters(since=since, until=until, repo=repo, states=tuple(states))
    kept, filtered_out = reportmod.apply_filters(rows, filters)
    hooks = reportmod.hook_decisions(
        reportmod.read_hook_log(hooklog.log_path(config)),
        clone,
        all_repos=all_repos,
        filters=filters,
        enabled=config.telemetry.hook_log,
    )
    data = reportmod.build_report(
        kept,
        source=source,
        filters=filters,
        filtered_out=filtered_out,
        output_format=output_format,
        include_rows=include_rows,
        hook_decisions=hooks,
    )
    instruction, next_command = _guidance(source, config.telemetry.mirror)
    instruction = _hook_guidance(instruction, hooks["source"])
    finish(Envelope(data=data, next_instruction=instruction, next_command=next_command))


def _hook_guidance(instruction: str | None, source: dict[str, Any]) -> str | None:
    """Add the gaps in the hook decision log to the report's instruction."""
    parts = [instruction] if instruction else []
    if source["error"]:
        parts.append(
            "The hook decision log exists but cannot be read, so its counts are null. "
            "Make `data.hook_decisions.source.path` readable, then run the report again."
        )
    if source["malformed"]:
        parts.append(
            "Some hook decision log lines are not readable decisions and are not counted. "
            "`data.hook_decisions.source.malformed_lines` lists up to 20 of them by line number."
        )
    return " ".join(parts) or None


COMMANDS = (report,)


def register(group: click.Group) -> None:
    """Add the report command to a CLI group."""
    for cli_command in COMMANDS:
        group.add_command(cli_command)
