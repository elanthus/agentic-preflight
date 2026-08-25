"""Running a shell stage (lint, test) and capturing what happened.

Two rules matter more than the rest:

**Pass/fail is the exit code, full stop.** Never parse stdout for "0 errors".
Output formats differ per tool and change between versions, so a parser that
quietly stops matching turns a red stage green — the single worst failure this
tool can have. Exit codes are the one signal every tool agrees on.

**Dotenv values are redacted from logs.** ``copy_files`` paths commonly hold
local environment data. Assignment values are parsed without executing the
file, then scrubbed from captured output before an agent can read it back.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import command as command_plan

HEAD_LINES = 50
TAIL_LINES = 200
# Named here rather than read from ``os.name`` at each use so a test can select
# a platform's process handling without patching ``os`` for the whole process —
# which would also redirect the unrelated platform checks in sibling modules.
WINDOWS = os.name == "nt"
# Conventional "command not found": the configuration named something this
# machine cannot execute at all.
EXIT_UNRUNNABLE = 127
REDACTION_FAILURE_OUTPUT = (
    "[agentic-preflight] command output withheld because copied-file secret "
    "redaction became unavailable"
)

_DOTENV_ASSIGNMENT = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?[^=\s#]+[ \t]*=[ \t]*")
_DOUBLE_QUOTE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "'": "'",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


@dataclass
class StageResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False
    stdout: str | None = None
    stderr: str | None = None
    copied_files_changed: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class SecretRedactionError(RuntimeError):
    """A copied file cannot be read safely enough to redact stage output."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot read copied file {str(path)!r} for secret redaction: {reason}")
        self.path = path


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _copied_file_fingerprints(
    worktree_path: Path | str,
    copied_files: list[str] | tuple[str, ...],
) -> tuple[tuple[str, tuple[int, ...], tuple[int, ...]], ...] | None:
    """Capture path and target identity without reading secret contents."""
    root = Path(worktree_path)
    fingerprints: list[tuple[str, tuple[int, ...], tuple[int, ...]]] = []
    try:
        for rel in copied_files:
            path = root / rel
            link_stat = path.lstat()
            target_stat = path.stat()
            if not stat.S_ISREG(target_stat.st_mode):
                return None
            fingerprints.append((rel, _stat_fingerprint(link_stat), _stat_fingerprint(target_stat)))
    except OSError:
        return None
    return tuple(fingerprints)


class _CopiedFileMutationGuard:
    """Notice copied-file writes even when their contents are later restored."""

    def __init__(
        self,
        worktree_path: Path | str,
        copied_files: list[str] | tuple[str, ...],
    ) -> None:
        self.worktree_path = worktree_path
        self.copied_files = copied_files
        self.initial = _copied_file_fingerprints(worktree_path, copied_files)
        self.changed = threading.Event()
        self.stop_requested = threading.Event()
        self.thread: threading.Thread | None = None
        if self.initial is None:
            self.changed.set()

    def _check(self) -> None:
        if _copied_file_fingerprints(self.worktree_path, self.copied_files) != self.initial:
            self.changed.set()

    def _watch(self) -> None:
        while not self.stop_requested.wait(0.001):
            self._check()

    def start(self) -> None:
        if not self.copied_files:
            return
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def stop(self) -> bool:
        self._check()
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join()
        self._check()
        return self.changed.is_set()


def _process_group_kwargs() -> dict:
    """Popen arguments that isolate the child and its descendants.

    Both platforms need the same guarantee for the same reason: a test runner
    spawns workers, and a timeout must be able to reach all of them.
    """
    if WINDOWS:
        # Fetched dynamically because the constant does not exist in the POSIX
        # build of the standard library, where this branch is unreachable. The
        # zero default is the "no special creation flags" value.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the timed-out child together with everything it spawned."""
    if WINDOWS:
        # Windows has no process group to signal: CREATE_NEW_PROCESS_GROUP only
        # scopes console events, which a non-console child never receives. The
        # parent/child tree that taskkill /T walks is the reachable equivalent.
        killed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
        )
        if killed.returncode != 0:
            with suppress(OSError):
                process.kill()
        return

    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        # The session leader may already have exited while descendants
        # remain in the process group identified by its original PID.
        process_group = process.pid
    except PermissionError:
        # start_new_session guarantees that the child's PID is its process
        # group ID, so a denied lookup does not justify abandoning workers.
        process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        with suppress(ProcessLookupError):
            process.kill()


