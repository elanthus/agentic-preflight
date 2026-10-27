"""Typed description of the stdout contract, one ``data`` model per command.

These models document what each command puts in ``Envelope.data``; they are not
applied at runtime and never change a payload. ``tools/generate_schema.py`` renders
them with ``model_json_schema()`` into ``skill/reference/schema/``, and the tests keep
the checked-in files and ``skill/reference/commands.md`` in step with them.

A command's ``data`` differs by outcome (success, a refusal, a stage failure), so
every field is optional: a field is listed when at least one outcome emits it. Error
paths may add detail beyond the listed fields, so each model allows extra keys.
Nested structures that other modules own, or that are open-ended, stay
``dict[str, Any]``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import Producer

Obj = dict[str, Any]


class _Data(BaseModel):
    """Base for a command's ``data`` object."""

    model_config = ConfigDict(extra="allow")


class RunDataBase(_Data):
    """Keys any run command may add through the shared envelope builder."""

    needs_human: bool | None = Field(
        None, description="True once the run hit the restart limit and needs a person."
    )
    validation_restarts: int | None = None
    max_restarts: int | None = None
    telemetry: Obj | list[Obj] | None = Field(
        None, description="Summary outcome written when this command ended a run."
    )
    source_worktree_path: str | None = None


# ---------------------------------------------------------------- envelope


class NextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str | None
    command: str | None


class ErrorBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    detail: Obj | None = None


class EnvelopeModel(BaseModel):
    """The single JSON object every agent-facing command prints. Every key is present."""

    model_config = ConfigDict(extra="forbid", title="Envelope")

    ok: bool
    run_id: str | None
    state: str | None
    stage: str | None
    data: Obj
    blocking: list[Any]
    next: NextBlock | None
    error: ErrorBlock | None


# ---------------------------------------------------------------- setup


class InitData(_Data):
    repo_root: str | None = None
    config_path: str | None = None
    config_written: bool | None = None
    hook_path: str | None = None
    hook_installed: bool | None = None
    hooks_path_overridden: bool | None = None
    worktree_root: str | None = None
    worktree_mode: str | None = None


class IntegrationsData(_Data):
    scope: str | None = None
    integrations: list[Obj] | None = None


# ---------------------------------------------------------------- running validation


class StartData(RunDataBase):
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_mode: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    head_sha: str | None = None
    source_head_sha: str | None = None
    merge_base_sha: str | None = None
    intent: str | None = None
    intent_source: str | None = None
    producer: Producer | None = None
    sync: Obj | None = None
    changed_files: list[str] | None = None
    risk: Obj | None = None
    copied_files: list[str] | None = None
    setup: Obj | None = None
    applicability: dict[str, Obj] | None = None
    housekeeping: Obj | None = None
    resumed: bool | None = None
    resume_reason: str | None = None
    # Refusals and sync failures.
    existing_intent: str | None = None
    requested_intent: str | None = None
    conflicting_run_id: str | None = None
    base_sha: str | None = None
    head_before: str | None = None
    conflicting_files: list[str] | None = None
    error_type: str | None = None
    detail: str | None = None


class ContextData(RunDataBase):
    section: str | None = None
    worktree_path: str | None = None
    base: str | None = None
    head: str | None = None
    intent: str | None = None
    intent_source: str | None = None
    changed_files: list[str] | None = None
    excluded_files: list[Any] | None = None
    diff: str | None = None
    diff_bytes: int | None = None
    risk: Obj | None = None
    grounding: Obj | None = None
    review_coverage: Obj | None = None
    contract_checks: Obj | None = None
    doc_surface: list[Obj] | None = None
    require_changelog: bool | None = None
    coverage_invalidated: bool | None = None
    # diff_too_large
    mode: str | None = None
    total_bytes: int | None = None
    max_bytes: int | None = None
    by_file: list[list[Any]] | None = None


class SubmitFindingsData(RunDataBase):
    accepted: list[Obj] | None = None
    total: int | None = None
    coverage: Obj | None = None
    risk: Obj | str | None = None
    mode: str | None = None


class _OutputSummary(BaseModel):
    output_head: str | None = None
    output_tail: str | None = None
    truncated: bool | None = None
    total_lines: int | None = None


class ReviewRunData(SubmitFindingsData, _OutputSummary):
    executor: str | None = None
    command: str | None = None
    exit_code: int | None = None
    reviewer_invocations: int | None = None
    output_sha256: str | None = None
    log_path: str | None = None
    timed_out: bool | None = None
    contract_checks: Obj | None = None
    copied_files: list[str] | None = None
    copied_file: str | None = None
    phase: str | None = None
    stage: str | None = None
    attempts: int | None = None


