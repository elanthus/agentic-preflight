"""The local log of pre-push hook decisions.

``hook-check`` appends one JSON line for each decision to ``hook-decisions.jsonl``
in the user-level telemetry directory, beside the run-summary mirror::

    {"allowed": false, "at": "...", "reason": "missing_attestation", "remote": "origin",
     "repo": {"name", "remote", "root_commit", "source_worktree_path"},
     "updates": [{"force": false, "local_ref", "remote_ref", "sha": "1a2b3c4"}]}

The log follows the telemetry rules:

* Nothing is sent anywhere, and nothing is written inside the repository.
* A line records refs, a seven-character commit prefix, the remote name, and a
  reason code. It never copies the block message, remote credentials, or output.
* Writing never changes the push. ``hook-check`` decides its exit code and message
  before it writes. The write is bounded in time, every failure is swallowed, and
  nothing here writes to stdout or stderr.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import threading
import time
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from . import filelock, gitx, telemetry
from .config import REPO_CONFIG_NAME, USER_CONFIG_NAME, Config, TelemetrySection, load_config
from .hook import Decision

LOG_NAME = "hook-decisions.jsonl"
_LOCK_NAME = "hook-decisions.jsonl.lock"
#: How long ``hook-check`` waits for the log after the decision: for the repository
#: identity, the lock, and the append.
TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01
SHA_PREFIX_LENGTH = 7
_THREAD_NAME = "agentic-preflight-hook-log"
#: A Git remote name. Anything else is a URL or a path, and is normalized.
_REMOTE_NAME = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_REMOTE_LIMIT = 200

#: What :func:`record` did. Only tests read it; ``hook-check`` ignores it.
WRITTEN = "written"
DISABLED = "disabled"
UNRESOLVED = "unresolved"
TIMED_OUT = "timed_out"
FAILED = "failed"


class LoggedUpdate(BaseModel):
    """One pushed ref update, as the log records it."""

    model_config = ConfigDict(extra="forbid")

    local_ref: str
    remote_ref: str
    #: The first seven characters of the pushed commit.
    sha: str
    #: Whether the update rewrites history the remote has. ``None`` when the hook
    #: did not evaluate it.
    force: StrictBool | None


class HookDecisionLine(BaseModel):
    """One line of ``hook-decisions.jsonl``."""

    model_config = ConfigDict(extra="forbid")

    at: str
    repo: telemetry.RepoIdentity
    #: The remote name Git passed to the hook, or a URL as host and path without
    #: credentials. ``None`` when the hook did not receive it.
    remote: str | None
    allowed: StrictBool
    #: One of :data:`agentic_preflight.hook.REASONS`.
    reason: str
    updates: list[LoggedUpdate]


def log_path(config: Config | None = None) -> Path:
    return telemetry.telemetry_dir(config) / LOG_NAME


def remote_label(value: str | None) -> str | None:
    """A remote name as given, or a URL or path reduced to host and path without credentials.

    Git passes the URL as the remote name when a push names no configured remote,
    and that URL can contain a token.
    """
    value = (value or "").strip()
    if not value:
        return None
    label = value if _REMOTE_NAME.fullmatch(value) else telemetry.normalize_remote(value)
    return label[:_REMOTE_LIMIT] if label else None


def build_line(
    decision: Decision,
    *,
    repo: telemetry.RepoIdentity,
    remote: str | None,
    at: str | None = None,
) -> HookDecisionLine:
    return HookDecisionLine(
        at=at or datetime.now(UTC).isoformat(timespec="seconds"),
        repo=repo,
        remote=remote_label(remote),
        allowed=decision.allowed,
        reason=decision.reason,
        updates=[
            LoggedUpdate(
                local_ref=item.update.local_ref,
                remote_ref=item.update.remote_ref,
                sha=item.update.local_sha[:SHA_PREFIX_LENGTH],
                force=item.force,
            )
            for item in decision.updates
        ],
    )


# -- settings -------------------------------------------------------------------


def load_settings() -> tuple[Path | None, Config | None]:
    """The repository root and configuration, or ``None`` for what cannot be loaded.

    For the path on which ``hook-check`` has not loaded them itself. It never raises,
    and it discards what the configuration loader prints, so ``hook-check``'s stderr
    stays as it was.
    """
    repo_root: Path | None = None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            repo_root = gitx.repo_root(Path.cwd())
            return repo_root, load_config(repo_root)
    except Exception:  # noqa: BLE001 - a broken log must never block or alter a push
        return repo_root, None


def fallback_section(
    repo_root: Path, *, user_config_dir: Path | str | None = None
) -> TelemetrySection | None:
    """The ``[telemetry]`` preference when the full configuration cannot be loaded.

    Each file's ``[telemetry]`` section is read on its own, and a repository section
    replaces the user section, as in :func:`~agentic_preflight.config.load_config`.
    ``None`` when a file that exists cannot be read as TOML, or when the section
    that applies is invalid. The preference is then unknown, so nothing is logged.
    """
    if user_config_dir is None:
        user_config_dir = Path.home() / ".config" / "agentic-preflight"
    section: Any = {}
    for path in (Path(user_config_dir) / USER_CONFIG_NAME, repo_root / REPO_CONFIG_NAME):
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        if "telemetry" in data:
            section = data["telemetry"]
    try:
        return TelemetrySection.model_validate(section)
    except ValidationError:
        return None


# -- writing --------------------------------------------------------------------


@contextlib.contextmanager
def _locked(path: Path, deadline: float) -> Iterator[None]:
    """Hold the log lock, waiting no later than ``deadline`` (``time.monotonic``).

    A lock taken after the deadline is released unused: the caller has stopped
    waiting, so a late line would be written by a write it already abandoned.
    """
    while True:
        with filelock.try_exclusive(path) as acquired:
            if acquired and time.monotonic() < deadline:
                yield
                return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{path} stayed locked")
        time.sleep(_LOCK_POLL_SECONDS)


def append(directory: Path, line: HookDecisionLine, *, deadline: float) -> Path:
    """Append one line under the log lock, ending any interrupted line first."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / LOG_NAME
    payload = json.dumps(line.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    with _locked(directory / _LOCK_NAME, deadline), open(target, "ab+") as handle:
        handle.seek(0, os.SEEK_END)
        prefix = b""
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = b"\n"
        handle.write(prefix + payload + b"\n")
        handle.flush()
    return target


class Pending:
    """One decision's log write, started before the decision is known.

    :func:`begin` starts a daemon thread that resolves the repository identity while
    ``hook-check`` evaluates the push. :meth:`finish` hands it the decision and waits
    at most ``timeout`` seconds for the line to be appended. A write still running
    then is abandoned, and the process exits without it.
    """

    def __init__(self, status: str | None = None) -> None:
        self._status = status
        self._result: list[str] = []
        self._ready = threading.Event()
        self._decision: Decision | None = None
        self._remote: str | None = None
        self._deadline = 0.0
        self._timeout = TIMEOUT_SECONDS
        self._root = Path()
        self._directory = Path()
        self._worker: threading.Thread | None = None

    def _start(self, root: Path, directory: Path, timeout: float) -> None:
        self._root, self._directory, self._timeout = root, directory, timeout
        self._worker = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        try:
            identity = telemetry.repo_identity(self._root)
            self._ready.wait()
            decision = self._decision
            if decision is None or time.monotonic() >= self._deadline:
                self._result.append(TIMED_OUT)
                return
            line = build_line(decision, repo=identity, remote=self._remote)
            append(self._directory, line, deadline=self._deadline)
            self._result.append(WRITTEN)
        except TimeoutError:
            self._result.append(TIMED_OUT)
        except BaseException:  # noqa: BLE001 - a broken log must never block or alter a push
            self._result.append(FAILED)

    def finish(self, decision: Decision, *, remote: str | None) -> str:
        """Append ``decision`` and return what happened. Never raises."""
        if self._status is not None or self._worker is None:
            return self._status or FAILED
        try:
            self._decision, self._remote = decision, remote
            self._deadline = time.monotonic() + self._timeout
            self._ready.set()
            self._worker.join(max(0.0, self._deadline - time.monotonic()))
            return self._result[0] if self._result else TIMED_OUT
        except Exception:  # noqa: BLE001 - a broken log must never block or alter a push
            return FAILED


def begin(
    *, repo_root: Path | None, config: Config | None, timeout: float = TIMEOUT_SECONDS
) -> Pending:
    """Start logging one decision. Never raises.

    ``config`` is the loaded configuration, or ``None`` when it could not be loaded,
    and then :func:`fallback_section` decides. ``repo_root`` defaults to the current
    directory, where Git runs the hook. Nothing starts when logging is off or the
    preference is unknown.
    """
    try:
        root = repo_root or Path.cwd()
        section = config.telemetry if config is not None else fallback_section(root)
        if section is None:
            return Pending(UNRESOLVED)
        if not section.hook_log:
            return Pending(DISABLED)
        directory = telemetry.telemetry_dir(
            config if config is not None else Config(telemetry=section)
        )
        pending = Pending()
        pending._start(root, directory, timeout)
        return pending
    except Exception:  # noqa: BLE001 - a broken log must never block or alter a push
        return Pending(FAILED)


def record(
    decision: Decision,
    *,
    repo_root: Path | None,
    config: Config | None,
    remote: str | None,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Log a decision that is already known: :func:`begin`, then :meth:`Pending.finish`."""
    pending = begin(repo_root=repo_root, config=config, timeout=timeout)
    return pending.finish(decision, remote=remote)
