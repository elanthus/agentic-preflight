"""Click adapters for run orchestration commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import runs
from .cli_support import as_error, command, fail, finish
from .envelope import ExitCode


@click.command()
@click.option("--base-ref", default=None, help="Branch to diff against (default: config).")
@click.option(
    "--intent",
    default=None,
    help="The user's objective and acceptance criteria, in their own terms.",
)
@command
def start(base_ref: str | None, intent: str | None) -> None:
    """Create a run and prepare its validation checkout."""
    session = runs.open_session()
    finish(runs.start(session, base_ref=base_ref, intent=intent))


@click.command()
@click.option("--section", default="review", type=click.Choice(["review", "docs"]))
@command
def context(section: str) -> None:
    """Return the material the agent needs to judge this stage."""
    session = runs.open_session()
    finish(runs.context(session, section=section))


@click.command("submit-findings")
@click.option(
    "--file",
    "file_path",
    required=True,
    help="Path to a findings JSON file, or - for stdin.",
)
@command
def submit_findings(file_path: str) -> None:
    """Record the agent's findings for the active stage."""
    raw = sys.stdin.read() if file_path == "-" else Path(file_path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(
            as_error(
                "invalid_findings",
                f"findings file is not valid JSON: {exc}",
                ExitCode.PRECONDITION,
            )
        )
        return
    session = runs.open_session()
    finish(runs.submit_findings(session, payload))


@click.group()
def review() -> None:
    """Independent review execution."""


@review.command("run")
@command
def review_run() -> None:
    """Run the configured reviewer over the current review bundle."""
    session = runs.open_session()
    finish(runs.run_review_command(session))


@click.command()
@click.option("--id", "finding_id", required=True, help="Finding id, e.g. F001.")
@click.option(
    "--action",
    required=True,
    type=click.Choice(runs.RESPONSE_ACTIONS),
    help="How the finding was resolved.",
)
@click.option("--commit", default=None, help="Commit that fixes it (required for `fixed`).")
@click.option("--note", default=None, help="Why it was dismissed or accepted.")
@command
def respond(finding_id: str, action: str, commit: str | None, note: str | None) -> None:
    """Resolve one finding. Claims about commits are verified, not trusted."""
    session = runs.open_session()
    finish(runs.respond(session, finding_id=finding_id, action=action, commit=commit, note=note))


@click.command()
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@command
def events(limit: int | None) -> None:
    """The run's history, oldest first."""
    session = runs.open_session()
    finish(runs.events(session, limit=limit))


@click.command()
@command
def mergeback() -> None:
    """Cherry-pick verified fixes onto your branch. Never auto-resolves."""
    session = runs.open_session()
    finish(runs.mergeback(session))


@click.command()
@command
def gate() -> None:
    """Summarise what would be pushed and mint a confirmation token."""
    session = runs.open_session()
    finish(runs.gate(session))


@click.command()
@click.option("--confirm", default=None, help="Token minted by `gate`.")
@click.option("--dry-run", is_flag=True, help="Report what would be pushed, push nothing.")
@command
def push(confirm: str | None, dry_run: bool) -> None:
    """Push the verified branch. Requires the gate token."""
    session = runs.open_session()
    finish(runs.push(session, confirm=confirm, dry_run=dry_run))


@click.command()
@command
def finish_run() -> None:
    """Mark a pushed validation run complete."""
    session = runs.open_session()
    finish(runs.finish(session))


@click.group()
def stage() -> None:
    """Deterministic shell stages."""


@stage.command("run")
@click.argument("name", type=click.Choice(["lint", "test"]))
@click.option("--command", "command_str", default=None, help="Command to run.")
@click.option("--record", is_flag=True, help="Acknowledge an explicitly chosen command.")
@click.option("--baseline", is_flag=True, help="Also run against the base commit.")
@command
def stage_run(name: str, command_str: str | None, record: bool, baseline: bool) -> None:
    """Run a stage. Pass/fail is the exit code and nothing else."""
    session = runs.open_session()
    finish(runs.run_stage(session, name, command=command_str, record=record, baseline=baseline))


@click.command()
@click.option("--stage", "stage_name", required=True, type=click.Choice(["review", "lint", "test"]))
@command
def logs(stage_name: str) -> None:
    """The full captured output of a stage."""
    session = runs.open_session()
    finish(runs.logs(session, stage_name=stage_name))


@click.command()
@click.option("--force", is_flag=True, help="Discard unmerged fix commits.")
@command
def abort(force: bool) -> None:
    """End the run and release its validation worktree."""
    session = runs.open_session()
    finish(runs.abort(session, force=force))


@click.command()
@click.option("--force", is_flag=True, help="Remove even when work would be lost.")
@command
def gc(force: bool) -> None:
    """Reconcile run directories, git worktrees, and ap/* branches."""
    session = runs.open_session()
    finish(runs.gc(session, force=force))


@click.command()
@command
def status() -> None:
    """Where the run is and what to do next. Legal in every state."""
    session = runs.open_session()
    finish(runs.status(session))


COMMANDS = (
    start,
    context,
    submit_findings,
    review,
    respond,
    events,
    mergeback,
    gate,
    push,
    finish_run,
    stage,
    logs,
    abort,
    gc,
    status,
)


def register(group: click.Group) -> None:
    for cli_command in COMMANDS:
        name = "finish" if cli_command is finish_run else None
        group.add_command(cli_command, name=name)
