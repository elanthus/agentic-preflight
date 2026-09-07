"""Committed declarations first; bounded source-marker compatibility second.

Only the supplied protected-base revision participates in negotiation. See
docs/schema-compatibility.md for declaration precedence and retirement conditions.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import gitx


def _supports(repo: Path | str, revision: str, *, section: str, key: str, version: int) -> bool:
    policy = gitx.run(repo, "show", f"{revision}:.agentic-preflight.toml", check=False)
    if policy.returncode == 0:
        try:
            declaration = tomllib.loads(policy.stdout).get(section, {})
        except ValueError:
            return False
        if not isinstance(declaration, dict):
            return False
        if key in declaration:
            value = declaration[key]
            return type(value) is int and value == version
    return _legacy_source_supports(repo, revision, version)


def _legacy_source_supports(repo: Path | str, revision: str, version: int) -> bool:
    # Keep historical spellings only for bases without a declaration. Do not
    # broaden this heuristic into parsing or executing proposed Python code.
    path, marker = {
        5: ("refresh_validation.py", "\nREFRESH_WIRE_VERSION = 5\n"),
        6: ("ci_models.py", "\nclass TestDelegation(BaseModel):\n"),
    }[version]
    result = gitx.run(repo, "show", f"{revision}:agentic_preflight/{path}", check=False)
    return result.returncode == 0 and marker in result.stdout


def base_supports_refresh(repo: Path | str, base: str) -> bool:
    return _supports(repo, base, section="reuse", key="attestation_schema", version=5)


def consumer_installed(repo: Path | str, revision: str) -> bool:
    return _supports(repo, revision, section="ci", key="consumer_schema", version=6)
