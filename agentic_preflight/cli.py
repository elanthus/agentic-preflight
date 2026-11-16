"""Stable CLI root: command registration."""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import click

from . import gitx
from .cli_support import EnvelopeGroup, as_error, fail
from .envelope import ExitCode

RUN_ID_PATTERN = re.compile(r"r_[0-9a-f]{10}")
#: Top-level commands that run without the Git version check.
NO_GIT_CHECK = frozenset({"reviewer"})


def _use_utf8_streams() -> None:
    """Read and write the agent protocol as UTF-8 regardless of the locale.

    The envelope itself is ASCII-safe, but the prose written to stderr and the
    findings read from stdin are not: both carry file paths and review text
    from the repository. Left to the platform default these become ``cp1252``
    on Windows, where a single non-ASCII path turns a working command into a
    ``UnicodeEncodeError``.

    Streams that cannot be reconfigured (a captured buffer under test, a pipe
    already wrapped by a caller) are left alone rather than replaced.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue


#: The module that defines each top-level command, as a submodule of this package.
#: Running a command imports only its own module, so ``hook-check``, which runs on
#: every push, does not pay for importing every other command.
COMMAND_MODULES: dict[str, str] = {
    **dict.fromkeys(
        (
            "start",
            "context",
            "submit-findings",
            "review",
            "respond",
            "events",
            "mergeback",
            "gate",
            "push",
            "finish",
            "stage",
            "logs",
            "abort",
            "gc",
            "status",
        ),
        "cli_runs",
    ),
    **dict.fromkeys(("verify", "approval-check", "hook-check"), "cli_policy"),
    **dict.fromkeys(("integrations", "init"), "cli_integrations"),
    "telemetry": "cli_telemetry",
    "report": "cli_report",
    "reviewer": "cli_reviewer",
}


class LazyGroup(EnvelopeGroup):
    """The root group, which imports a command's module when the command is resolved.

    Reading :attr:`commands`, as help and the tests do, imports every module, so it
    always lists every command. An import that fails raises; it never hides a command.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._commands: dict[str, click.Command] = {}
        self._loaded: set[str] = set()
        super().__init__(*args, **kwargs)

    @property
    def commands(self) -> MutableMapping[str, click.Command]:
        for module in dict.fromkeys(COMMAND_MODULES.values()):
            self._load(module)
        return self._commands

    @commands.setter
    def commands(self, value: MutableMapping[str, click.Command]) -> None:
        self._commands = dict(value)

    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        name = name or cmd.name
        if name is None:
            raise TypeError("Command has no name.")
        self._commands[name] = cmd

    def _load(self, module: str) -> None:
        if module in self._loaded:
            return
        importlib.import_module(f"{__package__}.{module}").register(self)
        self._loaded.add(module)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        module = COMMAND_MODULES.get(cmd_name)
        if module is not None:
            self._load(module)
        return self._commands.get(cmd_name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(self.commands)


@click.group(cls=LazyGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="agentic-preflight")
@click.option("--run", "run_id", default=None, help="Operate on a specific stored run.")
@click.pass_context
def main(ctx: click.Context, run_id: str | None) -> None:
    """Agent-driven quality gate. Every command prints one JSON object.

    The exceptions are hook-check, which Git consumes, and reviewer run, whose stdout
    review run reads as the review submission.
    """
    _use_utf8_streams()
    # The reviewer never runs Git, and a version envelope on its stdout would be read as
    # the review submission, so it keeps the example wrappers' independence from Git.
    if ctx.invoked_subcommand not in NO_GIT_CHECK:
        try:
            gitx.require_minimum_version()
        except gitx.GitVersionError as exc:
            fail(as_error("unsupported_git_version", str(exc), ExitCode.ERROR))
    if run_id is not None and RUN_ID_PATTERN.fullmatch(run_id) is None:
        fail(
            as_error(
                "invalid_run_id",
                f"--run must look like r_ followed by 10 lowercase hex digits, got {run_id!r}",
                ExitCode.USAGE_ERROR,
            )
        )
    ctx.ensure_object(dict)
    ctx.obj["run_id"] = run_id


if __name__ == "__main__":
    main()
