"""Verify bot provenance using authenticated API responses and inert Git objects.

Run this script from the protected base checkout, never from the proposed branch.
The caller must fetch the exact event head and obtain both JSON files through gh.
It must also pass the event name and the event payload file. Commit metadata shows
only that GitHub created a commit, not who placed it at the head; the payload's sender
names the account whose action produced the event. A re-run replays that payload.
This verifies provenance only; it does not replace attestation or CI validation.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from agentic_preflight import gitx

OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BOT_LOGIN = "dependabot[bot]"
# Only these pull_request_target actions name the account that set the current head as
# their sender. Any other action, such as reopened, ready_for_review, or an auto-merge
# toggle, names whoever performed that action, and the head may have changed while the
# pull request was closed, so the account that set the head cannot be established.
HEAD_SETTING_ACTIONS = frozenset({"opened", "synchronize"})


def _pages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("commits must be API pages")
    result = []
    for page in value:
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise ValueError("invalid commits page")
        result.extend(page)
    return result


def _head_actor_reason(event_name: str, event: object, head: str, base: str) -> str | None:
    """Fail closed unless Dependabot itself set the exact head through this event."""
    if event_name != "pull_request_target" or not isinstance(event, dict):
        return "head_actor_unknown"
    action = event.get("action")
    if action not in HEAD_SETTING_ACTIONS:
        return "head_actor_unknown"
    pr = event["pull_request"]
    if pr["head"]["sha"] != head or pr["base"]["sha"] != base:
        return "event_mismatch"
    # A synchronize event's after SHA is the head that the sender's push produced.
    if action == "synchronize" and event["after"] != head:
        return "event_mismatch"
    sender = event["sender"]
    if sender["login"] != BOT_LOGIN or sender["type"] != "Bot":
        return "head_set_by_other_actor"
    return None


def _snapshot_matches(pr: dict[str, Any], repository: str, head: str, base: str) -> bool:
    return (
        pr["user"]["login"] == BOT_LOGIN
        and pr["user"]["type"] == "Bot"
        and pr["head"]["repo"]["full_name"] == repository
        and pr["base"]["repo"]["full_name"] == repository
        and pr["head"]["sha"] == head
        and pr["base"]["sha"] == base
    )


def _commit_reason(repo: Path, commit: dict[str, Any], base: str) -> str | None:
    sha = commit["sha"]
    parents = [parent["sha"] for parent in commit["parents"]]
    if parents != gitx.out(repo, "show", "-s", "--format=%P", sha).split():
        return "parent_mismatch"
    if len(parents) == 1:
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        verification = commit["commit"]["verification"]
        if (
            author.get("login") != BOT_LOGIN
            or author.get("type") != "Bot"
            or committer.get("login") != "web-flow"
            or verification["verified"] is not True
            or verification.get("reason") != "valid"
        ):
            return "unverified_non_merge_commit"
        return None
    if len(parents) != 2:
        return "unsupported_merge"
    # Only a clean merge of the protected base into the bot branch is allowed.
    # Comparing trees catches conflict resolutions and edits hidden in a merge.
    if gitx.run(repo, "merge-base", "--is-ancestor", parents[1], base, check=False).returncode:
        return "merge_parent_outside_base"
    merged = gitx.merge_tree(repo, *parents)
    if merged is None or merged != gitx.tree_sha(repo, sha):
        return "merge_changed_content"
    return None


def verify(
    repo: Path,
    *,
    pr: object,
    commits: object,
    repository: str,
    head: str,
    base: str,
    event_name: str,
    event: object,
) -> dict[str, Any]:
    """Fail closed unless Dependabot set the head and every contribution is bot work.

    Every non-merge commit must be signed bot work, and every merge commit must be a
    clean merge of the protected base.
    """
    result = {"verified": False, "head_sha": head, "base_sha": base}
    try:
        if not OID.fullmatch(head) or not OID.fullmatch(base) or not isinstance(pr, dict):
            return {**result, "reason": "invalid_input"}
        actor_reason = _head_actor_reason(event_name, event, head, base)
        if actor_reason is not None:
            return {**result, "reason": actor_reason}
        if not _snapshot_matches(pr, repository, head, base):
            return {**result, "reason": "snapshot_mismatch"}
        items = _pages(commits)
        shas = [item["sha"] for item in items]
        if not all(isinstance(sha, str) and OID.fullmatch(sha) for sha in shas):
            return {**result, "reason": "invalid_commit"}
        actual = set(gitx.out(repo, "rev-list", f"{base}..{head}").split())
        if (
            not items
            or pr["commits"] != len(items)
            or len(set(shas)) != len(shas)
            or set(shas) != actual
        ):
            return {**result, "reason": "incomplete_history"}
        if not any(len(item["parents"]) == 1 for item in items):
            return {**result, "reason": "missing_bot_commit"}
        for item in items:
            reason = _commit_reason(repo, item, base)
            if reason is not None:
                return {**result, "reason": reason}
    except (KeyError, TypeError, ValueError, gitx.GitError):
        return {**result, "reason": "unreadable_provenance"}
    return {**result, "verified": True, "reason": "verified_dependabot"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-file", type=Path, required=True)
    parser.add_argument("--commits-file", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-file", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                Path.cwd(),
                pr=json.loads(args.pr_file.read_text()),
                commits=json.loads(args.commits_file.read_text()),
                repository=args.repository,
                head=args.head,
                base=args.base,
                event_name=args.event_name,
                event=json.loads(args.event_file.read_text()),
            )
        )
    )


if __name__ == "__main__":
    main()
