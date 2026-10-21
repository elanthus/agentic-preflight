"""Reviewer-authored contracts and code-owned execution receipts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


ProbeKind = Literal["ordinary", "boundary", "composition"]
ReceiptStatus = Literal["passed", "failed", "unverified", "not_applicable"]


class ContractProbe(_Strict):
    kind: ProbeKind
    code: str = Field(min_length=1, max_length=16_000)


class LegacyContractCheck(_Strict):
    id: str = Field(pattern=r"^C[0-9]{3,}$", max_length=20)
    contract: str = Field(min_length=1, max_length=2000)
    producer: str = Field(min_length=1, max_length=1000)
    consumer: str = Field(min_length=1, max_length=1000)
    oracle: str = Field(min_length=1, max_length=2000)
    probes: list[ContractProbe] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def input_classes(self) -> LegacyContractCheck:
        if {probe.kind for probe in self.probes} != {"ordinary", "boundary", "composition"}:
            raise ValueError("each check needs ordinary, boundary, and composition probes")
        return self


class LegacyContractUnit(_Strict):
    unit: str = Field(pattern=r"^U[0-9]{4,}$")
    disposition: Literal["checks", "not_applicable", "unverified"]
    checks: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def checked_or_explained(self) -> LegacyContractUnit:
        if bool(self.checks) != (self.disposition == "checks"):
            raise ValueError("only a checks disposition may reference nonempty checks")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("a unit may not reference a check twice")
        return self


def _references(units: Sequence[LegacyContractUnit], checks: Sequence[LegacyContractCheck]) -> None:
    unit_ids = [unit.unit for unit in units]
    check_ids = [check.id for check in checks]
    references = {check for unit in units for check in unit.checks}
    if len(set(unit_ids)) != len(unit_ids) or len(set(check_ids)) != len(check_ids):
        raise ValueError("contract unit and check identities must be unique")
    if references != set(check_ids):
        raise ValueError("all checks must be referenced, and all references must exist")


class LegacyContractPlan(_Strict):
    """Read schema-1 receipts without changing their digest-bound wire shape."""

    schema_version: Literal["1"] = "1"
    manifest: str = Field(pattern=r"^[0-9a-f]{64}$")
    units: list[LegacyContractUnit] = Field(max_length=10_000)
    checks: list[LegacyContractCheck] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def complete_references(self) -> LegacyContractPlan:
        _references(self.units, self.checks)
        return self


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class OracleProvenance(_Strict):
    kind: Literal["documented_contract", "reference_consumer", "derived_property"]
    source: Text
    rationale: Text
    input_domain: Text
    runtime: Text
    limitations: list[Text] = Field(max_length=16)


class ProbeReference(_Strict):
    check: str = Field(pattern=r"^C[0-9]{3,}$", max_length=20)
    kind: ProbeKind


class InputClassCoverage(_Strict):
    name: Text
    disposition: Literal["probes", "unverified"]
    probes: list[ProbeReference] = Field(default_factory=list, max_length=96)
    reason: Text

    @model_validator(mode="after")
    def probed_or_gap(self) -> InputClassCoverage:
        if bool(self.probes) != (self.disposition == "probes"):
            raise ValueError("input classes need probe references or an explicit unverified gap")
        keys = [(probe.check, probe.kind) for probe in self.probes]
        if len(set(keys)) != len(keys):
            raise ValueError("input class probe references must be unique")
        return self


class BehaviorCoverage(_Strict):
    transformation: Text
    input_classes: list[InputClassCoverage] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def distinct_classes(self) -> BehaviorCoverage:
        names = [item.name for item in self.input_classes]
        if len(set(names)) != len(names):
            raise ValueError("input class names must be unique within a transformation")
        return self


class ContractUnit(LegacyContractUnit):
    inventory: list[BehaviorCoverage] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def inventoried_or_explained(self) -> ContractUnit:
        if self.disposition == "not_applicable" and self.inventory:
            raise ValueError("not_applicable units cannot claim behavioral coverage")
        if self.disposition == "checks" and not self.inventory:
            raise ValueError("checked units need a behavioral coverage inventory")
        transformations = [item.transformation for item in self.inventory]
        if len(set(transformations)) != len(transformations):
            raise ValueError("transformation names must be unique within a unit")
        return self


class ContractCheck(LegacyContractCheck):
    oracle_provenance: OracleProvenance


class ContractPlan(_Strict):
    schema_version: Literal["2"] = "2"
    manifest: str = Field(pattern=r"^[0-9a-f]{64}$")
    units: list[ContractUnit] = Field(max_length=10_000)
    checks: list[ContractCheck] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def complete_references(self) -> ContractPlan:
        _references(self.units, self.checks)
        referenced = set()
        for unit in self.units:
            unit_checks = set()
            for behavior in unit.inventory:
                for input_class in behavior.input_classes:
                    for probe in input_class.probes:
                        if probe.check not in unit.checks:
                            raise ValueError("inventory probes must belong to their unit's checks")
                        referenced.add((probe.check, probe.kind))
                        unit_checks.add(probe.check)
            if unit_checks != set(unit.checks):
                raise ValueError("every unit check needs an inventory input class")
        expected = {(check.id, probe.kind) for check in self.checks for probe in check.probes}
        if referenced != expected:
            raise ValueError("every probe needs an inventory input class")
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
    schema_version: Literal["1", "2"] = "2"
    manifest: str
    head_sha: str
    config_sha256: str
    plan_sha256: str
    image: str | None
    snapshot_sha256: str | None = None
    status: ReceiptStatus
    plan: Annotated[ContractPlan | LegacyContractPlan, Field(discriminator="schema_version")]
    checks: list[CheckResult]
    limitation: str = (
        "Execution checks the proposed assertions, not the correctness of their oracle."
    )

    @model_validator(mode="after")
    def results_match_plan(self) -> ContractReceipt:
        if self.schema_version != self.plan.schema_version:
            raise ValueError("receipt and plan schema versions must match")
        if [result.id for result in self.checks] != [check.id for check in self.plan.checks]:
            raise ValueError("receipt results must match the plan checks in order")
        for check, result in zip(self.plan.checks, self.checks, strict=True):
            if [p.kind for p in result.probes] != [p.kind for p in check.probes]:
                raise ValueError(f"receipt results for {check.id} must match its plan probes")
        return self
