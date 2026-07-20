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
    _finish(
        runs.respond(
            session, finding_id=finding_id, action=action, commit=commit, note=note
        )
    )


@main.command()
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@command
def events(limit: int | None) -> None:
    """The run's history, oldest first."""
    session = runs.open_session()
    _finish(runs.events(session, limit=limit))


@main.command()
@click.option("--force", is_flag=True, help="Replace an existing pre-push hook.")
@click.option("--no-hook", is_flag=True, help="Write config only, skip the hook.")
@command
def init(force: bool, no_hook: bool) -> None:
    """Install the pre-push hook and seed .agentic-cli.toml."""
    from . import gitx, initcmd

    repo_root = gitx.repo_root(Path.cwd())
    try:
        _finish(initcmd.init(repo_root, force=force, install_hook=not no_hook))
    except FileExistsError as exc:
        _fail(
            _as_error(
                "hook_exists",
                f"a pre-push hook already exists at {exc} and was not written by "
                f"agentic-cli; refusing to replace it",
                ExitCode.PRECONDITION,
                "Inspect the existing hook. Re-run with --force to replace it, or "
                "merge the `agentic-cli hook-check` call into it by hand.",
                "agentic-cli init --force",
            )
        )


@main.command("hook-check")
def hook_check() -> None:
    """Pre-push predicate over the ledger. Reads stdin, writes prose to stderr.

    Deliberately not wrapped in the envelope contract: its consumer is git, not
    the agent, and git judges it by exit code alone.
    """
    from . import gitx, hook as hookmod
    from .config import load_config
    from .store import Store

    raw = sys.stdin.read()
    updates = hookmod.parse_stdin(raw)
    if not updates:
        sys.exit(int(ExitCode.OK))

    try:
        repo_root = gitx.repo_root(Path.cwd())
        store = Store(gitx.git_common_dir(Path.cwd()) / runs.STATE_DIR_NAME)
        ledger = store.load_ledger()
        allow_force = load_config(repo_root).hook.allow_force_push
    except Exception as exc:  # never brick a repo over our own failure
        sys.stderr.write(f"agentic-cli: hook check unavailable ({exc}); allowing push\n")
        sys.exit(int(ExitCode.OK))

    decision = hookmod.evaluate(
        ledger,
        updates,
        is_ancestor=lambda a, b: gitx.is_ancestor(repo_root, a, b),
        allow_force_push=allow_force,
    )
    if decision.allowed:
        sys.exit(int(ExitCode.OK))

    sys.stderr.write(decision.message + "\n")
    sys.exit(int(ExitCode.HOOK_BLOCK))


@main.command()
@command
def mergeback() -> None:
    """Cherry-pick verified fixes onto your branch. Never auto-resolves."""
    session = runs.open_session()
    _finish(runs.mergeback(session))


@main.command()
@command
def gate() -> None:
    """Summarise what would be pushed and mint a confirmation token."""
    session = runs.open_session()
    _finish(runs.gate(session))


@main.command()
@click.option("--confirm", default=None, help="Token minted by `gate`.")
@click.option("--dry-run", is_flag=True, help="Report what would be pushed, push nothing.")
@command
def push(confirm: str | None, dry_run: bool) -> None:
    """Push the verified branch. Requires the gate token."""
    session = runs.open_session()
    _finish(runs.push(session, confirm=confirm, dry_run=dry_run))


@main.command()
@click.option("--draft/--no-draft", default=None, help="Override [publish] draft_pr.")
@command
def pr(draft: bool | None) -> None:
    """Open a pull request via the gh CLI. No credentials are ever handled here."""
    session = runs.open_session()
    _finish(runs.pull_request(session, draft=draft))


@main.group()
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
    _finish(
        runs.run_stage(
            session, name, command=command_str, record=record, baseline=baseline
        )
    )


@main.command()
@click.option("--stage", "stage_name", required=True, type=click.Choice(["lint", "test"]))
@command
def logs(stage_name: str) -> None:
    """The full captured output of a stage."""
    session = runs.open_session()
    _finish(runs.logs(session, stage_name=stage_name))


@main.command()
@click.option("--force", is_flag=True, help="Discard unmerged fix commits.")
@command
def abort(force: bool) -> None:
    """End the run and reclaim its worktree."""
    session = runs.open_session()
    _finish(runs.abort(session, force=force))


@main.command()
@click.option("--force", is_flag=True, help="Remove even when work would be lost.")
@command
def gc(force: bool) -> None:
    """Reconcile run directories, git worktrees, and ac/* branches."""
    session = runs.open_session()
    _finish(runs.gc(session, force=force))


@main.command()
@command
def status() -> None:
    """Where the run is and what to do next. Legal in every state."""
    session = runs.open_session()
    _finish(runs.status(session))


if __name__ == "__main__":
    main()
