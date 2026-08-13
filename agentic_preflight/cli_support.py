"""Shared stdout and error boundary for every JSON CLI command."""

from __future__ import annotations

import sys
import traceback
from functools import wraps

from .config import ConfigError
from .envelope import Envelope, ExitCode, emit
from .errors import AgenticError
from .gitx import GitError
from .worktree import CopiedFileInCommit, CopyRefused, WorktreeError


def finish(envelope: Envelope, code: int = ExitCode.OK) -> None:
    emit(envelope)
    sys.exit(int(code))


def fail(exc: AgenticError) -> None:
    finish(exc.to_envelope(), exc.exit_code)


def command(fn):
    """Wrap a command body so every failure still emits a valid envelope."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AgenticError as exc:
            fail(exc)
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
