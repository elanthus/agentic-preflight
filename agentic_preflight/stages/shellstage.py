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
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
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
# How long a scratch directory's removal waits for a stopped process to let go of it.
_SCRATCH_REMOVAL_WAIT_SECONDS = 5.0
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


# Linux delivers a signal sent to a process group to a member that is forking
# either before the fork, which then starts again, or to both processes after
# it. macOS lists the group's members first and signals them afterwards: a child
# whose fork completes in between is in the group and was never signalled, and
# it runs on after its parent has been killed. Every other POSIX platform is
# treated like macOS, because repeating the signal is harmless where one is enough.
_GROUP_SIGNAL_CAN_MISS_A_FORK = sys.platform != "linux"
# How long a group kill goes on signalling a group whose members are still alive.
# It normally ends within milliseconds, on the first call that finds nobody; this
# bounds a member that has been killed and is slow to die.
_GROUP_KILL_SECONDS = 2.0
# The pause between two signals to the same group, which doubles up to the longest.
_GROUP_KILL_FIRST_PAUSE_SECONDS = 0.001
_GROUP_KILL_LONGEST_PAUSE_SECONDS = 0.02

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

    def process_group_kwargs() -> dict:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def kill_process_tree(process: subprocess.Popen) -> None:
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

    def process_group_kwargs() -> dict:
        return {"start_new_session": True}

    def _kill_process_group(process_group: int) -> None:
        """Send SIGKILL to the group until none of its processes is left to signal.

        One ``killpg`` is not enough where ``_GROUP_SIGNAL_CAN_MISS_A_FORK``. A
        member that is forking stays alive, and so stays signalable, until its
        fork has finished, so ``killpg`` succeeds for as long as a missed child
        can still appear and the next call reaches that child. The call that
        finds nobody raises: ``ProcessLookupError`` when the group is gone,
        ``PermissionError`` on macOS when only unreaped members are left.

        Raises whatever the first ``killpg`` raises, and nothing after that.
        """
        os.killpg(process_group, signal.SIGKILL)
        if not _GROUP_SIGNAL_CAN_MISS_A_FORK:
            return
        deadline = time.monotonic() + _GROUP_KILL_SECONDS
        pause = _GROUP_KILL_FIRST_PAUSE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except OSError:
                return
            time.sleep(pause)
            pause = min(pause * 2, _GROUP_KILL_LONGEST_PAUSE_SECONDS)

    def kill_process_tree(process: subprocess.Popen) -> None:
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
            _kill_process_group(process_group)
        except ProcessLookupError:
            pass
        except PermissionError:
            with suppress(ProcessLookupError):
                process.kill()


class TerminationGuard:
    """Where a ``kill_on_termination`` or ``kill_when_parent_dies`` block names its process."""

    def __init__(self, bound: Callable[[], None]) -> None:
        self.process: subprocess.Popen | None = None
        self._bound = bound

    def bind(self, process: subprocess.Popen) -> None:
        """Name the process to stop. A signal that arrived before this is acted on now."""
        self.process = process
        self._bound()


if sys.platform == "win32":

    @contextmanager
    def kill_on_termination(
        stop: Callable[[subprocess.Popen], None] = kill_process_tree,
    ) -> Iterator[TerminationGuard]:
        """Stop the bound Windows process tree with ``stop`` before propagating Ctrl+C.

        A Ctrl+C that arrives before the process is bound propagates without
        stopping anything: this does not defer the interrupt the way the POSIX
        guard defers a signal.
        """
        guard = TerminationGuard(lambda: None)
        try:
            yield guard
        except KeyboardInterrupt:
            if guard.process is not None:
                stop(guard.process)
            raise

