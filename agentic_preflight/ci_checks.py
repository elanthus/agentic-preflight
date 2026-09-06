"""Trusted check publication and event/scheduled reconciliation."""

from __future__ import annotations

from pathlib import Path

from . import ci_merge
from .ci_authority import CHECK_NAME, dispatch
from .digests import json_digest
from .github_api import GitHub


def publish_check(api: GitHub, head: str, result: dict, *, app_id: int) -> None:
    """Update only our exact-head check; callers serialize reconciliation jobs."""
    checks = api.pages(
        f"commits/{head}/check-runs?check_name={CHECK_NAME.replace(' ', '%20')}", "check_runs"
    )
    owned = [
        item
        for item in checks
        if (item.get("external_id") or "").startswith("agentic-preflight-ci-v1:")
        and item.get("app", {}).get("id") == app_id
    ]
    external_id = "agentic-preflight-ci-v1:" + json_digest(result.get("candidate", {"head": head}))
    ready = result.get("merge_requirements_satisfied") is True
    pending = result.get("status") in {"pending", "unavailable", "approval_pending"}
    instruction, command = ci_merge.next_action(result)
    summary = f"{result.get('reason', 'Trusted CI tests pending')}\n\n{instruction}\n\n`{command}`"
    if result.get("tests", {}).get("run_url"):
        summary += f"\n\nRun: {result['tests']['run_url']}"
    body = {
        "name": CHECK_NAME,
        "external_id": external_id,
        "status": "in_progress" if pending else "completed",
        "output": {
            "title": "Merge requirements satisfied"
            if ready
            else "Merge requirements not satisfied",
            "summary": summary,
        },
    }
    if not pending:
        body["conclusion"] = "success" if ready else "failure"
    if owned:
        published = api.request(
            f"check-runs/{max(owned, key=lambda item: item['id'])['id']}", method="PATCH", body=body
        )
    else:
        published = api.request("check-runs", method="POST", body={**body, "head_sha": head})
    if not isinstance(published, dict) or published.get("app", {}).get("id") != app_id:
        raise ValueError(
            "check-writing token does not belong to the configured dedicated GitHub App"
        )


def reconcile(repo: Path, api: GitHub, *, check_app_id: int, pr: int | None = None) -> list[dict]:
    """Run in a protected job with actions/checks write, never in a PR test job."""
    pulls = [api.request(f"pulls/{pr}")] if pr else api.pages("pulls?state=open")
    outcomes = []
    for pull in pulls:
        number, head = pull["number"], pull["head"]["sha"]
        pending = {
            "repository": api.repository,
            "pr": number,
            "status": "pending",
            "reason": "Rechecking current candidate and trusted CI authority",
        }
        publish_check(api, head, pending, app_id=check_app_id)
        # evaluate supplies precise unavailable/stale explanations even when
        # dispatch cannot establish a candidate. Do not let a dispatch failure
        # accidentally preserve a previous success.
        try:
            request = dispatch(api, number)
        except (ValueError, KeyError, TypeError):
            request = None
        result = ci_merge.evaluate(repo, api, number)
        if result.get("check_app_id", check_app_id) != check_app_id:
            result = {
                **result,
                "status": "stale",
                "merge_requirements_satisfied": False,
                "reason": "configured check App differs from protected policy",
            }
        if request and request["dispatched"]:
            result = {
                **result,
                "status": "pending",
                "merge_requirements_satisfied": False,
                "reason": "Fresh integration run dispatched; waiting for GitHub to report its attempt",
            }
        current = api.request(f"pulls/{number}")
        if current["head"]["sha"] != head:
            result = {
                **result,
                "status": "stale",
                "merge_requirements_satisfied": False,
                "reason": "PR head changed during check publication",
            }
        if result.get("candidate") and current["base"]["sha"] != result["candidate"]["base_sha"]:
            result = {
                **result,
                "status": "stale",
                "merge_requirements_satisfied": False,
                "reason": "PR base changed during check publication",
            }
        publish_check(api, head, result, app_id=check_app_id)
        outcomes.append(result)
    return outcomes
