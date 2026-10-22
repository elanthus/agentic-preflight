"""Join hosted pull-request and check outcomes onto pushed runs.

``agentic-preflight telemetry sync-hosted`` is the only telemetry command that
talks to a network service. The user runs it. No workflow command calls it.

* It reads GitHub with ``gh api`` GET requests and never writes to GitHub.
  Each request names only the repository and the pushed commit.
* It writes local derived files only: ``hosted.json`` in each run directory it
  fetched, then that run's ``summary.json`` and mirror row, through the same
  code as ``telemetry rebuild``. It changes no run record and appends no events.
* ``hosted.json`` is never evidence. No fingerprint, evidence digest, or
  attestation reads it.
* A failure is reported in the envelope, never raised. Tokens and ``gh``'s own
  error output are never copied into it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import gitx, telemetry
from .config import Config
from .stages.command import resolve_on_path
from .store import Store, _atomic_write

DEFAULT_LIMIT = 50
SOURCE = "gh"
GITHUB_HOST = "github.com"
GH_TIMEOUT_SECONDS = 60
_PAGE_SIZE = 100
#: A commit with more check runs than this has its first pages counted only.
_MAX_CHECK_PAGES = 10
_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out"})
#: Failures that concern one commit. The sync records them and goes on to older runs.
_COMMIT_ERRORS = frozenset({"http_error", "invalid_response"})
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")
_MESSAGE_LIMIT = 200
_NETWORK_HINTS = (
    "error connecting",
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake",
)


# -- running gh ---------------------------------------------------------------


@dataclass(frozen=True)
class GhResult:
    """What one ``gh`` process returned."""

    returncode: int
    stdout: str
    stderr: str


#: Runs ``gh`` with the given arguments. Tests inject a fake one.
GhRunner = Callable[[Sequence[str]], GhResult]


def gh_runner(timeout: float = GH_TIMEOUT_SECONDS) -> GhRunner | None:
    """A runner for the ``gh`` on PATH, or ``None`` when PATH has no ``gh``.

    The executable is resolved on PATH only, never in the working directory,
    for the same reason as ``git`` (see :func:`resolve_on_path`).
    """
    executable = resolve_on_path("gh")
    if executable is None:
        return None
    environment = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
    }

    def run(args: Sequence[str]) -> GhResult:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=timeout,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return GhResult(completed.returncode, completed.stdout, completed.stderr)

    return run


class GhError(Exception):
    """A ``gh api`` request that gave no usable answer.

    ``message`` is written by this module, or quotes GitHub's own error
    message. It never copies ``gh``'s error output.
    """

    def __init__(self, reason: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.http_status = http_status

    def describe(self, run_id: str | None) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "run_id": run_id,
            "http_status": self.http_status,
            "message": self.message,
        }


def _safe(text: str) -> str:
    """Text for the envelope: credentials and tokens removed, one short line."""
    text = _TOKEN.sub("[redacted]", gitx.redact_remote_credentials(text))
    return " ".join(text.split())[:_MESSAGE_LIMIT]


def _api_message(stdout: str) -> str:
    """GitHub's ``message`` from an error body, or an empty string."""
    try:
        body = json.loads(stdout)
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return _safe(message) if isinstance(message, str) else ""


def _classify(result: GhResult) -> GhError:
    """Name the reason a request failed. ``gh``'s stderr is read, never copied."""
    match = _HTTP_STATUS.search(result.stderr)
    status = int(match.group(1)) if match else None
    detail = _api_message(result.stdout)
    text = f"{detail}\n{result.stderr}".lower()
    if result.returncode == 4 or status == 401 or "gh auth login" in text:
        return GhError(
            "gh_unavailable",
            "gh is not logged in to github.com, or GitHub refused its token.",
            status,
        )
    if status == 429 or (status == 403 and "rate limit" in text):
        return GhError("rate_limited", f"GitHub rate limit reached: {detail or status}", status)
    if status == 403:
        return GhError("forbidden", f"GitHub refused the request: {detail or status}", status)
    if status == 422 and "no commit found" in text:
        return GhError("commit_not_found", "GitHub does not know this commit.", status)
    if status == 404:
        return GhError(
            "not_found", "GitHub has no such repository that this gh account can read.", status
        )
    if status is not None:
        return GhError(
            "http_error", f"GitHub returned HTTP {status}: {detail}".rstrip(": "), status
        )
    if any(hint in text for hint in _NETWORK_HINTS):
        return GhError("network_error", "gh could not reach GitHub.")
    return GhError("request_failed", f"gh exited {result.returncode} without an HTTP status.")


