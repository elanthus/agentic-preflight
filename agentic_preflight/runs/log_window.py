"""Bounded windows over a stored stage log.

`logs` returns a window of a stage log rather than the whole file, so one envelope
cannot flood a caller's context. A window is a run of whole lines from the head or
the tail of the log, cut further to a byte budget.

The stored log is already redacted, so a window cannot expose a secret. A byte cut
still never splits a multi-byte UTF-8 character, and it drops a ``[redacted]``
marker that it would split, so the output never ends or starts with a marker
fragment that reads like log content.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..stages.shellstage import REDACTED

#: Lines the default window returns from the end of the log.
DEFAULT_TAIL_LINES = 200
#: Byte budget for a `--head` or `--tail` window; `--all` has none.
MAX_WINDOW_BYTES = 64 * 1024

_MARKER = REDACTED.encode("utf-8")


@dataclass(frozen=True)
class LogWindow:
    output: str
    truncated: bool
    total_bytes: int
    total_lines: int


def read_log_window(
    path: Path,
    *,
    head: int | None = None,
    tail: int | None = None,
    max_bytes: int | None = MAX_WINDOW_BYTES,
) -> LogWindow:
    """Read ``head`` or ``tail`` lines of ``path``, at most ``max_bytes`` bytes.

    With neither ``head`` nor ``tail``, every line is selected; with ``max_bytes``
    also ``None``, the whole log is returned.
    """
    if head is not None and tail is not None:
        raise ValueError("choose head or tail, not both")
    data = path.read_bytes()
    start, end = 0, len(data)
    if head is not None:
        end = _after_first_lines(data, head)
        if max_bytes is not None and end > max_bytes:
            end = _safe_end(data, max_bytes)
    else:
        if tail is not None:
            start = _start_of_last_lines(data, tail)
        if max_bytes is not None and end - start > max_bytes:
            start = _line_start(data, _safe_start(data, end - max_bytes), end)
    return LogWindow(
        output=data[start:end].decode("utf-8", errors="replace"),
        truncated=(start, end) != (0, len(data)),
        total_bytes=len(data),
        total_lines=data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
    )


def _after_first_lines(data: bytes, count: int) -> int:
    position = 0
    for _ in range(count):
        newline = data.find(b"\n", position)
        if newline == -1:
            return len(data)
        position = newline + 1
    return position


def _start_of_last_lines(data: bytes, count: int) -> int:
    # A final newline terminates the last line; it does not begin an empty one.
    limit = len(data) - 1 if data.endswith(b"\n") else len(data)
    start = 0
    for _ in range(count):
        newline = data.rfind(b"\n", 0, limit)
        if newline == -1:
            return 0
        limit = newline
        start = newline + 1
    return start


def _line_start(data: bytes, start: int, end: int) -> int:
    """Move a cut start to the next line when that line begins inside the window.

    A window holding one partial line, or ending at the only newline, keeps its cut.
    """
    if start == 0 or data[start - 1 : start] == b"\n":
        return start
    newline = data.find(b"\n", start, end)
    return newline + 1 if newline != -1 and newline + 1 < end else start


def _is_continuation(byte: int) -> bool:
    return byte & 0xC0 == 0x80


def _safe_end(data: bytes, end: int) -> int:
    """Move a cut end back off a split character or redaction marker."""
    while 0 < end < len(data) and _is_continuation(data[end]):
        end -= 1
    for offset in range(1, len(_MARKER)):
        if end - offset >= 0 and data[end - offset : end - offset + len(_MARKER)] == _MARKER:
            return end - offset
    return end


def _safe_start(data: bytes, start: int) -> int:
    """Move a cut start forward off a split character or redaction marker."""
    while start < len(data) and _is_continuation(data[start]):
        start += 1
    for offset in range(1, len(_MARKER)):
        if start - offset >= 0 and data[start - offset : start - offset + len(_MARKER)] == _MARKER:
            return start - offset + len(_MARKER)
    return start
