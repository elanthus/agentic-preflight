"""Atomic, lock-guarded persistence for runs, events, and findings.

A run spans multiple agent turns, so Python cannot hold state in memory between
invocations — it lives on disk and every mutation follows the same discipline:

    load -> guard -> mutate -> write tmp -> os.replace

all of it inside a :mod:`~agentic_preflight.filelock` exclusive lock held for the
entire read-modify-write window.
Two parallel ``Bash`` calls in a single agent turn are a real hazard, not a
theoretical one. ``expect_seq`` is the second, independent defense: it catches a
*logically* stale write (the caller read the document, thought about it for a
turn, and is now writing back over someone else's newer version) which locking
alone cannot detect.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import filelock
from .models import Finding, RunDoc

# Roughly a second of total backoff. Long enough to outlast a concurrent read
# or a scanner's grab, short enough that a genuinely stuck file surfaces as an
# error while the agent is still waiting on the command.
_REPLACE_ATTEMPTS = 8
_REPLACE_INITIAL_DELAY = 0.005
_REPLACE_MAX_DELAY = 0.25

_REMOVED_LIFECYCLE_FIELDS = {
    "pr_url",
    "ci_started_at",
    "ci_last_checked_at",
    "ci_status",
    "ci_failures",
    "ci_logs",
    "cleanup_token",
    "cleanup_preview",
}
_REMOVED_LIFECYCLE_STATES = {
    "PR_OPEN",
    "CI_MONITORING",
    "CI_FAILED",
    "CHECKS_PASSED",
    "CI_TIMED_OUT",
    "PR_MERGED",
}


def _parse_run(payload: str) -> RunDoc:
    """Read current documents and migrate the removed hosted-PR lifecycle."""
    raw = json.loads(payload)
    for field in _REMOVED_LIFECYCLE_FIELDS:
        raw.pop(field, None)
    if raw.get("state") in _REMOVED_LIFECYCLE_STATES:
        raw["state"] = "PUSHED"
    return RunDoc.model_validate(raw)


class StoreError(Exception):
    """Base class for persistence failures."""


class UnknownRun(StoreError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"no such run: {run_id}")
        self.run_id = run_id


class StaleWrite(StoreError):
    """The document moved on since the caller last read it."""

    def __init__(self, run_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"refusing stale write to {run_id}: expected seq {expected}, "
            f"found seq {actual}; run `agentic-preflight status` and retry"
        )
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


class CurrentRunExists(StoreError):
    """A repository-wide run lease is already held."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} is already active")
        self.run_id = run_id


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _replace(tmp: Path, path: Path) -> None:
    """``os.replace``, retried while Windows reports the target as in use.

    POSIX ``rename`` cannot fail because someone else has the destination open;
    Windows can, and does. A reader holding ``run.json`` for the microseconds of
    a ``read_text`` is enough, and so is a virus scanner or the search indexer
    opening the file behind everyone's back.

    Retrying is safe precisely because the operation is atomic: it either
    replaced the file or it did not, so a failed attempt has no partial effect
    to undo. The retry is Windows-only — a ``PermissionError`` on POSIX is a
    real permissions problem, and quietly grinding on it for a second would
    hide the cause rather than fix it.

    What this fixes is a *transient* hold, which is the one that actually
    occurs: every reader in this module opens the document, reads it, and
    closes it. A process that keeps the handle open indefinitely still blocks
    the replace, and no amount of retrying would change that — Python's
    ``open`` gives no way to ask for the share-delete access that would.
    """
    if sys.platform == "win32":
        _replace_with_retry(tmp, path)
    else:
        os.replace(tmp, path)


