"""Running a shell stage (lint, test) and capturing what happened.

Two rules matter more than the rest:

**Pass/fail is the exit code alone.** Stdout is never parsed for "0 errors".
Output formats differ per tool and change between versions, so a parser that
quietly stops matching turns a red stage green. Exit codes are the one signal every tool agrees on.

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
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any

from . import command as command_plan

HEAD_LINES = 50
TAIL_LINES = 200
# Conventional "command not found": the configuration named something this
# machine cannot execute at all.
EXIT_UNRUNNABLE = 127
# Generous for a process-tree kill, and short enough that a wedged taskkill
# cannot hold a stage open indefinitely after its own timeout has fired.
_TASKKILL_TIMEOUT_SECONDS = 30
# How long to wait for the killed process to be reaped after a timeout.
_POST_KILL_WAIT_SECONDS = 3
# Each captured stream keeps at most this many bytes: half from its start and
# half from its end. A test run that prints gigabytes must not be held in
# memory, written to the run's log, or redacted in full.
MAX_CAPTURED_BYTES = 8 * 1024 * 1024
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
    """Notice copied-file writes even when their contents are later restored.

    Any write updates ``st_ctime_ns``, which a process cannot set back, so
    comparing fingerprints before and after the stage is sufficient.
    """

    def __init__(
        self,
        worktree_path: Path | str,
        copied_files: list[str] | tuple[str, ...],
    ) -> None:
        self.worktree_path = worktree_path
        self.copied_files = copied_files
        self.initial = _copied_file_fingerprints(worktree_path, copied_files)

    def stop(self) -> bool:
        """Return whether any copied file changed since the guard was created."""
        if self.initial is None:
            return True
        if not self.copied_files:
            return False
        return _copied_file_fingerprints(self.worktree_path, self.copied_files) != self.initial


# Both platforms need the same two guarantees (isolate the child so a timeout
# can reach every process it spawned, then kill all of them) but they provide
# them through APIs the other does not have at all.
#
# The definitions are split on ``sys.platform`` rather than branching inside a
# shared function because a type checker resolves the standard library for the
# platform it is running on: ``os.getpgid``, ``os.killpg``, and
# ``signal.SIGKILL`` simply do not exist when checking on Windows. Guarding at
# definition means each body is only ever checked against the library it uses.
if sys.platform == "win32":

    def _process_group_kwargs() -> dict:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def _kill_process_tree(process: subprocess.Popen) -> None:
        """Kill the timed-out child together with everything it spawned.

        Windows has no process group to signal: ``CREATE_NEW_PROCESS_GROUP``
        only scopes console control events, which a child without a console
        never receives. The parent/child tree that ``taskkill /T`` walks is the
        reachable equivalent. Falling back to killing the direct child is worse
        than killing the tree, and better than leaving it running.

        Every way ``taskkill`` can fail leads to that same fallback, including
        not being on PATH and not returning. This runs on the timeout path, so
        an unbounded wait here would mean the stage timeout no longer bounds
        anything, and an escaping exception would replace the timed-out result
        the caller is owed with a crash.
        """
        try:
            killed = subprocess.run(
                [
                    # Full path: a bare name would let ``CreateProcess`` search
                    # the current directory ahead of System32.
                    command_plan.windows_system_tool("taskkill.exe"),
                    "/F",
                    "/T",
                    "/PID",
                    str(process.pid),
                ],
                capture_output=True,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
            )
            tree_killed = killed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            tree_killed = False

        if not tree_killed:
            with suppress(OSError):
                process.kill()

else:

    def _process_group_kwargs() -> dict:
        return {"start_new_session": True}

    def _kill_process_tree(process: subprocess.Popen) -> None:
        """Kill the timed-out child's whole process group.

        ``start_new_session`` made the child a session leader, so its PID is
        also its process group ID, which is why a failed lookup below is
        survivable rather than a reason to abandon its workers.
        """
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


if sys.platform == "win32":

    @contextmanager
    def _kill_on_termination(process: subprocess.Popen) -> Iterator[None]:
        """Stop the Windows stage tree before propagating Ctrl+C."""
        try:
            yield
        except KeyboardInterrupt:
            _kill_process_tree(process)
            raise

else:

    @contextmanager
    def _kill_on_termination(process: subprocess.Popen) -> Iterator[None]:
        """Kill ``process``'s tree if SIGTERM or SIGINT arrives while it runs."""
        previous: dict[int, signal.Handlers | int | Callable[..., object] | None] = {}

        def restore() -> None:
            for number, old_handler in previous.items():
                signal.signal(number, old_handler)
            previous.clear()

        def handler(signum: int, frame: object) -> None:
            _kill_process_tree(process)
            restore()
            signal.raise_signal(signum)

        try:
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[int(number)] = signal.signal(number, handler)
        except ValueError:
            # Signal handlers can only be installed from the main thread.
            restore()
        try:
            yield
        finally:
            restore()


