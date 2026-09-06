"""Click adapters for attestations, approval policy, and the Git hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import click

from . import runs
from .cli_support import command, finish, finish_locked
from .envelope import Envelope, ExitCode
from .errors import AgenticError, AttestationFailed, NeedsHuman
from .gitx import GitError
from .models import Stage


@click.command()
@click.argument("sha", required=False)
@click.option("--purpose", type=click.Choice(["local", "publish"]), default="local")
@command
def verify(sha: str | None, purpose: Literal["local", "publish"]) -> None:
    """Confirm an active review stage, or validate SHA's Git-note attestation."""
    if sha is not None:
        from . import attestation as attestationmod
        from . import gitx

        repo_root = gitx.repo_root(Path.cwd())
        try:
            value = attestationmod.verify(repo_root, sha, purpose=purpose)
        except (attestationmod.InvalidAttestation, GitError) as exc:
            if isinstance(exc, attestationmod.DelegatedTestsPending):
                raise AttestationFailed(
                    str(exc),
                    data={
                        "purpose": purpose,
                        "test_status": "delegated_pending",
                        "merge_requirements_satisfied": False,
                    },
                    next_instruction=(
                        "Retrieve current trusted CI evidence with "
                        "agentic-preflight ci status --repo OWNER/REPO --pr N, "
                        "replacing OWNER/REPO and N with the repository and PR number. "
                        "Do not repeat local review merely because tests are pending."
                    ),
                ) from exc
            raise _evidence_failure(
                exc, {"sha": sha, "notes_ref": attestationmod.NOTES_REF}
            ) from exc
        finish(
            Envelope(
                data={
                    "verified": True,
                    "purpose": purpose,
                    "test_status": value.stages[Stage.TEST].status,
                    "merge_requirements_satisfied": False,
                    "sha": value.sha,
                    "tree_sha": value.tree_sha,
                    "notes_ref": attestationmod.NOTES_REF,
                    "attestation": value.model_dump(mode="json"),
                }
            )
        )
        return
    finish_locked(runs.verify)


def _evidence_failure(exc: Exception, data: dict) -> AttestationFailed:
    from .attestation import InvalidAttestation, recovery

    reason = exc.reason if isinstance(exc, InvalidAttestation) else "git_failure"
    message = str(exc) if isinstance(exc, InvalidAttestation) else "Git evidence read failed"
    return AttestationFailed(
        message, data={**data, "reason": reason}, next_instruction=recovery(reason)
    )


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
    from . import attestation as attestationmod
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
    except (attestationmod.InvalidAttestation, GitError) as exc:
        raise _evidence_failure(exc, {"sha": sha, "base_sha": base_sha}) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise AttestationFailed(
            f"cannot evaluate human approval: {exc}",
            data={"sha": sha, "base_sha": base_sha},
        ) from exc
    _finish_approval(result, report_only=report_only)


def _finish_approval(result: dict, *, report_only: bool) -> None:
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


@click.command("hosted-check")
@click.argument("sha")
@click.option("--base", "base_sha", required=True, help="Original protected event-base SHA.")
@click.option("--source-remote", required=True, help="Configured source remote (including forks).")
@click.option("--head-ref", required=True, help="Full source refs/heads/... from the event.")
@click.option("--mode", type=click.Choice(["verify", "approval"]), default="verify")
@click.option("--reviews-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--author")
@click.option("--environment-approved", is_flag=True)
@click.option("--report-only", is_flag=True)
@command
def hosted_check(
    sha: str,
    base_sha: str,
    source_remote: str,
    head_ref: str,
    mode: str,
    reviews_file: Path | None,
    author: str | None,
    environment_approved: bool,
    report_only: bool,
) -> None:
    """Retry remote note absence, then verify/evaluate one pinned notes snapshot."""
    from . import approval, gitx, note_availability
    from .config import load_config
    from .models import Attestation

    repo = gitx.repo_root(Path.cwd())
    reviews: object = []
    if mode == "approval":
        if reviews_file is None or author is None:
            raise AgenticError("approval mode requires --reviews-file and --author")
        try:
            reviews = json.loads(reviews_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise AttestationFailed(
                "Cannot read reviews JSON", data={"reason": "invalid_reviews"}
            ) from exc

    def evaluate(value: Attestation) -> dict:
        if mode == "verify":
            return {
                "verified": True,
                "sha": value.sha,
                "tree_sha": value.tree_sha,
                "merge_requirements_satisfied": False,
            }
        return approval.evaluate_value(
            repo,
            value=value,
            cfg=load_config(repo),
            base_sha=base_sha,
            head_sha=sha,
            reviews=reviews,
            pull_request_author=author or "",
            environment_approved=environment_approved,
        )

    try:
        result = note_availability.check(
            repo,
            remote=source_remote,
            head_ref=head_ref,
            expected_head=sha,
            base_sha=base_sha,
            evaluate=evaluate,
        )
    except note_availability.HostedCheckFailed as exc:
        raise _evidence_failure(
            exc, {"sha": sha, "base_sha": base_sha, "availability": exc.diagnostics}
        ) from exc
    except ValueError as exc:
        raise AttestationFailed(
            "Invalid hosted-check candidate or policy inputs", data={"reason": "invalid_inputs"}
        ) from exc
    if mode == "approval":
        _finish_approval(result, report_only=report_only)
    else:
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
        attestationmod.verify(repo_root, sha, purpose="publish")
    except (attestationmod.InvalidAttestation, GitError):
        return False
    return True


COMMANDS = (verify, approval_check, hosted_check, hook_check)


def register(group: click.Group) -> None:
    for cli_command in COMMANDS:
        group.add_command(cli_command)