def _replace_with_retry(tmp: Path, path: Path) -> None:
    delay = _REPLACE_INITIAL_DELAY
    for _ in range(_REPLACE_ATTEMPTS - 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_MAX_DELAY)
    # The last attempt is deliberately unguarded: if the target is still held
    # after backing off, the caller needs the real error, not a silent loss.
    os.replace(tmp, path)


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a same-directory temp file and rename.

    Same directory matters: ``os.replace`` is only atomic within a filesystem.
    The temp file is removed on any failure so a crashed write leaves no debris.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Store:
    """Everything under ``$GIT_COMMON_DIR/agentic-preflight/``."""

    def __init__(self, root: Path, *, worktrees_root: Path | None = None) -> None:
        self.root = Path(root)
        self._worktrees_root = Path(worktrees_root) if worktrees_root else None

    # -- paths ---------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def run_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def findings_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "findings.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def logs_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "logs"

    @property
    def current_path(self) -> Path:
        return self.root / "current"

    @property
    def worktrees_dir(self) -> Path:
        # ``None`` preserves the v1 location for callers constructing Store
        # directly. Normal sessions pass the external cache location.
        return self._worktrees_root or self.root / "worktrees"

    def set_worktrees_root(self, path: Path) -> None:
        self._worktrees_root = Path(path)

    # -- runs ----------------------------------------------------------------

    def create_run(self, run: RunDoc) -> RunDoc:
        run.created_at = run.created_at or _utcnow()
        run.updated_at = run.created_at
        self.run_dir(run.run_id).mkdir(parents=True, exist_ok=True)
        _atomic_write(self.run_path(run.run_id), run.model_dump_json(indent=2))
        return run

    def load_run(self, run_id: str) -> RunDoc:
        path = self.run_path(run_id)
        if not path.exists():
            raise UnknownRun(run_id)
        return _parse_run(path.read_text(encoding="utf-8"))

    def list_runs(self) -> list[str]:
        runs = self.root / "runs"
        if not runs.exists():
            return []
        return sorted(p.name for p in runs.iterdir() if (p / "run.json").exists())

    @contextmanager
    def transaction(self, run_id: str, *, expect_seq: int | None = None) -> Iterator[RunDoc]:
        """Read-modify-write a run document under an exclusive lock.

        The document yielded is a fresh load; mutate it in place. It is written
        back — with ``seq`` bumped — only if the body completes without raising,
        so an exception anywhere in the body leaves the on-disk state untouched.
        """
        path = self.run_path(run_id)
        if not path.exists():
            raise UnknownRun(run_id)

        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            run = _parse_run(path.read_text(encoding="utf-8"))
            if expect_seq is not None and run.seq != expect_seq:
                raise StaleWrite(run_id, expect_seq, run.seq)

            yield run

            run.seq += 1
            run.updated_at = _utcnow()
            _atomic_write(path, run.model_dump_json(indent=2))

    # -- findings ------------------------------------------------------------

    def load_findings(self, run_id: str) -> list[Finding]:
        path = self.findings_path(run_id)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [Finding.model_validate(item) for item in payload]

    def save_findings(self, run_id: str, findings: list[Finding]) -> None:
        payload = json.dumps([f.model_dump(mode="json") for f in findings], indent=2)
        _atomic_write(self.findings_path(run_id), payload)

    # -- events --------------------------------------------------------------

    def append_event(self, run_id: str, event: dict) -> None:
        """Events are append-only and deliberately *not* atomic-replaced: an
        append is already a single small write, and losing the tail of an audit
        log is survivable in a way that losing ``run.json`` is not."""
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _utcnow(), **event}, sort_keys=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def load_events(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    # -- current pointer -----------------------------------------------------

    @contextmanager
    def _current_lock(self) -> Iterator[None]:
        with filelock.exclusive(self.root / ".current.lock"):
            yield

    def _set_current_unlocked(self, run_id: str | None) -> None:
        if run_id is None:
            self.current_path.unlink(missing_ok=True)
            return
        _atomic_write(self.current_path, run_id + "\n")

    def set_current(self, run_id: str | None) -> None:
        with self._current_lock():
            self._set_current_unlocked(run_id)

    def claim_current(self, run_id: str) -> None:
        """Atomically claim the repository's single active-run lease."""
        with self._current_lock():
            current = self.get_current()
            if current:
                raise CurrentRunExists(current)
            self._set_current_unlocked(run_id)

    def clear_current_if(self, run_id: str) -> bool:
        """Release only the caller's lease, never a newer run's pointer."""
        with self._current_lock():
            if self.get_current() != run_id:
                return False
            self._set_current_unlocked(None)
            return True

    def get_current(self) -> str | None:
        if not self.current_path.exists():
            return None
        return self.current_path.read_text(encoding="utf-8").strip() or None
