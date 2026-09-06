"""Small, bounded GitHub REST transport; no artifact or log execution."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class APIUnavailable(ValueError):
    """GitHub could not establish an authoritative result."""


class GitHub:
    def __init__(self, repository: str, *, public: bool = False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
            raise ValueError("repository must be OWNER/REPO")
        self.repository = repository
        self.public = public

    def scoped(self, repository: str, *, public: bool = False) -> GitHub:
        return GitHub(repository, public=public)

    def _public_request(self, path: str) -> Any:
        # A base-repository installation token need not have access to a
        # contributor's fork. Public notes require no credentials at all.
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "agentic-preflight"},
        )
        try:
            with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS GitHub host
                payload = response.read(8_000_001)
            if len(payload) > 8_000_000:
                raise APIUnavailable("public GitHub response exceeded the read limit")
            return json.loads(payload)
        except (URLError, OSError, ValueError) as exc:
            raise APIUnavailable(
                "public fork API unavailable; retry after restoring access"
            ) from exc

    def request(self, path: str, *, method: str = "GET", body: dict | None = None) -> Any:
        if self.public and (method != "GET" or body is not None):
            raise ValueError("public fork access is read-only")
        args = ["gh", "api", "--method", method, f"repos/{self.repository}/{path}"]
        if body is not None:
            args += ["--input", "-"]
        try:
            result = subprocess.run(
                args,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise APIUnavailable("GitHub API unavailable; retry after restoring gh access") from exc
        if result.returncode:
            if self.public:
                return self._public_request(path)
            raise APIUnavailable(f"GitHub API unavailable for {method} {path.split('?')[0]}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise APIUnavailable("GitHub returned malformed JSON") from exc

    def pages(self, path: str, key: str | None = None) -> list[dict]:
        rows: list[dict] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 11):
            response = self.request(f"{path}{separator}per_page=100&page={page}")
            items = response.get(key) if key and isinstance(response, dict) else response
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise APIUnavailable("GitHub returned malformed pagination")
            rows.extend(items)
            if len(items) < 100:
                if key and response.get("total_count", len(rows)) != len(rows):
                    raise APIUnavailable(
                        "GitHub results changed or were incomplete during pagination"
                    )
                return rows
        raise APIUnavailable("GitHub result exceeds the bounded pagination limit")

    def contents(self, path: str, revision: str) -> str:
        result = self.request(f"contents/{quote(path, safe='/')}?ref={quote(revision, safe='')}")
        try:
            if result["encoding"] != "base64":
                raise ValueError("unsupported encoding")
            return base64.b64decode(result["content"]).decode("utf-8")
        except (KeyError, TypeError, ValueError) as exc:
            raise APIUnavailable("GitHub could not supply protected file contents") from exc

    def note(self, sha: str) -> str:
        ref = self.request("git/ref/notes/agentic-preflight")
        commit = self.request(f"git/commits/{ref['object']['sha']}")
        tree = self.request(f"git/trees/{commit['tree']['sha']}?recursive=1")
        if tree.get("truncated"):
            raise APIUnavailable("published notes tree was truncated")
        matches = [
            entry
            for entry in tree["tree"]
            if entry["type"] == "blob" and entry["path"].replace("/", "") == sha
        ]
        if len(matches) != 1:
            raise APIUnavailable("exact head has no unique published preflight note")
        blob = self.request(f"git/blobs/{matches[0]['sha']}")
        if blob.get("encoding") != "base64":
            raise APIUnavailable("published note has an unsupported encoding")
        try:
            return base64.b64decode(blob["content"]).decode("utf-8")
        except (KeyError, TypeError, ValueError) as exc:
            raise APIUnavailable("published note could not be decoded") from exc
