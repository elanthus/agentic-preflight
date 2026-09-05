"""Deterministic applicability fingerprints for reusable preflight evidence.

See ``docs/fingerprint-contract.md`` for the design this module implements, and
issue #85 for the problem it is the first slice of.

This module answers one narrow question: *given the recorded inputs a stage's
green result depended on, and the same inputs recomputed against a new commit,
should that result be reused, discarded, or treated as unprovable?* It does not
decide *when* to ask that question, store its answer on a run, or change what
``agentic-preflight start`` does — that wiring, and the derived-attestation
issuance that would let a reused stage survive onto a new commit, are tracked
separately so this contract can be reviewed and tested on its own.

Every fingerprint is intentionally a flat, ``extra="forbid"`` model over
*content* identifiers (tree SHAs, content digests) rather than *history*
identifiers (commit SHAs): the whole point of this contract is to let evidence
survive a rebase or restack that does not touch the content a stage examined.
Two fingerprints compare equal only when every declared input matches; a field
this contract does not yet know how to compare must not be silently ignored,
so unknown provenance always classifies as ``unknown`` rather than reusable.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import gitx
from .attestation import intent_digest
from .config import config_digest
from .diff import ReviewManifest
from .models import Sha
from .stages import docs as docsstage

#: Bumped whenever a fingerprint's field set or comparison semantics change.
#: A version mismatch between an old and new fingerprint is treated the same
#: as a missing fingerprint: ``unknown``, never ``reusable``.
FINGERPRINT_VERSION = 1


class Disposition(StrEnum):
    """A stage's applicability verdict. Code-owned; no model assigns this."""

    REUSABLE = "reusable"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class ReasonCode(StrEnum):
    """Why a stage was not classified ``reusable``, most specific fact first."""

    FINGERPRINT_MISSING = "fingerprint_missing"
    FINGERPRINT_VERSION_MISMATCH = "fingerprint_version_mismatch"
    BASE_TREE_CHANGED = "base_tree_changed"
    HEAD_TREE_CHANGED = "head_tree_changed"
    DIFF_CONTENT_CHANGED = "diff_content_changed"
    EXCLUSIONS_CHANGED = "exclusions_changed"
    GROUNDING_CHANGED = "grounding_changed"
    INTENT_CHANGED = "intent_changed"
    EXECUTOR_CHANGED = "executor_changed"
    CONFIG_CHANGED = "config_changed"
    DOC_SURFACE_CHANGED = "doc_surface_changed"


class Classification(BaseModel):
    """A stage's disposition plus every reason that produced it."""

    model_config = ConfigDict(extra="forbid")

    disposition: Disposition
    reasons: tuple[ReasonCode, ...] = ()


class ReviewFingerprint(BaseModel):
    """Applicability inputs for one review-stage green result.

    ``config_sha256`` is scoped to :func:`review_relevant_config`, not the
    run's full configuration digest, so a change to an unrelated section (for
    example ``[commands] test``) cannot invalidate review evidence that never
    depended on it.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = FINGERPRINT_VERSION
    base_tree_sha: Sha
    head_tree_sha: Sha
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excluded_files: tuple[str, ...] = ()
    grounding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor: Literal["in_harness", "command"]
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocsFingerprint(BaseModel):
    """Applicability inputs for one docs-stage green result."""

    model_config = ConfigDict(extra="forbid")

    version: int = FINGERPRINT_VERSION
    base_tree_sha: Sha
    head_tree_sha: Sha
    doc_surface_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def review_relevant_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The configuration subset a review fingerprint must bind to.

    ``general.base_ref`` selects the trust boundary being merged into,
    ``review`` governs the executor and blocking policy, ``policy`` governs
    risk-derived executor escalation, ``context`` governs grounding delivery,
    and ``diff`` governs which files and hunks are in scope at all.
    """
    return {
        section: snapshot.get(section)
        for section in ("general", "review", "policy", "context", "diff")
    }


