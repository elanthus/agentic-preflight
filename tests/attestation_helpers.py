from datetime import UTC, datetime

from agentic_preflight.digests import json_digest
from agentic_preflight.fingerprints import DocsFingerprint, ReviewFingerprint
from agentic_preflight.models import (
    Attestation,
    AttestedStage,
    OriginalExecution,
    Stage,
    StageEvidence,
)
from agentic_preflight.shell_fingerprints import ShellFingerprint


def make_attestation(
    *,
    stages: dict[Stage, AttestedStage],
    sha: str = "a" * 40,
    tree_sha: str = "b" * 40,
    merge_base_sha: str = "c" * 40,
    branch: str = "feature/x",
    findings_summary: dict[str, int] | None = None,
) -> Attestation:
    """Build structurally complete evidence for model and policy unit tests."""
    snapshot: dict = {}
    config_sha256 = json_digest(snapshot)
    fingerprints = {
        Stage.REVIEW: ReviewFingerprint(
            base_tree_sha="1" * 40,
            head_tree_sha="2" * 40,
            diff_sha256="3" * 64,
            intent_sha256="4" * 64,
            executor=stages[Stage.REVIEW].executor or "in_harness",
            config_sha256="5" * 64,
        ),
        Stage.DOCS: DocsFingerprint(
            base_tree_sha="1" * 40,
            head_tree_sha="2" * 40,
            doc_surface_sha256="6" * 64,
            config_sha256="7" * 64,
        ),
        Stage.LINT: ShellFingerprint(
            base_tree_sha="1" * 40,
            head_tree_sha="2" * 40,
            command_sha256="8" * 64,
            config_sha256="9" * 64,
        ),
        Stage.TEST: ShellFingerprint(
            base_tree_sha="1" * 40,
            head_tree_sha="2" * 40,
            command_sha256="a" * 64,
            config_sha256="b" * 64,
        ),
    }
    evidence = {}
    for stage, result in stages.items():
        if result.status == "delegated":
            continue
        origin = OriginalExecution(
            run_id="r_test",
            source_worktree_id="wt_test",
            stage=stage,
            head_sha=sha,
            base_sha=merge_base_sha,
            branch=branch,
            base_ref="main",
            config_sha256=config_sha256,
            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
            config_snapshot=snapshot,
            result=result,
            fingerprint=fingerprints[stage],
        )
        evidence[stage] = StageEvidence(
            origin=origin,
            origin_sha256=json_digest(origin.model_dump(mode="json")),
            fingerprint=fingerprints[stage],
        )
    return Attestation(
        schema_version=7,
        outcome="verified",
        sha=sha,
        tree_sha=tree_sha,
        branch=branch,
        base_ref="main",
        merge_base_sha=merge_base_sha,
        intent_sha256="4" * 64,
        config_sha256=config_sha256,
        run_id="r_test",
        green_at="2026-01-01T00:00:00+00:00",
        stages=stages,
        findings_summary=findings_summary or {},
        evidence=evidence,
        config_snapshot=snapshot,
    )
