"""Atomic, lock-guarded persistence for runs, events, and findings.

A run spans multiple agent turns, so Python cannot hold state in memory between
invocations, so it lives on disk and every mutation follows the same discipline:

    load -> guard -> mutate -> write tmp -> os.replace

all of it inside a :mod:`~agentic_preflight.filelock` exclusive lock held for the
entire read-modify-write window. See that module for why concurrent
invocations need it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from . import filelock, fileperms
from .config import snapshot_run_settings
from .machine import TERMINAL_STATES, State
from .models import Finding, RunDoc


def _parse_run(payload: str) -> RunDoc:
    """Parse and validate a current run document."""
    return RunDoc.model_validate(json.loads(payload))


def _parse_findings(payload: str) -> list[Finding]:
    """Parse and validate a findings list, as :meth:`Store.load_findings` reads it."""
    return [Finding.model_validate(item) for item in json.loads(payload)]


def _check_run_payload(payload: str) -> None:
    """Apply every check :meth:`Store._load_run` applies to a stored run."""
    snapshot_run_settings(_parse_run(payload).config_snapshot)


class StoreError(Exception):
    """Base class for persistence failures."""


RUN_READ_RECOVERY = (
    "Use a compatible tool version or inspect the retained run record. "
    "Do not replace the run or delete its ownership pointers; --force cannot "
    "establish cleanup eligibility for an unreadable record."
)


#: Modes whose new run needs no resource an unreadable run may still hold. A
#: ``reusable`` run leases the clone's single runner on its own branch, and
#: acquisition refuses while that branch is checked out, so releasing the
#: ownership pointer alone would not let any worktree start a run.
_RELEASABLE_MODES = frozenset({"in_place", "strict"})


def run_read_release(pointer_paths: list[Path]) -> str:
    """Recovery for a command refused by an unreadable ``in_place`` or ``strict`` run.

    Deleting the ownership pointer is the one manual step that lets the worktree
    start again without touching the record, its worktree, or its branch, so the
    instruction names that file exactly and leaves the decision to the user.
    """
    names = " and ".join(str(path) for path in pointer_paths)
    return (
        "Use a compatible tool version or inspect the retained run record; --force "
        "cannot establish cleanup eligibility for an unreadable record. Do not replace "
        "or delete the record. Only if a compatible version cannot read it either and "
        f"the user decides to abandon the run, delete the ownership pointer {names} so "
        "the worktree can start a fresh run. That run can then no longer be resumed "
        "from this worktree. The record, its worktree, and its branch stay in place; gc "
        "retains them while the record stays unreadable, and a version that can read it "
        "may later orphan and reclaim it."
    )


RUN_READ_RUNNER_RECOVERY = (
    "Use a compatible tool version or inspect the retained run record; --force cannot "
    "establish cleanup eligibility for an unreadable record. This run may still lease "
    "the clone's reusable runner on its own branch, which may hold unmerged fix commits, "
    "so no worktree in this clone can start a reusable run until the record is resolved. "
    "Do not delete the record, its ownership pointers, the runner, or its branch. Show "
    "the user the record path and stop; the user decides how to recover it."
)


class RunReadError(StoreError):
    """An existing record could not be read or validated; preserve its resources."""

    def __init__(
        self,
        run_id: str,
        path: Path,
        reason: str,
        detail: str,
        *,
        pointer_paths: list[Path] | None = None,
        worktree_mode: str | None = None,
    ):
        super().__init__(detail)
        self.run_id = run_id
        self.path = path
        self.reason = reason
        #: Worktree ownership pointers that name this run.
        self.pointer_paths = pointer_paths or []
        #: The worktree mode the raw record names, or ``None`` when it cannot be read.
        self.worktree_mode = worktree_mode

    def recovery(self) -> tuple[str, list[Path]]:
        """Return the recovery instruction and the pointers it offers for deletion.

        A pointer is offered only when the record names a mode whose new run
        needs nothing this run may still hold. Otherwise, including when the
        mode is unknown, nothing is offered for deletion.
        """
        if not self.pointer_paths:
            return RUN_READ_RECOVERY, []
        if self.worktree_mode in _RELEASABLE_MODES:
            return run_read_release(self.pointer_paths), self.pointer_paths
        return RUN_READ_RUNNER_RECOVERY, []

    def details(self) -> dict:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "reason": self.reason,
            "diagnostic": str(self),
        }


class RecordValidationError(StoreError):
    """A record failed the loader's validation before it was written; nothing was written.

    The message names the failing fields but never their values, like
    :class:`RunReadError`, because records can carry copied-file text.
    """

    def __init__(self, path: Path, fields: list[str]) -> None:
        names = ", ".join(fields) or "(record)"
        super().__init__(f"refusing to write {path.name}: invalid fields {names}")
        self.path = path
        self.fields = fields


def _require_loadable(path: Path, payload: str, check: Callable[[str], object]) -> None:
    """Raise unless ``payload`` passes the loader's own parse, so a write cannot strand a run.

    Models are mutated by attribute assignment and ``model_copy(update=...)``,
    neither of which runs validators. Parsing the serialized text back through
    the loader catches every such bypass at the one place records are written.
    """
    try:
        check(payload)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or "(record)"
                for error in exc.errors(include_input=False, include_context=False)
            }
        )
        raise RecordValidationError(path, fields) from None


class UnknownRun(StoreError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"no such run: {run_id}")
        self.run_id = run_id


class CurrentRunExists(StoreError):
    """A worktree-scoped run lease is already held."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} is already active")
        self.run_id = run_id


