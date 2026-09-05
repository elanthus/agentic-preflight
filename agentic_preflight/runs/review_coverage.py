"""Review coverage validation and snapshot invalidation.

Coverage is evidence about one exact tree.  This module owns both halves of
that invariant: validating a submitted manifest and reopening review when the
recorded head no longer matches the current head.
"""

from __future__ import annotations

from .. import diff as diffmod
from .. import gitx
from ..errors import InvalidFindings
from ..machine import Action
from ..models import FindingSubmission, ReviewCoverage, RunDoc, Stage, StageRecord
from ._session import Session, _apply, _require_worktree


def validate(
    submissions: list[FindingSubmission],
    *,
    manifest: diffmod.ReviewManifest,
    submitted_manifest: str | None,
) -> tuple[list[FindingSubmission], ReviewCoverage]:
    """Bind findings to units and account for every unit in the current manifest."""
    if submitted_manifest != manifest.manifest:
        raise InvalidFindings(
            "review coverage does not match the current diff; fetch fresh "
            "`context` and submit its review_coverage.manifest"
        )

    assigned = _assign_units(submissions, manifest)
    cited = sorted({submission.unit for submission in assigned if submission.unit})
    all_units = [unit.id for unit in manifest.units]
    cited_set = set(cited)
    coverage = ReviewCoverage(
        manifest=manifest.manifest,
        head_sha=manifest.head_sha,
        total_units=len(all_units),
        cited_units=cited,
        clean_units=[unit for unit in all_units if unit not in cited_set],
        excluded_files=list(manifest.excluded_files),
    )
    return assigned, coverage


def _assign_units(
    submissions: list[FindingSubmission], manifest: diffmod.ReviewManifest
) -> list[FindingSubmission]:
    """Bind each finding to a manifest unit, inferring only unambiguous cases."""
    by_id = {unit.id: unit for unit in manifest.units}
    assigned: list[FindingSubmission] = []
    for submission in submissions:
        if submission.unit is not None:
            unit = by_id.get(submission.unit)
            if unit is None:
                raise InvalidFindings(
                    f"finding cites unknown review unit {submission.unit!r}; "
                    f"valid units: {sorted(by_id)}"
                )
            if unit.path != submission.path:
                raise InvalidFindings(
                    f"review unit {unit.id} belongs to {unit.path!r}, not "
                    f"finding path {submission.path!r}"
                )
            assigned.append(submission)
            continue

        candidates = [unit for unit in manifest.units if unit.path == submission.path]
        if submission.line is not None:
            containing = [
                unit
                for unit in candidates
                if unit.kind == "hunk"
                and unit.new_start is not None
                and unit.new_count is not None
                and unit.new_count > 0
                and unit.new_start <= submission.line < unit.new_start + unit.new_count
            ]
            if len(containing) == 1:
                assigned.append(submission.model_copy(update={"unit": containing[0].id}))
                continue
        if len(candidates) == 1:
            assigned.append(submission.model_copy(update={"unit": candidates[0].id}))
            continue
        raise InvalidFindings(
            f"finding against {submission.path!r} must name a review `unit`; "
            f"the path has {[unit.id for unit in candidates] or 'no review units'}"
        )
    return assigned


def invalidate_stage_result(run: RunDoc, stage: Stage) -> None:
    """Discard stale process evidence without resetting its convergence guard."""
    prior = run.stages.pop(stage, None)
    run.evidence.pop(stage, None)
    if prior is not None:
        run.stages[stage] = StageRecord(attempts=prior.attempts)


def reopen_if_stale(session: Session, run: RunDoc) -> tuple[RunDoc, bool]:
    """Invalidate review evidence when the validation snapshot has moved."""
    coverage = run.review_coverage
    worktree_path = _require_worktree(run)
    current_head = gitx.rev_parse(worktree_path, "HEAD")
    if coverage is not None and coverage.head_sha == current_head:
        return run, False
    with session.store.transaction(run.run_id) as doc:
        reviewed_head = doc.review_coverage.head_sha if doc.review_coverage is not None else None
        doc.review_coverage = None
        invalidate_stage_result(doc, Stage.REVIEW)
        invalidate_stage_result(doc, Stage.DOCS)
        invalidate_stage_result(doc, Stage.LINT)
        invalidate_stage_result(doc, Stage.TEST)
        _apply(doc, Action.INVALIDATE_REVIEW)
        run = doc
    session.store.append_event(
        run.run_id,
        {
            "event": "review_coverage_invalidated",
            "reviewed_head": reviewed_head,
            "current_head": current_head,
        },
    )
    return run, True
