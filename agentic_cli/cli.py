"""Argument parsing and envelope emission. No logic lives here.

Every command follows the identical shape: build a session, call into
``runs.py``, emit exactly one JSON envelope, exit with the mapped code. Keeping
this file mechanical is what makes the stdout contract easy to guarantee — there
is only one place that writes to stdout, and it writes only envelopes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import runs
from .config import ConfigError
from .envelope import Envelope, ExitCode, emit, error_envelope
from .errors import AgenticError
from .gitx import GitError
from .worktree import CopiedFileInCommit, CopyRefused, WorktreeError


def _finish(envelope: Envelope, code: int = ExitCode.OK) -> None:
    emit(envelope)
    sys.exit(int(code))


def _fail(exc: AgenticError) -> None:
    _finish(
        error_envelope(
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            data=exc.data,
            blocking=exc.blocking,
            state=exc.state,
            run_id=exc.run_id,
            stage=exc.stage,
            next_instruction=exc.next_instruction,
            next_command=exc.next_command,
        ),
        exc.exit_code,
    )


def command(fn):
    """Wrap a command body so every failure still emits a valid envelope.

    An agent that gets a traceback on stdout has lost the contract entirely, so
    even an internal error is reported as JSON.
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AgenticError as exc:
            _fail(exc)
        except CopyRefused as exc:
            _fail(
                _as_error(
                    "copy_refused", str(exc), ExitCode.PRECONDITION,
                    "Add the file to .gitignore and commit that, then start again.",
                    "git status",
                )
            )
        except CopiedFileInCommit as exc:
            _fail(
                _as_error(
                    "copied_file_in_commit", str(exc), ExitCode.PRECONDITION,
                    "Rewrite the commit without that file, then retry.",
                    "agentic-cli status",
                )
            )
        except (WorktreeError, GitError) as exc:
            _fail(_as_error("git_error", str(exc), ExitCode.USAGE))
        except ConfigError as exc:
            _fail(_as_error("config_error", str(exc), ExitCode.USAGE))

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _as_error(code, message, exit_code, instruction=None, next_command=None) -> AgenticError:
    err = AgenticError(message, next_instruction=instruction, next_command=next_command)
    err.code = code
    err.exit_code = exit_code
    return err


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="agentic-cli")
def main() -> None:
    """Agent-driven quality gate. Every command prints one JSON object."""


@main.command()
@click.option("--base-ref", default=None, help="Branch to diff against (default: config).")
@command
def start(base_ref: str | None) -> None:
    """Create a run and its disposable worktree."""
    session = runs.open_session()
    _finish(runs.start(session, base_ref=base_ref))


@main.command()
@click.option("--section", default="review", type=click.Choice(["review", "docs"]))
@command
def context(section: str) -> None:
    """Return the material the agent needs to judge this stage."""
    session = runs.open_session()
    _finish(runs.context(session, section=section))


@main.command("submit-findings")
@click.option(
    "--file",
    "file_path",
    required=True,
    help="Path to a findings JSON file, or - for stdin.",
)
@command
def submit_findings(file_path: str) -> None:
    """Record the agent's findings for the active stage."""
    raw = sys.stdin.read() if file_path == "-" else Path(file_path).read_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(_as_error("invalid_findings", f"findings file is not valid JSON: {exc}",
                        ExitCode.PRECONDITION))
        return
    session = runs.open_session()
    _finish(runs.submit_findings(session, payload))


@main.command()
@command
def verify() -> None:
    """Confirm nothing blocks the active stage."""
    session = runs.open_session()
    _finish(runs.verify(session))


@main.command()
@command
def status() -> None:
    """Where the run is and what to do next. Legal in every state."""
    session = runs.open_session()
    _finish(runs.status(session))


if __name__ == "__main__":
    main()