class EventLog(NamedTuple):
    """A run's parsed event log and the number of unreadable lines it skipped."""

    events: list[dict[str, Any]]
    skipped_lines: int


def _recorded_worktree_mode(payload: str) -> str | None:
    """Read the worktree mode an invalid record names, without trusting the rest of it."""
    try:
        raw = json.loads(payload)
        mode = raw["config_snapshot"]["worktree"]["mode"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return None
    return mode if isinstance(mode, str) else None


def _findings_payload(findings: list[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings], indent=2)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _replace(tmp: Path, path: Path) -> None:
    """``os.replace``, retried while Windows reports the target as in use.

    POSIX ``rename`` cannot fail because someone else has the destination open;
    Windows can, and does. A reader holding ``run.json`` for the microseconds of
    a ``read_text`` is enough, and so is a virus scanner or the search indexer
    opening the file.

    Retrying is safe precisely because the operation is atomic: it either
    replaced the file or it did not, so a failed attempt has no partial effect
    to undo. The retry is Windows-only; a ``PermissionError`` on POSIX is a
    real permissions problem, and retrying it would
    hide the cause rather than fix it.

    What this fixes is a *transient* hold, which is the one that actually
    occurs: every reader in this module opens the document, reads it, and
    closes it. A process that keeps the handle open indefinitely still blocks
    the replace, and no amount of retrying would change that; Python's
    ``open`` gives no way to ask for the share-delete access that would.
    """
    if sys.platform == "win32":
        _replace_with_retry(tmp, path)
    else:
        os.replace(tmp, path)


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.005)


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

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._worktrees_root: Path | None = None
        #: Called once for each run that a transaction moves into a terminal
        #: state, after the record is durably written and the run lock is free.
        #: Sessions wire it to the local run summary; it must never raise.
        self.on_terminal: Callable[[RunDoc], None] | None = None

    # -- paths ---------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def operation_lock_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / ".operation.lock"

    def run_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def findings_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "findings.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def logs_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "logs"

    @property
    def active_dir(self) -> Path:
        return self.root / "active"

    def active_path(self, owner_id: str) -> Path:
        return self.active_dir / f"{owner_id}.run"

    @property
    def worktrees_dir(self) -> Path:
        # ``None`` keeps worktrees under the store root, for callers constructing
        # Store directly. Normal sessions pass the external cache location.
        return self._worktrees_root or self.root / "worktrees"

    def set_worktrees_root(self, path: Path) -> None:
        self._worktrees_root = Path(path)

    # -- runs ----------------------------------------------------------------

    def create_run(self, run: RunDoc) -> RunDoc:
        run.updated_at = run.created_at
        payload = run.model_dump_json(indent=2)
        _require_loadable(self.run_path(run.run_id), payload, _check_run_payload)
        fileperms.private_directory(self.root)
        self.run_dir(run.run_id).mkdir(parents=True, exist_ok=True)
        _atomic_write(self.run_path(run.run_id), payload)
        self.append_event(
            run.run_id,
            {
                "event": "state_transition",
                "from": None,
                "to": run.state.value,
                "action": None,
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        return run

    def load_run(self, run_id: str) -> RunDoc:
        """Load a current run, validating cached settings and ignoring retired keys.

        The stored snapshot remains unchanged so its digest still binds the original
        evidence. Unknown keys and invalid current settings remain read failures.
        """
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            return self._load_run(run_id)

    def peek(self, run_id: str, *fields: str) -> dict[str, Any] | None:
        """Return top-level fields of a run record without validating it.

        This is a prefilter for loops that skip most runs, such as other branches.
        It reads under the same lock as ``load_run``
        but returns ``None`` on any failure, so a caller that sees ``None`` must
        fall back to ``load_run`` and handle that record exactly as before.
        """
        try:
            with filelock.exclusive(self.run_dir(run_id) / ".lock"):
                raw = json.loads(self.run_path(run_id).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - any failure defers to load_run's diagnostics
            return None
        if not isinstance(raw, dict):
            return None
        return {field: raw.get(field) for field in fields}

    def _load_run(self, run_id: str) -> RunDoc:
        """Read under the caller's run lock, retaining value-free read errors."""
        path = self.run_path(run_id)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise UnknownRun(run_id) from exc
        except (UnicodeDecodeError, OSError) as exc:
            raise RunReadError(
                run_id,
                path,
                "unreadable",
                "Cannot read run record.",
                pointer_paths=self._pointers_to(run_id),
            ) from exc
        try:
            run = _parse_run(payload)
            snapshot_run_settings(run.config_snapshot)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RunReadError(
                run_id,
                path,
                "invalid",
                "Run record is invalid or unsupported.",
                pointer_paths=self._pointers_to(run_id),
                worktree_mode=_recorded_worktree_mode(payload),
            ) from exc
        return run

    def _pointers_to(self, run_id: str) -> list[Path]:
        """Return the ownership pointers that name ``run_id``, for read-failure recovery."""
        try:
            owners = self.list_active()
        except OSError:
            return []
        return sorted(
            self.active_path(owner) for owner, active in owners.items() if active == run_id
        )

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
        findings: list[Finding] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> Iterator[RunDoc]:
        """Read-modify-write a run document under an exclusive lock.

        The document yielded is a fresh load; mutate it in place. It is written
        back, with ``seq`` bumped, only if the body completes without raising,
        so an exception in the body leaves the records untouched. When findings
        accompany the run, findings are atomically replaced before the run. A
        failure between these writes can leave new findings with the previous
        run; there is no automatic roll-forward recovery. ``events`` are
        appended after the state transitions, with the same ``seq``, only when
        the write succeeds.

        This is the one place a run becomes terminal, so it is also where
        :attr:`on_terminal` fires: once, after the commit and outside the run
        lock. A failure there is reported on stderr and never undoes the commit.
        """
        path = self.run_path(run_id)
        if not path.exists():
            raise UnknownRun(run_id)

        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            run = self._load_run(run_id)
            was_terminal = run.state in TERMINAL_STATES

            yield run

            failed = violations(run)
            if failed:
                raise RunInvariantViolation(run_id, failed)

            run.seq += 1
            run.updated_at = _utcnow()
            # Validate both records before writing either, so a refused write
            # leaves the previous files byte-identical.
            findings_payload = None
            if findings is not None:
                findings_payload = _findings_payload(findings)
                _require_loadable(self.findings_path(run_id), findings_payload, _parse_findings)
            run_payload = run.model_dump_json(indent=2)
            _require_loadable(path, run_payload, _check_run_payload)
            if findings_payload is not None:
                _atomic_write(self.findings_path(run_id), findings_payload)
            _atomic_write(path, run_payload)
            for event in [*run._state_events, *(events or [])]:
                self.append_event(run_id, {**event, "seq": run.seq})
            run._state_events.clear()

        if not was_terminal and run.state in TERMINAL_STATES:
            self._notify_terminal(run)

    def _notify_terminal(self, run: RunDoc) -> None:
        """Run the terminal hook without letting it fail a committed transaction."""
        if self.on_terminal is None:
            return
        try:
            self.on_terminal(run)
        except Exception:  # noqa: BLE001 - derived output must never fail the command
            traceback.print_exc(file=sys.stderr)

    # -- findings ------------------------------------------------------------

    def load_findings(self, run_id: str) -> list[Finding]:
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            path = self.findings_path(run_id)
            if not path.exists():
                return []
            return _parse_findings(path.read_text(encoding="utf-8"))

    def save_findings(self, run_id: str, findings: list[Finding]) -> None:
        payload = _findings_payload(findings)
        _require_loadable(self.findings_path(run_id), payload, _parse_findings)
        with filelock.exclusive(self.run_dir(run_id) / ".lock"):
            _atomic_write(self.findings_path(run_id), payload)

    # -- events --------------------------------------------------------------

    def append_event(self, run_id: str, event: dict) -> None:
        """Events are append-only and deliberately *not* atomic-replaced: an
        append is already a single small write, and losing the tail of an audit
        log is survivable in a way that losing ``run.json`` is not. A per-run events lock
        keeps concurrent appends from interleaving within a line.

        An interrupted append can leave a partial last line with no newline. The
        next append starts a new line first, so the torn line stays on its own line
        and readers skip it instead of the new event being glued onto it."""
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _utcnow(), **event}, sort_keys=True)
        with (
            filelock.exclusive(self.run_dir(run_id) / ".events.lock"),
            open(path, "a+b") as handle,
        ):
            prefix = b""
            end = handle.seek(0, os.SEEK_END)
            if end:
                handle.seek(end - 1)
                if handle.read(1) != b"\n":
                    prefix = b"\n"
            handle.write(prefix + (line + "\n").encode("utf-8"))

    def read_events(self, run_id: str) -> EventLog:
        """Read the event log, skipping lines that are not JSON objects.

        A line that is not valid UTF-8 JSON is what an interrupted append leaves
        behind. It, and a line that parses to something other than an object, is
        counted in ``skipped_lines`` rather than failing the read, wherever it sits."""
        path = self.events_path(run_id)
        if not path.exists():
            return EventLog([], 0)
        events: list[dict[str, Any]] = []
        skipped = 0
        for line in path.read_bytes().split(b"\n"):
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict):
                events.append(event)
            else:
                skipped += 1
        return EventLog(events, skipped)

    def load_events(self, run_id: str) -> list[dict]:
        return self.read_events(run_id).events

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

    @contextmanager
    def operation(self, run_id: str) -> Iterator[None]:
        """Serialize a complete mutating command for one run."""
        with filelock.exclusive(self.operation_lock_path(run_id)):
            yield

    @contextmanager
    def try_operation(self, run_id: str) -> Iterator[bool]:
        """Probe whether a run is between commands without waiting for it."""
        with filelock.try_exclusive(self.operation_lock_path(run_id)) as acquired:
            yield acquired

    @contextmanager
    def resource(self, name: str) -> Iterator[None]:
        """Serialize a narrow clone-wide Git or runner resource."""
        if not name.replace("-", "").isalnum():
            raise ValueError(f"invalid resource lock name: {name!r}")
        with filelock.exclusive(self.root / "resources" / f"{name}.lock"):
            yield