def docs_relevant_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The configuration subset a docs fingerprint must bind to."""
    return {section: snapshot.get(section) for section in ("general", "docs", "diff")}


def compute_review_fingerprint(
    repo: Path | str,
    *,
    base_sha: str,
    head_sha: str,
    manifest: ReviewManifest,
    executor: Literal["in_harness", "command"],
    intent: str,
    config_snapshot: dict[str, Any],
) -> ReviewFingerprint:
    """Fingerprint the inputs a green review of ``head_sha`` depended on."""
    return ReviewFingerprint(
        base_tree_sha=gitx.tree_sha(repo, base_sha),
        head_tree_sha=gitx.tree_sha(repo, head_sha),
        diff_sha256=manifest.diff_sha256,
        excluded_files=tuple(sorted(manifest.excluded_files)),
        grounding_sha256=manifest.grounding_sha256,
        intent_sha256=intent_digest(intent or ""),
        executor=executor,
        config_sha256=config_digest(review_relevant_config(config_snapshot)),
    )


def compute_docs_fingerprint(
    repo: Path | str,
    *,
    base_sha: str,
    head_sha: str,
    changed_files: list[str],
    doc_paths: list[str],
    config_snapshot: dict[str, Any],
) -> DocsFingerprint:
    """Fingerprint the inputs a green docs stage against ``head_sha`` depended on.

    The documentation surface is read from the worktree, so this must be
    called while ``head_sha`` is actually checked out there — the same
    convention ``review_protocol.context_data`` and ``grounding.assemble``
    already rely on.
    """
    inventory = docsstage.build_inventory(repo, changed_files, doc_paths)
    payload = [entry.as_dict() for entry in inventory]
    doc_surface_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DocsFingerprint(
        base_tree_sha=gitx.tree_sha(repo, base_sha),
        head_tree_sha=gitx.tree_sha(repo, head_sha),
        doc_surface_sha256=doc_surface_sha256,
        config_sha256=config_digest(docs_relevant_config(config_snapshot)),
    )


def classify_review(
    old: ReviewFingerprint | None, new: ReviewFingerprint
) -> Classification:
    """Classify whether a prior green review remains applicable to ``new``."""
    if old is None:
        return Classification(disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINGERPRINT_MISSING,))
    if old.version != new.version:
        return Classification(
            disposition=Disposition.UNKNOWN,
            reasons=(ReasonCode.FINGERPRINT_VERSION_MISMATCH,),
        )
    reasons: list[ReasonCode] = []
    if old.base_tree_sha != new.base_tree_sha:
        reasons.append(ReasonCode.BASE_TREE_CHANGED)
    if old.head_tree_sha != new.head_tree_sha:
        reasons.append(ReasonCode.HEAD_TREE_CHANGED)
    if old.diff_sha256 != new.diff_sha256:
        reasons.append(ReasonCode.DIFF_CONTENT_CHANGED)
    if old.excluded_files != new.excluded_files:
        reasons.append(ReasonCode.EXCLUSIONS_CHANGED)
    if old.grounding_sha256 != new.grounding_sha256:
        reasons.append(ReasonCode.GROUNDING_CHANGED)
    if old.intent_sha256 != new.intent_sha256:
        reasons.append(ReasonCode.INTENT_CHANGED)
    if old.executor != new.executor:
        reasons.append(ReasonCode.EXECUTOR_CHANGED)
    if old.config_sha256 != new.config_sha256:
        reasons.append(ReasonCode.CONFIG_CHANGED)
    if reasons:
        return Classification(disposition=Disposition.INVALID, reasons=tuple(reasons))
    return Classification(disposition=Disposition.REUSABLE)


def classify_docs(old: DocsFingerprint | None, new: DocsFingerprint) -> Classification:
    """Classify whether a prior green docs stage remains applicable to ``new``."""
    if old is None:
        return Classification(disposition=Disposition.UNKNOWN, reasons=(ReasonCode.FINGERPRINT_MISSING,))
    if old.version != new.version:
        return Classification(
            disposition=Disposition.UNKNOWN,
            reasons=(ReasonCode.FINGERPRINT_VERSION_MISMATCH,),
        )
    reasons: list[ReasonCode] = []
    if old.base_tree_sha != new.base_tree_sha:
        reasons.append(ReasonCode.BASE_TREE_CHANGED)
    if old.head_tree_sha != new.head_tree_sha:
        reasons.append(ReasonCode.HEAD_TREE_CHANGED)
    if old.doc_surface_sha256 != new.doc_surface_sha256:
        reasons.append(ReasonCode.DOC_SURFACE_CHANGED)
    if old.config_sha256 != new.config_sha256:
        reasons.append(ReasonCode.CONFIG_CHANGED)
    if reasons:
        return Classification(disposition=Disposition.INVALID, reasons=tuple(reasons))
    return Classification(disposition=Disposition.REUSABLE)
