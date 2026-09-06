"""Strict declarations for opt-in, protected GitHub Actions test authority."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CISection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_authority: Literal["local", "github_actions"] = "local"
    consumer_schema: Literal[6] | None = None
    repository_id: int | None = Field(default=None, gt=0)
    base_branch: str = "main"
    workflow_id: int | None = Field(default=None, gt=0)
    check_app_id: int | None = Field(default=None, gt=0)
    workflow_path: str = Field(
        default=".github/workflows/preflight-tests.yml",
        pattern=r"^\.github/workflows/[A-Za-z0-9_-]+\.ya?ml$",
    )
    required_jobs: list[str] = Field(default_factory=list, max_length=100)
    max_age_seconds: int = Field(default=86400, ge=60, le=604800)

    @model_validator(mode="after")
    def validate_authority(self) -> CISection:
        if self.test_authority == "github_actions" and (
            self.consumer_schema != 6
            or self.repository_id is None
            or self.workflow_id is None
            or self.check_app_id is None
            or not self.base_branch.strip()
            or not self.required_jobs
            or len(set(self.required_jobs)) != len(self.required_jobs)
            or any(
                not name.strip() or name in {"prepare", "approval"} for name in self.required_jobs
            )
        ):
            raise ValueError(
                "CI delegation requires consumer_schema=6, repository/workflow/check-app IDs, "
                "a base branch, and unique required test jobs (prepare/approval are reserved)"
            )
        return self


class TestDelegation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Literal["integration"] = "integration"
    policy_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy: CISection

    @model_validator(mode="after")
    def enabled(self) -> TestDelegation:
        if self.policy.test_authority != "github_actions":
            raise ValueError("delegation requires GitHub Actions authority")
        return self
