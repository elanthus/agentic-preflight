"""Wire-version rules; callers retain one normalized Attestation model."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from .digests import json_digest

if TYPE_CHECKING:
    from .models import Attestation

SchemaVersion = Literal[4, 5, 6]


def producer_schema(*, delegated: bool, refresh_available: bool) -> SchemaVersion:
    return 6 if delegated else 5 if refresh_available else 4


def has_refresh_evidence(value: Attestation) -> bool:
    return value.schema_version in {5, 6}


def has_pending_tests(value: Attestation) -> bool:
    return value.schema_version == 6


def validate_version(value: Attestation) -> None:
    from .models import AttestedStage, Stage

    if value.schema_version < 6 and (
        value.green_at is None
        or value.test_delegation is not None
        or value.publication_ready_at is not None
        or any(item.status == "delegated" for item in value.stages.values())
    ):
        raise ValueError("legacy attestations must describe completed local validation")
    if value.schema_version == 6 and (
        value.green_at is not None
        or value.publication_ready_at is None
        or value.test_delegation is None
        or value.stages.get(Stage.TEST, AttestedStage(status="skipped")).status != "delegated"
        or any(
            item.status == "delegated" for key, item in value.stages.items() if key != Stage.TEST
        )
    ):
        raise ValueError("v6 requires explicit pending test delegation and publication time")
    if value.schema_version == 4 and (
        value.evidence is not None or value.config_snapshot is not None
    ):
        raise ValueError("v4 attestations cannot carry refresh evidence")
    if value.schema_version in {5, 6}:
        if (
            value.config_snapshot is None
            or json_digest(value.config_snapshot) != value.config_sha256
        ):
            raise ValueError("configuration does not match its digest")
        required_evidence = set(Stage) - ({Stage.TEST} if value.schema_version == 6 else set())
        if value.evidence is None or set(value.evidence) != required_evidence:
            raise ValueError("requires a complete local per-stage evidence set")
        if any(item.origin.stage != stage for stage, item in value.evidence.items()):
            raise ValueError("evidence is attached to the wrong stage")


class InvalidAttestation(ValueError):
    """A strict evidence failure with a machine-readable recovery category."""

    def __init__(self, message: str, *, reason: str = "invalid_evidence") -> None:
        super().__init__(message)
        self.reason = reason


def encode(value: Attestation) -> str:
    payload = value.model_dump(mode="json")
    if value.schema_version < 6:
        payload.pop("test_delegation")
        payload.pop("publication_ready_at")
    if value.schema_version == 4:
        payload.pop("evidence")
        payload.pop("config_snapshot")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def decode(payload: str) -> Attestation:
    from .models import Attestation

    try:
        return Attestation.model_validate_json(payload)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        if any(error["type"] == "json_invalid" for error in errors):
            reason = "malformed_payload"
        elif any(
            error["type"] == "extra_forbidden" or error["loc"] == ("schema_version",)
            for error in errors
        ):
            reason = "incompatible_schema"
        else:
            reason = "invalid_evidence"
        # Pydantic's formatted exception includes raw inputs. Keep note bodies private.
        fields = ", ".join(
            f"{'.'.join(map(str, error['loc'])) or '<root>'}: {error['type']}"
            + (f" ({error['msg']})" if error["type"] == "value_error" else "")
            for error in errors
        )
        raise InvalidAttestation(
            f"attestation validation failed ({fields})", reason=reason
        ) from exc
