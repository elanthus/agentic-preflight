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
def reviewer_run(provider: str) -> None:
    """Review the context on stdin with a provider CLI; print the submission.

    Deliberately not wrapped in the envelope contract: `review run` starts it as the
    configured review command and reads its stdout as the strict submission. A failure
    exits 1 with one line on stderr, as the example wrappers did.
    """
    from .reviewers import PROVIDERS

    sys.exit(PROVIDERS[provider]())


def register(group: click.Group) -> None:
    """Add the reviewer commands to a CLI group."""
    group.add_command(reviewer)
