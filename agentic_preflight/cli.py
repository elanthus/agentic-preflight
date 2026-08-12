"""Argument parsing and envelope emission. No logic lives here.

Every command follows the identical shape: build a session, call into
``runs.py``, emit exactly one JSON envelope, exit with the mapped code. Keeping
this file mechanical is what makes the stdout contract easy to guarantee — there
is only one place that writes to stdout, and it writes only envelopes.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import TypedDict

import click

from . import runs
from .config import ConfigError
from .envelope import Envelope, ExitCode, emit
from .errors import AgenticError, AttestationFailed, NeedsHuman
from .gitx import GitError
from .integrations import SUPPORTED_INTEGRATIONS, IntegrationOperation
from .worktree import CopiedFileInCommit, CopyRefused, WorktreeError

INTEGRATION_NAMES = tuple(SUPPORTED_INTEGRATIONS)


def _finish(envelope: Envelope, code: int = ExitCode.OK) -> None:
    emit(envelope)
    sys.exit(int(code))


def _fail(exc: AgenticError) -> None:
    _finish(exc.to_envelope(), exc.exit_code)


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
                    "copy_refused",
                    str(exc),
                    ExitCode.PRECONDITION,
                    "Add the file to .gitignore and commit that, then start again.",
                    "git status",
                )
            )
        except CopiedFileInCommit as exc:
            _fail(
                _as_error(
                    "copied_file_in_commit",
                    str(exc),
                    ExitCode.PRECONDITION,
                    "Rewrite the commit without that file, then retry.",
                    "agentic-preflight status",
                )
            )
        except (WorktreeError, GitError) as exc:
            _fail(_as_error("git_error", str(exc), ExitCode.USAGE))
        except ConfigError as exc:
            _fail(_as_error("config_error", str(exc), ExitCode.USAGE))
        except Exception:  # noqa: BLE001 - the JSON stdout contract is the boundary
            traceback.print_exc(file=sys.stderr)
            _fail(
                _as_error(
                    "internal_error",
                    "an unexpected internal error occurred",
                    ExitCode.USAGE,
                )
            )

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _as_error(code, message, exit_code, instruction=None, next_command=None) -> AgenticError:
    err = AgenticError(message, next_instruction=instruction, next_command=next_command)
    err.code = code
    err.exit_code = exit_code
    return err


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="agentic-preflight")
def main() -> None:
    """Agent-driven quality gate. Every command prints one JSON object."""


@main.command()
@click.option("--base-ref", default=None, help="Branch to diff against (default: config).")
@click.option(
    "--intent",
    default=None,
    help="The user's objective and acceptance criteria, in their own terms.",
)
@command
def start(base_ref: str | None, intent: str | None) -> None:
    """Create a run and prepare its validation checkout."""
    session = runs.open_session()
    _finish(runs.start(session, base_ref=base_ref, intent=intent))


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
        _fail(
            _as_error(
                "invalid_findings", f"findings file is not valid JSON: {exc}", ExitCode.PRECONDITION
            )
        )
        return
    session = runs.open_session()
    _finish(runs.submit_findings(session, payload))


@main.group()
def review() -> None:
    """Independent review execution."""


@review.command("run")
@command
def review_run() -> None:
    """Run the configured reviewer over the current review bundle."""
    session = runs.open_session()
    _finish(runs.run_review_command(session))


@main.command()
@click.argument("sha", required=False)
@command
def verify(sha: str | None) -> None:
    """Confirm an active review stage, or validate SHA's Git-note attestation."""
    if sha is not None:
        from . import attestation as attestationmod
        from . import gitx

        repo_root = gitx.repo_root(Path.cwd())
        try:
            value = attestationmod.verify(repo_root, sha)
        except (attestationmod.InvalidAttestation, GitError) as exc:
            raise AttestationFailed(
                str(exc),
                data={"sha": sha, "notes_ref": attestationmod.NOTES_REF},
                next_instruction=(
                    "Fetch refs/notes/agentic-preflight from the remote if CI does not "
                    "have it; otherwise run a fresh preflight for this exact commit."
                ),
                next_command=(
                    "git fetch origin refs/notes/agentic-preflight:refs/notes/agentic-preflight"
                ),
            ) from exc
        _finish(
            Envelope(
                data={
                    "verified": True,
                    "sha": value.sha,
                    "tree_sha": value.tree_sha,
                    "notes_ref": attestationmod.NOTES_REF,
                    "attestation": value.model_dump(mode="json"),
                }
            )
        )
        return
    session = runs.open_session()
    _finish(runs.verify(session))


