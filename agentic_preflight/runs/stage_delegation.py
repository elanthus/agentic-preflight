"""Trusted-CI policy checks and the pending test transition."""

from __future__ import annotations

from .. import gitx
from ..ci_policy import base_enabled, declaration
from ..envelope import Envelope
from ..errors import StageFailed
from ..machine import Action
from ..models import RunDoc, Stage, StageRecord
from ._session import Session, _apply, _envelope_for, _require_worktree


def delegate_tests(
    session: Session,
    run: RunDoc,
    stage: Stage,
    *,
    command: str | None,
    record: bool,
    baseline: bool,
) -> Envelope | None:
    """Return the delegated envelope, or None when local execution applies."""
    worktree_path = _require_worktree(run)
    if (
        stage is Stage.TEST
        and session.config.ci.test_authority == "github_actions"
        and base_enabled(worktree_path, run.merge_base_sha)
    ):
        if command is not None or baseline or record:
            raise StageFailed(
                "delegated tests do not accept local command, record, or baseline flags"
            )
        try:
            requested = declaration(
                worktree_path,
                base=run.merge_base_sha,
                head=run.head_sha,
                base_ref=run.base_ref,
                effective=session.config,
            )
        except (ValueError, gitx.GitError) as exc:
            raise StageFailed(str(exc), stage="test") from exc
        with session.store.transaction(run.run_id) as doc:
            doc.test_delegation = requested
            doc.stages[Stage.TEST] = StageRecord(
                status="delegated", reason="trusted CI tests pending"
            )
            doc.evidence.pop(Stage.TEST, None)
            _apply(doc, Action.DELEGATE_TEST)
            run = doc
        session.store.append_event(
            run.run_id, {"event": "test_delegated", "subject": "integration"}
        )
        return _envelope_for(run, stage="test")

    return None
