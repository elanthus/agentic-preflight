"""Attestation wire-version rules; callers retain one normalized model."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from .models import Attestation


class InvalidAttestation(ValueError):
    """A strict evidence failure with a machine-readable recovery category."""

    def __init__(self, message: str, *, reason: str = "invalid_evidence") -> None:
        super().__init__(message)
        self.reason = reason


def encode(value: Attestation) -> str:
    payload = value.model_dump(mode="json")
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
