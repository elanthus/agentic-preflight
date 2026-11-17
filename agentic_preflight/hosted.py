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

from . import gitx, run_summary, telemetry
from .config import Config
from .stages.command import resolve_on_path
from .store import Store, atomic_write

DEFAULT_LIMIT = 50
DEFAULT_REVERTS_LIMIT = 10
_MAX_ESCAPE_PAGES = 10
_FAILURE_CLASSES = ("local_equivalent", "hosted_only", "unclassified")
SOURCE = "gh"
GITHUB_HOST = "github.com"
GH_TIMEOUT_SECONDS = 60
_PAGE_SIZE = 100
#: A commit with more check runs than this has its first pages counted only.
_MAX_CHECK_PAGES = 10
#: ``startup_failure`` is a workflow that could not start, so it never passed.
_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})
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
        "url": run_summary.as_str(item.get("html_url")),
        "state": run_summary.as_str(item.get("state")),
        "merged_at": run_summary.as_str(item.get("merged_at")),
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

    ``failure`` when any run failed, was cancelled, timed out, or could not
    start; otherwise
    ``pending`` when any run is not complete; ``none`` when there are no runs;
    else ``success``.
    """
    failed = sorted(
        {
            run_summary.as_str(run.get("name")) or "unnamed"
            for run in check_runs
            if run_summary.as_str(run.get("conclusion")) in _FAILED_CONCLUSIONS
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
        if any(not isinstance(item, Mapping) for item in batch):
            raise GhError("invalid_response", "GitHub returned a malformed check run.")
        runs.extend(batch)
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
        check_runs = _check_runs(runner, slug, sha)
        checks = reduce_checks(check_runs)
        checks["settled"] = len(check_runs) < _MAX_CHECK_PAGES * _PAGE_SIZE and all(
            item.get("status") == "completed" for item in check_runs
        )
    except GhError as exc:
        if exc.reason != "commit_not_found":
            raise
        return {**document, "missing": True}
    return {**document, "pr": pr, "checks": checks}


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _pushed_at(events: Sequence[Mapping[str, Any]], sha: str) -> datetime | None:
    matches = [
        event for event in events if event.get("event") == "pushed" and event.get("sha") == sha
    ]
    return _date(matches[-1].get("at")) if matches else None


def _list_all(runner: GhRunner, path: str) -> list[Mapping[str, Any]] | None:
    items: list[Mapping[str, Any]] = []
    for page in range(1, _MAX_ESCAPE_PAGES + 1):
        batch = _api(runner, f"{path}?per_page={_PAGE_SIZE}&page={page}")
        if not isinstance(batch, list) or any(not isinstance(item, Mapping) for item in batch):
            return None
        items.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return items
    return None  # A capped response cannot establish a zero or an exact count.


def _followups(
    items: list[Mapping[str, Any]] | None, pushed: datetime, validated: set[str]
) -> int | None:
    # GitHub's PR commit endpoint itself stops at 250, even with pagination.
    if items is None or len(items) >= 250:
        return None
    found: set[str] = set()
    for item in items:
        sha = item.get("sha")
        commit = run_summary.as_dict(item.get("commit")) or {}
        committer = run_summary.as_dict(commit.get("committer")) or {}
        date = _date(committer.get("date"))
        if not isinstance(sha, str) or not _SHA.fullmatch(sha) or date is None:
            return None
        if date > pushed and sha not in validated:
            found.add(sha)
    return len(found)


def _human_count(
    items: list[Mapping[str, Any]] | None,
    pushed: datetime,
    *,
    date_key: str,
    state: str | None = None,
) -> int | None:
    if items is None:
        return None
    found: set[int] = set()
    for item in items:
        user = run_summary.as_dict(item.get("user")) or {}
        if user.get("type") == "Bot":
            continue
        if user.get("type") != "User":
            return None
        if state is not None:
            if not isinstance(item.get("state"), str):
                return None
            if item["state"] != state:
                continue
        date = _date(item.get(date_key))
        identity = item.get("id")
        if date is None or type(identity) is not int:
            return None
        if date > pushed:
            found.add(identity)
    return len(found)


def _reverted(runner: GhRunner, slug: str, number: int) -> bool | None:
    pr = _api(runner, f"/repos/{slug}/pulls/{number}")
    if not isinstance(pr, Mapping):
        return None
    sha, merged = pr.get("merge_commit_sha"), _date(pr.get("merged_at"))
    base = run_summary.as_dict(pr.get("base")) or {}
    branch = base.get("ref")
    if (
        not isinstance(sha, str)
        or not _SHA.fullmatch(sha)
        or merged is None
        or not isinstance(branch, str)
    ):
        return None
    # One bounded page per eligible merged run. A full page without a match is unknown.
    batch = _api(
        runner,
        f"/repos/{slug}/commits?sha={quote(branch, safe='')}&since={quote(merged.isoformat(), safe='')}&per_page={_PAGE_SIZE}",
    )
    if not isinstance(batch, list):
        return None
    valid = True
    pattern = re.compile(r"(?im)^This reverts commit " + re.escape(sha) + r"\.?(?:\s|$)")
    for item in batch:
        commit = run_summary.as_dict(item.get("commit")) if isinstance(item, Mapping) else None
        committer = run_summary.as_dict(commit.get("committer")) if commit else None
        date = _date(committer.get("date")) if committer else None
        message = commit.get("message") if commit else None
        if date is None or not isinstance(message, str):
            valid = False
        elif date > merged and (
            pattern.search(message)
            or (
                message.startswith('Revert "')
                and re.search(
                    r"(?<![0-9a-f])" + re.escape(sha) + r"(?![0-9a-f])", message.split("\n", 1)[-1]
                )
            )
        ):
            return True
    return False if valid and len(batch) < _PAGE_SIZE else None


def _stage_executions(
    record: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> dict[str, bool | None]:
    # Attempts count failures, not executions: a first-pass success has zero.
    executed: dict[str, bool | None] = {}
    for name, stage in run_summary._stages(record).items():
        if stage["status"] == "skipped" or stage["reused"] is True:
            executed[name] = False
        elif stage["status"] in {"green", "red"} and stage["reused"] is False:
            executed[name] = True
        else:
            executed[name] = None
    # Completion events also retain an earlier execution when the final result
    # was later replaced by reused evidence or a skipped stage.
    for event in events:
        event_name = event.get("event")
        if event_name in ("lint_finished", "test_finished"):
            executed[event_name.removesuffix("_finished")] = True
        elif event_name == "findings_submitted" and event.get("stage") in ("review", "docs"):
            executed[event["stage"]] = True
    return executed


def _failed_classes(
    checks: Mapping[str, Any],
    mapping: Mapping[str, str] | None,
    executed: Mapping[str, bool | None],
) -> dict[str, int | None]:
    failed = checks.get("failed")
    if not checks.get("settled") or not isinstance(failed, list):
        return dict.fromkeys(_FAILURE_CLASSES)
    if mapping is None:
        return {"local_equivalent": None, "hosted_only": None, "unclassified": len(failed)}
    counts = dict.fromkeys(_FAILURE_CLASSES, 0)
    for name in failed:
        stage = mapping.get(name)
        if stage is None:
            category = "hosted_only"
        else:
            ran = executed.get(stage)
            category = (
                "local_equivalent"
                if ran is True
                else "hosted_only"
                if ran is False
                else "unclassified"
            )
        counts[category] += 1
    return {name: counts[name] for name in _FAILURE_CLASSES}


def _escapes(
    runner: GhRunner,
    slug: str,
    document: dict[str, Any],
    candidate: _Candidate,
    validated: set[str],
    mapping: Mapping[str, str] | None,
    *,
    reverts: bool,
) -> None:
    document.update(
        followup_commits=None,
        changes_requested=None,
        review_comments=None,
        reverted=None,
        failed_checks=_failed_classes(
            document.get("checks") or {}, mapping, candidate.executed_stages
        ),
    )
    pr = document.get("pr") or {}
    number = pr.get("number")
    if type(number) is not int:
        return
    if candidate.pushed_at is not None:
        prefix = f"/repos/{slug}/pulls/{number}"
        document["followup_commits"] = _followups(
            _list_all(runner, prefix + "/commits"), candidate.pushed_at, validated
        )
        document["changes_requested"] = _human_count(
            _list_all(runner, prefix + "/reviews"),
            candidate.pushed_at,
            date_key="submitted_at",
            state="CHANGES_REQUESTED",
        )
        document["review_comments"] = _human_count(
            _list_all(runner, prefix + "/comments"),
            candidate.pushed_at,
            date_key="created_at",
        )
    if reverts and pr.get("merged_at"):
        document["reverted"] = _reverted(runner, slug, number)


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
    pushed_at: datetime | None
    executed_stages: Mapping[str, bool | None]


def _summary_fetched_at(store: Store, run_id: str) -> Any:
    """The ``hosted.fetched_at`` that the run's ``summary.json`` carries, if any."""
    try:
        path = store.run_dir(run_id) / telemetry.SUMMARY_NAME
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    hosted = summary.get("hosted") if isinstance(summary, dict) else None
    return hosted.get("fetched_at") if isinstance(hosted, dict) else None