def read_capped(handle: IO[bytes], limit: int) -> str:
    """Decode a captured stream, keeping only its head and tail beyond ``limit`` bytes."""
    size = handle.seek(0, os.SEEK_END)
    handle.seek(0)
    if size <= limit:
        data = handle.read()
    else:
        half = limit // 2
        head = handle.read(half)
        handle.seek(size - half)
        omitted = f"\n[output truncated: {size - 2 * half} bytes omitted]\n"
        data = head + omitted.encode("ascii") + handle.read(half)
    # The same universal-newline translation that text-mode pipes apply.
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
    is that the exit code decides, and a configuration the machine cannot run
    is a failure the agent should report, not a crash.

    ``start_new_session`` puts the child in its own process group so that a
    test runner which spawns workers does not leave them orphaned when the
    timeout fires; killing only the direct child would strand them.
    """
    try:
        argv = command_plan.build_argv(
            command_plan.plan(command, cwd=worktree_path), cwd=worktree_path
        )
    except command_plan.ShellUnavailable as exc:
        return StageResult(command=command, exit_code=EXIT_UNRUNNABLE, output=str(exc))

    guard = _CopiedFileMutationGuard(worktree_path, guarded_files)
    try:
        # Output goes to anonymous temporary files rather than pipes: reading
        # them back is capped, and a descendant that escapes a timeout kill
        # cannot hold a pipe open and cost the output captured so far.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(worktree_path),
                    stdout=stdout_file,
                    stderr=stderr_file if separate_stderr else subprocess.STDOUT,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **_process_group_kwargs(),
                )
            except OSError as exc:
                # The program resolved when the command was planned, but the OS
                # refused to execute it: a script with no shebang, a file whose
                # format or extension is not executable, or a program deleted
                # since planning. The same contract as a missing shell, a red
                # stage the agent can report, not a crash.
                return StageResult(
                    command=command,
                    exit_code=EXIT_UNRUNNABLE,
                    output=f"cannot run {command!r}: {exc}",
                )
            timed_out = False
            try:
                with _kill_on_termination(process):
                    process.communicate(input=stdin_text, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(process)
                if process.stdin is not None:
                    with suppress(OSError):
                        process.stdin.close()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=_POST_KILL_WAIT_SECONDS)
            # Captured output is a *report*, not structured data: a linter that
            # emits one stray byte must not crash the gate, and the exit code
            # (which is what decides the stage) is unaffected either way.
            stdout = read_capped(stdout_file, MAX_CAPTURED_BYTES)
            stderr = read_capped(stderr_file, MAX_CAPTURED_BYTES) if separate_stderr else ""
        output = stdout + stderr
        if timed_out:
            output += f"\n[timed out after {timeout_seconds}s]"
        result = StageResult(
            command=command,
            exit_code=124 if timed_out else process.returncode,
            output=output,
            timed_out=timed_out,
            stdout=stdout if separate_stderr else None,
            stderr=stderr if separate_stderr else None,
        )
    finally:
        copied_files_changed = guard.stop()
    result.copied_files_changed = copied_files_changed
    return result


REDACTED = "[redacted]"


def _redact_sequentially(text: str, secrets: list[str]) -> str:
    """Replace each secret in turn, in the order given."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def _trie_alternation(node: dict[str, Any]) -> str:
    """Regex for a prefix trie, preferring the longest secret at each position.

    Continuations of a node all start with different characters, so at most
    one can match. Trying it before ending at the node therefore yields the
    longest secret that matches here, which is what an alternation sorted
    longest-first would choose, without trying every secret at every position.
    """
    branches = []
    for char in sorted(key for key in node if key):
        literal = [char]
        child = node[char]
        # Collapse runs of single-continuation nodes into one literal.
        while len(child) == 1 and "" not in child:
            ((char, child),) = child.items()
            literal.append(char)
        branches.append(re.escape("".join(literal)) + _trie_alternation(child))
    if not branches:
        return ""
    body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    return f"(?:{body})?" if "" in node else body


