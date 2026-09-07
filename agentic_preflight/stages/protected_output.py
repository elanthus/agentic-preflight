"""Copied-file protection and durable output shared by command executors."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from . import shellstage


@dataclass
class ProtectedResult:
    result: shellstage.StageResult
    clean_output: str
    log_path: Path
    redaction_error: shellstage.SecretRedactionError | None
    failure_reason: str | None


@dataclass
class OutputProtection:
    """Capture before running-state bookkeeping; finish after command execution.

    Callers retain execution, dirty-tree checks, retries, and protocol parsing.
    Secret snapshots remain private and must never enter an envelope or log.
    """

    worktree_path: Path
    copied_files: list[str]
    _secrets: list[str] = field(repr=False)

    @classmethod
    def capture(cls, worktree_path: Path | str, copied_files: list[str]) -> OutputProtection:
        """Fail closed before the caller starts its command or records an attempt."""
        return cls(
            Path(worktree_path),
            list(copied_files),
            shellstage.read_secrets(worktree_path, copied_files),
        )

    def finish(self, result: shellstage.StageResult, log_path: Path) -> ProtectedResult:
        """Apply the post-command policy and write exactly the safe report bytes.

        Successful protection preserves separate, unredacted stdout for review
        protocol parsing. Withholding discards every captured stream and forces
        a failing exit code, while retaining timeout and mutation metadata.
        """
        redaction_error = None
        try:
            post_secrets = shellstage.read_secrets(self.worktree_path, self.copied_files)
        except shellstage.SecretRedactionError as exc:
            redaction_error = exc
            post_secrets = []
        failure_reason = None
        if redaction_error is not None:
            failure_reason = "copied-file redaction became unavailable"
        elif result.copied_files_changed:
            failure_reason = "copied file changed during command execution"
        if failure_reason is not None:
            clean_output = shellstage.REDACTION_FAILURE_OUTPUT
            result = replace(
                result,
                exit_code=result.exit_code or 1,
                output=clean_output,
                stdout=None,
                stderr=None,
            )
        else:
            clean_output = shellstage.redact(
                result.output, shellstage.combine_secrets(self._secrets, post_secrets)
            )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(clean_output, encoding="utf-8", newline="\n")
        return ProtectedResult(result, clean_output, log_path, redaction_error, failure_reason)
