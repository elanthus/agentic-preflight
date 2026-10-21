"""Snapshot-bound contract planning, receipts and review context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .. import gitx
from ..contract_models import ContractPlan, ContractReceipt, ImageInventory
from ..contract_runner import execute_plan, list_image_packages
from ..diff import DiffBundle, ReviewManifest
from ..digests import json_digest
from ..envelope import Envelope
from ..errors import InvalidFindings
from ..machine import State
from ..models import RunDoc
from ..store import _atomic_write
from ._session import (
    Session,
    _assert_fresh,
    _check_restart_limit,
    _envelope_for,
    _load_current,
    _require_state,
    _require_worktree,
)

# The delivered package listing stays bounded; the private cache file keeps every entry.
PACKAGES_BYTES = 24_000
PACKAGES_LIMITATION = (
    "Lists the distributions installed in the configured image. It states availability "
    "only; it does not say which consumer contracts the repository supports."
)
MAX_INVENTORY_FILE_BYTES = 1024 * 1024


def _directory(session: Session, run: RunDoc, manifest: ReviewManifest) -> Path:
    identity = json_digest(
        {
            "manifest": manifest.manifest,
            "head": manifest.head_sha,
            "config": session.config.review.contracts.model_dump(mode="json"),
        }
    )
    return session.store.run_dir(run.run_id) / "contracts" / identity


def receipt_for(
    session: Session,
    run: RunDoc,
    manifest: ReviewManifest,
) -> ContractReceipt | None:
    if not session.config.review.contracts.enabled:
        return None
    folder = _directory(session, run, manifest)
    pointer = folder / "current.json"
    if not pointer.exists():
        return None
    try:
        if folder.is_symlink() or folder.parent.is_symlink() or pointer.is_symlink():
            raise ValueError("receipt path contains a symlink")
        raw = json.loads(pointer.read_text(encoding="utf-8"))
        digest = raw["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid receipt identity")
        path = folder / (digest + ".json")
        # JSON escaping can expand each retained output byte sixfold. Accommodate
        # the largest validated plan (32 checks, three 64 KiB streams each).
        if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("unsafe receipt file")
        data = json.loads(path.read_text(encoding="utf-8"))
        receipt = ContractReceipt.model_validate(data)
        config_digest = json_digest(session.config.review.contracts.model_dump(mode="json"))
        if (
            json_digest(data) != digest
            or receipt.manifest != manifest.manifest
            or receipt.head_sha != manifest.head_sha
            or receipt.config_sha256 != config_digest
            or receipt.plan.manifest != manifest.manifest
            or receipt.plan_sha256 != json_digest(receipt.plan.model_dump(mode="json"))
        ):
            raise ValueError("receipt does not bind the current review")
        return receipt
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InvalidFindings(
            "contract receipt is unreadable or changed; run review contracts again"
        ) from exc


def _image_cache(session: Session, run: RunDoc, image: str) -> Path:
    """Listings are keyed by image digest alone: the same bytes list the same packages."""
    digest = image.rsplit("sha256:", 1)[1]
    return session.store.run_dir(run.run_id) / "contracts" / "images" / (digest + ".json")


def _read_inventory(path: Path) -> ImageInventory | None:
    try:
        if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
            return None
        if not path.is_file() or path.stat().st_size > MAX_INVENTORY_FILE_BYTES:
            return None
        return ImageInventory.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        # An unreadable cache entry is simply derived again; it is not evidence.
        return None


def image_packages(session: Session, run: RunDoc) -> tuple[ImageInventory, Path | None]:
    """Return the configured image's listing, starting at most one container per run."""
    config = session.config.review.contracts
    if config.image is None:
        return list_image_packages(config), None
    path = _image_cache(session, run, config.image)
    cached = _read_inventory(path)
    if cached is not None:
        return cached, path
    inventory = list_image_packages(config)
    folder = path.parent
    if inventory.container_name is None or folder.is_symlink() or folder.parent.is_symlink():
        # Nothing was attempted (no Docker), so the next call re-checks for it cheaply.
        return inventory, None
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(
        path, json.dumps(inventory.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    )
    return inventory, path


def _packages_context(session: Session, run: RunDoc) -> dict[str, Any]:
    inventory, path = image_packages(session, run)
    packages: list[dict[str, str]] = []
    used = 0
    for package in inventory.packages:
        item = {"name": package.name, "version": package.version}
        used += len(json.dumps(item).encode())
        if used > PACKAGES_BYTES:
            break
        packages.append(item)
    return {
        "status": inventory.status,
        "reason": inventory.reason,
        "path": None if path is None else str(path),
        "packages": packages,
        "packages_omitted": len(inventory.packages) - len(packages),
        "limitation": PACKAGES_LIMITATION,
    }


def contract_context(session: Session, run: RunDoc, manifest: ReviewManifest) -> dict[str, Any]:
    receipt = receipt_for(session, run, manifest)
    config = session.config.review.contracts
    data: dict[str, Any] = {
        "enabled": True,
        "max_checks": config.max_checks,
        "execution": {
            "image": config.image,
            "timeout_seconds": config.timeout_seconds,
            "total_timeout_seconds": config.total_timeout_seconds,
            "max_output_bytes": config.max_output_bytes,
            "installed_packages": _packages_context(session, run),
        },
        "plan_schema": ContractPlan.model_json_schema(),
        "receipt": None,
    }
    if receipt is None:
        return data
    digest = json_digest(receipt.model_dump(mode="json"))
    # Full source, probes and output stay in a private immutable artifact. Keep the
    # delivered summary small even at the largest configured plan/output limits.
    details = []
    used = 0
    for check, result in zip(receipt.plan.checks, receipt.checks, strict=True):
        detail = {
            "id": check.id,
            "contract": check.contract,
            "oracle": check.oracle,
            "probes": [p.model_dump(mode="json") for p in result.probes],
        }
        size = len(json.dumps(detail).encode())
        if used + size > 16_000:
            continue
        details.append(detail)
        used += size
    data.pop("plan_schema")
    data["receipt"] = {
        "sha256": digest,
        "status": receipt.status,
        "path": str(_directory(session, run, manifest) / (digest + ".json")),
        "unverified_units": sum(unit.disposition == "unverified" for unit in receipt.plan.units),
        "checks": [
            {
                "id": result.id,
                "probes": [{"kind": p.kind, "status": p.status} for p in result.probes],
            }
            for result in receipt.checks
        ],
        "details": details,
        "details_omitted": len(receipt.checks) - len(details),
        "limitation": receipt.limitation,
    }
    return data


def record_plan(session: Session, run: RunDoc, payload: Any, bundle: DiffBundle) -> ContractReceipt:
    from . import review_protocol

    if not session.config.review.contracts.enabled:
        raise InvalidFindings("contract checks are not enabled for this run")
    manifest = review_protocol.grounded_manifest(session, run, bundle)
    try:
        plan = ContractPlan.model_validate(payload)
        if len(json.dumps(payload).encode()) > 2_000_000:
            raise ValueError("contract plan byte budget exceeded")
        if plan.manifest != manifest.manifest or {u.unit for u in plan.units} != {
            u.id for u in manifest.units
        }:
            raise ValueError("contract plan must account for exactly the current review units")
        if len(plan.checks) > session.config.review.contracts.max_checks:
            raise ValueError("contract check count exceeds the configured limit")
    except (ValidationError, ValueError) as exc:
        raise InvalidFindings(f"invalid contract plan: {exc}") from exc
    worktree = Path(_require_worktree(run))
    if not gitx.is_clean(worktree):
        raise InvalidFindings("contract execution requires a clean committed worktree")
    receipt = execute_plan(worktree, manifest.head_sha, plan, session.config.review.contracts)
    session.clear_memo()
    current = review_protocol.grounded_manifest(
        session, run, review_protocol.bundle_for(session, run)
    )
    if (
        current.manifest != manifest.manifest
        or current.head_sha != manifest.head_sha
        or not gitx.is_clean(worktree)
    ):
        raise InvalidFindings(
            "review inputs changed during contract execution; fetch fresh context"
        )
    data = receipt.model_dump(mode="json")
    digest = json_digest(data)
    folder = _directory(session, run, manifest)
    if folder.is_symlink() or folder.parent.is_symlink():
        raise InvalidFindings("contract receipt path contains a symlink")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (digest + ".json")
    if not path.exists():
        _atomic_write(path, json.dumps(data, sort_keys=True, separators=(",", ":")))
    _atomic_write(folder / "current.json", json.dumps({"sha256": digest}))
    session.store.append_event(
        run.run_id,
        {
            "event": "review_contracts_checked",
            "manifest": manifest.manifest,
            "receipt_sha256": digest,
            "status": receipt.status,
        },
    )
    return receipt


def check_contracts(session: Session, payload: Any) -> Envelope:
    from . import review_protocol

    run = _load_current(session)
    _check_restart_limit(run)
    _assert_fresh(session, run)
    _require_state(run, State.REVIEW_AWAITING_FINDINGS, command="review contracts")
    record_plan(session, run, payload, review_protocol.bundle_for(session, run))
    manifest = review_protocol.grounded_manifest(
        session, run, review_protocol.bundle_for(session, run)
    )
    return _envelope_for(
        run,
        stage="review",
        data={"contract_checks": contract_context(session, run, manifest)},
        next_command="agentic-preflight context",
    )