class ReviewContractsData(RunDataBase):
    contract_checks: Obj | None = None


class ReviewCompareData(RunDataBase):
    manifest: str | None = None
    head_sha: str | None = None
    executors: list[str] | None = None
    units: dict[str, int] | None = None
    findings: dict[str, list[Obj]] | None = None
    agreement_rate: float | None = None
    log_path: str | None = None
    exit_code: int | None = None


class RespondData(RunDataBase):
    finding: Obj | None = None
    remaining_blocking: int | None = None
    risk: Obj | None = None
    coverage_invalidated: bool | None = None


class VerifyData(RunDataBase):
    # Active review/docs stage.
    stage: str | None = None
    blocking_count: int | None = None
    coverage_invalidated: bool | None = None
    # verify SHA.
    verified: bool | None = None
    purpose: str | None = None
    test_status: str | None = None
    merge_requirements_satisfied: bool | None = None
    sha: str | None = None
    tree_sha: str | None = None
    notes_ref: str | None = None
    attestation: Obj | None = None
    reason: str | None = None


class ApprovalCheckData(_Data):
    approved: bool | None = None
    requires_human_approval: bool | None = None
    approval_mode: str | None = None
    approval_environment: str | None = None
    manual_merge_required: bool | None = None
    environment_approved: bool | None = None
    risk_level: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    changed_files: list[str] | None = None
    path_reasons: list[Obj] | None = None
    severe_findings: Any = None
    human_approvers: list[str] | None = None
    approved_head: str | None = None
    approval_carried: bool | None = None
    sha: str | None = None
    reason: str | None = None
    notes_ref: str | None = None


class Candidate(BaseModel):
    command: str
    source: str


class StageRunData(RunDataBase, _OutputSummary):
    stage: str | None = None
    command: str | None = None
    exit_code: int | None = None
    log_path: str | None = None
    timed_out: bool | None = None
    copied_file: str | None = None
    copied_files: list[str] | None = None
    baseline_red: Any = None
    mode: str | None = None
    candidates: list[Candidate] | None = None
    attempts: int | None = None
    setup_failure: Obj | None = None
    validation_restarted: bool | None = None
    coverage_invalidated: bool | None = None


class MergebackData(RunDataBase):
    pre_sha: str | None = None
    post_sha: str | None = None
    applied: list[str] | None = None
    local_tree_sha: str | None = None
    worktree_tree_sha: str | None = None
    tree_equivalent: bool | None = None
    green_transferred: bool | None = None
    worktree_mode: str | None = None
    validation_restarted: bool | None = None
    inputs_invalidated: bool | None = None
    coverage_invalidated: bool | None = None
    # Conflict report.
    conflicting_commit: str | None = None
    conflicting_files: list[str] | None = None
    fix_commits: list[str] | None = None
    worktree_path: str | None = None
    branch_restored: bool | None = None
    resolution: list[str] | None = None
    mode: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    affected_paths: list[str] | None = None
    overlapping_status: Any = None


class GateData(RunDataBase):
    remote: str | None = None
    refspec: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    pr_mode: str | None = None
    automated_cleanup: bool | None = None
    approval_mode: str | None = None
    commits: list[Obj] | None = None
    risk: Obj | None = None
    token: str | None = None
    push_commands: list[str] | None = None
    manual_command: str | None = None


class PushData(RunDataBase):
    dry_run: bool | None = None
    would_push: str | None = None
    pushed: bool | None = None
    sha: str | None = None
    remote: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    pr_mode: str | None = None
    automated_cleanup: bool | None = None


class FinishData(RunDataBase):
    pushed_sha: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    pr_mode: str | None = None
    automated_cleanup: bool | None = None


# ---------------------------------------------------------------- inspection and recovery


class HookStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str | None = None
    active: bool | None = None
    error: str | None = None


class StatusData(RunDataBase):
    has_run: bool | None = None
    hook: HookStatus | None = None
    dangling_run_id: str | None = None
    previous_run_id: str | None = None
    previous_state: str | None = None
    readable: bool | None = None
    read_failure: Obj | None = None
    evidence_valid: bool | None = None
    evidence_failure: Obj | None = None
    interrupted_legacy_update: bool | None = None
    seq: int | None = None
    branch: str | None = None
    base_ref: str | None = None
    head_sha: str | None = None
    source_head_sha: str | None = None
    intent: str | None = None
    intent_source: str | None = None
    producer: Producer | None = None
    sync: Obj | None = None
    current_tip: str | None = None
    stale: bool | None = None
    worktree_path: str | None = None
    source_worktree_id: str | None = None
    source_worktree_available: bool | None = None
    owner_ids: list[str] | None = None
    worktree_branch: str | None = None
    worktree_mode: str | None = None
    worktree_released: bool | None = None
    config_digest: str | None = None
    pr_mode: str | None = None
    automated_cleanup: bool | None = None
    approval_mode: str | None = None
    gate_token: str | None = None
    pushed_sha: str | None = None
    fix_commits: list[str] | None = None
    mergeback_attempt: Obj | None = None
    mergeback_conflict: Obj | None = None
    review_coverage: Obj | None = None
    stages: dict[str, Obj] | None = None
    setup_failure: Obj | None = None
    copied_files: list[str] | None = None
    findings: list[Obj] | None = None
    findings_summary: Obj | None = None
    applicability: dict[str, Obj] | None = None
    risk: Obj | None = None
    reuse_error: Any = None
    # status --all
    active: Any = None
    runs: list[Obj] | None = None
    count: int | None = None


