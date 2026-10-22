"""The test-session guard over the user's real telemetry files.

The suite points ``XDG_DATA_HOME`` at a per-test directory, and the product keeps
repositories under the system temporary directory out of the default telemetry
location. Either can regress, and a CLI subprocess that a test or the eval runner
spawns would then append rows to the developer's own ``runs.jsonl`` or
``hook-decisions.jsonl``. The guard reads those files themselves, so it sees a row
whichever process wrote it.

At session start it records the lines each real file holds. At session end it reads
the lines added since and reports each row whose ``repo.source_worktree_path`` lies
under this session's pytest base directory or under a system temporary directory.
Rows of other repositories are left alone: the developer may run preflight elsewhere
while the tests run. So are rows under another pytest session's base directory,
which that session wrote.

This module computes everything itself, from the environment captured before any
test changes it. It does not ask the product which directories are temporary,
because the product rule is one of the things it checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

TELEMETRY_FILES = ("runs.jsonl", "hook-decisions.jsonl")
#: The subdirectory that each pytest session creates under ``pytest-of-<user>``.
_SESSION_DIR = re.compile(r"pytest-\d+")
_XDIST_WORKER_DIR = re.compile(r"popen-gw\d+")
#: How many offending rows the failure message lists.
MESSAGE_LIMIT = 20

ISOLATION_RULE = (
    "Tests and eval subprocesses must never write the user's telemetry. Run the CLI with "
    "XDG_DATA_HOME and XDG_CACHE_HOME pointing at a test directory (tests/conftest.py does "
    "this for every test, and evals/run.py for every CLI subprocess), and keep fixture "
    "repositories under the system temporary directory, which the product never mirrors "
    "or hook-logs by default. See docs/local-telemetry.md#temporary-repositories."
)


@dataclass(frozen=True)
class LeakedRow:
    """One row added to a real telemetry file by a temporary repository."""

    file: Path
    line: int
    source_worktree_path: str
    where: str
    run_id: str | None

    def describe(self) -> str:
        run = f" run {self.run_id}" if self.run_id else ""
        return f"{self.file}:{self.line}{run}: {self.source_worktree_path} (under {self.where})"


def _resolved(path: Path | str) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def real_telemetry_dirs(environ: Mapping[str, str], home: Path) -> list[Path]:
    """The user's own telemetry directories, as the environment before the tests names them.

    The default location without ``XDG_DATA_HOME``, the ``XDG_DATA_HOME`` location when
    it is absolute, and the ``[telemetry] path`` of the real user configuration file.
    """
    candidates = [home / ".local" / "share" / "agentic-preflight" / "telemetry"]
    xdg = environ.get("XDG_DATA_HOME", "")
    if xdg and Path(xdg).is_absolute():
        candidates.append(Path(xdg) / "agentic-preflight" / "telemetry")
    configured = _configured_path(home / ".config" / "agentic-preflight" / "config.toml")
    if configured is not None:
        candidates.append(configured)
    directories: list[Path] = []
    for candidate in candidates:
        resolved = _resolved(candidate)
        if resolved is not None and resolved not in directories:
            directories.append(resolved)
    return directories


def _configured_path(config_file: Path) -> Path | None:
    try:
        with open(config_file, "rb") as handle:
            section = tomllib.load(handle).get("telemetry")
    except (OSError, ValueError):
        return None
    value = section.get("path") if isinstance(section, dict) else None
    return Path(value).expanduser() if isinstance(value, str) and value else None


def temporary_roots(environ: Mapping[str, str]) -> list[Path]:
    """The system temporary directories, resolved: ``gettempdir()``, ``$TMPDIR``, ``/tmp``."""
    candidates = [tempfile.gettempdir()]
    if environ.get("TMPDIR"):
        candidates.append(environ["TMPDIR"])
    if os.name != "nt":
        candidates.append("/tmp")  # noqa: S108 - compared against row paths only
    roots: list[Path] = []
    for candidate in candidates:
        resolved = _resolved(candidate)
        if resolved is not None and resolved != Path(resolved.anchor) and resolved not in roots:
            roots.append(resolved)
    return roots


def session_basetemp(basetemp: Path) -> Path:
    """The base directory of the whole session: an xdist worker's parent directory."""
    resolved = _resolved(basetemp) or basetemp
    return resolved.parent if _XDIST_WORKER_DIR.fullmatch(resolved.name) else resolved


