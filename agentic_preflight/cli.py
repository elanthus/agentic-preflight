"""Stable CLI root: command registration and the shared protocol export."""

from __future__ import annotations

import click

from .cli_integrations import register as register_integrations
from .cli_policy import register as register_policy
from .cli_runs import register as register_runs
from .cli_support import command

__all__ = ["command", "main"]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="agentic-preflight")
def main() -> None:
    """Agent-driven quality gate. Every command prints one JSON object."""


register_runs(main)
register_policy(main)
register_integrations(main)


if __name__ == "__main__":
    main()