else:

    @contextmanager
    def kill_on_termination(
        stop: Callable[[subprocess.Popen], None] = kill_process_tree,
    ) -> Iterator[TerminationGuard]:
        """Stop the bound process's tree with ``stop`` if SIGTERM, SIGINT, or SIGHUP arrives.

        The child runs in its own session, so a signal to this process, or a hangup
        of its terminal, does not reach the child unless this forwards it.

        Start the child inside the block and pass it to ``bind``. The handlers are in
        place before the child exists, so there is no moment at which the child runs
        and a signal still has its default action. A signal that arrives before
        ``bind`` waits for it: the child is then stopped, the previous handlers are
        restored, and the signal is raised again. If the block ends without a bound
        process, because the child never started, the signal is raised again then.

        The signals are deferred by a handler, not blocked with ``pthread_sigmask``:
        a child inherits the signal mask, and would start with them blocked.
        """
        previous: dict[int, signal.Handlers | int | Callable[..., object] | None] = {}
        # The first signal that arrived while there was no process to stop.
        waiting: list[int] = []

        def restore() -> None:
            for number, old_handler in previous.items():
                signal.signal(number, old_handler)
            previous.clear()

        def stop_and_raise(process: subprocess.Popen, signum: int) -> None:
            stop(process)
            restore()
            signal.raise_signal(signum)

        def handler(signum: int, frame: object) -> None:
            if guard.process is None:
                if not waiting:
                    waiting.append(signum)
                return
            stop_and_raise(guard.process, signum)

        def bound() -> None:
            # ``bind`` set the process before calling this, so a signal from here on
            # is handled directly and cannot be added to ``waiting``.
            if waiting and guard.process is not None:
                stop_and_raise(guard.process, waiting.pop())

        guard = TerminationGuard(bound)
        try:
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                # An ignored signal (``nohup``, or a parent's SIG_IGN) must stay
                # ignored: forwarding it would stop the child while this process,
                # restored to SIG_IGN, carried on without it.
                if signal.getsignal(number) is signal.SIG_IGN:
                    continue
                previous[int(number)] = signal.signal(number, handler)
        except ValueError:
            # Signal handlers can only be installed from the main thread.
            restore()
        try:
            yield guard
        finally:
            restore()
            if waiting:
                signal.raise_signal(waiting.pop())


# How long leaving a ``kill_when_parent_dies`` block waits for its watcher to exit,
# first after telling it to and then after killing it.
_WATCHER_EXIT_WAIT_SECONDS = 5.0
# What ``kill_when_parent_dies`` sends its watcher when the block ends normally.
_WATCHER_STAND_DOWN = b"stand down\n"
# Run by ``kill_when_parent_dies``. Stdin carries the process group to kill, on one
# line. The end of the stream straight after that line is the parent's death.
# Anything else is not acted on: no group, part of one, or a group followed by the
# stand-down message.
#
# The kill is the loop in ``_kill_process_group``, for the reason given there,
# written out because the watcher imports nothing from this package. It runs on
# every platform: nobody waits for the watcher, so the signals that Linux does not
# need cost nothing.
_PARENT_DEATH_WATCHER = f"""\
import os, signal, time
received = b""
while chunk := os.read(0, 64):
    received += chunk
group = received[:-1]
if received.endswith(b"\\n") and group.isdigit():
    deadline = time.monotonic() + {_GROUP_KILL_SECONDS!r}
    pause = {_GROUP_KILL_FIRST_PAUSE_SECONDS!r}
    while time.monotonic() < deadline:
        try:
            os.killpg(int(group), signal.SIGKILL)
        except OSError:
            break
        time.sleep(pause)
        pause = min(pause * 2, {_GROUP_KILL_LONGEST_PAUSE_SECONDS!r})
"""


if sys.platform == "win32":

    @contextmanager
    def kill_when_parent_dies() -> Iterator[TerminationGuard]:
        """Do nothing: Windows stops a process tree by parentage, not by process group.

        ``kill_process_tree`` runs ``taskkill /T`` there, which reaches a child of
        the killed process whatever process group that child was started in.
        """
        yield TerminationGuard(lambda: None)

