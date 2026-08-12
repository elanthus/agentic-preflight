"""Running a shell stage (lint, test) and capturing what happened.

Two rules matter more than the rest:

**Pass/fail is the exit code, full stop.** Never parse stdout for "0 errors".
Output formats differ per tool and change between versions, so a parser that
quietly stops matching turns a red stage green — the single worst failure this
tool can have. Exit codes are the one signal every tool agrees on.

**Copied files are redacted from logs.** ``copy_files`` paths hold local
environment data, and a stage that cats one of them must not immortalise its
contents in a log the agent will read back.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

HEAD_LINES = 50
TAIL_LINES = 200


@dataclass
class StageResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False
    stdout: str | None = None
    stderr: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def run_stage(
    worktree_path: Path | str,
    command: str,
    *,
    timeout_seconds: int = 600,
    stdin_text: str | None = None,
    separate_stderr: bool = False,
) -> StageResult:
    """Run ``command`` in the worktree, killing the whole process group on timeout.

    ``start_new_session`` puts the child in its own process group so that a
    test runner which spawns workers does not leave them orphaned when the
    timeout fires — killing only the direct child would strand them.
    """
    process = subprocess.Popen(
        ["bash", "-lc", command],
        cwd=str(worktree_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_seconds)
        output = (stdout or "") + (stderr or "")
        return StageResult(
            command=command,
            exit_code=process.returncode,
            output=output,
            stdout=stdout if separate_stderr else None,
            stderr=stderr if separate_stderr else None,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        stdout, stderr = process.communicate()
        output = (stdout or "") + (stderr or "")
        return StageResult(
            command=command,
            exit_code=124,
            output=output + f"\n[timed out after {timeout_seconds}s]",
            timed_out=True,
            stdout=stdout if separate_stderr else None,
            stderr=stderr if separate_stderr else None,
        )


def redact(text: str, secrets: list[str]) -> str:
    """Replace the contents of copied files wherever they appear."""
    cleaned = text
    for secret in secrets:
        if secret.strip():
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def read_secrets(worktree_path: Path | str, copied_files: list[str]) -> list[str]:
    """The literal contents of copied files, for redaction only.

    Read here and nowhere else, held only long enough to scrub a log, and never
    placed in an envelope.
    """
    secrets: list[str] = []
    for rel in copied_files:
        path = Path(worktree_path) / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        secrets.extend(line.strip() for line in content.splitlines() if len(line.strip()) > 3)
        if content.strip():
            secrets.append(content.strip())
    # Longest first, so a full-file match wins over a per-line one.
    return sorted(set(secrets), key=len, reverse=True)


def summarise(output: str) -> dict:
    """Head and tail for the envelope, with an explicit truncation flag.

    The full text always lives in the log file; the envelope carries enough to
    diagnose without blowing the agent's context on a 20k-line test run.
    """
    lines = output.splitlines()
    if len(lines) <= HEAD_LINES + TAIL_LINES:
        return {
            "output_head": output,
            "output_tail": "",
            "truncated": False,
            "total_lines": len(lines),
        }
    return {
        "output_head": "\n".join(lines[:HEAD_LINES]),
        "output_tail": "\n".join(lines[-TAIL_LINES:]),
        "truncated": True,
        "total_lines": len(lines),
    }
