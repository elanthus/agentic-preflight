"""Shared stdout and error boundary for every JSON CLI command."""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

import click

from .config import ConfigError
from .envelope import Envelope, ExitCode, emit
from .errors import AgenticError, SourceWorktreeMissing
from .gitx import GitError
from .store import RUN_READ_RECOVERY, RunReadError
from .worktree import CopiedFileInCommit, CopyRefused, WorktreeError


def finish(envelope: Envelope, code: int = ExitCode.OK) -> None:
    emit(envelope)
    sys.exit(int(code))


def fail(exc: AgenticError) -> None:
    finish(exc.to_envelope(), exc.exit_code)


def selected_run_id() -> str | None:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return None
    root = ctx.find_root()
    return (root.obj or {}).get("run_id")


def open_cli_session():
    from . import runs

    return runs.open_session(run_id=selected_run_id())


def finish_locked(callback: Callable[[Any], Envelope]) -> None:
    """Run one mutating command under its durable per-run operation lock."""
    session = open_cli_session()
    run_id = session.active_run_id()
    if run_id is None or not session.store.run_path(run_id).exists():
        finish(callback(session))
        return
    if not session.source_worktree_available:
        run = session.store.load_run(run_id)
        raise SourceWorktreeMissing(
            f"source worktree for run {run_id} no longer exists",
            state=run.state.value,
            run_id=run_id,
            data={"source_worktree_path": run.source_worktree_path},
            next_instruction=(
                "Inspect the preserved run with `status` or `events`, then run `gc` "
                "from another worktree in the same clone to reconcile it."
            ),
            next_command="agentic-preflight gc",
        )
    with session.store.operation(run_id):
        finish(callback(session))


#: Commands whose consumer is not the agent and which keep Click's own usage output.
RAW_COMMANDS = frozenset({"hook-check"})

#: Click 8.2 and later raise this for a bare group invocation; Click 8.1 has no such class.
_NO_ARGS_IS_HELP = getattr(click.exceptions, "NoArgsIsHelpError", None)


def _is_help_request(exc: click.UsageError) -> bool:
    return _NO_ARGS_IS_HELP is not None and isinstance(exc, _NO_ARGS_IS_HELP)


def fail_usage(exc: click.UsageError) -> None:
    """Emit a Click usage error as one JSON envelope with its own exit code."""
    message = exc.format_message()
    ctx = exc.ctx
    help_command = f"{ctx.command_path} --help" if ctx is not None else "agentic-preflight --help"
    fail(
        as_error(
            "usage_error",
            message,
            ExitCode.USAGE_ERROR,
            f"Fix the invocation: {message} Run `{help_command}` for valid usage.",
            help_command,
        )
    )


class EnvelopeGroup(click.Group):
    """Root group that routes Click usage errors through the JSON envelope."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except click.UsageError as exc:
            if _is_help_request(exc):
                raise
            fail_usage(exc)
            raise

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Print root help and exit 0 for a bare invocation on every Click version."""
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit(0)
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as exc:
            if _is_help_request(exc):
                raise
            if exc.ctx is not None and exc.ctx.command.name in RAW_COMMANDS:
                raise
            fail_usage(exc)
            raise


def _unreadable_input(message: str) -> AgenticError:
    return as_error(
        "invalid_findings",
        message,
        ExitCode.PRECONDITION,
        "Write the file as UTF-8 JSON at a readable path, then retry.",
    )


def read_json_file(file_path: str, label: str) -> Any:
    """Parse a JSON input file, or stdin for ``-``, mapping read failures to an envelope."""
    try:
        raw = sys.stdin.read() if file_path == "-" else Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable_input(f"{label} could not be read as UTF-8 text: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _unreadable_input(f"{label} is not valid JSON: {exc}") from exc


def command(fn):
    """Wrap a command body so every failure still emits a valid envelope."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AgenticError as exc:
            fail(exc)
        except RunReadError as exc:
            error = AgenticError(
                str(exc), run_id=exc.run_id, data=exc.details(), next_instruction=RUN_READ_RECOVERY
            )
            error.code = "run_record_unreadable"
            error.exit_code = ExitCode.PRECONDITION
            fail(error)
        except CopyRefused as exc:
            fail(
                as_error(
                    "copy_refused",
                    str(exc),
                    ExitCode.PRECONDITION,
                    "Add the file to .gitignore and commit that, then start again.",
                    "git status",
                )
            )
        except CopiedFileInCommit as exc:
            fail(
                as_error(
                    "copied_file_in_commit",
                    str(exc),
                    ExitCode.PRECONDITION,
                    "Rewrite the commit without that file, then retry.",
                    "agentic-preflight status",
                )
            )
        except (WorktreeError, GitError) as exc:
            fail(as_error("git_error", str(exc), ExitCode.USAGE))
        except ConfigError as exc:
            fail(as_error("config_error", str(exc), ExitCode.USAGE))
        except Exception:  # noqa: BLE001 - the JSON stdout contract is the boundary
            traceback.print_exc(file=sys.stderr)
            fail(
                as_error(
                    "internal_error",
                    "an unexpected internal error occurred",
                    ExitCode.USAGE,
                )
            )

    return wrapper


def as_error(
    code,
    message,
    exit_code,
    instruction=None,
    next_command=None,
) -> AgenticError:
    err = AgenticError(message, next_instruction=instruction, next_command=next_command)
    err.code = code
    err.exit_code = exit_code
    return err
