"""Canonical wire protocol for human and command review executors.

This module is deliberately free of state transitions.  It defines the input
bundle every executor receives and parses the strict submission shape returned
by an executor.  Keeping those two paths together prevents command review from
quietly drifting away from in-harness review.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import ValidationError

from .. import diff as diffmod
from .. import gitx
from ..errors import InvalidFindings
from ..models import FindingSubmission, ReviewSubmission, RunDoc, Stage
from ..stages import docs as docsstage
from ._session import Session, _require_worktree

ReviewExecutor = Literal["in_harness", "command"]


def bundle_for(session: Session, run: RunDoc) -> diffmod.DiffBundle:
    """Build the exact diff snapshot shared by all review executors."""
    return diffmod.build_bundle(
        run.worktree_path or session.repo_root,
        run.merge_base_sha,
        "HEAD",
        exclude=session.config.diff.exclude,
    )


def effective_executor(session: Session, run: RunDoc) -> ReviewExecutor:
    """Resolve policy overrides before accepting or launching a review."""
    if run.risk is not None and run.risk.level.value in session.config.review.require_command_for:
        return "command"
    return cast(ReviewExecutor, session.config.review.executor)


def context_data(
    session: Session,
    run: RunDoc,
    *,
    section: str,
    bundle: diffmod.DiffBundle,
) -> dict[str, Any]:
    """Build the single canonical bundle used by context and command review."""
    worktree_path = _require_worktree(run)
    review_manifest = (
        diffmod.build_review_manifest(worktree_path, bundle) if section == "review" else None
    )
    data: dict[str, Any] = {
        "section": section,
        "worktree_path": run.worktree_path,
        "base": run.merge_base_sha,
        "head": gitx.rev_parse(worktree_path, "HEAD"),
        "intent": run.intent,
        "intent_source": run.intent_source,
        "changed_files": bundle.files,
        "excluded_files": bundle.excluded,
        "diff": bundle.text,
        "diff_bytes": bundle.total_bytes,
        "risk": run.risk.model_dump(mode="json") if run.risk is not None else None,
    }
    if review_manifest is not None:
        data["review_coverage"] = review_manifest.as_dict()
    if section == "docs":
        inventory = docsstage.build_inventory(
            worktree_path, bundle.files, session.config.docs.paths
        )
        data["doc_surface"] = [entry.as_dict() for entry in inventory]
        data["require_changelog"] = session.config.docs.require_changelog
    return data


def parse_submission(
    payload: Any, *, stage: Stage
) -> tuple[list[FindingSubmission], str | None]:
    """Parse an executor submission without applying coverage or finding policy."""
    if stage is Stage.REVIEW:
        try:
            submission = ReviewSubmission.model_validate(payload)
        except ValidationError as exc:
            raise InvalidFindings(describe_validation(exc)) from exc
        return submission.findings, submission.coverage.manifest

    if isinstance(payload, dict):
        payload = payload.get("findings", [])
    if not isinstance(payload, list):
        raise InvalidFindings(
            "expected a JSON list of findings, or an object with a `findings` key"
        )
    try:
        return [FindingSubmission.model_validate(item) for item in payload], None
    except ValidationError as exc:
        raise InvalidFindings(describe_validation(exc)) from exc


def validate_command_output(payload: Any) -> None:
    """Check the command executor's output before entering submission orchestration."""
    ReviewSubmission.model_validate(payload)


def describe_validation(exc: ValidationError) -> str:
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
