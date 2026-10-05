"""The documentation stage: the review sub-machine, pointed at docs.

Agent-driven and with no shell command of its own; it reuses the findings
machinery entirely.

Two things distinguish it from review:

- **Findings may target files outside the diff.** That is the whole point: the
  diff changed code, and the doc that should have changed did not. So the
  changed-file constraint relaxes to a *documentation allowlist*. It does not
  become unconstrained; a "docs" finding against ``src/auth.py`` is still
  rejected, because it belongs to the review stage.
- **``require_changelog`` is owned by code.** Whether a changelog was touched is
  a mechanical fact, and mechanical facts should not depend on the agent
  remembering a rule.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..diff import match_prefixes, path_matches
from ..models import Finding, FindingAction, Severity, Stage

#: The documentation surface every repo is assumed to have. Anything in
#: ``[docs] paths`` is added to this.
STANDARD_DOC_PATTERNS: tuple[str, ...] = (
    "README*",
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING*",
    "CHANGELOG*",
    "docs/**",
    ".claude/rules/**",
    ".github/instructions/**",
    "PRODUCT.md",
    "DESIGN.md",
)

CHANGELOG_PATTERNS: tuple[str, ...] = ("CHANGELOG*", "docs/CHANGELOG*")


@dataclass
class DocEntry:
    path: str
    exists: bool
    size: int
    touched_by_diff: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "exists": self.exists,
            "size": self.size,
            "touched_by_diff": self.touched_by_diff,
        }


def _iter_candidates(worktree_path: Path, patterns: tuple[str, ...] | list[str]):
    """Every tracked-looking file matching any documentation pattern.

    The result feeds ``doc_surface_sha256``, so it must stay exactly what
    filtering ``sorted(worktree_path.rglob("*"))`` would give: the same files,
    in ``Path`` order. Only the walk is narrower.
    """
    # Bare-name patterns match at the root only, so only path-shaped patterns
    # can justify entering a directory.
    prefixes = tuple(
        prefix for pattern in patterns if "/" in pattern for prefix in match_prefixes(pattern)
    )
    found = [
        path
        for path, rel in _walk(worktree_path, prefixes)
        if any(_matches(rel, pattern) for pattern in patterns) and path.is_file()
    ]
    for path in sorted(found):
        yield path.relative_to(worktree_path).as_posix()


def _walk(root: Path, prefixes: tuple[str, ...]) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, rel)`` for the entries ``root.rglob("*")`` would yield.

    It skips the root ``.git`` and any directory no prefix can reach. Otherwise
    it follows rglob: hidden entries are listed, symlinked directories are
    listed but not entered, and directories that cannot be read are skipped.
    """
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, rel_dir = stack.pop()
        try:
            with os.scandir(directory) as scandir_it:
                entries = list(scandir_it)
        except OSError:
            continue
        for entry in entries:
            path = directory / entry.name
            rel = rel_dir + entry.name
            yield path, rel
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            subtree = rel + "/"
            if is_dir and rel != ".git" and _reachable(subtree, prefixes):
                stack.append((path, subtree))


def _reachable(subtree: str, prefixes: tuple[str, ...]) -> bool:
    """Could a path under ``subtree`` (which ends in ``/``) start with a prefix?"""
    return any(subtree.startswith(prefix) or prefix.startswith(subtree) for prefix in prefixes)


def _matches(rel: str, pattern: str) -> bool:
    # A bare-name pattern like README* should match at the repo root only,
    # while docs/** and configured globs are path-shaped.
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel, pattern) and "/" not in rel
    return path_matches(rel, pattern)


def build_inventory(
    worktree_path: Path | str,
    changed_files: list[str],
    extra_paths: list[str] | None = None,
) -> list[DocEntry]:
    """Assemble the documentation surface. Code does this so the agent need not.

    An agent left to hunt for docs will find different files on different runs,
    which makes the stage's behaviour unrepeatable. A code-built inventory makes
    it deterministic.
    """
    worktree_path = Path(worktree_path)
    patterns = list(STANDARD_DOC_PATTERNS) + list(extra_paths or [])
    changed = set(changed_files)

    entries: list[DocEntry] = []
    for rel in _iter_candidates(worktree_path, patterns):
        full = worktree_path / rel
        entries.append(
            DocEntry(
                path=rel,
                exists=True,
                size=full.stat().st_size,
                touched_by_diff=rel in changed,
            )
        )
    return entries


def allowlist(inventory: list[DocEntry], extra_paths: list[str] | None = None) -> set[str]:
    """Paths a docs finding may legitimately target."""
    return {entry.path for entry in inventory}


def changelog_finding(
    inventory: list[DocEntry],
    changed_files: list[str],
    *,
    finding_id: str,
) -> Finding | None:
    """The code-owned changelog check.

    Returns a blocking finding when a changelog exists in the repo but the diff
    left it alone. Owned by code rather than delegated to the agent because it
    is a mechanical rule that an agent can forget to apply.
    """
    changelogs = [
        entry.path
        for entry in inventory
        if any(_matches(entry.path, pattern) for pattern in CHANGELOG_PATTERNS)
    ]
    if not changelogs:
        return None
    if any(path in set(changed_files) for path in changelogs):
        return None

    target = changelogs[0]
    return Finding(
        id=finding_id,
        stage=Stage.DOCS,
        code_owned=True,
        path=target,
        severity=Severity.HIGH,
        action=FindingAction.AUTO_FIX,
        title=f"changelog not updated ({target})",
        detail=(
            "[docs] require_changelog is enabled and this change does not touch "
            f"{target}. Add an entry describing the change, or set "
            "require_changelog = false if this change does not warrant one."
        ),
    )
