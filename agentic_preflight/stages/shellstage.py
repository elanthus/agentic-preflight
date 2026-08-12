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
import subprocess
from dataclasses import dataclass
from pathlib import Path

HEAD_LINES = 50
TAIL_LINES = 200

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
    """Replace known copied-file values wherever they appear."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


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
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
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
