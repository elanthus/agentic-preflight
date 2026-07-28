"""Pull requests via the `gh` CLI, deliberately not the API.

``gh`` owns authentication. "No credential handling in our code" is a design
invariant, not a convenience: we never read a token, never touch a keyring,
never plumb ``GITHUB_TOKEN``. If `gh` is missing or unauthenticated we hand back
a prefilled compare URL and stop, which is a worse experience and a much better
security posture.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GhUnavailable(Exception):
    """`gh` is missing, or present but not authenticated."""


@dataclass
class PullRequest:
    url: str
    created: bool


@dataclass
class PullRequestStatus:
    url: str
    state: str
    merged_at: str | None
    head: str
    base: str

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED" and self.merged_at is not None


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    conclusion: str
    details_url: str = ""
    run_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "details_url": self.details_url,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class PullRequestHealth:
    url: str
    state: str
    merge_state: str
    outcome: str
    checks: list[CheckResult]
    failed_checks: list[CheckResult]

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "state": self.state,
            "merge_state": self.merge_state,
            "outcome": self.outcome,
            "checks": [check.as_dict() for check in self.checks],
            "failed_checks": [check.as_dict() for check in self.failed_checks],
        }


FAILED_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
SUCCESS_CONCLUSIONS = {"NEUTRAL", "SKIPPED", "SUCCESS"}
RUN_ID_PATTERN = re.compile(r"/actions/runs/(?P<run_id>\d+)(?:/|$)")


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated(cwd: Path | str) -> bool:
    if not gh_available():
        return False
    result = subprocess.run(
        ["gh", "auth", "status"], cwd=str(cwd), capture_output=True, text=True
    )
    return result.returncode == 0


def create_pull_request(
    cwd: Path | str,
    *,
    base: str,
    head: str,
    title: str,
    body: str,
    draft: bool = False,
) -> PullRequest:
    """Open a PR. Note the argv: no token is ever passed."""
    if not gh_available():
        raise GhUnavailable("gh is not installed or not on PATH")
    if not gh_authenticated(cwd):
        raise GhUnavailable("gh is installed but not authenticated (`gh auth login`)")

    argv = [
        "gh", "pr", "create",
        "--base", base,
        "--head", head,
        "--title", title,
        "--body", body,
    ]
    if draft:
        argv.append("--draft")

    result = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise GhUnavailable(f"gh pr create failed: {result.stderr.strip()}")

    url = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")),
        "",
    )
    return PullRequest(url=url, created=True)


def create_or_update_pull_request(
    cwd: Path | str,
    *,
    base: str,
    head: str,
    title: str,
    body: str,
    draft: bool = False,
) -> PullRequest:
    """Update an existing open PR for the branch, or create its first PR."""
    if not gh_available():
        raise GhUnavailable("gh is not installed or not on PATH")
    if not gh_authenticated(cwd):
        raise GhUnavailable("gh is installed but not authenticated (`gh auth login`)")
    listed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "url",
            "--limit",
            "1",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        raise GhUnavailable(f"gh pr list failed: {listed.stderr.strip()}")
    try:
        matches = json.loads(listed.stdout)
        existing_url = str(matches[0]["url"]) if matches else ""
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise GhUnavailable("gh pr list returned invalid data") from exc

    if not existing_url:
        return create_pull_request(
            cwd, base=base, head=head, title=title, body=body, draft=draft
        )

    edited = subprocess.run(
        ["gh", "pr", "edit", existing_url, "--title", title, "--body", body],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if edited.returncode != 0:
        raise GhUnavailable(f"gh pr edit failed: {edited.stderr.strip()}")
    return PullRequest(url=existing_url, created=False)


def pull_request_status(cwd: Path | str, url: str) -> PullRequestStatus:
    """Read merge state through ``gh``; authentication remains entirely gh's."""
    if not gh_available():
        raise GhUnavailable("gh is not installed or not on PATH")
    if not gh_authenticated(cwd):
        raise GhUnavailable("gh is installed but not authenticated (`gh auth login`)")

    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json",
            "url,state,mergedAt,headRefName,baseRefName",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhUnavailable(f"gh pr view failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
        return PullRequestStatus(
            url=str(payload.get("url") or url),
            state=str(payload["state"]),
            merged_at=payload.get("mergedAt"),
            head=str(payload["headRefName"]),
            base=str(payload["baseRefName"]),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GhUnavailable("gh pr view returned invalid merge status") from exc


def parse_pr_health(payload: dict[str, object]) -> PullRequestHealth:
    """Classify GitHub's PR/check rollup into one deterministic outcome."""
    try:
        url = str(payload["url"])
        state = str(payload["state"]).upper()
        merge_state = str(payload.get("mergeStateStatus") or "UNKNOWN").upper()
        raw_checks = payload.get("statusCheckRollup") or []
        if not isinstance(raw_checks, list):
            raise TypeError("statusCheckRollup is not a list")
    except (KeyError, TypeError) as exc:
        raise GhUnavailable("gh pr view returned invalid health data") from exc

    checks: list[CheckResult] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            continue
        details_url = str(item.get("detailsUrl") or item.get("targetUrl") or "")
        context_state = str(item.get("state") or "").upper()
        conclusion = str(item.get("conclusion") or context_state).upper()
        status = str(item.get("status") or "").upper()
        if not status and context_state:
            status = "IN_PROGRESS" if context_state == "PENDING" else "COMPLETED"
        match = RUN_ID_PATTERN.search(details_url)
        checks.append(
            CheckResult(
                name=str(item.get("name") or item.get("context") or "unnamed check"),
                status=status,
                conclusion=conclusion,
                details_url=details_url,
                run_id=match.group("run_id") if match else None,
            )
        )

    failed = [check for check in checks if check.conclusion in FAILED_CONCLUSIONS]
    if merge_state == "DIRTY":
        failed.append(
            CheckResult(
                name="mergeability",
                status="COMPLETED",
                conclusion="CONFLICT",
            )
        )

    if state == "MERGED":
        outcome = "merged"
    elif state == "CLOSED":
        outcome = "closed"
    elif failed:
        outcome = "failed"
    elif all(check.conclusion in SUCCESS_CONCLUSIONS for check in checks):
        outcome = "checks_passed" if merge_state in {"CLEAN", "HAS_HOOKS"} else "pending"
    else:
        outcome = "pending"

    return PullRequestHealth(
        url=url,
        state=state,
        merge_state=merge_state,
        outcome=outcome,
        checks=checks,
        failed_checks=failed,
    )


def pull_request_health(cwd: Path | str, url: str) -> PullRequestHealth:
    """Read PR state, mergeability, and checks through the authenticated gh CLI."""
    if not gh_available():
        raise GhUnavailable("gh is not installed or not on PATH")
    if not gh_authenticated(cwd):
        raise GhUnavailable("gh is installed but not authenticated (`gh auth login`)")

    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json",
            "url,state,mergeStateStatus,statusCheckRollup",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GhUnavailable(f"gh pr view failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError("payload is not an object")
        return parse_pr_health(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GhUnavailable("gh pr view returned invalid health data") from exc


def failed_check_logs(
    cwd: Path | str,
    checks: list[CheckResult],
    *,
    max_chars: int = 64_000,
) -> dict[str, str]:
    """Fetch failed GitHub Actions logs once per run, with a context-safe cap."""
    logs: dict[str, str] = {}
    for run_id in dict.fromkeys(check.run_id for check in checks if check.run_id):
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--log-failed"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        output = result.stdout if result.returncode == 0 else result.stderr
        if len(output) > max_chars:
            output = output[:max_chars] + "\n[truncated by agentic-preflight]"
        logs[str(run_id)] = output.strip()
    return logs
