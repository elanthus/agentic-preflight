"""Data shapes for runs, stages, and findings.

The division of labour encoded here is the heart of the findings pipeline:
**code owns identity, the agent owns judgment.** ``FindingSubmission`` is what
the agent sends and deliberately has no ``id``, ``stage``, or ``code_owned``
field, with ``extra="forbid"`` so that inventing one is a loud validation error
instead of a quietly honoured lie. ``Finding`` is what we store, and it adds the
fields only code may assign.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .machine import State

SHA_PATTERN = r"^[0-9a-f]{7,40}$"

Sha = Annotated[str, Field(pattern=SHA_PATTERN)]
ReviewUnitId = Annotated[str, Field(pattern=r"^U\d{4,}$")]


class Stage(StrEnum):
    REVIEW = "review"
    DOCS = "docs"
    LINT = "lint"
    TEST = "test"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingAction(StrEnum):
    AUTO_FIX = "auto_fix"
    ASK_USER = "ask_user"
    NO_OP = "no_op"


class FindingStatus(StrEnum):
    OPEN = "open"
    FIXED = "fixed"
    DISMISSED = "dismissed"
    ACCEPTED = "accepted"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Verdict(StrEnum):
    CLEAR = "pass"
    CHANGES_REQUIRED = "changes_required"
    NEEDS_HUMAN = "needs_human"


class RiskReason(BaseModel):
    """One deterministic input to a run's risk classification."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["human_review_path", "high_risk_path", "medium_risk_path", "finding"]
    level: RiskLevel
    path: str | None = None
    pattern: str | None = None
    finding_id: str | None = None
    severity: Severity | None = None


class RiskAssessment(BaseModel):
    """Policy-derived risk and verdict; no model is allowed to set either."""

    model_config = ConfigDict(extra="forbid")

    level: RiskLevel = RiskLevel.LOW
    verdict: Verdict = Verdict.CLEAR
    requires_human_review: bool = False
    reasons: list[RiskReason] = Field(default_factory=list)


class FindingSubmission(BaseModel):
    """What the agent sends. No identity fields — see module docstring."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    unit: ReviewUnitId | None = None
    line: int | None = Field(default=None, ge=1)
    severity: Severity
    action: FindingAction
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=4000)
    suggestion: str | None = Field(default=None, max_length=4000)


class Finding(BaseModel):
    """What code stores; identity, status, and ownership are code-assigned."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^F\d{3,}$")
    stage: Stage
    code_owned: bool = False
    status: FindingStatus = FindingStatus.OPEN
    fix_commit: str | None = None
    response_note: str | None = Field(default=None, max_length=4000)

    path: str
    unit: str | None = None
    line: int | None = None
    severity: Severity
    action: FindingAction
    title: str
    detail: str = ""
    suggestion: str | None = None

    @classmethod
    def from_submission(cls, submission: FindingSubmission, *, id: str, stage: Stage) -> Finding:
        return cls(id=id, stage=stage, **submission.model_dump())


class ReviewCoverageSubmission(BaseModel):
    """The agent's compact assertion over a tool-derived review manifest."""

    model_config = ConfigDict(extra="forbid")

    manifest: str = Field(pattern=r"^[0-9a-f]{64}$")
    examined: Literal["all"]


class ReviewSubmission(BaseModel):
    """Strict review-stage payload; legacy findings-only payloads are invalid."""

    model_config = ConfigDict(extra="forbid")

    coverage: ReviewCoverageSubmission
    findings: list[FindingSubmission]


