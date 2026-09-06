"""Combine portable local evidence, live CI execution and human merge policy."""

from __future__ import annotations

import os
from pathlib import Path

from . import approval, attestation, evidence_transport, gitx
from .ci_authority import Candidate, CandidatePending, evaluate_tests, snapshot
from .ci_policy import enforce_local_policy
from .config import Config
from .github_api import APIUnavailable, GitHub
from .models import Stage


def published_attestation(repo: Path, api: GitHub, candidate: Candidate, cfg: Config):
    source = api.scoped(candidate.head_repository, public=not candidate.head_repository_private)
    value = attestation.decode(source.note(candidate.head_sha))
    objects = {candidate.base_sha, candidate.head_sha}
    missing = [
        sha
        for sha in sorted(objects)
        if gitx.run(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode
    ]
    if missing:
        fetched = gitx.run(
            repo,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            f"https://github.com/{api.repository}.git",
            *missing,
            check=False,
        )
        if fetched.returncode:
            gitx.run(
                repo,
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "credential.helper=!gh auth git-credential",
                "fetch",
                "--no-tags",
                f"https://github.com/{source.repository}.git",
                *missing,
            )
    for sha in evidence_transport.missing(repo, value):
        gitx.run(
            repo,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            f"https://github.com/{source.repository}.git",
            evidence_transport.ref_for(sha),
        )
        # The requested ref name alone is not evidence of the object's identity.
        gitx.rev_parse(repo, f"{sha}^{{commit}}")
    attestation.verify_value(repo, value, candidate.head_sha, purpose="publish")
    if value.config_snapshot is None:
        raise ValueError("CI merge verification requires per-stage local evidence (schema 5 or 6)")
    # The original declaration was checked against its original protected base.
    # Current remote CI policy replaces it without rewriting local review evidence.
    enforce_local_policy(Config.model_validate(value.config_snapshot), cfg, include_ci=False)
    if cfg.commands.lint and value.stages[Stage.LINT].command != cfg.commands.lint:
        raise ValueError("local lint execution differs from protected-base command")
    if value.base_ref not in {candidate.base_branch, f"origin/{candidate.base_branch}"}:
        raise ValueError("local evidence targets a different base branch")
    # A forward base update may change the integration tree without changing the
    # reviewed contribution. Require the same fork-point tree and unchanged policy.
    fork_point = gitx.out(repo, "merge-base", candidate.base_sha, candidate.head_sha)
    if gitx.tree_sha(repo, fork_point) != gitx.tree_sha(repo, value.merge_base_sha):
        raise ValueError("local evidence no longer applies to the current PR contribution")
    return value


def evaluate(repo: Path, api: GitHub, pr: int) -> dict:
    result: dict = {
        "purpose": "merge",
        "pr": pr,
        "repository": api.repository,
        "merge_requirements_satisfied": False,
        "test_authority": "github_actions",
    }
    try:
        candidate, cfg, pull = snapshot(api, pr)
        result["candidate"] = candidate.model_dump()
        result["check_app_id"] = cfg.ci.check_app_id
        value = published_attestation(repo, api, candidate, cfg)
        result["publication_ready"] = True
        tests = evaluate_tests(api, candidate, cfg)
        result["tests"] = tests
        result["status"] = tests["status"]
        result["reason"] = tests["reason"]
        if tests["status"] != "success":
            return result
        reviews = api.pages(f"pulls/{pr}/reviews")
        handling = approval.evaluate_value(
            repo,
            value=value,
            cfg=cfg,
            base_sha=candidate.base_sha,
            head_sha=candidate.head_sha,
            reviews=reviews,
            pull_request_author=pull["user"]["login"],
            environment_approved=cfg.approval.mode == "environment",
        )
        result["approval"] = handling
        if not handling["approved"] or (
            handling["manual_merge_required"] and pull.get("auto_merge")
        ):
            return {
                **result,
                "status": "approval_pending",
                "reason": "current human approval or manual-merge policy is not satisfied",
            }
        # Recheck remote notes too: published evidence is mutable audit data, and
        # a concurrent notes update must not silently change this decision.
        if (
            attestation.decode(
                api.scoped(
                    candidate.head_repository, public=not candidate.head_repository_private
                ).note(candidate.head_sha)
            )
            != value
        ):
            return {
                **result,
                "status": "stale",
                "reason": "published evidence changed during evaluation",
            }
        fresh, fresh_cfg, fresh_pull = snapshot(api, pr)
        if (
            fresh != candidate
            or fresh_cfg != cfg
            or fresh_pull.get("auto_merge") != pull.get("auto_merge")
        ):
            return {
                **result,
                "status": "stale",
                "reason": "candidate or merge policy changed during evaluation",
            }
        if api.pages(f"pulls/{pr}/reviews") != reviews:
            return {**result, "status": "stale", "reason": "human review changed during evaluation"}
        if evaluate_tests(api, candidate, cfg) != tests:
            return {
                **result,
                "status": "pending",
                "reason": "CI run or attempt changed during evaluation",
            }
        if snapshot(api, pr)[0] != candidate:
            return {
                **result,
                "status": "stale",
                "reason": "candidate changed before the final result",
            }
        return {**result, "status": "success", "merge_requirements_satisfied": True}
    except CandidatePending as exc:
        return {**result, "status": "pending", "reason": str(exc)}
    except (APIUnavailable, gitx.GitError, OSError, KeyError, TypeError, AttributeError) as exc:
        return {**result, "status": "unavailable", "reason": str(exc)}
    except ValueError as exc:
        return {**result, "status": "stale", "reason": str(exc)}


def next_action(result: dict) -> tuple[str, str]:
    command = f"agentic-preflight ci status --repo {result['repository']} --pr {result['pr']}"
    status = result["status"]
    if status == "success":
        return "Merge requirements are satisfied. Follow the reported human merge policy.", command
    if status == "unavailable":
        return (
            "Restore GitHub/notes access and retry. The result is unknown, not a test pass.",
            command,
        )
    if status == "approval_pending":
        return (
            "Obtain the required human approval or disable auto-merge for manual handling, then retry.",
            command,
        )
    tests = result.get("tests", {})
    if tests.get("run_id") and status in {"failed", "expired", "stale"}:
        return (
            "Inspect the linked run. Rerun all jobs if source is unchanged; source repairs invalidate affected local evidence.",
            command.replace("ci status", "ci dispatch") + " --force",
        )
    if status == "stale" and not result.get("publication_ready"):
        return (
            "Refresh the affected local evidence and publish its note for the current candidate.",
            "agentic-preflight start",
        )
    return (
        "Wait for current jobs, or dispatch the fresh integration candidate after a base update. Local review is retained when applicable.",
        command.replace("ci status", "ci dispatch"),
    )
