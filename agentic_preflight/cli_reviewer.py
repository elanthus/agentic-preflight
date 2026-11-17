"""Click adapter for the packaged command reviewer."""

from __future__ import annotations

import sys

import click


@click.group()
def reviewer() -> None:
    """Packaged independent reviewers for `[review] executor = "command"`."""


@reviewer.command("run")
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    required=True,
    help="The provider CLI to run: `claude` or `codex`.",
)
@click.option(
    "--retries",
    type=click.IntRange(0, 5),
    default=0,
    show_default=True,
    help=(
        "Retries per phase on malformed output or timeout. "
        "Timeout retries need --budget-seconds above AP_REVIEWER_TIMEOUT."
    ),
)
@click.option(
    "--budget-seconds",
    type=click.FloatRange(min=0, min_open=True),
    help="Total wall-time budget, including retries and verification; defaults to AP_REVIEWER_TIMEOUT.",
)
@click.option(
    "--verify", is_flag=True, help="Confirm or reject each candidate in a second provider call."
)
def reviewer_run(provider: str, retries: int, budget_seconds: float | None, verify: bool) -> None:
    """Review the context on stdin with a provider CLI; print the submission.

    Deliberately not wrapped in the envelope contract: `review run` starts it as the
    configured review command and reads its stdout as the strict submission. A failure
    exits 1 with one line on stderr, as the example wrappers did.
    """
    from .reviewers.orchestration import main

    sys.exit(main(provider, retries, budget_seconds, verify))


def register(group: click.Group) -> None:
    """Add the reviewer commands to a CLI group."""
    group.add_command(reviewer)
