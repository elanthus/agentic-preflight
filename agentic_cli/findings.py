"""The findings pipeline, shared by the review and docs stages.

The division of responsibility is the design's central idea, restated here
because every function below is an instance of it:

- **Code validates** — path containment, membership of the allowed set, line
  bounds, enums, length caps, volume.
- **The agent is trusted for** — severity, action, title, detail, suggestion.
  These are judgment, and judgment is the agent's job.
- **Code derives** — id, stage, status, ordering, and the blocking set.

Finding IDs are append-only across the *whole run*, not per stage. A docs
finding following two review findings is ``F003``. Renumbering per stage would
make ``respond --id F001`` ambiguous once a run has touched two stages.
"""

from __future__ import annotations

from pathlib import Path

from .machine import State
from .models import (
    Finding,
    FindingAction,
    FindingStatus,
    FindingSubmission,
    Severity,
    Stage,
)


class FindingRejected(Exception):
    """A submission failed validation. The agent must correct and resubmit."""


_STAGE_BY_PREFIX = {"REVIEW_": Stage.REVIEW, "DOCS_": Stage.DOCS}


def stage_for_state(state: State) -> Stage | None:
    """Derive the stage from the active state.

    This is why ``FindingSubmission`` has no ``stage`` field: the stage is a
    fact about where the run currently is, not a claim the agent gets to make.
    """
    for prefix, stage in _STAGE_BY_PREFIX.items():
        if state.name.startswith(prefix):
            return stage
    return None


def next_id(existing: list[Finding]) -> str:
    highest = 0
    for finding in existing:
        try:
            highest = max(highest, int(finding.id[1:]))
        except (ValueError, IndexError):
            continue
    return f"F{highest + 1:03d}"


def _resolve_within(worktree_path: Path, raw_path: str) -> Path:
    """Resolve a submitted path, refusing anything that leaves the worktree.

    ``resolve()`` follows symlinks, which is the point: a symlink pointing out
    of the worktree is an escape even though the path text looks innocent.
    """
    worktree_root = Path(worktree_path).resolve()
    candidate = (worktree_root / raw_path).resolve()
    if not candidate.is_relative_to(worktree_root):
        raise FindingRejected(
            f"path {raw_path!r} resolves outside the worktree; findings may only "
            f"reference files inside {worktree_root}"
        )
    return candidate


def _check_line_bounds(resolved: Path, raw_path: str, line: int | None) -> None:
    if line is None or not resolved.is_file():
        return
    try:
        total = len(resolved.read_text().splitlines())
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable: bounds are not meaningful, skip the check
    if line > total:
        raise FindingRejected(
            f"{raw_path}:{line} is out of bounds; the file has {total} lines"
        )


def validate_and_assign(
    submissions: list[FindingSubmission],
    *,
    stage: Stage,
    worktree_path: Path | str,
    allowed_paths: set[str],
    existing: list[Finding] | None = None,
    max_findings: int = 50,
) -> list[Finding]:
    """Validate a batch and turn it into stored findings with assigned identity.

    ``allowed_paths`` is supplied by the caller rather than computed here: for
    review it is the changed-file set, for docs it is the documentation
    allowlist. Keeping that policy outside this function is what lets one
    pipeline serve both stages without branching on stage internally.

    All-or-nothing: a single bad submission rejects the batch, so the agent
    never ends up half-recorded and unsure what landed.
    """
    existing = list(existing or [])

    total = len(existing) + len(submissions)
    if total > max_findings:
        raise FindingRejected(
            f"{total} findings exceeds max_findings ({max_findings}); "
            f"raise the limit in .agentic-cli.toml or report only what blocks the change"
        )

    assigned: list[Finding] = []
    running = list(existing)

    for submission in submissions:
        resolved = _resolve_within(Path(worktree_path), submission.path)

        if submission.path not in allowed_paths:
            hint = (
                "not in the changed-file set"
                if stage is Stage.REVIEW
                else "not in the documentation allowlist"
            )
            raise FindingRejected(
                f"path {submission.path!r} is {hint} for this run; "
                f"allowed paths: {sorted(allowed_paths)}"
            )

        _check_line_bounds(resolved, submission.path, submission.line)

        finding = Finding.from_submission(
            submission, id=next_id(running), stage=stage
        )
        assigned.append(finding)
        running.append(finding)

    return assigned


def blocking(
    items: list[Finding],
    *,
    blocking_severities: list[str],
) -> list[Finding]:
    """The blocking set: unresolved findings that are severe or need a human.

    ``ask_user`` blocks at any severity — the whole point of that action is that
    the agent has declined to decide, so proceeding would be deciding by default.
    """
    severities = {Severity(s) for s in blocking_severities}
    return [
        f
        for f in items
        if f.status is FindingStatus.OPEN
        and (f.severity in severities or f.action is FindingAction.ASK_USER)
    ]
