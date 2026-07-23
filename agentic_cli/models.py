"""Data shapes for runs, stages, and findings.

The division of labour encoded here is the heart of the findings pipeline:
**code owns identity, the agent owns judgment.** ``FindingSubmission`` is what
the agent sends and deliberately has no ``id`` and no ``stage`` field, with
``extra="forbid"`` so that inventing one is a loud validation error instead of a
quietly honoured lie. ``Finding`` is what we store, and it adds the fields only
code may assign.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .machine import State

SHA_PATTERN = r"^[0-9a-f]{7,40}$"

Sha = Annotated[str, Field(pattern=SHA_PATTERN)]


class Stage(str, Enum):
    REVIEW = "review"
    DOCS = "docs"
    LINT = "lint"
    TEST = "test"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingAction(str, Enum):
    AUTO_FIX = "auto_fix"
    ASK_USER = "ask_user"
    NO_OP = "no_op"


class FindingStatus(str, Enum):
    OPEN = "open"
    FIXED = "fixed"
    DISMISSED = "dismissed"
    ACCEPTED = "accepted"


class FindingSubmission(BaseModel):
    """What the agent sends. No identity fields — see module docstring."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    line: int | None = Field(default=None, ge=1)
    severity: Severity
    action: FindingAction
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=4000)
    suggestion: str | None = Field(default=None, max_length=4000)


class Finding(BaseModel):
    """What code stores. ``id``, ``stage``, and ``status`` are code-assigned."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^F\d{3,}$")
    stage: Stage
    status: FindingStatus = FindingStatus.OPEN
    fix_commit: str | None = None
    response_note: str | None = Field(default=None, max_length=4000)

    path: str
    line: int | None = None
    severity: Severity
    action: FindingAction
    title: str
    detail: str = ""
    suggestion: str | None = None

    @classmethod
    def from_submission(
        cls, submission: FindingSubmission, *, id: str, stage: Stage
    ) -> "Finding":
        return cls(id=id, stage=stage, **submission.model_dump())


class StageRecord(BaseModel):
    """Per-stage outcome recorded on the run document."""

    model_config = ConfigDict(extra="forbid")

    status: str = "pending"
    attempts: int = 0
    command: str | None = None
    exit_code: int | None = None
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

    fix_commits: list[str] = Field(default_factory=list)
    stages: dict[Stage, StageRecord] = Field(default_factory=dict)

    stale: bool = False
    gate_token: str | None = None
    pushed_sha: str | None = None
    pr_url: str | None = None
    ci_started_at: str | None = None
    ci_last_checked_at: str | None = None
    ci_status: str | None = None
    ci_failures: list[dict[str, Any]] = Field(default_factory=list)
    ci_logs: dict[str, str] = Field(default_factory=dict)
    cleanup_token: str | None = None
    cleanup_preview: dict[str, Any] | None = None

    created_at: str | None = None
    updated_at: str | None = None


class LedgerEntry(BaseModel):
    """One green tip. ``tree_sha`` is unused in v1 but present so that a
    rebase-tolerant v2 predicate is a one-line change."""

    model_config = ConfigDict(extra="forbid")

    sha: str
    tree_sha: str
    branch: str
    base_ref: str
    merge_base_sha: str
    run_id: str
    green_at: str
    stages: dict[Stage, str] = Field(default_factory=dict)
    findings_summary: dict[str, int] = Field(default_factory=dict)


class Ledger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    entries: dict[str, LedgerEntry] = Field(default_factory=dict)
