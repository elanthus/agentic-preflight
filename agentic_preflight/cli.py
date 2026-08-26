"""Stable CLI root: command registration and the shared protocol export."""

from __future__ import annotations

import sys

import click

from .cli_integrations import register as register_integrations
from .cli_policy import register as register_policy
from .cli_runs import register as register_runs
from .cli_support import command

__all__ = ["command", "main"]


def _use_utf8_streams() -> None:
    """Read and write the agent protocol as UTF-8 regardless of the locale.

    The envelope itself is ASCII-safe, but the prose written to stderr and the
    findings read from stdin are not: both carry file paths and review text
    from the repository. Left to the platform default these become ``cp1252``
    on Windows, where a single non-ASCII path turns a working command into a
    ``UnicodeEncodeError``.

    Streams that cannot be reconfigured — a captured buffer under test, a pipe
    already wrapped by a caller — are left alone rather than replaced.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="agentic-preflight")
def main() -> None:
    """Agent-driven quality gate. Every command prints one JSON object."""
    _use_utf8_streams()


register_runs(main)
register_policy(main)
register_integrations(main)


if __name__ == "__main__":
    main()
