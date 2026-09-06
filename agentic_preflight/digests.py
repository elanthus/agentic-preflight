"""Canonical content digests shared by configuration and evidence models."""

import hashlib
import json
from typing import Any


def json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
