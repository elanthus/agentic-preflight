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
    return Attestation(
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        run_id=run.run_id,
        green_at=datetime.now(UTC).isoformat(timespec="seconds"),
        stages=stages,
        findings_summary=findings_summary,
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
