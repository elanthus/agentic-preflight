"""Protected-base consumer for per-stage evidence and bounded derivation."""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import diff, findings, gitx, risk
from .config import Config, _validate_enums
from .digests import json_digest
from .fingerprints import (
    FINGERPRINT_VERSION,
    Disposition,
    DocsFingerprint,
    ReviewFingerprint,
    classify_docs,
    classify_review,
    docs_relevant_config,
    review_relevant_config,
)
from .models import Attestation, OriginalExecution, ReviewCoverage, Stage, StageEvidence
from .shell_fingerprints import ShellFingerprint, ShellInputContract, classify_shell

# Producers check this exact marker in the synchronized protected-base blob.
# A base without the consumer gets a complete legacy run and a v4 note.
REFRESH_WIRE_VERSION = 5


def base_supports_refresh(repo: Path | str, base: str) -> bool:
    result = gitx.run(repo, "show", f"{base}:agentic_preflight/refresh_validation.py", check=False)
    if result.returncode == 0 and "\nREFRESH_WIRE_VERSION = 5\n" in result.stdout:
        return True
    policy = gitx.run(repo, "show", f"{base}:.agentic-preflight.toml", check=False)
    if policy.returncode != 0:
        return False
    try:
        return tomllib.loads(policy.stdout).get("reuse", {}).get("attestation_schema") == 5
    except (ValueError, AttributeError):
        return False


def shell_execution_config(snapshot: dict, stage: Stage) -> dict:
    return {"stage": snapshot.get("stage"), "worktree": snapshot.get("worktree")}


def contract_is_committed(
    repo: Path | str, head: str, stage: Stage, contract: ShellInputContract | None
) -> bool:
    if contract is None:
        return False
    result = gitx.run(repo, "show", f"{head}:.agentic-preflight.toml", check=False)
    if result.returncode != 0:
        return False
    try:
        declaration = tomllib.loads(result.stdout).get("reuse", {}).get(stage.value)
        return ShellInputContract.model_validate(declaration) == contract
    except (ValueError, AttributeError):
        return False


def _manifest(repo: Path | str, origin: OriginalExecution, *, head: str, base: str):
    cfg = Config.model_validate(origin.config_snapshot)
    bundle = diff.build_bundle(repo, base, head, exclude=cfg.diff.exclude)
    fingerprint = origin.fingerprint
    if not isinstance(fingerprint, ReviewFingerprint):
        raise ValueError("review origin has the wrong fingerprint")
    manifest = diff.build_review_manifest(
        repo, bundle, grounding_sha256=fingerprint.grounding_sha256
    )
    if manifest.diff_sha256 != fingerprint.diff_sha256:
        raise ValueError("review fingerprint does not match the Git diff")
    if tuple(sorted(manifest.excluded_files)) != fingerprint.excluded_files:
        raise ValueError("review exclusions do not match the Git diff")
    return manifest


def rebound_coverage(
    repo: Path | str, origin: OriginalExecution, *, head: str, base: str
) -> ReviewCoverage:
    """Rebind identity only after accounting for all old and current units."""
    original = origin.result.coverage
    if original is None:
        raise ValueError("original review lacks coverage")
    old = _manifest(repo, origin, head=origin.head_sha, base=origin.base_sha)
    new = _manifest(repo, origin, head=head, base=base)
    if (
        old.manifest != original.manifest
        or original.head_sha != old.head_sha
        or original.grounding_sha256 != old.grounding_sha256
        or original.total_units != len(old.units)
        or set(original.cited_units + original.clean_units) != {unit.id for unit in old.units}
        or original.excluded_files != list(old.excluded_files)
    ):
        raise ValueError("original coverage does not account for its Git manifest")
    if old.units != new.units or old.excluded_files != new.excluded_files:
        raise ValueError("review derivation does not preserve every original/current unit")
    return original.model_copy(update={"manifest": new.manifest, "head_sha": new.head_sha})


def _verify_fingerprint(repo: Path | str, origin: OriginalExecution) -> None:
    fp = origin.fingerprint
    if fp.version != FINGERPRINT_VERSION:
        raise ValueError("unsupported fingerprint version")
    if fp.base_tree_sha != gitx.tree_sha(repo, origin.base_sha):
        raise ValueError("original base tree does not match its fingerprint")
    if fp.head_tree_sha != gitx.tree_sha(repo, origin.head_sha):
        raise ValueError("original head tree does not match its fingerprint")
    cfg = Config.model_validate(origin.config_snapshot)
    _validate_enums(cfg)
    if isinstance(fp, ReviewFingerprint):
        expected = json_digest(review_relevant_config(origin.config_snapshot))
        if fp.config_sha256 != expected or fp.executor != origin.result.executor:
            raise ValueError("original review policy does not match its fingerprint")
        rebound_coverage(repo, origin, head=origin.head_sha, base=origin.base_sha)
    elif isinstance(fp, DocsFingerprint):
        if fp.config_sha256 != json_digest(docs_relevant_config(origin.config_snapshot)):
            raise ValueError("original docs policy does not match its fingerprint")
    else:
        contract = getattr(cfg.reuse, origin.stage.value)
        expected = json_digest(
            {
                "execution": shell_execution_config(origin.config_snapshot, origin.stage),
                "contract": contract.model_dump(mode="json") if contract else None,
            }
        )
        if fp.config_sha256 != expected:
            raise ValueError("original shell policy does not match its fingerprint")
        if origin.result.status == "green" and fp.command_sha256 != json_digest_command(
            origin.result.command or ""
        ):
            raise ValueError("original command does not match its fingerprint")


