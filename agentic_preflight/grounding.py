"""Deterministic retrieval of repository-owned review context.

Grounding deliberately reads only committed files and persisted local runs. That
keeps the bundle tied to the reviewed Git snapshot without introducing a model,
an embedding index, a network dependency, or filesystem iteration order.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gitx
from . import risk as riskmod
from .diff import path_matches
from .models import RunDoc

if TYPE_CHECKING:
    from .runs._session import Session

_CONVENTION_PATHS = ("AGENTS.md", "CLAUDE.md")
_WORD_CHARACTER = r"[A-Za-z0-9_]"
_WORD_RUN = re.compile(rf"{_WORD_CHARACTER}+")


def _compact_bytes(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _entry(value: dict[str, Any], *, truncated: bool = False) -> dict[str, Any]:
    completed = {**value, "truncated": truncated}
    completed["bytes"] = _compact_bytes(completed)
    return completed


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


def _terms(changed_files: list[str]) -> list[str]:
    terms: set[str] = set()
    for path in sorted(set(changed_files)):
        candidate = Path(path)
        values = [path, candidate.name]
        if candidate.suffix == ".py":
            values.append(candidate.stem)
            parts = candidate.parts
            if len(parts) > 1:
                relative = "/".join(parts[1:])
                values.extend((relative, path.removesuffix(".py").replace("/", ".")))
        for value in values:
            if len(value) < 4 or value == "__init__":
                continue
            terms.add(value)
    return sorted(terms)


@dataclass(frozen=True)
class _Term:
    """A search term with the word runs every whole-token occurrence must contain.

    A match cannot touch a word character on either side, so each word run in the
    term must appear as a complete token of the searched text. Checking that against
    a token set first leaves the regex for the few compound terms that pass, instead
    of scanning every document once per term.
    """

    text: str
    runs: frozenset[str]
    pattern: re.Pattern[str]
    single_run: bool


def _compile_terms(terms: list[str]) -> list[_Term]:
    return [
        _Term(
            text=term,
            runs=frozenset(_WORD_RUN.findall(term)),
            pattern=re.compile(rf"(?<!{_WORD_CHARACTER}){re.escape(term)}(?!{_WORD_CHARACTER})"),
            single_run=_WORD_RUN.fullmatch(term) is not None,
        )
        for term in terms
    ]


def _matching_terms(text: str, terms: list[_Term]) -> list[_Term]:
    tokens = set(_WORD_RUN.findall(text))
    return [
        term
        for term in terms
        if term.runs <= tokens and (term.single_run or term.pattern.search(text) is not None)
    ]


def _excerpt(text: str, terms: list[_Term]) -> str:
    lines = text.splitlines(keepends=True)
    selected: set[int] = set()
    for index, line in enumerate(lines):
        if not _matching_terms(line, terms):
            continue
        selected.update(range(max(0, index - 1), min(len(lines), index + 2)))
    return "".join(lines[index] for index in sorted(selected))


def _doc_entries(
    texts: dict[str, str],
    changed_files: list[str],
    entry_max_bytes: int,
) -> list[dict[str, Any]]:
    terms = _compile_terms(_terms(changed_files))
    entries = []
    for source in sorted(path for path in texts if path.startswith("docs/")):
        text = texts[source]
        matches = _matching_terms(text, terms)
        if not matches:
            continue
        excerpt, truncated = _truncate_lines(_excerpt(text, matches), entry_max_bytes)
        entries.append(
            _entry(
                {
                    "kind": "doc",
                    "source": source,
                    "terms": [term.text for term in matches],
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
    texts: dict[str, str],
    extra_paths: list[str],
    entry_max_bytes: int,
) -> list[dict[str, Any]]:
    entries = []
    for source in _convention_sources(set(texts), extra_paths):
        raw_content = texts[source]
        content, truncated = _truncate_lines(raw_content, entry_max_bytes)
        entries.append(
            _entry(
                {"kind": "convention", "source": source, "content": content},
                truncated=truncated,
            )
        )
    return entries


def _other_branch(peeked: dict[str, Any] | None, branch: str) -> bool:
    """Whether a raw record read shows a run on a different branch."""
    if peeked is None or not isinstance(peeked["branch"], str):
        return False
    return peeked["branch"] != branch


def _history_entries(
    session: Session, run: RunDoc, changed_files: list[str]
) -> list[dict[str, Any]]:
    """Return prior findings from runs on this run's own branch.

    ``list_runs()`` spans every run in the shared git-common-dir store,
    including concurrent runs in other linked worktrees on other branches. The
    branch filter keeps `grounding_sha256` stable between this run's `context`
    and `submit-findings` calls when a concurrent run records a finding on a
    path this run also changed. A raw read of each record's branch skips other
    branches before the full validated load; a record that cannot be read that
    way still goes through ``load_run`` and its handling below.
    """
    changed = set(changed_files)
    entries = []
    for run_id in session.store.list_runs():
        if run_id == run.run_id:
            continue
        if _other_branch(session.store.peek(run_id, "branch"), run.branch):
            continue
        try:
            other_run = session.store.load_run(run_id)
        except Exception:  # noqa: BLE001, S112 - history must survive one corrupt record
            continue
        if other_run.branch != run.branch:
            continue
        try:
            findings = sorted(session.store.load_findings(run_id), key=lambda finding: finding.id)
        except Exception:  # noqa: BLE001, S112 - history must survive one corrupt record
            continue
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
    texts, omitted, _ = _load_sources(
        repo,
        lambda path: (
            path.startswith("docs/")
            or path in _CONVENTION_PATHS
            or any(path_matches(path, pattern) for pattern in config.extra_paths)
        ),
    )
    groups = (
        ("doc", _doc_entries(texts, changed_files, config.entry_max_bytes)),
        (
            "convention",
            _convention_entries(texts, config.extra_paths, config.entry_max_bytes),
        ),
        ("prior_finding", _history_entries(session, run, changed_files)),
        ("policy", _policy_entries(session, changed_files)),
    )
    entries, dropped = _apply_total_budget(groups, config.max_bytes)
    result = {"enabled": True, "entries": entries, "dropped": dropped}
    if omitted:
        result["omitted_sources"] = omitted
    return result


def digest(grounding: dict[str, Any]) -> str:
    """Bind a coverage manifest to the exact grounding object it accompanies."""
    payload = json.dumps(grounding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


MAX_SOURCE_BYTES = 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_SOURCES = 1024
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".woff",
        ".woff2",
        ".mp4",
        ".mp3",
        ".mov",
        ".ttf",
    }
)


def _load_sources(
    repo: Path | str, select: Callable[[str], bool]
) -> tuple[dict[str, str], dict[str, int], set[str]]:
    """Select by metadata before one batched content read; report every omitted source.

    Object IDs from ls-tree bind subsequent reads to that tree even if HEAD moves.
    Neither staged files nor filesystem symlink targets enter the source set.
    """
    output = gitx.run(repo, "ls-tree", "-r", "-l", "-z", "HEAD").stdout
    candidates = []
    paths = set()
    omitted: Counter[str] = Counter()
    for record in output.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        if not select(path):
            continue
        paths.add(path)
        mode, kind, oid, size = metadata.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            omitted["non_regular"] += 1
        elif Path(path).suffix.lower() in _BINARY_SUFFIXES:
            omitted["binary"] += 1
        elif int(size) > MAX_SOURCE_BYTES:
            omitted["oversized"] += 1
        else:
            candidates.append((path, oid, int(size)))
    selected: list[tuple[str, str]] = []
    total = 0
    for path, oid, size in sorted(candidates):
        if len(selected) >= MAX_SOURCES or total + size > MAX_READ_BYTES:
            omitted["read_budget"] += 1
            continue
        total += size
        selected.append((path, oid))
    blobs = gitx.read_blobs(repo, [oid for _, oid in selected])
    texts = {}
    for (path, _), blob in zip(selected, blobs, strict=True):
        if b"\0" in blob:
            omitted["binary"] += 1
            continue
        texts[path] = blob.decode("utf-8", errors="backslashreplace")
    return texts, dict(sorted(omitted.items())), paths
