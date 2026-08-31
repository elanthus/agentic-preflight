"""Click adapters for attestations, approval policy, and the Git hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import runs
from .cli_support import command, finish, finish_locked
from .envelope import Envelope, ExitCode
from .errors import AttestationFailed, NeedsHuman
from .gitx import GitError


@click.command()
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
        finish(
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
    finish_locked(runs.verify)


@click.command("approval-check")
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
        reviews = json.loads(reviews_file.read_text(encoding="utf-8"))
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
        raise NeedsHuman(message, data=result, next_instruction=instruction)
    finish(Envelope(data=result))


@click.command("hook-check")
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


COMMANDS = (verify, approval_check, hook_check)


def register(group: click.Group) -> None:
    for cli_command in COMMANDS:
        group.add_command(cli_command)
