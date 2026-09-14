"""Portable per-commit attestations stored as Git notes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import gitx
from .models import Attestation, AttestedStage, RunDoc, Stage
from .wire_schema import (
    InvalidAttestation as InvalidAttestation,
)
from .wire_schema import (
    decode as decode,
)
from .wire_schema import (
    encode as encode,
)

NOTES_REF = "refs/notes/agentic-preflight"


def recovery(reason: str) -> str:
    if reason in {"missing_note", "missing_notes_ref"}:
        return (
            "Confirm publication of the expected head and its note to the source remote; "
            "fetch fresh notes or rerun the bounded hosted check. Local verify stays offline."
        )
    if reason == "incompatible_schema":
        return (
            "The verifier cannot understand this attestation format. Compare the trusted "
            "verifier revision with the producer and update them to the same release or emit "
            "a supported format. Unknown fields do not identify a producer version."
        )
    if reason == "verifier_mismatch":
        return "Install and run the helper from the original protected event-base checkout."
    if reason == "stale_candidate":
        return "Run the check for the current PR event; never substitute its head into this event."
    if reason in {"git_failure", "git_timeout", "io_failure"}:
        return (
            "Inspect Git access, authentication, permissions, and transport; absence is unproven."
        )
    if reason == "delegated_pending":
        return (
            "Use ci status for current trusted CI evidence; delegated tests are not locally green."
        )
    return (
        "Inspect the invalid evidence and its commit/tree bindings. Restore valid evidence "
        "for this exact head; fetching repeatedly cannot repair a present invalid note."
    )


class DelegatedTestsPending(InvalidAttestation):
    """Publication evidence is valid, but delegated tests are not local completion."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="delegated_pending")


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
        if stage is Stage.TEST and record.status == "delegated" and run.test_delegation:
            stages[stage] = AttestedStage(status="delegated", reason="trusted CI tests pending")
            continue
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
    from .refresh_validation import rebound_coverage, verify_evidence

    delegated = run.test_delegation is not None
    required_evidence = set(Stage) - ({Stage.TEST} if delegated else set())
    if run.worktree_path is None:
        raise InvalidAttestation("run has no validation worktree for evidence verification")
    if set(run.evidence) != required_evidence:
        raise InvalidAttestation("run lacks complete per-stage evidence")
    if run.config_snapshot is None:
        raise InvalidAttestation("run has no effective configuration snapshot")
    stages[Stage.REVIEW].coverage = rebound_coverage(
        run.worktree_path,
        run.evidence[Stage.REVIEW].origin,
        head=sha,
        base=run.merge_base_sha,
    )
    value = Attestation(
        schema_version=7,
        outcome="tests_pending" if delegated else "verified",
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        intent_sha256=intent_digest(run.intent or ""),
        config_sha256=run.config_digest,
        run_id=run.run_id,
        green_at=None if delegated else datetime.now(UTC).isoformat(timespec="seconds"),
        publication_ready_at=datetime.now(UTC) if delegated else None,
        test_delegation=run.test_delegation,
        stages=stages,
        findings_summary=findings_summary,
        evidence=run.evidence,
        config_snapshot=run.config_snapshot,
    )
    verify_evidence(run.worktree_path, value)
    return value


def write(repo: Path | str, value: Attestation) -> None:
    from . import evidence_transport

    evidence_transport.retain(repo, value)
    gitx.write_note(repo, NOTES_REF, value.sha, encode(value))


def read(repo: Path | str, sha: str) -> Attestation | None:
    payload = gitx.read_note(repo, NOTES_REF, sha)
    if payload is None:
        return None
    return decode(payload)


def verify(
    repo: Path | str, sha: str, *, purpose: Literal["local", "publish"] = "local"
) -> Attestation:
    if purpose not in {"local", "publish"}:
        raise InvalidAttestation("unknown verification purpose")
    resolved = gitx.rev_parse(repo, sha)
    value = read(repo, resolved)
    if value is None:
        raise InvalidAttestation(
            f"commit {resolved} has no agentic-preflight attestation in {NOTES_REF}",
            reason="missing_note",
        )
    return verify_value(repo, value, resolved, purpose=purpose)


def verify_value(
    repo: Path | str, value: Attestation, resolved: str, *, purpose: Literal["local", "publish"]
) -> Attestation:
    """Validate an already decoded note, including notes retrieved through GitHub."""
    if purpose not in {"local", "publish"}:
        raise InvalidAttestation("unknown verification purpose")
    if value.sha != resolved:
        raise InvalidAttestation(
            f"attestation names {value.sha}, but it is attached to {resolved}",
            reason="commit_mismatch",
        )
    actual_tree = gitx.tree_sha(repo, resolved)
    if value.tree_sha != actual_tree:
        raise InvalidAttestation(
            f"attestation tree {value.tree_sha} does not match commit tree {actual_tree}",
            reason="tree_mismatch",
        )
    from .refresh_validation import verify_evidence

    try:
        verify_evidence(repo, value)
    except gitx.GitError as exc:
        raise InvalidAttestation(
            f"Git evidence validation failed with exit {exc.returncode}", reason="git_failure"
        ) from exc
    except ValueError as exc:
        raise InvalidAttestation(str(exc)) from exc
    if value.outcome == "tests_pending":
        from .ci_policy import verify_declaration

        try:
            verify_declaration(repo, value)
        except gitx.GitError as exc:
            raise InvalidAttestation(
                f"Git evidence validation failed with exit {exc.returncode}", reason="git_failure"
            ) from exc
        except ValueError as exc:
            raise InvalidAttestation(str(exc)) from exc
        if purpose != "publish":
            raise DelegatedTestsPending(
                "tests are delegated, not locally green; use ci status --repo OWNER/REPO --pr N "
                "to verify merge readiness, or verify --purpose publish for publication only"
            )
    return value
