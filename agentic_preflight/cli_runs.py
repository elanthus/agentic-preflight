"""Click adapters for run orchestration commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import runs
from .cli_support import as_error, command, fail, finish, finish_locked, open_cli_session
from .envelope import ExitCode


@click.command()
@click.option("--base-ref", default=None, help="Branch to diff against (default: config).")
@click.option(
    "--intent",
    default=None,
    help="The user's objective and acceptance criteria, in their own terms.",
)
@click.option(
    "--replace",
    is_flag=True,
    help="Orphan a non-stale active run in this worktree and start a new one.",
)
@command
def start(base_ref: str | None, intent: str | None, replace: bool) -> None:
    """Create a run and prepare its validation checkout."""
    session = open_cli_session()
    with session.store.resource(f"start-{session.owner_id}"):
        finish(runs.start(session, base_ref=base_ref, intent=intent, replace=replace))


@click.command()
@click.option("--section", default="review", type=click.Choice(["review", "docs"]))
@command
def context(section: str) -> None:
    """Return the material the agent needs to judge this stage."""
    finish_locked(lambda session: runs.context(session, section=section))


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
    finish_locked(lambda session: runs.submit_findings(session, payload))


@click.group()
def review() -> None:
    """Independent review execution."""


@review.command("run")
@command
def review_run() -> None:
    """Run the configured reviewer over the current review bundle."""
    finish_locked(runs.run_review_command)


@review.command("compare")
@click.option(
    "--file",
    "file_path",
    default=None,
    help="Second review submission; otherwise run the configured shadow reviewer.",
)
@command
def review_compare(file_path: str | None) -> None:
    """Compare in-harness and command review submissions."""
    payload = None
    if file_path is not None:
        try:
            payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(
                as_error(
                    "invalid_findings",
                    f"comparison file is not valid JSON: {exc}",
                    ExitCode.PRECONDITION,
                )
            )
            return
    finish_locked(lambda session: runs.compare_reviews(session, payload))


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
    finish_locked(
        lambda session: runs.respond(
            session, finding_id=finding_id, action=action, commit=commit, note=note
        )
    )


@click.command()
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@command
def events(limit: int | None) -> None:
    """The run's history, oldest first."""
    session = open_cli_session()
    finish(runs.events(session, limit=limit))


@click.command()
@command
def mergeback() -> None:
    """Cherry-pick verified fixes onto your branch. Never auto-resolves."""
    finish_locked(runs.mergeback)


@click.command()
@command
def gate() -> None:
    """Summarise what would be pushed and mint a confirmation token."""
    finish_locked(runs.gate)


@click.command()
@click.option("--confirm", default=None, help="Token minted by `gate`.")
@click.option("--dry-run", is_flag=True, help="Report what would be pushed, push nothing.")
@command
def push(confirm: str | None, dry_run: bool) -> None:
    """Push the verified branch. Requires the gate token."""
    finish_locked(lambda session: runs.push(session, confirm=confirm, dry_run=dry_run))


@click.command()
@command
def finish_run() -> None:
    """Mark a pushed validation run complete."""
    finish_locked(runs.finish)


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
    finish_locked(
        lambda session: runs.run_stage(
            session, name, command=command_str, record=record, baseline=baseline
        )
    )


@click.command()
@click.option("--stage", "stage_name", required=True, type=click.Choice(["review", "lint", "test"]))
@command
def logs(stage_name: str) -> None:
    """The full captured output of a stage."""
    session = open_cli_session()
    finish(runs.logs(session, stage_name=stage_name))


@click.command()
@click.option("--force", is_flag=True, help="Discard unmerged fix commits.")
@command
def abort(force: bool) -> None:
    """End the run and release its validation worktree."""
    finish_locked(lambda session: runs.abort(session, force=force))


@click.command()
@click.option("--force", is_flag=True, help="Remove even when work would be lost.")
@command
def gc(force: bool) -> None:
    """Reconcile run directories, git worktrees, and ap/* branches."""
    session = open_cli_session()
    finish(runs.gc(session, force=force))


@click.command()
@click.option("--all", "all_runs", is_flag=True, help="List runs across every worktree.")
@command
def status(all_runs: bool) -> None:
    """Where the run is and what to do next. Legal in every state."""
    session = open_cli_session()
    finish(runs.status(session, all_runs=all_runs))


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
