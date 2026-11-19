"""The ``audit`` command: render a run's audit trail as one HTML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cli_contract import AuditData
from ..envelope import Envelope
from ..errors import NoRun, OutputRefused
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


def _is_git_pointer(session: Session, run: RunDoc, path: Path) -> bool:
    """Whether ``path`` is the ``.git`` file that points a worktree at its Git directory.

    A linked worktree, a submodule, and a checkout with a separate Git directory
    each have ``.git`` as a file. It sits in the worktree, outside the Git directory,
    and replacing it leaves Git unable to open that worktree. The worktrees this
    session and the run know are checked by path, and any other by its content.
    """
    roots = (
        session.caller_root,
        session.repo_root,
        run.worktree_path,
        run.source_worktree_path,
    )
    for root in roots:
        if root and path == (Path(root) / ".git").resolve():
            return True
    # Only a regular file is read: opening a named pipe would wait for a writer.
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == b"gitdir: "
    except OSError:
        return False


def _destination(session: Session, run: RunDoc, output: str | None) -> Path:
    """Where the report goes, refusing a path that would overwrite the repository's records.

    The default, ``audit.html`` in the run's directory, is the one file this command
    may write inside the Git directory. Any other path there could replace a run
    record, a ref, or an object, and the command would still report success. A
    worktree's ``.git`` pointer file is refused for the same reason, and so is a
    directory, at the default path too.
    """
    from ..report.audit import default_output

    default = default_output(session, run.run_id)
    path = Path(output).resolve() if output else default
    named = f"--output {output}" if output else str(default)
    if path.is_dir():
        raise OutputRefused(
            f"{named} is a directory; the report needs a file to write",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=(
                "Pass a file path, or omit --output."
                if output
                else "Pass --output with a file path, or remove the directory."
            ),
            next_command="agentic-preflight audit",
        )
    if not output or path == default.resolve():
        return path
    git_dir = session.store.root.parent.resolve()
    if path == git_dir or git_dir in path.parents or _is_git_pointer(session, run, path):
        raise OutputRefused(
            f"{named} is inside the repository's Git directory, or is a worktree's .git "
            "file, where a report could overwrite a run record or a Git file",
            state=run.state.value,
            run_id=run.run_id,
            next_instruction=(
                "Choose a path outside the Git directory, or omit --output to write "
                "audit.html in the run's own directory."
            ),
            next_command="agentic-preflight audit",
        )
    return path


def audit(session: Session, *, output: str | None = None, local_details: bool = False) -> Envelope:
    """Write a run's audit report. Changes no run state and appends no event.

    The run is the one ``--run`` selects, else the worktree's active run, else the
    newest run this worktree started, so the report still works after ``finish``.
    """
    from ..report.audit import captured_audit, write_report
    from ..report.audit_html import render

    run, selection = _select(session)
    path = _destination(session, run, output)
    # The report reads the run again if it moves, so the envelope describes the
    # record the report was built from, not the one loaded before.
    data, run = captured_audit(session, run, local_details=local_details)
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
