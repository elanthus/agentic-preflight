"""Explicit publication-independent CI retrieval and protected workflow commands."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import click

from . import ci_authority, ci_checks, ci_merge, gitx
from .cli_support import command, finish
from .envelope import Envelope, ExitCode
from .errors import AttestationFailed
from .github_api import GitHub


@click.group()
def ci() -> None:
    """Delegate tests to protected GitHub Actions and retrieve live merge readiness."""


@ci.command("status")
@click.option("--repo", "repository", required=True)
@click.option("--pr", type=click.IntRange(min=1), required=True)
@command
def status(repository: str, pr: int) -> None:
    result = ci_merge.evaluate(gitx.repo_root(Path.cwd()), GitHub(repository), pr)
    instruction, next_command = ci_merge.next_action(result)
    ready = result["merge_requirements_satisfied"]
    finish(
        Envelope(ok=ready, data=result, next_instruction=instruction, next_command=next_command),
        code=ExitCode.OK if ready else ExitCode.PRECONDITION,
    )


@ci.command("dispatch")
@click.option("--repo", "repository", required=True)
@click.option("--pr", type=click.IntRange(min=1), required=True)
@click.option("--force", is_flag=True, help="Request a new complete run for the same candidate.")
@command
def dispatch(repository: str, pr: int, force: bool) -> None:
    try:
        result = ci_authority.dispatch(GitHub(repository), pr, force=force)
    except ValueError as exc:
        raise AttestationFailed(
            str(exc), next_command=f"agentic-preflight ci dispatch --repo {repository} --pr {pr}"
        ) from exc
    finish(
        Envelope(
            data={**result, "purpose": "publish", "merge_requirements_satisfied": False},
            next_command=f"agentic-preflight ci status --repo {repository} --pr {pr}",
        )
    )


@ci.command("prepare")
@click.option("--repo", "repository", required=True)
@click.option("--candidate", required=True)
@click.option("--candidate-id", required=True)
@click.option("--workflow-sha", required=True)
@command
def prepare(repository: str, candidate: str, candidate_id: str, workflow_sha: str) -> None:
    try:
        result = ci_authority.prepare(
            GitHub(repository), candidate, workflow_sha=workflow_sha, candidate_id=candidate_id
        )
    except ValueError as exc:
        raise AttestationFailed(str(exc)) from exc
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        from .ci_policy import parse_policy

        policy = parse_policy(GitHub(repository).contents(".agentic-preflight.toml", workflow_sha))
        if any(char in policy.approval.environment for char in "\r\n"):
            raise AttestationFailed("approval environment cannot contain newlines")
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"merge_sha={result.merge_sha}\n")
            handle.write(f"approval_mode={policy.approval.mode}\n")
            handle.write(f"approval_environment={policy.approval.environment}\n")
    finish(Envelope(data={"candidate": result.model_dump(), "validated": True}))


@ci.command("reconcile")
@click.option("--repo", "repository", required=True)
@click.option("--pr", type=click.IntRange(min=1))
@click.option("--check-app-id", type=click.IntRange(min=1), required=True)
@command
def reconcile(repository: str, pr: int | None, check_app_id: int) -> None:
    try:
        results = ci_checks.reconcile(
            gitx.repo_root(Path.cwd()), GitHub(repository), check_app_id=check_app_id, pr=pr
        )
    except ValueError as exc:
        raise AttestationFailed(str(exc)) from exc
    finish(Envelope(data={"checks": results, "purpose": "merge"}))


@ci.command("templates")
@click.option("--directory", type=click.Path(path_type=Path), required=True)
@command
def templates(directory: Path) -> None:
    """Write workflow templates for protected-base installation; never overwrite."""
    source = files("agentic_preflight").joinpath("templates", "ci")
    names = ("preflight-tests.yml", "preflight-ci.yml")
    if any((directory / name).exists() for name in names):
        raise AttestationFailed("workflow destination already exists; inspect it before replacing")
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        content = source.joinpath(name).read_text(encoding="utf-8")
        try:
            with (directory / name).open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise AttestationFailed(
                "workflow destination already exists; inspect it before replacing"
            ) from exc
    finish(
        Envelope(
            data={"written": [str(directory / name) for name in names]},
            next_instruction="Review the templates, configure the trusted matrix and repository/workflow IDs, and install consumers on the protected base before enabling producers.",
        )
    )
