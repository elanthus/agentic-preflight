"""Pull requests via the `gh` CLI, deliberately not the API.

``gh`` owns authentication. "No credential handling in our code" is a design
invariant, not a convenience: we never read a token, never touch a keyring,
never plumb ``GITHUB_TOKEN``. If `gh` is missing or unauthenticated we hand back
a prefilled compare URL and stop, which is a worse experience and a much better
security posture.
"""

from __future__ import annotations

import json
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
