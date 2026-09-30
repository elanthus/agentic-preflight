"""Canonical content digests shared by configuration and evidence models."""

import hashlib
import json
from typing import Any


def json_digest(value: Any) -> str:
    """Return the SHA-256 hex digest of a value's canonical JSON form."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
