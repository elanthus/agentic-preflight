"""Preserve the configuration snapshot consumed by legacy v4/v5 verifiers."""

from typing import Any

from .ci_models import CISection


def compatible_snapshot(snapshot: dict[str, Any], ci: CISection) -> dict[str, Any]:
    # Default local configuration predates [ci]. Non-default authority must
    # remain in the snapshot and its digest; never silently downgrade it.
    if ci == CISection():
        snapshot.pop("ci", None)
    return snapshot