def _secret_pattern(secrets: list[str]) -> re.Pattern[str] | None:
    trie: dict[str, Any] = {}
    for secret in secrets:
        if not secret:
            continue
        node = trie
        for char in secret:
            node = node.setdefault(char, {})
        node[""] = {}
    if not trie:
        return None
    return re.compile(_trie_alternation(trie))


def redact(text: str, secrets: list[str]) -> str:
    """Replace known copied-file values wherever they appear, in one pass.

    At each position the longest secret that starts there is replaced, and
    replaced text is never searched again. This gives the same result as
    replacing secrets one at a time longest first, with two exceptions. Where
    two secrets overlap in the text, the one that starts first is replaced
    rather than the longer one. And a shorter secret is never matched against
    a ``[redacted]`` marker left by a longer one. No secret occurrence in
    ``text`` survives intact either way.
    """
    try:
        pattern = _secret_pattern(secrets)
    except (RecursionError, re.error):
        # Thousands of nested prefixes; keep the per-secret replacement.
        return _redact_sequentially(text, secrets)
    return text if pattern is None else pattern.sub(REDACTED, text)


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
        # Also redact literal lines, for copied files that are not
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


@dataclass
class ProtectedResult:
    result: StageResult
    clean_output: str
    log_path: Path
    redaction_error: SecretRedactionError | None
    failure_reason: str | None


@dataclass
class OutputProtection:
    """Capture before running-state bookkeeping; finish after command execution.

    Callers retain execution, dirty-tree checks, retries, and protocol parsing.
    Secret snapshots remain private and must never enter an envelope or log.
    """

    worktree_path: Path
    copied_files: list[str]
    _secrets: list[str] = field(repr=False, compare=False)

    @classmethod
    def capture(cls, worktree_path: Path | str, copied_files: list[str]) -> OutputProtection:
        """Fail closed before the caller starts its command or records an attempt."""
        return cls(
            Path(worktree_path),
            list(copied_files),
            read_secrets(worktree_path, copied_files),
        )

    def finish(self, result: StageResult, log_path: Path) -> ProtectedResult:
        """Apply the post-command policy and write exactly the safe report bytes.

        Successful protection preserves separate, unredacted stdout for review
        protocol parsing. Withholding discards every captured stream and forces
        a failing exit code, while retaining timeout and mutation metadata.
        """
        redaction_error = None
        try:
            post_secrets = read_secrets(self.worktree_path, self.copied_files)
        except SecretRedactionError as exc:
            redaction_error = exc
            post_secrets = []
        failure_reason = None
        if redaction_error is not None:
            failure_reason = "copied-file redaction became unavailable"
        elif result.copied_files_changed:
            failure_reason = "copied file changed during command execution"
        if failure_reason is not None:
            clean_output = REDACTION_FAILURE_OUTPUT
            result = replace(
                result,
                exit_code=result.exit_code or 1,
                output=clean_output,
                stdout=None,
                stderr=None,
            )
        else:
            clean_output = redact(result.output, combine_secrets(self._secrets, post_secrets))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(clean_output, encoding="utf-8", newline="\n")
        return ProtectedResult(result, clean_output, log_path, redaction_error, failure_reason)
