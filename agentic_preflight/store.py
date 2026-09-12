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

from pydantic import BaseModel, ConfigDict, ValidationError

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


class _RunUpdate(BaseModel):
    """Write-ahead record for one run/findings commit; never includes Git effects."""

    model_config = ConfigDict(extra="forbid")

    run: RunDoc
    findings: list[Finding]


def _parse_run(payload: str) -> RunDoc:
    """Read current documents and migrate the removed hosted-PR lifecycle."""
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise InvalidRunRoot("run record must be a JSON object")
    for field in _REMOVED_LIFECYCLE_FIELDS:
        raw.pop(field, None)
    if isinstance(raw.get("state"), str) and raw["state"] in _REMOVED_LIFECYCLE_STATES:
        raw["state"] = "PUSHED"
    return RunDoc.model_validate(raw)


class StoreError(Exception):
    """Base class for persistence failures."""


RUN_READ_RECOVERY = (
    "Use a compatible tool version or inspect the retained run record. "
    "Do not replace the run or delete its ownership pointers; --force cannot "
    "establish cleanup eligibility for an unreadable record."
)


class InvalidRunRoot(ValueError):
    """A JSON value cannot represent a run document."""


class RunReadError(StoreError):
    """An existing record could not be read or validated; preserve its resources."""

    def __init__(self, run_id: str, path: Path, reason: str, diagnostic: str, *, fields=None):
        super().__init__(diagnostic)
        self.run_id = run_id
        self.path = path
        self.reason = reason
        self.fields = fields or []

    def details(self) -> dict:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "reason": self.reason,
            "diagnostic": str(self),
            "fields": self.fields,
        }


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
    """A worktree-scoped run lease is already held."""

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
        """The pre-v0.6 repository-wide pointer retained for migration."""
        return self.root / "current"

    @property
    def active_dir(self) -> Path:
        return self.root / "active"

    def active_path(self, owner_id: str) -> Path:
        return self.active_dir / f"{owner_id}.run"

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
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            self._recover_update(run_id)
            return self._load_run(run_id)

    def _load_run(self, run_id: str) -> RunDoc:
        """Read under the caller's run lock, retaining structured read errors."""
        path = self.run_path(run_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise UnknownRun(run_id) from exc
        except UnicodeDecodeError as exc:
            raise RunReadError(
                run_id, path, "invalid_encoding", "Run record is not UTF-8."
            ) from exc
        except OSError as exc:
            raise RunReadError(
                run_id,
                path,
                "io_error",
                f"Cannot read run record ({type(exc).__name__}, errno {exc.errno}).",
            ) from exc
        try:
            return _parse_run(payload)
        except json.JSONDecodeError as exc:
            raise RunReadError(
                run_id,
                path,
                "malformed_json",
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}.",
            ) from exc
        except InvalidRunRoot as exc:
            raise RunReadError(run_id, path, "invalid_record", str(exc)) from exc
        except ValidationError as exc:
            raise RunReadError(
                run_id,
                path,
                "invalid_or_unsupported_schema",
                "Run record does not match the supported schema; it may be incompatible or invalid.",
                fields=[
                    {"location": list(error["loc"]), "category": error["type"]}
                    for error in exc.errors(
                        include_url=False, include_context=False, include_input=False
                    )
                ],
            ) from exc

    def list_runs(self) -> list[str]:
        runs = self.root / "runs"
        try:
            entries = list(runs.iterdir())
        except FileNotFoundError:
            return []
        known = []
        for entry in entries:
            try:
                (entry / "run.json").stat()
            except FileNotFoundError:
                continue
            except OSError:
                # Inability to inspect a record is not evidence of absence.
                # load_run supplies the per-record diagnostic to callers.
                pass
            known.append(entry.name)
        return sorted(known)

    @contextmanager
    def transaction(
        self,
        run_id: str,
        *,
        expect_seq: int | None = None,
        findings: list[Finding] | None = None,
    ) -> Iterator[RunDoc]:
        """Read-modify-write a run document under an exclusive lock.

        The document yielded is a fresh load; mutate it in place. It is written
        back — with ``seq`` bumped — only if the body completes without raising,
        so an exception in the body leaves the records untouched. When findings
        accompany the run, a durable journal commits both: readers finish an
        interrupted installation before returning either record. An I/O error
        after journal publication may therefore represent a committed update.
        """
        path = self.run_path(run_id)
        if not path.exists():
            raise UnknownRun(run_id)

        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            self._recover_update(run_id)
            run = self._load_run(run_id)
            if expect_seq is not None and run.seq != expect_seq:
                raise StaleWrite(run_id, expect_seq, run.seq)

            yield run

            run.seq += 1
            run.updated_at = _utcnow()
            if findings is None:
                _atomic_write(path, run.model_dump_json(indent=2))
            else:
                update = _RunUpdate(run=run, findings=findings)
                _atomic_write(self.update_path(run_id), update.model_dump_json(indent=2))
                self._recover_update(run_id)

    def update_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "pending-update.json"

    def _recover_update(self, run_id: str) -> None:
        """Roll forward a committed pair while holding the run lock.

        Validate before overwriting anything. The journal is retained on every
        failure, including an unsupported record or an unexpected sequence.
        """
        path = self.update_path(run_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (UnicodeDecodeError, OSError) as exc:
            raise RunReadError(
                run_id, path, "unreadable_pending_update", "Pending run update cannot be read."
            ) from exc
        try:
            update = _RunUpdate.model_validate_json(payload)
            current = self._load_run(run_id)
            if update.run.run_id != run_id or not (
                update.run.seq == current.seq + 1
                or (update.run.seq == current.seq and update.run == current)
            ):
                raise ValueError("pending update does not follow the current run")
        except ValueError as exc:
            raise RunReadError(
                run_id,
                path,
                "invalid_pending_update",
                "Pending run update is invalid; preserve it.",
            ) from exc
        _atomic_write(
            self.findings_path(run_id),
            json.dumps([f.model_dump(mode="json") for f in update.findings], indent=2),
        )
        _atomic_write(self.run_path(run_id), update.run.model_dump_json(indent=2))
        path.unlink()

    # -- findings ------------------------------------------------------------

    def load_findings(self, run_id: str) -> list[Finding]:
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            self._recover_update(run_id)
            path = self.findings_path(run_id)
            if not path.exists():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [Finding.model_validate(item) for item in payload]

    def save_findings(self, run_id: str, findings: list[Finding]) -> None:
        payload = json.dumps([f.model_dump(mode="json") for f in findings], indent=2)
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            self._recover_update(run_id)
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

    # -- active-run pointers -------------------------------------------------

    @contextmanager
    def _active_lock(self, owner_id: str) -> Iterator[None]:
        with filelock.exclusive(self.active_dir / f"{owner_id}.lock"):
            yield

    def _set_active_unlocked(self, owner_id: str, run_id: str | None) -> None:
        path = self.active_path(owner_id)
        if run_id is None:
            path.unlink(missing_ok=True)
            return
        _atomic_write(path, run_id + "\n")

    def set_active(self, owner_id: str, run_id: str | None) -> None:
        with self._active_lock(owner_id):
            self._set_active_unlocked(owner_id, run_id)

    def claim_active(self, owner_id: str, run_id: str) -> None:
        """Atomically claim one worktree's active-run lease."""
        with self._active_lock(owner_id):
            current = self.get_active(owner_id)
            if current:
                raise CurrentRunExists(current)
            self._set_active_unlocked(owner_id, run_id)

    def clear_active_if(self, owner_id: str, run_id: str) -> bool:
        """Release only the caller's lease, never a newer run's pointer."""
        with self._active_lock(owner_id):
            if self.get_active(owner_id) != run_id:
                return False
            self._set_active_unlocked(owner_id, None)
            return True

    def get_active(self, owner_id: str) -> str | None:
        path = self.active_path(owner_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return payload.strip() or None

    def list_active(self) -> dict[str, str]:
        try:
            entries = list(self.active_dir.iterdir())
        except FileNotFoundError:
            return {}
        active: dict[str, str] = {}
        for path in entries:
            if path.suffix != ".run":
                continue
            try:
                run_id = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            if run_id:
                active[path.stem] = run_id
        return active

    def clear_run(self, run_id: str) -> list[str]:
        """Release every worktree alias still pointing at ``run_id``."""
        cleared: list[str] = []
        for owner_id, active_run_id in self.list_active().items():
            if active_run_id == run_id and self.clear_active_if(owner_id, run_id):
                cleared.append(owner_id)
        return cleared

    def migrate_legacy_current(self, owner_id: str) -> str | None:
        """Move the old clone-wide pointer to the invoking worktree once."""
        with filelock.exclusive(self.root / ".current.lock"):
            try:
                run_id = self.current_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return self.get_active(owner_id)
            if not run_id:
                self.current_path.unlink(missing_ok=True)
                return self.get_active(owner_id)
            try:
                self.load_run(run_id)
            except RunReadError:
                # Do not rewrite even legacy ownership for an unreadable run.
                return self.get_active(owner_id) or run_id
            except UnknownRun:
                pass  # Preserve established missing-pointer recovery.
            with self._active_lock(owner_id):
                current = self.get_active(owner_id)
                if current is None:
                    self._set_active_unlocked(owner_id, run_id)
                    current = run_id
            self.current_path.unlink(missing_ok=True)
            return current

    @contextmanager
    def operation(self, run_id: str) -> Iterator[None]:
        """Serialize a complete mutating command for one run."""
        with filelock.exclusive(self.run_dir(run_id) / ".operation.lock"):
            yield

    @contextmanager
    def try_operation(self, run_id: str) -> Iterator[bool]:
        """Probe whether a run is between commands without waiting for it."""
        with filelock.try_exclusive(self.run_dir(run_id) / ".operation.lock") as acquired:
            yield acquired

    @contextmanager
    def resource(self, name: str) -> Iterator[None]:
        """Serialize a narrow clone-wide Git or runner resource."""
        if not name.replace("-", "").isalnum():
            raise ValueError(f"invalid resource lock name: {name!r}")
        with filelock.exclusive(self.root / "resources" / f"{name}.lock"):
            yield
