"""Working out where a push would go, from the remote URL alone.

Host-aware rather than hostname-matching on ``github.com``, so GitHub Enterprise
installations work instead of being silently classed as unsupported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SSH_SCP = re.compile(r"^(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+):(?P<path>.+?)(?:\.git)?/?$")
URL_FORM = re.compile(
    r"^(?P<scheme>ssh|https?|git)://(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+)"
    r"(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"
)


@dataclass
class Remote:
    url: str
    host: str
    owner: str
    repo: str

    @property
    def provider(self) -> str:
        # Any host whose name starts with `github` is a GitHub deployment:
        # github.com, github.example.com, github.internal, and so on.
        first_label = self.host.split(".")[0].lower()
        return "github" if first_label == "github" else "unsupported"

    @property
    def web_base(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}"


def parse_remote(url: str) -> Remote:
    """Parse SSH (both forms) and HTTPS remote URLs into host/owner/repo."""
    match = URL_FORM.match(url) or SSH_SCP.match(url)
    if not match:
        return Remote(url=url, host="", owner="", repo="")

    host = match.group("host")
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]

    parts = path.split("/")
    owner = parts[0] if parts else ""
    repo = parts[-1] if len(parts) > 1 else ""
    return Remote(url=url, host=host, owner=owner, repo=repo)


def compare_url(remote: Remote, *, base: str, head: str) -> str:
    """A prefilled compare page — the fallback when `gh` cannot be used."""
    return f"{remote.web_base}/compare/{base}...{head}?expand=1"