def _foreign_session(path: Path, roots: Sequence[Path]) -> bool:
    """Whether ``path`` is under ``pytest-of-<user>/pytest-<n>`` of some temporary root."""
    for root in roots:
        if not path.is_relative_to(root):
            continue
        parts = path.relative_to(root).parts
        if (
            len(parts) >= 2
            and parts[0].startswith("pytest-of-")
            and _SESSION_DIR.fullmatch(parts[1])
        ):
            return True
    return False


def classify(source: object, *, basetemp: Path, roots: Sequence[Path]) -> str | None:
    """Where a row's source worktree puts it: ``"basetemp"``, ``"temporary"``, or ``None``.

    ``basetemp`` is this session's base directory (:func:`session_basetemp`). A path
    under another pytest session's base directory, or outside every temporary root,
    is ``None``: some other process wrote that row.
    """
    if not isinstance(source, str) or not source:
        return None
    path = _resolved(source)
    if path is None:
        return None
    if path.is_relative_to(basetemp):
        return "basetemp"
    if _foreign_session(path, roots):
        return None
    if any(path.is_relative_to(root) for root in roots):
        return "temporary"
    return None


def _digest(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def snapshot(directories: Iterable[Path]) -> dict[Path, set[str]]:
    """The digests of the lines each real telemetry file holds now."""
    state: dict[Path, set[str]] = {}
    for directory in directories:
        for name in TELEMETRY_FILES:
            path = directory / name
            state[path] = {_digest(line) for line in _lines(path)}
    return state


def _lines(path: Path) -> list[bytes]:
    try:
        return path.read_bytes().split(b"\n")
    except OSError:
        return []


def added_lines(path: Path, before: set[str]) -> list[tuple[int, bytes]]:
    """The lines of ``path`` that were not in it at the snapshot, with their numbers.

    Compared by content, so a mirror that ``telemetry rebuild`` rewrote in another
    order is still read correctly.
    """
    return [
        (number, line)
        for number, line in enumerate(_lines(path), 1)
        if line.strip() and _digest(line) not in before
    ]


def leaked_rows(
    path: Path, lines: Iterable[tuple[int, bytes]], *, basetemp: Path, roots: Sequence[Path]
) -> list[LeakedRow]:
    """The added rows of one file whose source worktree is temporary."""
    leaked: list[LeakedRow] = []
    for number, line in lines:
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        repo = row.get("repo") if isinstance(row, dict) else None
        source = repo.get("source_worktree_path") if isinstance(repo, dict) else None
        where = classify(source, basetemp=basetemp, roots=roots)
        if where is None:
            continue
        run_id = row.get("run_id")
        leaked.append(
            LeakedRow(
                file=path,
                line=number,
                source_worktree_path=str(source),
                where="pytest's basetemp" if where == "basetemp" else "the system temp directory",
                run_id=run_id if isinstance(run_id, str) else None,
            )
        )
    return leaked


def check(
    before: Mapping[Path, set[str]], *, basetemp: Path, roots: Sequence[Path]
) -> list[LeakedRow]:
    """Every leaked row added to the real telemetry files since ``before``."""
    leaked: list[LeakedRow] = []
    for path, digests in before.items():
        leaked.extend(leaked_rows(path, added_lines(path, digests), basetemp=basetemp, roots=roots))
    return leaked


def failure_message(leaked: Sequence[LeakedRow]) -> str:
    listed = [f"  {row.describe()}" for row in leaked[:MESSAGE_LIMIT]]
    if len(leaked) > MESSAGE_LIMIT:
        listed.append(f"  ... and {len(leaked) - MESSAGE_LIMIT} more")
    return "\n".join(
        [
            f"The test session added {len(leaked)} row(s) from temporary repositories to the "
            "user's real telemetry files:",
            *listed,
            ISOLATION_RULE,
        ]
    )
