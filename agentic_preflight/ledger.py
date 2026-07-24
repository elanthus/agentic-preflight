"""The green ledger: which exact commits passed every enabled stage.

Written once per run, for the final local tip. The pre-push hook is a pure
predicate over this file and nothing else — no network, no mutation, no run
state — which is what lets it stay under the latency budget and keeps a broken
tool from bricking a repo.

``tree_sha`` is recorded but unused in v1. It is here so that a v2
rebase-tolerant predicate (accept a tip whose *tree* matches a green entry, even
though rebasing changed its SHA) is a one-line change rather than a schema
migration.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import Ledger, LedgerEntry, RunDoc, Stage

MAX_ENTRIES = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_entry(
    run: RunDoc,
    *,
    sha: str,
    tree_sha: str,
    stages: dict[Stage, str],
    findings_summary: dict[str, int],
) -> LedgerEntry:
    return LedgerEntry(
        sha=sha,
        tree_sha=tree_sha,
        branch=run.branch,
        base_ref=run.base_ref,
        merge_base_sha=run.merge_base_sha,
        run_id=run.run_id,
        green_at=_now(),
        stages={k: v for k, v in stages.items()},
        findings_summary=findings_summary,
    )


def record(ledger: Ledger, entry: LedgerEntry) -> Ledger:
    """Add an entry and prune to the most recent ``MAX_ENTRIES``."""
    ledger.entries[entry.sha] = entry
    if len(ledger.entries) > MAX_ENTRIES:
        ordered = sorted(ledger.entries.items(), key=lambda kv: kv[1].green_at)
        for sha, _ in ordered[: len(ledger.entries) - MAX_ENTRIES]:
            del ledger.entries[sha]
    return ledger


def is_green(ledger: Ledger, sha: str) -> bool:
    """The hook's whole question, asked of one exact SHA."""
    return sha in ledger.entries
