"""Portable per-commit attestations stored as Git notes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from . import gitx
from .models import Attestation, AttestedStage, RunDoc, Stage

NOTES_REF = "refs/notes/agentic-preflight"


class InvalidAttestation(ValueError):
    pass


def output_digest(output: str) -> str:
    return hashlib.sha256(output.encode()).hexdigest()


def intent_digest(intent: str) -> str:
    return hashlib.sha256(intent.encode()).hexdigest()


def build(
    run: RunDoc,
    *,
    sha: str,
    tree_sha: str,
    docs_enabled: bool,
    findings_summary: dict[str, int],
) -> Attestation:
    if run.config_digest is None:
        raise InvalidAttestation("run has no effective configuration digest")
    if run.review_coverage is None:
        raise InvalidAttestation("review stage has no coverage evidence")
    review_record = run.stages.get(Stage.REVIEW)
    if review_record is None or review_record.status != "green" or review_record.executor is None:
        raise InvalidAttestation("review stage has no executor evidence")
    stages: dict[Stage, AttestedStage] = {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor=review_record.executor,
            command=review_record.command,
            exit_code=review_record.exit_code,
            output_sha256=review_record.output_sha256,
            coverage=run.review_coverage,
        ),
        Stage.DOCS: AttestedStage(
            status="green" if docs_enabled else "skipped",
            reason=None if docs_enabled else "disabled by configuration",
        ),
    }
    for stage in (Stage.TEST, Stage.LINT):
        record = run.stages.get(stage)
        if record is None:
            raise InvalidAttestation(f"{stage.value} stage has no recorded result")
        if record.status == "skipped":
            if stage is Stage.LINT:
                raise InvalidAttestation("lint stage is not green")
            stages[stage] = AttestedStage(status="skipped", reason=record.reason)
            continue
        if record.status != "green":
            raise InvalidAttestation(f"{stage.value} stage is not green")
        stages[stage] = AttestedStage(
            status="green",
            command=record.command,
            exit_code=record.exit_code,
            output_sha256=record.output_sha256,
        )
    from .refresh_validation import base_supports_refresh, rebound_coverage, verify_evidence

    use_refresh = (
        run.worktree_path is not None
        and set(run.evidence) == set(Stage)
        and base_supports_refresh(run.worktree_path, run.merge_base_sha)
    )
    if use_refresh:
        stages[Stage.REVIEW].coverage = rebound_coverage(
            run.worktree_path or "",
            run.evidence[Stage.REVIEW].origin,
            head=sha,
            base=run.merge_base_sha,
        )
    value = Attestation(
        schema_version=5 if use_refresh else 4,
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        intent_sha256=intent_digest(run.intent or ""),
        config_sha256=run.config_digest,
        run_id=run.run_id,
        green_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stages=stages,
        findings_summary=findings_summary,
        evidence=run.evidence if use_refresh else None,
        config_snapshot=run.config_snapshot if use_refresh else None,
    )
    if use_refresh:
        verify_evidence(run.worktree_path or "", value)
    return value


def encode(value: Attestation) -> str:
    payload = value.model_dump(mode="json")
    if value.schema_version == 4:
        payload.pop("evidence")
        payload.pop("config_snapshot")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def decode(payload: str) -> Attestation:
    try:
        return Attestation.model_validate_json(payload)
    except ValidationError as exc:
        raise InvalidAttestation(str(exc)) from exc


def write(repo: Path | str, value: Attestation) -> None:
    gitx.write_note(repo, NOTES_REF, value.sha, encode(value))


def read(repo: Path | str, sha: str) -> Attestation | None:
    payload = gitx.read_note(repo, NOTES_REF, sha)
    if payload is None:
        return None
    return decode(payload)


def verify(repo: Path | str, sha: str) -> Attestation:
    resolved = gitx.rev_parse(repo, sha)
    value = read(repo, resolved)
    if value is None:
        raise InvalidAttestation(
            f"commit {resolved} has no agentic-preflight attestation in {NOTES_REF}"
        )
    if value.sha != resolved:
        raise InvalidAttestation(f"attestation names {value.sha}, but it is attached to {resolved}")
    actual_tree = gitx.tree_sha(repo, resolved)
    if value.tree_sha != actual_tree:
        raise InvalidAttestation(
            f"attestation tree {value.tree_sha} does not match commit tree {actual_tree}"
        )
    if value.schema_version == 5:
        from .refresh_validation import verify_evidence

        try:
            verify_evidence(repo, value)
        except (ValueError, gitx.GitError) as exc:
            raise InvalidAttestation(str(exc)) from exc
    return value


def _has_reusable_stage_results(value: Attestation) -> bool:
    """Require the mandatory lint result and the terminal test outcome."""
    return value.stages[Stage.LINT].status == "green" and value.stages[Stage.TEST].status in {
        "green",
        "skipped",
    }


def reuse_exact(
    repo: Path | str,
    *,
    sha: str,
    base_sha: str,
    branch: str,
    base_ref: str,
    intent: str,
    config_digest: str,
) -> Attestation | None:
    """Reuse green only when the exact attested SHA remains merge-equivalent."""
    repo = Path(repo)
    target_sha = gitx.rev_parse(repo, sha)
    try:
        verified = verify(repo, target_sha)
    except InvalidAttestation:
        return None
    reusable_metadata = (
        verified.branch == branch
        and verified.base_ref == base_ref
        and verified.intent_sha256 == intent_digest(intent)
        and verified.config_sha256 == config_digest
        and _has_reusable_stage_results(verified)
        and gitx.is_ancestor(repo, base_sha, target_sha)
    )
    if not reusable_metadata:
        return None
    try:
        fresh_merge_tree = gitx.merge_tree(repo, base_sha, target_sha)
        attested_merge_tree = gitx.merge_tree(repo, verified.merge_base_sha, target_sha)
    except gitx.GitError:
        return None
    return (
        verified
        if fresh_merge_tree is not None and fresh_merge_tree == attested_merge_tree
        else None
    )