def _api(runner: GhRunner, path: str) -> Any:
    """One read-only ``gh api`` request, parsed. Raises :class:`GhError`."""
    args = [
        "api",
        "--method",
        "GET",
        "--hostname",
        GITHUB_HOST,
        "-H",
        "Accept: application/vnd.github+json",
        path,
    ]
    try:
        result = runner(args)
    except subprocess.TimeoutExpired as exc:
        raise GhError("network_error", "gh did not answer before the time limit.") from exc
    except OSError as exc:
        raise GhError("gh_unavailable", "gh could not be started.") from exc
    if result.returncode != 0:
        raise _classify(result)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GhError("invalid_response", "gh returned a response that is not JSON.") from exc


# -- one commit ---------------------------------------------------------------


def github_slug(remote: str | None) -> str | None:
    """``owner/repo`` for a ``github.com/owner/repo`` identity, else ``None``."""
    if not remote or not remote.startswith(f"{GITHUB_HOST}/"):
        return None
    parts = remote.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return f"{parts[1]}/{parts[2]}"


def _head(item: Mapping[str, Any]) -> Mapping[str, Any]:
    head = item.get("head")
    return head if isinstance(head, Mapping) else {}


def _pull_request(item: Mapping[str, Any]) -> dict[str, Any]:
    number = item.get("number")
    return {
        "number": number if type(number) is int else None,
        "url": telemetry._str(item.get("html_url")),
        "state": telemetry._str(item.get("state")),
        "merged_at": telemetry._str(item.get("merged_at")),
    }


def choose_pull_request(pulls: Any, sha: str, branch: str | None) -> dict[str, Any] | None:
    """The pull request of ``sha`` among those GitHub associates with it.

    The pull request whose head is ``sha`` wins, then the one whose head branch
    is the run's branch, then the first that GitHub lists.
    """
    if not isinstance(pulls, list):
        raise GhError("invalid_response", "GitHub's pull-request list is not a list.")
    items = [item for item in pulls if isinstance(item, Mapping)]
    tests: tuple[Callable[[Mapping[str, Any]], bool], ...] = (
        lambda item: _head(item).get("sha") == sha,
        lambda item: branch is not None and _head(item).get("ref") == branch,
        lambda item: True,
    )
    for test in tests:
        for item in items:
            if test(item):
                return _pull_request(item)
    return None