class LogsData(RunDataBase):
    stage: str | None = None
    log_path: str | None = None
    output: str | None = None


class EventsData(RunDataBase):
    events: list[Obj] | None = None
    count: int | None = None


class AbortData(RunDataBase):
    discarded_fix_commits: list[str] | None = None
    preserved_in_place_commits: list[str] | None = None
    fix_commits: list[str] | None = None
    worktree_path: str | None = None


class GcData(RunDataBase):
    removed: list[Any] | None = None
    retained: list[Any] | None = None
    orphans: list[Any] | None = None
    reclaimed_runners: list[str] | None = None
    legacy_worktrees: list[str] | None = None
    runs_known: list[str] | None = None
    active: Any = None


class TelemetryRebuildData(_Data):
    repo: Obj | None = None
    summarized: int | None = None
    skipped: int | None = None
    unreadable: int | None = None
    unreadable_runs: list[Obj] | None = None
    write_failures: list[Any] | None = None
    mirror: Obj | None = None


class TelemetrySyncHostedData(_Data):
    repo: Obj | None = None
    dry_run: bool | None = None
    refresh: bool | None = None
    limit: int | None = None
    run: str | None = None
    eligible: int | None = None
    not_eligible: dict[str, int] | None = None
    unreadable: int | None = None
    unreadable_runs: list[Obj] | None = None
    already_synced: int | None = None
    selected: int | None = None
    deferred: int | None = None
    synced: int | None = None
    commit_not_found: int | None = None
    stale_summaries: int | None = None
    gh_found: bool | None = None
    runs: list[Obj] | None = None
    errors: list[Obj] | None = None
    stopped: Obj | None = None
    summaries: Obj | None = None


class ReportData(_Data):
    format: str | None = None
    source: Obj | None = None
    filters: Obj | None = None
    filtered_out: Obj | None = None
    rows_reported: int | None = None
    overall: Obj | None = None
    by_repo: Any = None
    by_client: Any = None
    by_model: Any = None
    hook_decisions: Obj | None = None
    limits: list[str] | None = None
    markdown: str | None = None
    rows: list[Obj] | None = None
    # telemetry_mirror_unreadable
    path: str | None = None
    error: str | None = None


#: Every agent-facing command, keyed by its CLI path. ``hook-check`` emits no envelope.
COMMAND_MODELS: dict[str, type[BaseModel]] = {
    "init": InitData,
    "integrations install": IntegrationsData,
    "integrations status": IntegrationsData,
    "integrations update": IntegrationsData,
    "integrations uninstall": IntegrationsData,
    "start": StartData,
    "context": ContextData,
    "review run": ReviewRunData,
    "review contracts": ReviewContractsData,
    "review compare": ReviewCompareData,
    "submit-findings": SubmitFindingsData,
    "respond": RespondData,
    "verify": VerifyData,
    "approval-check": ApprovalCheckData,
    "stage run": StageRunData,
    "mergeback": MergebackData,
    "gate": GateData,
    "push": PushData,
    "finish": FinishData,
    "status": StatusData,
    "logs": LogsData,
    "events": EventsData,
    "abort": AbortData,
    "gc": GcData,
    "telemetry rebuild": TelemetryRebuildData,
    "telemetry sync-hosted": TelemetrySyncHostedData,
    "report": ReportData,
}

#: Commands that print no JSON envelope.
NO_ENVELOPE = frozenset({"hook-check"})


def schema_filename(command: str) -> str:
    return command.replace(" ", "-") + ".json"


def schemas() -> dict[str, dict[str, Any]]:
    """Every schema file name mapped to its JSON Schema."""
    out: dict[str, dict[str, Any]] = {"envelope.json": EnvelopeModel.model_json_schema()}
    for command, model in COMMAND_MODELS.items():
        schema = model.model_json_schema()
        schema["title"] = f"agentic-preflight {command} data"
        out[schema_filename(command)] = schema
    return out