def _unreadable(run_id: str, exc: telemetry.UnreadableRecord) -> dict[str, str]:
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
            record = telemetry.read_record(store, run_id)
        except telemetry.UnreadableRecord as exc:
            unreadable.append(_unreadable(run_id, exc))
            continue
        state, _ = run_summary.effective_state(record)
        sha = record.get("pushed_sha")
        if state != "DONE":
            left_out["not_done"] += 1
        elif not isinstance(sha, str) or not _SHA.fullmatch(sha):
            left_out["no_pushed_sha"] += 1
        else:
            existing = telemetry.read_hosted(store, run_id)
            synced = existing is not None and existing.get("sha") == sha
            try:
                events = store.load_events(run_id)
            except OSError:
                events = []
            eligible.append(
                _Candidate(
                    run_id=run_id,
                    sha=sha,
                    pushed_at=_pushed_at(events, sha),
                    executed_stages=_stage_executions(record, events),
                    branch=run_summary.as_str(record.get("branch")),
                    created_at=run_summary.as_str(record.get("created_at")) or "",
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
    store: Store, identity: run_summary.RepoIdentity, config: Config, run_ids: Sequence[str]
) -> dict[str, Any]:
    """Regenerate the runs' ``summary.json`` and mirror rows with the rebuild's code."""
    written_at = run_summary.utc_now()
    fresh: list[run_summary.RunSummary] = []
    unreadable: list[dict[str, str]] = []
    write_failures: list[dict[str, str]] = []
    for run_id in run_ids:
        try:
            summary = telemetry.summarize_stored_run_safely(store, run_id, identity, written_at)
        except telemetry.UnreadableRecord as exc:
            unreadable.append(_unreadable(run_id, exc))
            continue
        if summary is None:
            continue
        fresh.append(summary)
        try:
            path = store.run_dir(run_id) / telemetry.SUMMARY_NAME
            atomic_write(path, summary.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            write_failures.append({"run_id": run_id, "error": telemetry.describe_error(exc)})
    return {
        "regenerated": len(fresh),
        "unreadable_runs": unreadable,
        "write_failures": write_failures,
        "mirror": telemetry.replace_mirror_rows(
            config, fresh, identity, rewrite_without_rows=False
        ),
    }


class UnknownRun(LookupError):
    """``--run`` names no stored run in this clone."""


def _fetch_all(
    store: Store,
    runner: GhRunner,
    slug: str,
    selected: Sequence[_Candidate],
    data: dict[str, Any],
    config: Config,
    validated: set[str],
    *,
    reverts: bool,
    reverts_limit: int,
) -> list[str]:
    """Fetch and store each selected run. Returns the runs written.

    A failure that concerns one commit is recorded in ``data["errors"]``, and
    the older runs are still fetched. Any other failure stops the loop.
    """
    written: list[str] = []
    revert_requests = 0
    for candidate in selected:
        try:
            document = fetch(runner, slug, candidate.sha, candidate.branch)
            eligible_revert = bool(reverts and (document.get("pr") or {}).get("merged_at"))
            inspect_revert = eligible_revert and revert_requests < reverts_limit
            if inspect_revert:
                revert_requests += 1
            _escapes(
                runner,
                slug,
                document,
                candidate,
                validated,
                config.telemetry.hosted.local_equivalent_checks,
                reverts=inspect_revert,
            )
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
            atomic_write(path, json.dumps(document, indent=2) + "\n")
        except OSError as exc:
            data["stopped"] = GhError("write_failed", telemetry.describe_error(exc)).describe(
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
    reverts: bool = False,
    reverts_limit: int = DEFAULT_REVERTS_LIMIT,
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
        "reverts": reverts,
        "reverts_limit": reverts_limit,
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
    validated: set[str] = set()
    for retained_id in store.list_runs():
        try:
            retained = telemetry.read_record(store, retained_id)
        except telemetry.UnreadableRecord:
            continue
        pushed_sha = retained.get("pushed_sha")
        if isinstance(pushed_sha, str) and _SHA.fullmatch(pushed_sha):
            validated.add(pushed_sha)
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
            written = _fetch_all(
                store,
                runner,
                slug,
                selected,
                data,
                config,
                validated,
                reverts=reverts,
                reverts_limit=reverts_limit,
            )
    data["synced"] = len(written)
    if written or stale:
        data["summaries"] = resummarize(store, identity, config, [*written, *stale])
    return data