@main.command("approval-check")
@click.argument("sha")
@click.option("--base", "base_sha", required=True, help="Protected pull-request base SHA.")
@click.option(
    "--reviews-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="JSON returned by GitHub's pull-request reviews API.",
)
@click.option("--author", required=True, help="Pull-request author's GitHub login.")
@click.option(
    "--environment-approved",
    is_flag=True,
    help="Record that the configured GitHub Environment gate released this job.",
)
@click.option(
    "--report-only",
    is_flag=True,
    help="Report policy state without failing while a conditional approval job is pending.",
)
@command
def approval_check(
    sha: str,
    base_sha: str,
    reviews_file: Path,
    author: str,
    environment_approved: bool,
    report_only: bool,
) -> None:
    """Enforce configured merge handling when the attested change is high-risk."""
    from . import approval as approvalmod
    from . import gitx

    try:
        reviews = json.loads(reviews_file.read_text())
        result = approvalmod.evaluate(
            gitx.repo_root(Path.cwd()),
            base_sha=base_sha,
            head_sha=sha,
            reviews=reviews,
            pull_request_author=author,
            environment_approved=environment_approved,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise AttestationFailed(
            f"cannot evaluate human approval: {exc}",
            data={"sha": sha, "base_sha": base_sha},
        ) from exc
    if result["requires_human_approval"] and not result["approved"] and not report_only:
        mode = result["approval_mode"]
        if mode == "environment":
            message = (
                "high-risk pull request requires approval through GitHub Environment "
                f"{result['approval_environment']!r}"
            )
            instruction = (
                "Approve the waiting environment deployment for the exact current head, "
                "then rerun this check with --environment-approved."
            )
        else:
            message = (
                "high-risk pull request requires an eligible human approval for its exact head"
            )
            instruction = (
                "Ask an eligible human other than the pull-request author to review and "
                "approve the current head, then rerun this check."
            )
        raise NeedsHuman(
            message,
            data=result,
            next_instruction=instruction,
        )
    _finish(Envelope(data=result))


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
    _finish(runs.respond(session, finding_id=finding_id, action=action, commit=commit, note=note))


@main.command()
@click.option("--limit", type=int, default=None, help="Show only the last N events.")
@command
def events(limit: int | None) -> None:
    """The run's history, oldest first."""
    session = runs.open_session()
    _finish(runs.events(session, limit=limit))


@main.group()
def integrations() -> None:
    """Install the bundled skill into supported coding agents."""


def _integration_options(*, target_help: str, force_help: str | None = None):
    """Shared Click surface for every integration lifecycle command."""

    def decorate(fn):
        if force_help is not None:
            fn = click.option("--force", is_flag=True, help=force_help)(fn)
        fn = click.option(
            "--target",
            "targets",
            multiple=True,
            type=click.Path(path_type=Path, file_okay=False),
            help=target_help,
        )(fn)
        fn = click.option(
            "--scope",
            type=click.Choice(["user", "project"]),
            default="user",
            show_default=True,
            help="Use this user's skills directory or the current repository.",
        )(fn)
        return click.argument("agents", nargs=-1, type=click.Choice(INTEGRATION_NAMES))(fn)

    return decorate


def _integration_project_root(scope: str) -> Path | None:
    if scope != "project":
        return None
    from . import gitx

    return gitx.repo_root(Path.cwd())


def _require_integration_targets(agents: tuple[str, ...], targets: tuple[Path, ...]) -> None:
    if agents or targets:
        return
    _fail(
        _as_error(
            "integration_target_required",
            "name at least one integration, or pass --target with a skills directory",
            ExitCode.USAGE,
        )
    )


def _integration_envelope(scope: str, results: list[dict]) -> Envelope:
    lines = [
        f"{item['integration']}: {item.get('action', item['status'])} ({item['path']})"
        for item in results
    ]
    if any(item.get("action") in {"installed", "updated", "replaced"} for item in results):
        lines.append("Restart a running agent if the skill does not appear automatically.")
    return Envelope(
        data={"scope": scope, "integrations": results},
        human="\n".join(lines),
    )


class _IntegrationCommandSpec(TypedDict):
    help: str
    target_help: str
    force_help: str | None
    targets_required: bool


_INTEGRATION_COMMANDS: dict[IntegrationOperation, _IntegrationCommandSpec] = {
    IntegrationOperation.INSTALL: {
        "help": "Install or refresh the skill for AGENTS.",
        "target_help": "Also install under this custom skills directory.",
        "force_help": "Replace unmanaged or locally modified copies.",
        "targets_required": True,
    },
    IntegrationOperation.STATUS: {
        "help": "Report whether installed skills are current or modified.",
        "target_help": "Also inspect this custom skills directory.",
        "force_help": None,
        "targets_required": False,
    },
    IntegrationOperation.UPDATE: {
        "help": "Update installed skills, skipping integrations that are absent.",
        "target_help": "Also update under this custom skills directory.",
        "force_help": "Replace unmanaged or locally modified copies.",
        "targets_required": False,
    },
    IntegrationOperation.UNINSTALL: {
        "help": "Remove agentic-preflight-managed skill copies for AGENTS.",
        "target_help": "Also remove from this custom skills directory.",
        "force_help": "Remove unmanaged or locally modified copies.",
        "targets_required": True,
    },
}


def _integration_lifecycle_command(
    operation: IntegrationOperation, spec: _IntegrationCommandSpec
) -> click.Command:
    """Build the identical Click/result-shaping shell around one operation."""

    def invoke(
        agents: tuple[str, ...],
        scope: str,
        targets: tuple[Path, ...],
        force: bool = False,
    ) -> None:
        from . import integrations as integrations_module

        if spec["targets_required"]:
            _require_integration_targets(agents, targets)
        selected = agents or (() if targets else INTEGRATION_NAMES)
        results = integrations_module.manage_integrations(
            operation,
            selected,
            scope=scope,
            custom_roots=targets,
            force=force,
            project_root=_integration_project_root(scope),
        )
        _finish(_integration_envelope(scope, results))

    invoke.__name__ = f"integrations_{operation.value}"
    invoke.__doc__ = spec["help"]
    decorated = command(invoke)
    decorated = _integration_options(
        target_help=spec["target_help"], force_help=spec["force_help"]
    )(decorated)
    return click.command(operation.value)(decorated)


for _operation, _spec in _INTEGRATION_COMMANDS.items():
    integrations.add_command(_integration_lifecycle_command(_operation, _spec))
del _operation, _spec


@main.command()
@click.option("--force", is_flag=True, help="Replace an existing pre-push hook.")
@click.option("--no-hook", is_flag=True, help="Write config only, skip the hook.")
@command
def init(force: bool, no_hook: bool) -> None:
    """Install the pre-push hook and seed .agentic-preflight.toml."""
    from . import gitx, initcmd

    repo_root = gitx.repo_root(Path.cwd())
    try:
        _finish(initcmd.init(repo_root, force=force, install_hook=not no_hook))
    except FileExistsError as exc:
        _fail(
            _as_error(
                "hook_exists",
                f"a pre-push hook already exists at {exc} and was not written by "
                f"agentic-preflight; refusing to replace it",
                ExitCode.PRECONDITION,
                "Inspect the existing hook. Re-run with --force to replace it, or "
                "merge the `agentic-preflight hook-check` call into it by hand.",
                "agentic-preflight init --force",
            )
        )


@main.command("hook-check")
def hook_check() -> None:
    """Pre-push predicate over commit attestations. Reads stdin, writes prose to stderr.

    Deliberately not wrapped in the envelope contract: its consumer is git, not
    the agent, and git judges it by exit code alone.
    """
    from . import gitx
    from . import hook as hookmod
    from .config import load_config

    raw = sys.stdin.read()
    updates = hookmod.parse_stdin(raw)
    if not updates:
        sys.exit(int(ExitCode.OK))

    try:
        repo_root = gitx.repo_root(Path.cwd())
        allow_force = load_config(repo_root).hook.allow_force_push
    except Exception as exc:  # noqa: BLE001 - never brick a repo over our own failure
        sys.stderr.write(f"agentic-preflight: hook check unavailable ({exc}); allowing push\n")
        sys.exit(int(ExitCode.OK))

    decision = hookmod.evaluate(
        updates,
        is_ancestor=lambda a, b: gitx.is_ancestor(repo_root, a, b),
        has_attestation=lambda sha: _has_valid_attestation(repo_root, sha),
        allow_force_push=allow_force,
    )
    if decision.allowed:
        sys.exit(int(ExitCode.OK))

    sys.stderr.write(decision.message + "\n")
    sys.exit(int(ExitCode.HOOK_BLOCK))


def _has_valid_attestation(repo_root: Path, sha: str) -> bool:
    from . import attestation as attestationmod

    try:
        attestationmod.verify(repo_root, sha)
    except (attestationmod.InvalidAttestation, GitError):
        return False
    return True


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
@command
def finish() -> None:
    """Mark a pushed validation run complete."""
    session = runs.open_session()
    _finish(runs.finish(session))


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
    _finish(runs.run_stage(session, name, command=command_str, record=record, baseline=baseline))


@main.command()
@click.option("--stage", "stage_name", required=True, type=click.Choice(["review", "lint", "test"]))
@command
def logs(stage_name: str) -> None:
    """The full captured output of a stage."""
    session = runs.open_session()
    _finish(runs.logs(session, stage_name=stage_name))


@main.command()
@click.option("--force", is_flag=True, help="Discard unmerged fix commits.")
@command
def abort(force: bool) -> None:
    """End the run and release its validation worktree."""
    session = runs.open_session()
    _finish(runs.abort(session, force=force))


@main.command()
@click.option("--force", is_flag=True, help="Remove even when work would be lost.")
@command
def gc(force: bool) -> None:
    """Reconcile run directories, git worktrees, and ap/* branches."""
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