def run_stage(
    worktree_path: Path | str,
    command: str,
    *,
    timeout_seconds: int = 600,
    stdin_text: str | None = None,
    separate_stderr: bool = False,
    guarded_files: list[str] | tuple[str, ...] = (),
) -> StageResult:
    """Run ``command`` in the worktree, killing the whole process group on timeout.

    The command is planned first (see :mod:`agentic_preflight.stages.command`):
    a plain program and its arguments are executed directly, and only genuine
    shell grammar pays for a shell. A command that needs a shell where none
    exists is a red stage rather than an exception, because the stage contract
    is that the exit code decides — and a configuration the machine cannot run
    is a failure the agent should report, not a crash.

    ``start_new_session`` puts the child in its own process group so that a
    test runner which spawns workers does not leave them orphaned when the
    timeout fires — killing only the direct child would strand them.
    """
    try:
        argv = command_plan.build_argv(command_plan.plan(command, cwd=worktree_path))
    except command_plan.ShellUnavailable as exc:
        return StageResult(command=command, exit_code=EXIT_UNRUNNABLE, output=str(exc))

    guard = _CopiedFileMutationGuard(worktree_path, guarded_files)
    guard.start()
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(worktree_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            text=True,
            # Captured output is a *report*, not structured data: a linter that
            # emits one stray byte must not crash the gate, and the exit code —
            # which is what decides the stage — is unaffected either way.
            encoding="utf-8",
            errors="replace",
            **_process_group_kwargs(),
        )
        try:
            stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_seconds)
            output = (stdout or "") + (stderr or "")
            result = StageResult(
                command=command,
                exit_code=process.returncode,
                output=output,
                stdout=stdout if separate_stderr else None,
                stderr=stderr if separate_stderr else None,
            )
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
            output = (stdout or "") + (stderr or "")
            result = StageResult(
                command=command,
                exit_code=124,
                output=output + f"\n[timed out after {timeout_seconds}s]",
                timed_out=True,
                stdout=stdout if separate_stderr else None,
                stderr=stderr if separate_stderr else None,
            )
    finally:
        copied_files_changed = guard.stop()
    result.copied_files_changed = copied_files_changed
    return result


def redact(text: str, secrets: list[str]) -> str:
    """Replace known copied-file values wherever they appear."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def combine_secrets(*snapshots: list[str]) -> list[str]:
    """Combine redaction snapshots with longest matches first."""
    return sorted({secret for snapshot in snapshots for secret in snapshot}, key=len, reverse=True)


def _decode_quoted_value(value: str, quote: str) -> str:
    """Decode the escapes supported by common dotenv parsers.

    The undecoded form is also retained by ``_dotenv_values``. Keeping both
    covers programs that load dotenv syntax and shells that preserve escapes
    such as ``\\n`` inside double quotes.
    """
    decoded: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 == len(value):
            decoded.append(char)
            index += 1
            continue

        escaped = value[index + 1]
        if quote == "'":
            if escaped in {"\\", "'"}:
                decoded.append(escaped)
            else:
                decoded.extend((char, escaped))
        elif escaped in _DOUBLE_QUOTE_ESCAPES:
            decoded.append(_DOUBLE_QUOTE_ESCAPES[escaped])
        else:
            decoded.extend((char, escaped))
        index += 2
    return "".join(decoded)


def _dotenv_values(content: str) -> list[str]:
    """Extract non-empty assignment values without evaluating the file.

    Supports optional ``export``, unquoted values and single- or double-quoted
    values spanning lines. There is no interpolation or command execution.
    """
    values: list[str] = []
    position = 0
    while match := _DOTENV_ASSIGNMENT.search(content, position):
        value_start = match.end()
        if value_start >= len(content):
            break

        quote = content[value_start]
        if quote not in {"'", '"'}:
            value_end = content.find("\n", value_start)
            if value_end == -1:
                value_end = len(content)
            value = content[value_start:value_end].rstrip(" \t\r")
            for index, char in enumerate(value):
                if char == "#" and (index == 0 or value[index - 1].isspace()):
                    value = value[:index].rstrip()
                    break
            if value:
                values.append(value)
            position = value_end + 1
            continue

        cursor = value_start + 1
        while cursor < len(content):
            char = content[cursor]
            if char == "\\" and cursor + 1 < len(content):
                cursor += 2
                continue
            if char == quote:
                break
            cursor += 1

        raw_value = content[value_start + 1 : cursor]
        if raw_value:
            values.append(raw_value)
            decoded_value = _decode_quoted_value(raw_value, quote)
            if decoded_value:
                values.append(decoded_value)

        if cursor < len(content):
            line_end = content.find("\n", cursor + 1)
            position = len(content) if line_end == -1 else line_end + 1
        else:
            position = len(content)

    return values


def read_secrets(worktree_path: Path | str, copied_files: list[str]) -> list[str]:
    """Dotenv values and literal copied-file contents, for redaction only.

    Read here and nowhere else, held only long enough to scrub a log, and never
    placed in an envelope.
    """
    secrets: list[str] = []
    for rel in copied_files:
        path = Path(worktree_path) / rel
        if not path.is_file():
            raise SecretRedactionError(path, "the path is missing or is not a regular file")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SecretRedactionError(path, "the contents are not valid text") from exc
        except OSError as exc:
            raise SecretRedactionError(path, str(exc)) from exc
        secrets.extend(_dotenv_values(content))
        # Retain the previous literal fallback for copied files that are not
        # dotenv-formatted. Short *values* above are intentionally included;
        # short arbitrary lines are not, to avoid redacting common log text.
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
