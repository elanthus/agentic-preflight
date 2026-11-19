"""The ``audit`` command: render a run's audit trail as one HTML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cli_contract import AuditData
from ..envelope import Envelope
from ..errors import NoRun
from ..models import RunDoc
from ._session import Session, envelope_for, load_current


def _most_recent_own_run(session: Session) -> str | None:
    """The newest run this worktree started, such as one ``finish`` just released."""
    newest: tuple[str, str] | None = None
    for run_id in session.store.list_runs():
        fields = session.store.peek(run_id, "source_worktree_id", "created_at")
        if not fields or fields["source_worktree_id"] != session.owner_id:
            continue
        created = str(fields["created_at"] or "")
        if newest is None or (created, run_id) > newest:
            newest = (created, run_id)
    return newest[1] if newest else None


def _select(session: Session) -> tuple[RunDoc, str]:
    if session.selected_run_id is not None:
        return load_current(session), "selected"
    if session.active_run_id() is not None:
        return load_current(session), "active"
    run_id = _most_recent_own_run(session)
    if run_id is None:
        raise NoRun("no run of this worktree to report on; pass --run RUN_ID")
    return session.store.load_run(run_id), "most_recent"


def audit(session: Session, *, output: str | None = None, local_details: bool = False) -> Envelope:
    """Write a run's audit report. Changes no run state and appends no event.

    The run is the one ``--run`` selects, else the worktree's active run, else the
    newest run this worktree started, so the report still works after ``finish``.
    """
    from ..report.audit import build_audit, default_output, write_report
    from ..report.audit_html import render

    run, selection = _select(session)
    data = build_audit(session, run, local_details=local_details)
    path = Path(output).resolve() if output else default_output(session, run.run_id)
    size = write_report(path, render(data))
    return envelope_for(
        run,
        data=AuditData(
            path=str(path),
            size_bytes=size,
            run_selection=selection,
            local_details=local_details,
            chain_status=data["summary"]["chain_status"],
            findings_by_status=data["summary"]["findings"],
            attested=data["publication"]["attestation"].get("valid") is True,
            note_verified=_note_verified(data["publication"]["attestation"]),
        ),
    )


def _note_verified(note: dict[str, Any]) -> bool | None:
    """True or false for a note that was checked, ``None`` when none was.

    A note that is present but cannot be read has failed the check, whatever the
    report recorded for it: ``None`` is only for no note, or a check that could not run.
    """
    if note.get("present") and note.get("valid") is False:
        return False
    verified: bool | None = (note.get("verification") or {}).get("ok")
    return verified