else:

    @contextmanager
    def kill_when_parent_dies() -> Iterator[TerminationGuard]:
        """Kill the bound process's group if this process dies before the block ends.

        ``kill_on_termination`` covers the signals this process can handle. It
        cannot cover SIGKILL, which is what ``kill_process_tree`` sends to this
        process's group when this process is itself a stage that timed out or whose
        runner was terminated. A child started with ``process_group_kwargs`` is in
        another group, so that kill does not reach it, and it would run on alone.

        A watcher process, in a session of its own so that the same kill does not
        reach it either, holds the read end of a pipe whose only write end is in
        this process. The kernel closes that end however this process dies, and the
        watcher then kills the bound process's group. Leaving the block tells the
        watcher to stand down first, so a process that outlives the block is left
        alone.

        Start the child inside the block and pass it to ``bind``. Two cases are not
        covered, and the child then runs on as it would without this block: this
        process dies between the child's start and ``bind``, or the watcher could
        not be started or has already exited. Neither raises here.
        """
        watcher: subprocess.Popen[bytes] | None
        try:
            watcher = subprocess.Popen(
                # ``-I -S``: the watcher imports nothing from the environment or the
                # working directory, which can be a worktree under review.
                [sys.executable, "-I", "-S", "-c", _PARENT_DEATH_WATCHER],
                stdin=subprocess.PIPE,
                # It must not hold this process's output open after this process dies.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Unbuffered, so each message below is one ``write`` to the pipe: it
                # arrives whole, or fails, before the next line here runs.
                bufsize=0,
                start_new_session=True,
            )
        except OSError:
            watcher = None

        def send(message: bytes) -> None:
            if watcher is not None and watcher.stdin is not None:
                # A watcher that has exited leaves a broken pipe behind.
                with suppress(OSError):
                    watcher.stdin.write(message)

        def bound() -> None:
            if guard.process is not None:
                # ``start_new_session`` made the bound process's PID its process group ID.
                send(b"%d\n" % guard.process.pid)

        guard = TerminationGuard(bound)
        try:
            yield guard
        finally:
            if watcher is not None:
                # The message goes first: closing the pipe without it would read to
                # the watcher as this process's death.
                send(_WATCHER_STAND_DOWN)
                if watcher.stdin is not None:
                    with suppress(OSError):
                        watcher.stdin.close()
                # Both waits are bounded. The pipe is closed, so a watcher that is
                # still running here is one that never came to read it.
                try:
                    watcher.wait(timeout=_WATCHER_EXIT_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    with suppress(OSError):
                        watcher.kill()
                    with suppress(subprocess.TimeoutExpired):
                        watcher.wait(timeout=_WATCHER_EXIT_WAIT_SECONDS)


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
            timed_out = False
            try:
                # The guard is entered before the child is started, so a signal
                # that arrives while it starts still stops it.
                with kill_on_termination() as termination:
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
                            **process_group_kwargs(),
                        )
                    except OSError as exc:
                        # The program resolved when the command was planned, but the
                        # OS refused to execute it: a script with no shebang, a file
                        # whose format or extension is not executable, or a program
                        # deleted since planning. The same contract as a missing
                        # shell, a red stage the agent can report, not a crash.
                        return StageResult(
                            command=command,
                            exit_code=EXIT_UNRUNNABLE,
                            output=f"cannot run {command!r}: {exc}",
                        )
                    termination.bind(process)
                    process.communicate(input=stdin_text, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_tree(process)
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


@contextmanager
def scratch_directory(prefix: str) -> Iterator[str]:
    """Yield a temporary directory for a child process. Removing it never raises.

    Windows refuses to remove a directory that a process still has as its working
    directory, and a file that a process still has open. A child that was just
    stopped can hold either for a moment: the program that was started can be a
    wrapper (Git for Windows' ``git``, or a ``.cmd`` shim) around the process that
    does the work, and that process exits a moment after its wrapper. The removal
    is retried for that moment. A directory that is still held afterwards is left
    to the system's cleanup of temporary files: the caller already has its result,
    or the error that ended the child, and a leftover directory must not replace
    either.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        deadline = time.monotonic() + _SCRATCH_REMOVAL_WAIT_SECONDS
        while True:
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)


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
