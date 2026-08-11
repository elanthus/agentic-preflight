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
_INTENT_SUMMARY_PREFIX = "intent-sha256:"
_CONFIG_SUMMARY_PREFIX = "config-sha256:"


class InvalidAttestation(ValueError):
    pass


def output_digest(output: str) -> str:
    return hashlib.sha256(output.encode()).hexdigest()


def intent_summary_key(intent: str) -> str:
    """Encode intent binding without changing the v1 attestation schema."""
    return _INTENT_SUMMARY_PREFIX + hashlib.sha256(intent.encode()).hexdigest()


def config_summary_key(config_digest: str) -> str:
    """Encode config binding without changing the v1 attestation schema."""
    return _CONFIG_SUMMARY_PREFIX + config_digest


def build(
    run: RunDoc,
    *,
    sha: str,
    tree_sha: str,
    docs_enabled: bool,
    findings_summary: dict[str, int],
) -> Attestation:
    stages: dict[Stage, AttestedStage] = {
        Stage.REVIEW: AttestedStage(status="green"),
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
    portable_bindings = {intent_summary_key(run.intent or ""): 1}
    if run.config_digest is not None:
        portable_bindings[config_summary_key(run.config_digest)] = 1
    return Attestation(
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        run_id=run.run_id,
        green_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stages=stages,
        findings_summary={
            **findings_summary,
            **portable_bindings,
        },
    )


def encode(value: Attestation) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


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
    return value


def reuse_for_rebase(
    repo: Path | str,
    *,
    sha: str,
    base_sha: str,
    branch: str,
    base_ref: str,
    intent: str,
    config_digest: str,
) -> tuple[Attestation, str | None] | None:
    """Transfer green to ``sha`` only for a merge-equivalent tree rewrite.

    Matching trees alone are insufficient: Git's merge result also depends on
    ancestry.  A candidate is reusable only when both commits have the same
    complete tree, effective configuration, and Git merge result against the
    freshly synchronized base. An exact-note lookup additionally requires the
    fresh base to be an ancestor of the target, making that merge a fast-forward.
    """
    repo = Path(repo)
    target_sha = gitx.rev_parse(repo, sha)
    target_tree = gitx.tree_sha(repo, target_sha)
    required_intent_key = intent_summary_key(intent)
    required_config_key = config_summary_key(config_digest)

    exact = read(repo, target_sha)
    if exact is not None:
        try:
            verified = verify(repo, target_sha)
        except InvalidAttestation:
            return None
        reusable = (
            verified.branch == branch
            and verified.base_ref == base_ref
            and verified.findings_summary.get(required_intent_key) == 1
            and verified.findings_summary.get(required_config_key) == 1
            and verified.stages[Stage.LINT].status == "green"
            and gitx.is_ancestor(repo, base_sha, target_sha)
        )
        return (verified, None) if reusable else None

    target_merge_tree = gitx.merge_tree(repo, base_sha, target_sha)
    if target_merge_tree is None:
        return None

    candidates: list[Attestation] = []
    for noted_sha in gitx.list_noted_objects(repo, NOTES_REF):
        if not gitx.commit_exists(repo, noted_sha):
            continue
        try:
            candidate = verify(repo, noted_sha)
        except (InvalidAttestation, gitx.GitError):
            continue
        if (
            candidate.tree_sha == target_tree
            and candidate.branch == branch
            and candidate.base_ref == base_ref
            and candidate.findings_summary.get(required_intent_key) == 1
            and candidate.findings_summary.get(required_config_key) == 1
            and candidate.stages[Stage.LINT].status == "green"
        ):
            candidates.append(candidate)

    for candidate in sorted(candidates, key=lambda value: value.green_at, reverse=True):
        if gitx.merge_tree(repo, base_sha, candidate.sha) != target_merge_tree:
            continue
        reused = candidate.model_copy(
            update={
                "sha": target_sha,
                "tree_sha": target_tree,
                "merge_base_sha": gitx.rev_parse(repo, base_sha),
            }
        )
        write(repo, reused)
        return reused, candidate.sha
    return None
