"""Shared protected command execution; callers own state and failure envelopes."""

from __future__ import annotations

from collections.abc import Callable

from .. import gitx
from ..errors import StageFailed
from ..models import RunDoc
from ..stages import protected_output, shellstage
from ._session import Session, _require_worktree


def _run_protected(
    session: Session,
    run: RunDoc,
    command: str,
    *,
    log_name: str,
    retry_command: str,
    what: str,
    stage: str,
    stdin_text: str | None = None,
    separate_stderr: bool = False,
    before_run: Callable[[], None] | None = None,
    capture_data: dict | None = None,
) -> tuple[protected_output.ProtectedResult, bool]:
    """Capture before bookkeeping, execute, check once, and persist safe output."""
    wt = _require_worktree(run)
    try:
        protection = protected_output.OutputProtection.capture(wt, run.copied_files)
    except shellstage.SecretRedactionError as exc:
        raise StageFailed(
            f"the {what} cannot run because copied-file redaction is unavailable",
            state=run.state.value,
            run_id=run.run_id,
            stage=stage,
            data={"copied_file": str(exc.path), **(capture_data or {})},
            next_instruction=(
                "Restore the reported copied file as readable text, or remove it from "
                "[worktree] copy_files, then retry the command."
            ),
            next_command=retry_command,
        ) from exc
    if before_run is not None:
        before_run()
    result = shellstage.run_stage(
        wt,
        command,
        timeout_seconds=session.config.stage.timeout_seconds,
        stdin_text=stdin_text,
        separate_stderr=separate_stderr,
        guarded_files=run.copied_files,
    )
    session.clear_memo()
    clean_after = gitx.is_clean(wt)
    if not clean_after:
        result.exit_code = result.exit_code or 1
        result.output += f"\n[agentic-preflight] {what} changed the worktree"
    return protection.finish(result, session.store.logs_dir(run.run_id) / log_name), clean_after


def _withheld_message(what: str, redaction_error: shellstage.SecretRedactionError | None) -> str:
    reason = (
        "redaction became unavailable"
        if redaction_error is not None
        else "a copied file changed during execution"
    )
    return f"the {what} output was withheld because {reason}"
