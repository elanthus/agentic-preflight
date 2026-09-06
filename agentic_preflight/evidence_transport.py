"""Retain and publish the Git commits referenced by portable stage provenance."""

from __future__ import annotations

import re
from pathlib import Path

from . import gitx
from .models import Attestation

REF_PREFIX = "refs/agentic-preflight/evidence/"


def ref_for(sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("evidence refs require a full commit SHA")
    return REF_PREFIX + sha


def commits(value: Attestation) -> list[str]:
    if not value.evidence:
        return []
    result = {value.merge_base_sha}
    for item in value.evidence.values():
        result.update((item.origin.head_sha, item.origin.base_sha))
    return sorted(result)


def missing(repo: Path | str, value: Attestation) -> list[str]:
    return [
        sha
        for sha in commits(value)
        if gitx.run(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode
    ]


def retain(repo: Path | str, value: Attestation) -> None:
    # A notes blob containing a SHA does not retain that commit. Keep its graph
    # reachable locally even after worktree cleanup and later Git garbage collection.
    for sha in commits(value):
        gitx.rev_parse(repo, f"{sha}^{{commit}}")
        gitx.run(repo, "update-ref", ref_for(sha), sha)


def refspecs(value: Attestation) -> list[str]:
    # Use the immutable object ID as the source, independent of local ref movement.
    return [f"{sha}:{ref_for(sha)}" for sha in commits(value)]
