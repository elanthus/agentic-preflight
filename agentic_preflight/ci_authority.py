"""Evaluate protected, attempt-specific CI evidence for an integration candidate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from .ci_policy import parse_policy
from .config import Config
from .digests import json_digest
from .github_api import GitHub

CHECK_NAME = "preflight merge readiness"
TEST_STEPS = ("Verify integration checkout", "Run delegated tests")


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    repository_id: int = Field(gt=0)
    pr: int = Field(gt=0)
    head_repository_id: int = Field(gt=0)
    head_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    head_repository_private: bool
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_branch: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def title(self) -> str:
        return f"agentic-preflight-ci-v1 pr={self.pr} candidate={json_digest(self.model_dump())}"


class CandidatePending(ValueError):
    pass


def snapshot(api: GitHub, pr: int) -> tuple[Candidate, Config, dict]:
    """Bind the merge commit to exactly two current parents and protected policy."""
    pull = api.request(f"pulls/{pr}")
    if pull["state"] != "open":
        raise ValueError("pull request is not open")
    base, head = pull["base"], pull["head"]
    branch = api.request(f"branches/{quote(base['ref'], safe='')}")
    if branch.get("protected") is not True:
        raise ValueError("CI authority must be sourced from a protected base branch")
    if branch.get("name") != base["ref"] or branch.get("commit", {}).get("sha") != base["sha"]:
        raise CandidatePending("protected base moved; retry with the current PR snapshot")
    cfg = parse_policy(api.contents(".agentic-preflight.toml", base["sha"]))
    if (
        base["repo"]["id"] != cfg.ci.repository_id
        or base["repo"]["full_name"].casefold() != api.repository.casefold()
        or base["ref"] != cfg.ci.base_branch
    ):
        raise ValueError("repository or retargeted base does not match protected CI policy")
    merge_sha = pull.get("merge_commit_sha")
    if not merge_sha or pull.get("mergeable") is not True:
        raise CandidatePending(
            "integration commit is unavailable or conflicted; retry or resolve the conflict"
        )
    merge = api.request(f"git/commits/{merge_sha}")
    if merge["sha"] != merge_sha or [parent["sha"] for parent in merge["parents"]] != [
        base["sha"],
        head["sha"],
    ]:
        raise CandidatePending(
            "integration commit is stale; wait for GitHub to recompute the merge ref"
        )
    candidate = Candidate(
        repository=api.repository,
        repository_id=base["repo"]["id"],
        pr=pr,
        head_repository_id=head["repo"]["id"],
        head_repository=head["repo"]["full_name"],
        head_repository_private=head["repo"]["private"],
        head_sha=head["sha"],
        base_branch=base["ref"],
        base_sha=base["sha"],
        merge_sha=merge_sha,
        merge_tree=merge["tree"]["sha"],
        policy_sha256=json_digest(cfg.model_dump(mode="json")),
    )
    return candidate, cfg, pull


def workflow_runs(api: GitHub, candidate: Candidate, cfg: Config) -> list[dict]:
    workflow = api.request(f"actions/workflows/{cfg.ci.workflow_id}")
    if (
        workflow["id"] != cfg.ci.workflow_id
        or workflow["path"] != cfg.ci.workflow_path
        or workflow.get("state") != "active"
    ):
        raise ValueError("configured trusted workflow is missing, renamed, or disabled")
    runs = api.pages(
        f"actions/workflows/{cfg.ci.workflow_id}/runs?event=workflow_dispatch&head_sha={candidate.base_sha}",
        "workflow_runs",
    )
    return sorted(
        (run for run in runs if run.get("display_title") == candidate.title),
        key=lambda run: run["id"],
        reverse=True,
    )


def _binding(run: dict, candidate: Candidate, cfg: Config) -> bool:
    return (
        run.get("event") == "workflow_dispatch"
        and run.get("workflow_id") == cfg.ci.workflow_id
        and run.get("path") == cfg.ci.workflow_path
        and run.get("head_sha") == candidate.base_sha
        and run.get("head_branch") == candidate.base_branch
        and run.get("repository", {}).get("id") == candidate.repository_id
        and run.get("head_repository", {}).get("id") == candidate.repository_id
        and run.get("display_title") == candidate.title
        and isinstance(run.get("run_attempt"), int)
        and run["run_attempt"] > 0
    )


def _completed_at(value: object, now: datetime, max_age: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return now - timedelta(seconds=max_age) <= date <= now
    except (ValueError, TypeError):
        return False


def inspect_run(
    api: GitHub, candidate: Candidate, cfg: Config, run_id: int, *, now: datetime
) -> dict[str, Any]:
    run = api.request(f"actions/runs/{run_id}")
    result: dict[str, Any] = {
        "status": "pending",
        "run_id": run_id,
        "run_attempt": run.get("run_attempt"),
        "run_url": run.get("html_url"),
        "subject": "integration",
        "tested_sha": candidate.merge_sha,
        "tested_tree": candidate.merge_tree,
        "policy_revision": candidate.base_sha,
        "workflow_id": cfg.ci.workflow_id,
        "workflow_path": cfg.ci.workflow_path,
    }
    if run.get("id") != run_id or not _binding(run, candidate, cfg):
        return {
            **result,
            "status": "stale",
            "reason": "workflow execution identity does not match the protected candidate",
        }
    if run.get("status") != "completed":
        return {**result, "reason": "latest workflow attempt is pending"}
    if run.get("conclusion") != "success":
        return {
            **result,
            "status": "failed",
            "reason": f"latest attempt concluded {run.get('conclusion')}",
        }
    jobs = api.pages(f"actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    required: dict[str, tuple[str, ...]] = {"prepare": ("Validate candidate",)}
    required.update(dict.fromkeys(cfg.ci.required_jobs, TEST_STEPS))
    if cfg.approval.mode == "environment":
        validate_environment(api, cfg)
        required["approval"] = ("Environment released",)
    evidence = []
    for name, steps in required.items():
        matching = [job for job in jobs if job.get("name") == name]
        if len(matching) != 1:
            return {
                **result,
                "status": "failed",
                "reason": f"required job {name!r} missing or ambiguous in latest attempt",
            }
        job = matching[0]
        if (
            job.get("run_id") != run_id
            or job.get("run_attempt") != run["run_attempt"]
            or job.get("head_sha") != candidate.base_sha
        ):
            return {
                **result,
                "status": "stale",
                "reason": f"job {name!r} has an unrelated execution identity",
            }
        if job.get("status") != "completed":
            return {**result, "reason": f"required job {name!r} is pending"}
        if job.get("conclusion") != "success":
            return {
                **result,
                "status": "failed",
                "reason": f"required job {name!r} did not succeed",
            }
        for step in steps:
            matching_steps = [item for item in job.get("steps", []) if item.get("name") == step]
            if len(matching_steps) != 1 or any(
                item.get("status") != "completed" or item.get("conclusion") != "success"
                for item in matching_steps
            ):
                return {
                    **result,
                    "status": "failed",
                    "reason": f"required step {name!r}/{step!r} did not succeed",
                }
        if not _completed_at(job.get("completed_at"), now, cfg.ci.max_age_seconds):
            return {
                **result,
                "status": "expired",
                "reason": f"required job {name!r} is expired or has an invalid completion time",
            }
        evidence.append({"job_id": job["id"], "name": name, "completed_at": job["completed_at"]})
    return {
        **result,
        "status": "success",
        "jobs": evidence,
        "reason": "all trusted integration jobs succeeded",
    }


def validate_environment(api: GitHub, cfg: Config) -> None:
    environment = api.request(f"environments/{quote(cfg.approval.environment, safe='')}")
    if environment.get("name") != cfg.approval.environment or not any(
        rule.get("type") == "required_reviewers" and rule.get("reviewers")
        for rule in environment.get("protection_rules", [])
    ):
        raise ValueError("protected approval environment must exist with required reviewers")


def evaluate_tests(
    api: GitHub, candidate: Candidate, cfg: Config, *, now: datetime | None = None
) -> dict:
    runs = workflow_runs(api, candidate, cfg)
    if not runs:
        return {
            "status": "pending",
            "reason": "no trusted run for the current integration candidate",
        }
    return inspect_run(api, candidate, cfg, runs[0]["id"], now=now or datetime.now(UTC))


def dispatch(api: GitHub, pr: int, *, force: bool = False) -> dict:
    """Reuse a current request; explicit force requests a fresh complete run."""
    candidate, cfg, _ = snapshot(api, pr)
    current = evaluate_tests(api, candidate, cfg)
    if not force and current.get("run_id") and current["status"] != "expired":
        return {"dispatched": False, "candidate": candidate.model_dump(), "tests": current}
    # Never dispatch after a base/head change during the lookup.
    if snapshot(api, pr)[0] != candidate:
        raise CandidatePending("candidate changed before dispatch; retry")
    api.request(
        f"actions/workflows/{cfg.ci.workflow_id}/dispatches",
        method="POST",
        body={
            "ref": candidate.base_branch,
            "inputs": {
                "candidate": candidate.model_dump_json(),
                "candidate_id": json_digest(candidate.model_dump()),
            },
        },
    )
    return {"dispatched": True, "candidate": candidate.model_dump(), "tests": {"status": "pending"}}


def prepare(api: GitHub, supplied: str, *, workflow_sha: str, candidate_id: str) -> Candidate:
    """Validate dispatch input before protected workflow permits PR execution."""
    requested = Candidate.model_validate_json(supplied)
    actual, cfg, _ = snapshot(api, requested.pr)
    if (
        actual != requested
        or workflow_sha != actual.base_sha
        or candidate_id != json_digest(actual.model_dump())
    ):
        raise ValueError("dispatched candidate or workflow revision is stale or forged")
    if cfg.approval.mode == "environment":
        validate_environment(api, cfg)
    return actual