def violations(run: RunDoc) -> list[str]:
    """Return the stable names of every run-record rule that ``run`` violates."""
    failed: list[str] = []
    initial_setup_failure = run.setup_failure is not None and run.setup_failure.scope == "initial"

    if (
        initial_setup_failure
        and run.state is not State.SETUP_FAILED
        and run.state not in TERMINAL_STATES
    ):
        failed.append("initial_setup_failure_requires_setup_failed_state")
    if run.state is State.SETUP_FAILED and not initial_setup_failure:
        failed.append("setup_failed_requires_initial_setup_failure")
    if run.state is State.MERGEBACK_PENDING and run.mergeback_attempt is None:
        failed.append("mergeback_pending_requires_attempt")
    if run.state is State.AWAITING_PUSH_CONFIRM and run.gate_token is None:
        failed.append("awaiting_push_requires_gate_token")
    if run.state in {State.PUSHED, State.DONE} and run.pushed_sha is None:
        failed.append("pushed_state_requires_pushed_sha")
    if run.pushed_sha is not None and run.state not in {
        State.PUSHED,
        State.DONE,
        *TERMINAL_STATES,
    }:
        failed.append("pushed_sha_requires_pushed_or_terminal_state")
    if run.orphaned_reason is not None and run.state not in TERMINAL_STATES:
        failed.append("orphaned_reason_requires_terminal_state")
    if run.state is State.ORPHANED and run.orphaned_reason is None:
        failed.append("orphaned_requires_reason")
    if run.worktree_released and run.state not in TERMINAL_STATES:
        failed.append("released_worktree_requires_terminal_state")

    return failed


class RunInvariantViolation(StoreError):
    """A transaction produced a run record that is unsafe to persist."""

    def __init__(self, run_id: str, violations: list[str]) -> None:
        names = list(violations)
        super().__init__(f"run {run_id} violates record invariants: {', '.join(names)}")
        self.run_id = run_id
        self.violations = names
