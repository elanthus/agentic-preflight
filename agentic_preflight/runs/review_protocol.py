"""Canonical wire protocol for human and command review executors.

This module is deliberately free of state transitions.  It defines the input
bundle every executor receives and parses the strict submission shape returned
by an executor.  Keeping those two paths together prevents command review from
quietly drifting away from in-harness review.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ValidationError

from .. import diff as diffmod
from .. import gitx
from .. import grounding as groundingmod
from ..digests import json_digest
from ..errors import InvalidFindings
from ..fingerprints import doc_surface
from ..models import DocsSubmission, FindingSubmission, ReviewSubmission, RunDoc, Stage
from ..stages import docs as docsstage
from ._session import Session, _require_worktree

ReviewExecutor = Literal["in_harness", "command"]


def _snapshot(session: Session, run: RunDoc, head: str | None) -> tuple[str, str]:
    """Return the checkout review inputs are read from and its current HEAD.

    Every memo key below starts with this pair. HEAD is resolved on each lookup
    unless the caller just resolved it, which keeps a key correct when the
    command itself moves HEAD, as `start` does when it rebases.
    """
    repo = str(run.worktree_path or session.repo_root)
    return repo, head if head is not None else gitx.rev_parse(repo, "HEAD")


def bundle_for(session: Session, run: RunDoc, *, head: str | None = None) -> diffmod.DiffBundle:
    """Build the exact diff snapshot shared by all review executors."""
    repo, head = _snapshot(session, run, head)
    exclude = session.config.diff.exclude
    return session.memoized(
        ("bundle", repo, head, run.merge_base_sha, tuple(exclude)),
        lambda: diffmod.build_bundle(repo, run.merge_base_sha, "HEAD", exclude=exclude),
    )


def grounding_for(
    session: Session, run: RunDoc, changed_files: list[str], *, head: str | None = None
) -> dict[str, Any]:
    """Assemble the grounding delivered with this run's snapshot once per command.

    Grounding reads the committed tree at HEAD, the run's identity and branch
    (for review history), and the effective configuration, so all of them are
    part of the key.
    """
    repo, head = _snapshot(session, run, head)
    key = (
        "grounding",
        repo,
        head,
        run.run_id,
        run.branch,
        tuple(changed_files),
        json_digest(session.config.model_dump(mode="json")),
    )
    return session.memoized(key, lambda: groundingmod.assemble(session, run, changed_files))


def grounded_manifest(
    session: Session,
    run: RunDoc,
    bundle: diffmod.DiffBundle,
    *,
    grounding: dict[str, Any] | None = None,
    head: str | None = None,
) -> diffmod.ReviewManifest:
    """Bind review coverage to the grounding delivered with this diff snapshot."""
    worktree_path = _require_worktree(run)
    _, head = _snapshot(session, run, head)
    review_grounding = (
        grounding if grounding is not None else grounding_for(session, run, bundle.files, head=head)
    )
    grounding_sha256 = groundingmod.digest(review_grounding)
    key = (
        "manifest",
        worktree_path,
        head,
        bundle.base,
        bundle.head,
        tuple(bundle.files),
        tuple(bundle.excluded),
        json_digest(bundle.per_file),
        grounding_sha256,
    )
    return session.memoized(
        key,
        lambda: diffmod.build_review_manifest(
            worktree_path,
            bundle,
            grounding_sha256=grounding_sha256,
        ),
    )


def docs_inventory(
    session: Session, run: RunDoc, changed_files: list[str], *, head: str | None = None
) -> list[docsstage.DocEntry]:
    """Walk the documentation surface of the validation worktree once per command.

    The walk reads the worktree rather than a commit, so code that changes the
    worktree inside a command (a stage command) must clear the session memo.
    """
    worktree_path = _require_worktree(run)
    _, head = _snapshot(session, run, head)
    doc_paths = session.config.docs.paths
    return session.memoized(
        ("docs_inventory", worktree_path, head, tuple(changed_files), tuple(doc_paths)),
        lambda: docsstage.build_inventory(worktree_path, changed_files, doc_paths),
    )


def docs_surface(
    session: Session, run: RunDoc, changed_files: list[str], *, head: str | None = None
) -> list[dict[str, Any]]:
    """Hash the documentation inventory's file contents once per command."""
    worktree_path = _require_worktree(run)
    _, head = _snapshot(session, run, head)
    key = (
        "docs_surface",
        worktree_path,
        head,
        tuple(changed_files),
        tuple(session.config.docs.paths),
    )
    return session.memoized(
        key,
        lambda: doc_surface(worktree_path, docs_inventory(session, run, changed_files, head=head)),
    )


def effective_executor(session: Session, run: RunDoc) -> ReviewExecutor:
    """Resolve policy overrides before accepting or launching a review."""
    if run.risk is not None and run.risk.level.value in session.config.review.require_command_for:
        return "command"
    return session.config.review.executor


def context_data(
    session: Session,
    run: RunDoc,
    *,
    section: str,
    bundle: diffmod.DiffBundle,
    review_manifest: diffmod.ReviewManifest | None = None,
) -> dict[str, Any]:
    """Build the single canonical bundle used by context and command review."""
    worktree_path = _require_worktree(run)
    head = gitx.rev_parse(worktree_path, "HEAD")
    grounding = grounding_for(session, run, bundle.files, head=head)
    if section == "review" and review_manifest is None:
        review_manifest = grounded_manifest(session, run, bundle, grounding=grounding, head=head)
    data: dict[str, Any] = {
        "section": section,
        "worktree_path": run.worktree_path,
        "base": run.merge_base_sha,
        "head": head,
        "intent": run.intent,
        "intent_source": run.intent_source,
        "changed_files": bundle.files,
        "excluded_files": bundle.excluded,
        "diff": bundle.text,
        "diff_bytes": bundle.total_bytes,
        "risk": run.risk.model_dump(mode="json") if run.risk is not None else None,
        "grounding": grounding,
    }
    if review_manifest is not None:
        data["review_coverage"] = review_manifest.as_dict()
    if section == "docs":
        inventory = docs_inventory(session, run, bundle.files, head=head)
        data["doc_surface"] = [entry.as_dict() for entry in inventory]
        data["require_changelog"] = session.config.docs.require_changelog
    return data


def parse_submission(payload: Any, *, stage: Stage) -> tuple[list[FindingSubmission], str | None]:
    """Parse an executor submission without applying coverage or finding policy."""
    if stage is Stage.REVIEW:
        try:
            submission = ReviewSubmission.model_validate(payload)
        except ValidationError as exc:
            raise InvalidFindings(describe_validation(exc)) from exc
        return submission.findings, submission.coverage.manifest

    try:
        docs_submission = DocsSubmission.model_validate(payload)
    except ValidationError as exc:
        raise InvalidFindings(describe_validation(exc)) from exc
    return docs_submission.findings, None


def validate_command_output(payload: Any) -> None:
    """Check the command executor's output before entering submission orchestration."""
    ReviewSubmission.model_validate(payload)


def describe_validation(exc: ValidationError) -> str:
    """Describe a findings validation error for the agent."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        if error["type"] == "extra_forbidden" and error["loc"][-1] in {
            "id",
            "stage",
            "code_owned",
        }:
            parts.append(
                f"{location}: not a field you may set — id, stage, and code_owned are "
                f"assigned by agentic-preflight, never supplied by the agent"
            )
        elif error["type"] == "extra_forbidden":
            parts.append(f"{location}: unrecognised field")
        else:
            parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