def json_digest_command(command: str) -> str:
    import hashlib

    return hashlib.sha256(command.encode()).hexdigest()


def verify_stage(
    repo: Path | str, item: StageEvidence, *, head: str, base: str, run_id: str
) -> None:
    origin = item.origin
    _verify_fingerprint(repo, origin)
    fp = item.fingerprint
    if fp.head_tree_sha != gitx.tree_sha(repo, head) or fp.base_tree_sha != gitx.tree_sha(
        repo, base
    ):
        raise ValueError("current Git bindings do not match the stage fingerprint")
    if item.refreshed_at is None:
        if origin.run_id != run_id or fp != origin.fingerprint:
            raise ValueError("transferred evidence requires explicit derivation provenance")
        return
    old = origin.fingerprint
    if isinstance(old, ReviewFingerprint) and isinstance(fp, ReviewFingerprint):
        result = classify_review(old, fp)
        rebound_coverage(repo, origin, head=head, base=base)
    elif isinstance(old, DocsFingerprint) and isinstance(fp, DocsFingerprint):
        result = classify_docs(old, fp)
    elif isinstance(old, ShellFingerprint) and isinstance(fp, ShellFingerprint):
        result = classify_shell(old, fp)
        contract = getattr(Config.model_validate(origin.config_snapshot).reuse, origin.stage.value)
        if not contract_is_committed(
            repo, head, origin.stage, contract
        ) or not contract_is_committed(repo, origin.head_sha, origin.stage, contract):
            raise ValueError("shell derivation requires a committed input contract")
    else:
        raise ValueError("inconsistent fingerprint types")
    if result.disposition != Disposition.REUSABLE:
        raise ValueError("derived evidence inputs are invalid or unknown")


def verify_evidence(repo: Path | str, value: Attestation) -> None:
    if value.evidence is None or value.config_snapshot is None:
        raise ValueError("refresh attestation lacks per-stage evidence or configuration")
    cfg = Config.model_validate(value.config_snapshot)
    _validate_enums(cfg)
    if value.stages[Stage.LINT].status != "green":
        raise ValueError("lint evidence must be green")
    if (value.stages[Stage.DOCS].status == "skipped") == cfg.docs.enabled:
        raise ValueError("docs outcome does not match the enabled policy")
    if value.stages[Stage.TEST].status == "skipped":
        from .stages import change_scope

        if not change_scope.tests_are_not_applicable(
            gitx.changed_files(repo, value.merge_base_sha, value.sha),
            extra_doc_paths=cfg.docs.paths,
        ):
            raise ValueError("test skip is not applicable to the current change")
    summary: dict[str, int] = {}
    all_findings = []
    for stage, item in value.evidence.items():
        verify_stage(repo, item, head=value.sha, base=value.merge_base_sha, run_id=value.run_id)
        current = value.stages[stage]
        original = item.origin.result
        expected = original.model_copy(deep=True)
        if stage is Stage.REVIEW:
            expected.coverage = rebound_coverage(
                repo, item.origin, head=value.sha, base=value.merge_base_sha
            )
        if current != expected:
            raise ValueError("current stage result differs from original execution evidence")
        fp = item.fingerprint
        if isinstance(fp, ReviewFingerprint):
            if fp.intent_sha256 != value.intent_sha256 or fp.config_sha256 != json_digest(
                review_relevant_config(value.config_snapshot)
            ):
                raise ValueError("current review intent/policy binding changed")
        elif isinstance(fp, DocsFingerprint):
            if fp.intent_sha256 != value.intent_sha256 or fp.config_sha256 != json_digest(
                docs_relevant_config(value.config_snapshot)
            ):
                raise ValueError("current docs intent/policy binding changed")
        else:
            contract = getattr(cfg.reuse, stage.value)
            expected_config = json_digest(
                {
                    "execution": shell_execution_config(value.config_snapshot, stage),
                    "contract": contract.model_dump(mode="json") if contract else None,
                }
            )
            if fp.config_sha256 != expected_config:
                raise ValueError("current shell policy binding changed")
            configured_command = getattr(cfg.commands, stage.value)
            if (
                current.status == "green"
                and configured_command
                and current.command != configured_command
            ):
                raise ValueError("current shell command differs from configured command")
        severities = (
            cfg.docs.blocking_severities if stage is Stage.DOCS else cfg.review.blocking_severities
        )
        if findings.blocking(
            item.origin.findings, blocking_severities=severities
        ) or findings.actionable(item.origin.findings):
            raise ValueError("reused evidence contains unresolved blocking or actionable findings")
        for finding in item.origin.findings:
            all_findings.append(finding)
            for key in (finding.status.value, finding.severity.value):
                summary[key] = summary.get(key, 0) + 1
    if summary != value.findings_summary:
        raise ValueError("findings summary does not preserve original finding dispositions")
    assessment = risk.assess(
        gitx.changed_files(repo, value.merge_base_sha, value.sha),
        all_findings,
        policy=cfg.policy,
        review_blocking_severities=cfg.review.blocking_severities,
        docs_blocking_severities=cfg.docs.blocking_severities,
    )
    executor = (
        "command"
        if assessment.level.value in cfg.review.require_command_for
        else cfg.review.executor
    )
    if value.stages[Stage.REVIEW].executor != executor:
        raise ValueError("review execution does not meet current executor policy")
    if executor == "command" and value.stages[Stage.REVIEW].command != cfg.review.command:
        raise ValueError("review command does not meet current executor policy")