class ReviewCoverage(BaseModel):
    """Code-derived evidence that every unit in one diff snapshot was disposed."""

    model_config = ConfigDict(extra="forbid")

    manifest: str = Field(pattern=r"^[0-9a-f]{64}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    total_units: int = Field(ge=0)
    cited_units: list[ReviewUnitId] = Field(default_factory=list)
    clean_units: list[ReviewUnitId] = Field(default_factory=list)
    excluded_files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_accounting(self) -> ReviewCoverage:
        cited = set(self.cited_units)
        clean = set(self.clean_units)
        if len(cited) != len(self.cited_units) or len(clean) != len(self.clean_units):
            raise ValueError("coverage unit lists may not contain duplicates")
        if cited & clean:
            raise ValueError("a review unit cannot be both cited and clean")
        if len(cited | clean) != self.total_units:
            raise ValueError("coverage unit counts do not account for the manifest")
        return self

    def summary(self) -> dict[str, str | int | list[str]]:
        return {
            "manifest": self.manifest,
            "head_sha": self.head_sha,
            "total_units": self.total_units,
            "cited_count": len(self.cited_units),
            "clean_count": len(self.clean_units),
            "excluded_files": self.excluded_files,
        }


class StageRecord(BaseModel):
    """Per-stage outcome recorded on the run document."""

    model_config = ConfigDict(extra="forbid")

    status: str = "pending"
    attempts: int = 0
    executor: Literal["in_harness", "command"] | None = None
    command: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    log_path: str | None = None
    finished_at: str | None = None
    head_sha: str | None = None


class RunDoc(BaseModel):
    """The persisted state document, ``runs/<run_id>/run.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    seq: int = 0
    state: State

    branch: str
    base_ref: str
    merge_base_sha: str
    head_sha: str
    source_head_sha: str | None = None
    sync_base_sha: str | None = None
    sync_base_ref: str | None = None
    sync_remote: str | None = None
    intent: str | None = None
    intent_source: str | None = None

    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_released: bool = False
    copied_files: list[str] = Field(default_factory=list)
    config_snapshot: dict[str, Any] | None = None
    config_digest: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    review_coverage: ReviewCoverage | None = None
    risk: RiskAssessment | None = None

    fix_commits: list[str] = Field(default_factory=list)
    stages: dict[Stage, StageRecord] = Field(default_factory=dict)

    stale: bool = False
    gate_token: str | None = None
    pushed_sha: str | None = None

    created_at: str | None = None
    updated_at: str | None = None


class AttestedStage(BaseModel):
    """Portable evidence for one stage.

    Review carries coverage evidence rather than a process transcript. Docs and
    deliberately skipped shell stages have no process evidence. Shell stages may
    only be called green when the command, zero exit code, and digest of their
    redacted captured output are all present.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["green", "skipped"]
    executor: Literal["in_harness", "command"] | None = None
    command: str | None = None
    exit_code: int | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = None
    coverage: ReviewCoverage | None = None


class Attestation(BaseModel):
    """The JSON document stored in ``refs/notes/agentic-preflight``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["agentic-preflight-attestation"] = "agentic-preflight-attestation"
    schema_version: Literal[3] = 3
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str
    base_ref: str
    merge_base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str
    green_at: str
    stages: dict[Stage, AttestedStage]
    findings_summary: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_evidence(self) -> Attestation:
        required = set(Stage)
        if set(self.stages) != required:
            missing = sorted(stage.value for stage in required - set(self.stages))
            extra = sorted(stage.value for stage in set(self.stages) - required)
            raise ValueError(f"stage set must be complete (missing={missing}, extra={extra})")
        if self.stages[Stage.REVIEW].status != "green":
            raise ValueError("review stage must be green")
        if self.stages[Stage.REVIEW].coverage is None:
            raise ValueError("green review stage lacks coverage evidence")
        for stage, evidence in self.stages.items():
            process_fields = (
                evidence.command,
                evidence.exit_code,
                evidence.output_sha256,
            )
            if stage is Stage.REVIEW:
                if evidence.executor is None:
                    raise ValueError("green review stage lacks executor evidence")
                if evidence.executor == "command":
                    if (
                        not evidence.command
                        or evidence.exit_code != 0
                        or not evidence.output_sha256
                    ):
                        raise ValueError("command review lacks process evidence")
                elif any(value is not None for value in process_fields):
                    raise ValueError("in-harness review cannot carry process evidence")
            elif stage in {Stage.LINT, Stage.TEST} and evidence.status == "green":
                if not evidence.command or evidence.exit_code != 0 or not evidence.output_sha256:
                    raise ValueError(f"green {stage.value} stage lacks process evidence")
            elif any(value is not None for value in process_fields):
                raise ValueError(
                    f"{stage.value} stage cannot carry process evidence with "
                    f"status {evidence.status}"
                )
            if stage is not Stage.REVIEW and evidence.coverage is not None:
                raise ValueError(f"{stage.value} stage cannot carry review coverage")
            if stage is not Stage.REVIEW and evidence.executor is not None:
                raise ValueError(f"{stage.value} stage cannot carry review executor")
            if evidence.status == "skipped" and not evidence.reason:
                raise ValueError(f"skipped {stage.value} stage lacks a reason")
        return self
