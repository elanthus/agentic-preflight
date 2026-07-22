"""Atomic, lock-guarded persistence for runs, events, findings, and the ledger.

A run spans multiple agent turns, so Python cannot hold state in memory between
invocations — it lives on disk and every mutation follows the same discipline:

    load -> guard -> mutate -> write tmp -> os.replace

all of it inside an ``fcntl.flock`` held for the entire read-modify-write window.
Two parallel ``Bash`` calls in a single agent turn are a real hazard, not a
theoretical one. ``expect_seq`` is the second, independent defense: it catches a
*logically* stale write (the caller read the document, thought about it for a
turn, and is now writing back over someone else's newer version) which locking
alone cannot detect.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Ledger, Finding, RunDoc


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
            f"found seq {actual}; run `agentic-cli status` and retry"
        )
        self.run_id = run_id
        self.expected = expected
        self.actual = actual


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a same-directory temp file and rename.

    Same directory matters: ``os.replace`` is only atomic within a filesystem.
    The temp file is removed on any failure so a crashed write leaves no debris.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Store:
    """Everything under ``$GIT_COMMON_DIR/agentic-cli/``."""

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

    def diff_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "diff"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.json"

    @property
    def current_path(self) -> Path:
        return self.root / "current"

    @property
    def worktrees_dir(self) -> Path:
        # ``None`` preserves the v1 location for callers constructing Store
        # directly. Normal sessions pass the external cache location.
        return self._worktrees_root or self.root / "worktrees"

    @property
    def legacy_worktrees_dir(self) -> Path:
        return self.root / "worktrees"

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
        return RunDoc.model_validate_json(path.read_text())

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

        lock_path = self.run_dir(run_id) / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                run = RunDoc.model_validate_json(path.read_text())
                if expect_seq is not None and run.seq != expect_seq:
                    raise StaleWrite(run_id, expect_seq, run.seq)

                yield run

                run.seq += 1
                run.updated_at = _utcnow()
                _atomic_write(path, run.model_dump_json(indent=2))
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    # -- findings ------------------------------------------------------------

    def load_findings(self, run_id: str) -> list[Finding]:
        path = self.findings_path(run_id)
        if not path.exists():
            return []
        return [Finding.model_validate(item) for item in json.loads(path.read_text())]

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
        with open(path, "a") as handle:
            handle.write(line + "\n")

    def load_events(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- ledger --------------------------------------------------------------

    def load_ledger(self) -> Ledger:
        if not self.ledger_path.exists():
            return Ledger()
        return Ledger.model_validate_json(self.ledger_path.read_text())

    def save_ledger(self, ledger: Ledger) -> None:
        _atomic_write(self.ledger_path, ledger.model_dump_json(indent=2))

    # -- current pointer -----------------------------------------------------

    def set_current(self, run_id: str | None) -> None:
        if run_id is None:
            self.current_path.unlink(missing_ok=True)
            return
        _atomic_write(self.current_path, run_id + "\n")

    def get_current(self) -> str | None:
        if not self.current_path.exists():
            return None
        return self.current_path.read_text().strip() or None