def reduce_checks(check_runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One conclusion for a commit's check runs.

    ``failure`` when any run failed, was cancelled, or timed out; otherwise
    ``pending`` when any run is not complete; ``none`` when there are no runs;
    else ``success``.
    """
    failed = sorted(
        {
            telemetry._str(run.get("name")) or "unnamed"
            for run in check_runs
            if telemetry._str(run.get("conclusion")) in _FAILED_CONCLUSIONS
        }
    )
    if failed:
        conclusion = "failure"
    elif any(run.get("status") != "completed" for run in check_runs):
        conclusion = "pending"
    elif not check_runs:
        conclusion = "none"
    else:
        conclusion = "success"
    return {"conclusion": conclusion, "total": len(check_runs), "failed": failed}


def _check_runs(runner: GhRunner, slug: str, sha: str) -> list[Mapping[str, Any]]:
    runs: list[Mapping[str, Any]] = []
    for page in range(1, _MAX_CHECK_PAGES + 1):
        body = _api(
            runner, f"/repos/{slug}/commits/{sha}/check-runs?per_page={_PAGE_SIZE}&page={page}"
        )
        batch = body.get("check_runs") if isinstance(body, Mapping) else None
        if not isinstance(batch, list):
            raise GhError("invalid_response", "GitHub's check-run list is not a list.")
        runs.extend(item for item in batch if isinstance(item, Mapping))
        total = body.get("total_count")
        if len(batch) < _PAGE_SIZE or (type(total) is int and len(runs) >= total):
            break
    return runs


def _branch_pull_request(
    runner: GhRunner, slug: str, sha: str, branch: str
) -> dict[str, Any] | None:
    """The pull request of the run's branch whose head is ``sha``, if there is one.

    GitHub does not associate a commit with a pull request that was closed
    without merging. That pull request still names its last commit as its head.
    """
    head = quote(f"{slug.split('/', 1)[0]}:{branch}", safe="")
    pulls = _api(runner, f"/repos/{slug}/pulls?state=all&head={head}&per_page={_PAGE_SIZE}")
    if not isinstance(pulls, list):
        raise GhError("invalid_response", "GitHub's pull-request list is not a list.")
    for item in pulls:
        if isinstance(item, Mapping) and _head(item).get("sha") == sha:
            return _pull_request(item)
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fetch(runner: GhRunner, slug: str, sha: str, branch: str | None) -> dict[str, Any]:
    """The ``hosted.json`` document for one pushed commit. Raises :class:`GhError`.

    When GitHub associates no pull request with the commit, the pull requests
    of the run's branch are searched for one whose head is the commit. A commit
    that GitHub does not know is recorded with ``missing`` true, so the next
    sync does not ask for it again.
    """
    document: dict[str, Any] = {
        "fetched_at": _now(),
        "sha": sha,
        "pr": None,
        "checks": None,
        "missing": False,
        "source": SOURCE,
    }
    try:
        pulls = _api(runner, f"/repos/{slug}/commits/{sha}/pulls?per_page={_PAGE_SIZE}")
        pr = choose_pull_request(pulls, sha, branch)
        if pr is None and branch:
            pr = _branch_pull_request(runner, slug, sha, branch)
        checks = reduce_checks(_check_runs(runner, slug, sha))
    except GhError as exc:
        if exc.reason != "commit_not_found":
            raise
        return {**document, "missing": True}
    return {**document, "pr": pr, "checks": checks}


# -- the sync -----------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    run_id: str
    sha: str
    branch: str | None
    created_at: str
    synced: bool
    #: Synced, but ``summary.json`` does not carry this ``hosted.json`` yet.
    summary_stale: bool


def _summary_fetched_at(store: Store, run_id: str) -> Any:
    """The ``hosted.fetched_at`` that the run's ``summary.json`` carries, if any."""
    try:
        path = store.run_dir(run_id) / telemetry.SUMMARY_NAME
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    hosted = summary.get("hosted") if isinstance(summary, dict) else None
    return hosted.get("fetched_at") if isinstance(hosted, dict) else None


def _unreadable(run_id: str, exc: telemetry._Unreadable) -> dict[str, str]:
    return {
        "run_id": run_id,
        "path": str(exc.path),
        "reason": exc.reason,
        "diagnostic": exc.diagnostic,
    }


def _candidates(
    store: Store, run_ids: Sequence[str]
) -> tuple[list[_Candidate], dict[str, int], list[dict[str, str]]]:
    """The ``DONE`` runs with a pushed commit, newest first, and what was left out."""
    eligible: list[_Candidate] = []
    left_out = {"not_done": 0, "no_pushed_sha": 0}
    unreadable: list[dict[str, str]] = []
    for run_id in run_ids:
        try:
            record = telemetry._read_record(store, run_id)
        except telemetry._Unreadable as exc:
            unreadable.append(_unreadable(run_id, exc))
            continue
        state, _ = telemetry._effective_state(record)
        sha = record.get("pushed_sha")
        if state != "DONE":
            left_out["not_done"] += 1
        elif not isinstance(sha, str) or not _SHA.fullmatch(sha):
            left_out["no_pushed_sha"] += 1
        else:
            existing = telemetry._read_hosted(store, run_id)
            synced = existing is not None and existing.get("sha") == sha
            eligible.append(
                _Candidate(
                    run_id=run_id,
                    sha=sha,
                    branch=telemetry._str(record.get("branch")),
                    created_at=telemetry._str(record.get("created_at")) or "",
                    synced=synced,
                    summary_stale=synced
                    and existing is not None
                    and existing.get("fetched_at") != _summary_fetched_at(store, run_id),
                )
            )
    eligible.sort(key=lambda item: (item.created_at, item.run_id), reverse=True)
    return eligible, left_out, unreadable


def _outcome(candidate: _Candidate, document: Mapping[str, Any] | None) -> dict[str, Any]:
    """One run's line in ``data.runs``: the planned fetch, or what it found."""
    pr = (document or {}).get("pr") or {}
    checks = (document or {}).get("checks") or {}
    if document is None:
        outcome = "planned"
    elif document.get("missing"):
        outcome = "commit_not_found"
    else:
        outcome = "synced"
    return {
        "run_id": candidate.run_id,
        "sha": candidate.sha,
        "outcome": outcome,
        "pr_number": pr.get("number"),
        "pr_state": pr.get("state"),
        "merged": bool(pr.get("merged_at")) if pr else None,
        "checks_conclusion": checks.get("conclusion"),
    }


def resummarize(
    store: Store, identity: telemetry.RepoIdentity, config: Config, run_ids: Sequence[str]
) -> dict[str, Any]:
    """Regenerate the runs' ``summary.json`` and mirror rows with the rebuild's code."""
    written_at = telemetry._now()
    fresh: list[telemetry.RunSummary] = []
    unreadable: list[dict[str, str]] = []
    write_failures: list[dict[str, str]] = []
    for run_id in run_ids:
        try:
            summary = telemetry._summarize_stored_run_safely(store, run_id, identity, written_at)
        except telemetry._Unreadable as exc:
            unreadable.append(_unreadable(run_id, exc))
            continue
        if summary is None:
            continue
        fresh.append(summary)
        try:
            path = store.run_dir(run_id) / telemetry.SUMMARY_NAME
            _atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            write_failures.append({"run_id": run_id, "error": telemetry._describe(exc)})
    mirror: dict[str, Any] = {
        "enabled": config.telemetry.mirror,
        "path": None,
        "rows": None,
        "preserved_rows": None,
        "error": None,
    }
    if config.telemetry.mirror and fresh:
        target = telemetry.mirror_path(config)
        mirror["path"] = str(target)
        try:
            mirror["rows"], mirror["preserved_rows"] = telemetry._rewrite_mirror(
                target, fresh, identity.root_commit
            )
        except (OSError, UnicodeDecodeError) as exc:
            # An unreadable mirror is left exactly as it was.
            mirror["error"] = telemetry._describe(exc)
    return {
        "regenerated": len(fresh),
        "unreadable_runs": unreadable,
        "write_failures": write_failures,
        "mirror": mirror,
    }


class UnknownRun(LookupError):
    """``--run`` names no stored run in this clone."""


def _fetch_all(
    store: Store, runner: GhRunner, slug: str, selected: Sequence[_Candidate], data: dict[str, Any]
) -> list[str]:
    """Fetch and store each selected run. Returns the runs written.

    A failure that concerns one commit is recorded in ``data["errors"]``, and
    the older runs are still fetched. Any other failure stops the loop.
    """
    written: list[str] = []
    for candidate in selected:
        try:
            document = fetch(runner, slug, candidate.sha, candidate.branch)
        except GhError as exc:
            if exc.reason in _COMMIT_ERRORS:
                data["errors"].append(exc.describe(candidate.run_id))
                continue
            data["stopped"] = exc.describe(candidate.run_id)
            break
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            # An answer of an unexpected shape must not lose the runs already synced.
            reading = f"GitHub's answer could not be read ({type(exc).__name__})."
            data["errors"].append(GhError("invalid_response", reading).describe(candidate.run_id))
            continue
        try:
            path = store.run_dir(candidate.run_id) / telemetry.HOSTED_NAME
            _atomic_write(path, json.dumps(document, indent=2) + "\n")
        except OSError as exc:
            data["stopped"] = GhError("write_failed", telemetry._describe(exc)).describe(
                candidate.run_id
            )
            break
        written.append(candidate.run_id)
        data["runs"].append(_outcome(candidate, document))
        if document["missing"]:
            data["commit_not_found"] += 1
    return written


def sync_hosted(
    store: Store,
    repo_root: Path | str,
    config: Config,
    *,
    limit: int = DEFAULT_LIMIT,
    run_id: str | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    runner: GhRunner | None = None,
) -> dict[str, Any]:
    """Join GitHub outcomes onto this clone's pushed runs and return the envelope data.

    ``data["stopped"]`` names the failure that stopped the sync, and the runs
    synced before it stay synced. ``data["errors"]`` lists the commits that
    failed alone. A synced run whose summary does not carry its ``hosted.json``,
    for example after an interrupted sync, has its summary regenerated without a
    request. A dry run reads nothing from GitHub and writes nothing.
    """
    identity = telemetry.repo_identity(repo_root)
    repo = identity.model_dump(mode="json", exclude={"source_worktree_path"})
    if isinstance(repo["remote"], str):
        repo["remote"] = gitx.redact_remote_credentials(repo["remote"])
    data: dict[str, Any] = {
        "repo": repo,
        "dry_run": dry_run,
        "refresh": refresh,
        "limit": limit,
        "run": run_id,
        "eligible": 0,
        "not_eligible": {"not_done": 0, "no_pushed_sha": 0},
        "unreadable": 0,
        "unreadable_runs": [],
        "already_synced": 0,
        "selected": 0,
        "deferred": 0,
        "synced": 0,
        "commit_not_found": 0,
        "stale_summaries": 0,
        "gh_found": runner is not None or resolve_on_path("gh") is not None,
        "runs": [],
        "errors": [],
        "stopped": None,
        "summaries": None,
    }
    slug = github_slug(identity.remote)
    if slug is None:
        data["stopped"] = GhError(
            "unsupported_remote",
            f"origin is not a github.com repository: {repo['remote'] or 'no origin remote'}",
        ).describe(run_id)
        return data
    if run_id is not None and not store.run_path(run_id).exists():
        raise UnknownRun(run_id)
    candidates, left_out, unreadable = _candidates(
        store, [run_id] if run_id is not None else store.list_runs()
    )
    pending = [item for item in candidates if refresh or not item.synced]
    selected = pending[:limit]
    chosen = {item.run_id for item in selected}
    stale = [item.run_id for item in candidates if item.summary_stale and item.run_id not in chosen]
    data.update(
        eligible=len(candidates),
        not_eligible=left_out,
        unreadable=len(unreadable),
        unreadable_runs=unreadable,
        already_synced=len(candidates) - len(pending),
        selected=len(selected),
        deferred=len(pending) - len(selected),
        stale_summaries=len(stale),
    )
    if dry_run:
        data["runs"] = [_outcome(item, None) for item in selected]
        return data
    written: list[str] = []
    if selected:
        runner = runner or gh_runner()
        data["gh_found"] = runner is not None
        if runner is None:
            data["stopped"] = GhError("gh_unavailable", "gh was not found on PATH.").describe(None)
        else:
            written = _fetch_all(store, runner, slug, selected, data)
    data["synced"] = len(written)
    if written or stale:
        data["summaries"] = resummarize(store, identity, config, [*written, *stale])
    return data
