"""Deterministic retrieval of repository-owned review context.

Grounding deliberately reads only committed files and persisted local runs. That
keeps the bundle tied to the reviewed Git snapshot without introducing a model,
an embedding index, a network dependency, or filesystem iteration order.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gitx
from . import risk as riskmod
from .diff import path_matches
from .models import RunDoc

if TYPE_CHECKING:
    from .runs._session import Session

_CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_CONVENTION_PATHS = ("AGENTS.md", "CLAUDE.md")
_WORD_CHARACTER = r"[A-Za-z0-9_]"


def _compact_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _entry(value: dict[str, Any], *, truncated: bool = False) -> dict[str, Any]:
    completed = {**value, "truncated": truncated}
    completed["bytes"] = _compact_bytes(completed)
    return completed


def _tracked_files(repo: Path | str) -> list[str]:
    output = gitx.run(repo, "ls-files", "-z").stdout
    return sorted(path for path in output.split("\0") if path)


def _blob_text(repo: Path | str, path: str) -> str | None:
    """Read a tracked path's committed content, or ``None`` if it has none.

    ``_tracked_files`` lists the index, which can include a path staged but not
    yet committed. Reading such a path from ``HEAD`` raises rather than
    returning empty text, so callers must treat that as "skip this source",
    not as a reason to fail the whole grounding bundle.
    """
    try:
        return gitx.run(repo, "show", f"HEAD:{path}").stdout
    except gitx.GitError:
        return None


def _truncate_lines(text: str, max_bytes: int) -> tuple[str, bool]:
    if len(text.encode()) <= max_bytes:
        return text, False
    selected: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode())
        if used + line_bytes > max_bytes:
            break
        selected.append(line)
        used += line_bytes
    return "".join(selected), True


def _codeowners_entries(
    repo: Path | str, tracked: set[str], changed_files: list[str]
) -> list[dict[str, Any]]:
    source = next((path for path in _CODEOWNERS_PATHS if path in tracked), None)
    if source is None:
        return []
    text = _blob_text(repo, source)
    if text is None:
        return []

    rules: list[tuple[str, list[str]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        pattern = fields[0]
        owners = []
        for owner in fields[1:]:
            if owner.startswith("#"):
                break
            owners.append(owner)
        if owners:
            rules.append((pattern, owners))

    entries = []
    for path in sorted(set(changed_files)):
        matched = next(
            (
                (pattern, owners)
                for pattern, owners in reversed(rules)
                if path_matches(path, pattern)
            ),
            None,
        )
        if matched is None:
            continue
        pattern, owners = matched
        entries.append(
            _entry(
                {
                    "kind": "codeowners",
                    "source": source,
                    "path": path,
                    "owners": owners,
                    "pattern": pattern,
                }
            )
        )
    return entries


def _terms(changed_files: list[str]) -> list[str]:
    terms: set[str] = set()
    for path in sorted(set(changed_files)):
        candidate = Path(path)
        values = [path, candidate.name]
        if candidate.suffix == ".py":
            values.append(candidate.stem)
        if path.startswith("agentic_preflight/"):
            relative = path.removeprefix("agentic_preflight/")
            values.extend((relative, path.removesuffix(".py").replace("/", ".")))
        for value in values:
            if len(value) < 4 or value == "__init__":
                continue
            terms.add(value)
    return sorted(terms)


def _matching_terms(text: str, terms: list[str]) -> list[str]:
    return [
        term
        for term in terms
        if re.search(rf"(?<!{_WORD_CHARACTER}){re.escape(term)}(?!{_WORD_CHARACTER})", text)
    ]


def _excerpt(text: str, terms: list[str]) -> str:
    patterns = [
        re.compile(rf"(?<!{_WORD_CHARACTER}){re.escape(term)}(?!{_WORD_CHARACTER})")
        for term in terms
    ]
    lines = text.splitlines(keepends=True)
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if not any(pattern.search(line) for pattern in patterns):
            continue
        selected.update(range(max(0, index - 1), min(len(lines), index + 2)))
    return "".join(lines[index] for index in sorted(selected))


def _doc_entries(
    repo: Path | str,
    tracked: set[str],
    changed_files: list[str],
    entry_max_bytes: int,
) -> list[dict[str, Any]]:
    terms = _terms(changed_files)
    entries = []
    for source in sorted(path for path in tracked if path.startswith("docs/")):
        text = _blob_text(repo, source)
        if text is None:
            continue
        matches = _matching_terms(text, terms)
        if not matches:
            continue
        excerpt, truncated = _truncate_lines(_excerpt(text, matches), entry_max_bytes)
        entries.append(
            _entry(
                {
                    "kind": "doc",
                    "source": source,
                    "terms": matches,
                    "excerpt": excerpt,
                },
                truncated=truncated,
            )
        )
    return entries


def _convention_sources(tracked: set[str], extra_paths: list[str]) -> list[str]:
    sources = [path for path in _CONVENTION_PATHS if path in tracked]
    extras = sorted(
        path
        for path in tracked
        if any(path_matches(path, pattern) for pattern in extra_paths) and path not in sources
    )
    return [*sources, *extras]


def _convention_entries(
    repo: Path | str,
    tracked: set[str],
    extra_paths: list[str],
    entry_max_bytes: int,
) -> list[dict[str, Any]]:
    entries = []
    for source in _convention_sources(tracked, extra_paths):
        raw_content = _blob_text(repo, source)
        if raw_content is None:
            continue
        content, truncated = _truncate_lines(raw_content, entry_max_bytes)
        entries.append(
            _entry(
                {"kind": "convention", "source": source, "content": content},
                truncated=truncated,
            )
        )
    return entries


def _history_entries(
    session: Session, run: RunDoc, changed_files: list[str]
) -> list[dict[str, Any]]:
    """Return prior findings from runs on this run's own branch.

    ``list_runs()`` spans every run recorded under the shared git-common-dir
    store, including runs the "reusable" and "strict" worktree modes are
    running concurrently in other linked worktrees on other branches. Without
    the branch filter, a finding one of those genuinely concurrent runs
    records on a path this run also changed would flip `grounding_sha256`
    between this run's `context` and `submit-findings` calls even though
    nothing about this run's own reviewed snapshot changed.
    """
    changed = set(changed_files)
    entries = []
    for run_id in session.store.list_runs():
        if run_id == run.run_id:
            continue
        try:
            other_run = session.store.load_run(run_id)
        except Exception:  # noqa: BLE001, S112 - history must survive one corrupt record
            continue
        if other_run.branch != run.branch:
            continue
        findings = sorted(session.store.load_findings(run_id), key=lambda finding: finding.id)
        for finding in findings:
            if finding.path not in changed:
                continue
            entries.append(
                _entry(
                    {
                        "kind": "prior_finding",
                        "source": run_id,
                        "path": finding.path,
                        "line": finding.line,
                        "severity": finding.severity.value,
                        "title": finding.title,
                        "status": finding.status.value,
                        "fix_commit": finding.fix_commit,
                        "stage": finding.stage.value,
                    }
                )
            )
    return entries


def _policy_entries(session: Session, changed_files: list[str]) -> list[dict[str, Any]]:
    """Return path-policy reasons that stay fixed for the review snapshot.

    Finding-derived reasons describe the current run's own output, not repository
    knowledge. Including them would make the grounding digest drift after a submission
    even though the reviewed snapshot had not changed.
    """
    assessment = riskmod.assess(
        changed_files,
        [],
        policy=session.config.policy,
        review_blocking_severities=session.config.review.blocking_severities,
        docs_blocking_severities=session.config.docs.blocking_severities,
    )
    return [
        _entry(
            {
                "kind": "policy",
                "source": ".agentic-preflight.toml",
                "reason": reason.model_dump(mode="json"),
            }
        )
        for reason in assessment.reasons
    ]


def _apply_total_budget(
    groups: Iterable[tuple[str, list[dict[str, Any]]]], max_bytes: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    entries: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    used = 0
    exhausted = False
    for kind, candidates in groups:
        for entry in candidates:
            if exhausted or used + entry["bytes"] > max_bytes:
                exhausted = True
                dropped[kind] = dropped.get(kind, 0) + 1
                continue
            entries.append(entry)
            used += entry["bytes"]
    return entries, dropped


def assemble(session: Session, run: RunDoc, changed_files: list[str]) -> dict[str, Any]:
    """Assemble stable, bounded repository context for one review snapshot."""
    config = session.config.context
    if not config.enabled:
        return {"enabled": False, "entries": [], "dropped": {}}

    repo = Path(run.worktree_path or session.repo_root)
    tracked = set(_tracked_files(repo))
    groups = (
        ("codeowners", _codeowners_entries(repo, tracked, changed_files)),
        ("doc", _doc_entries(repo, tracked, changed_files, config.entry_max_bytes)),
        (
            "convention",
            _convention_entries(repo, tracked, config.extra_paths, config.entry_max_bytes),
        ),
        ("prior_finding", _history_entries(session, run, changed_files)),
        ("policy", _policy_entries(session, changed_files)),
    )
    entries, dropped = _apply_total_budget(groups, config.max_bytes)
    return {"enabled": True, "entries": entries, "dropped": dropped}


def digest(grounding: dict[str, Any]) -> str:
    """Bind a coverage manifest to the exact grounding object it accompanies."""
    payload = json.dumps(grounding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
