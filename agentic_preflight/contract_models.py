"""Reviewer-authored contracts and code-owned execution receipts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


ProbeKind = Literal["ordinary", "boundary", "composition"]
ReceiptStatus = Literal["passed", "failed", "unverified", "not_applicable"]


class ContractProbe(_Strict):
    kind: ProbeKind
    code: str = Field(min_length=1, max_length=16_000)


class ContractCheck(_Strict):
    id: str = Field(pattern=r"^C[0-9]{3,}$", max_length=20)
    contract: str = Field(min_length=1, max_length=2000)
    producer: str = Field(min_length=1, max_length=1000)
    consumer: str = Field(min_length=1, max_length=1000)
    oracle: str = Field(min_length=1, max_length=2000)
    probes: list[ContractProbe] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def input_classes(self) -> ContractCheck:
        if {probe.kind for probe in self.probes} != {"ordinary", "boundary", "composition"}:
            raise ValueError("each check needs ordinary, boundary, and composition probes")
        return self


class ContractUnit(_Strict):
    unit: str = Field(pattern=r"^U[0-9]{4,}$")
    disposition: Literal["checks", "not_applicable", "unverified"]
    checks: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def checked_or_explained(self) -> ContractUnit:
        if bool(self.checks) != (self.disposition == "checks"):
            raise ValueError("only a checks disposition may reference nonempty checks")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("a unit may not reference a check twice")
        return self


class ContractPlan(_Strict):
    schema_version: Literal["1"] = "1"
    manifest: str = Field(pattern=r"^[0-9a-f]{64}$")
    units: list[ContractUnit] = Field(max_length=10_000)
    checks: list[ContractCheck] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def complete_references(self) -> ContractPlan:
        unit_ids = [unit.unit for unit in self.units]
        check_ids = [check.id for check in self.checks]
        references = {check for unit in self.units for check in unit.checks}
        if len(set(unit_ids)) != len(unit_ids) or len(set(check_ids)) != len(check_ids):
            raise ValueError("contract unit and check identities must be unique")
        if references != set(check_ids):
            raise ValueError("all checks must be referenced, and all references must exist")
        return self


class ProbeResult(_Strict):
    kind: ProbeKind
    status: Literal["passed", "failed", "unverified"]
    exit_code: int | None = None
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0, ge=0)
    container_name: str | None = None
    cleanup_succeeded: bool | None = None


class CheckResult(_Strict):
    id: str
    probes: list[ProbeResult]


class InstalledPackage(_Strict):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)


class ImageInventory(_Strict):
    """Code-owned listing of the distributions installed in a pinned image.

    The listing states availability only: what a probe can import, never which
    consumer contracts the repository supports or what a consumer should output.
    """

    schema_version: Literal["1"] = "1"
    image: str
    status: Literal["available", "unavailable"]
    packages: list[InstalledPackage] = Field(default_factory=list)
    reason: str = ""
    exit_code: int | None = None
    container_name: str | None = None
    cleanup_succeeded: bool | None = None


class ContractReceipt(_Strict):
    schema_version: Literal["1"] = "1"
    manifest: str
    head_sha: str
    config_sha256: str
    plan_sha256: str
    image: str | None
    snapshot_sha256: str | None = None
    status: ReceiptStatus
    plan: ContractPlan
    checks: list[CheckResult]
    limitation: str = (
        "Execution checks the proposed assertions, not the correctness of their oracle."
    )
