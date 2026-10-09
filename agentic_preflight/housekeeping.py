"""Read-only inventory of one clone's preflight checkout footprint.

``start`` attaches this report so disk sprawl surfaces without a separate command.
Nothing here deletes or modifies anything; reclaiming space is ``gc``'s job.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import gitx
from .machine import TERMINAL_STATES
from .store import Store

#: Reclaimable bytes at or above this make the report ``noisy``, which tells the agent
#: to mention it to the user. 1 GiB keeps a few small leftover runners silent while
#: still reporting sprawl long before it reaches tens of gigabytes.
NOISY_RECLAIMABLE_BYTES = 1 << 30

GC_COMMAND = "agentic-preflight gc"

_TERMINAL = frozenset(state.value for state in TERMINAL_STATES)


def _real(path: str | Path) -> str:
    return os.path.realpath(path)


def directory_size(path: Path) -> int:
    """Sum file sizes under ``path`` without following symlinks or failing on errors."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _owners(store: Store, paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Map each checkout path to the run records that name it as ``worktree_path``.

    An unreadable record cannot prove which checkout it names, so it owns every
    unleased checkout: the report may under-count reclaimable space, never over-count.
    """
    owners: dict[str, list[dict[str, Any]]] = {path: [] for path in paths}
    for run_id in store.list_runs():
        fields = store.peek(run_id, "worktree_path", "state", "source_worktree_path")
        target = fields["worktree_path"] if fields is not None else None
        if fields is not None and (target is None or isinstance(target, str)):
            if target is None or _real(target) not in owners:
                continue
            source = fields["source_worktree_path"]
            owners[_real(target)].append(
                {
                    "run_id": run_id,
                    "readable": True,
                    "terminal": fields["state"] in _TERMINAL,
                    "source_exists": isinstance(source, str) and Path(source).exists(),
                }
            )
            continue
        for path in paths:
            owners[path].append({"run_id": run_id, "readable": False})
    return owners


def _classify(item: dict[str, Any], owners: list[dict[str, Any]]) -> tuple[str, str | None]:
    if item["leased"]:
        return "retained", f"leased on branch {item['branch']}"
    for owner in owners:
        if not owner["readable"]:
            return "retained", f"run {owner['run_id']} has an unreadable record"
    for owner in owners:
        if not owner["terminal"] and owner["source_exists"]:
            return "retained", f"owned by active run {owner['run_id']}"
    # gc keeps a released runner while any run naming it still has its source
    # worktree, so reporting it reclaimable would nag reusable-mode users forever.
    if any(owner["source_exists"] for owner in owners):
        return "retained", "reusable cache"
    return "reclaimable", None


def inventory(store: Store, repo_root: Path) -> dict[str, Any]:
    """Return the ``data.housekeeping`` report for ``start``. Never deletes anything."""
    registered: dict[str, dict[str, str]] = {
        _real(record["worktree"]): record
        for record in gitx.list_worktrees(repo_root)
        if "worktree" in record
    }
    candidates: set[str] = set()
    try:
        with os.scandir(store.worktrees_dir) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    candidates.add(_real(entry.path))
    except OSError:
        pass
    for path, entry_record in registered.items():
        if entry_record.get("branch", "").startswith("refs/heads/ap/"):
            candidates.add(path)

    paths = sorted(candidates)
    owners = _owners(store, paths)
    checkouts: list[dict[str, Any]] = []
    for path in paths:
        record = registered.get(path)
        branch = record.get("branch", "").removeprefix("refs/heads/") if record else ""
        item: dict[str, Any] = {
            "path": path,
            "bytes": directory_size(Path(path)),
            "registered": record is not None,
            "leased": bool(branch),
            "detached": record is not None and "detached" in record,
            "branch": branch or None,
            "owner_run_id": next((owner["run_id"] for owner in owners[path]), None),
        }
        item["status"], item["reason"] = _classify(item, owners[path])
        checkouts.append(item)

    branches = gitx.out(repo_root, "branch", "--list", "ap/*").splitlines()
    total = sum(item["bytes"] for item in checkouts)
    reclaimable = sum(item["bytes"] for item in checkouts if item["status"] == "reclaimable")
    return {
        "checkouts": checkouts,
        "total_bytes": total,
        "reclaimable_bytes": reclaimable,
        "ap_branches": sum(1 for line in branches if line.strip()),
        "noisy": reclaimable >= NOISY_RECLAIMABLE_BYTES,
        "next_command": GC_COMMAND if reclaimable > 0 else None,
    }


def report(store: Store, repo_root: Path) -> dict[str, Any]:
    """Like :func:`inventory`, but a failure becomes ``{"error": ...}`` instead of raising."""
    try:
        return inventory(store, repo_root)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never fail start
        return {"error": f"housekeeping inventory failed ({type(exc).__name__})"}
