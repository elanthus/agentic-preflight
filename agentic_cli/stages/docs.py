"""The documentation stage: the review sub-machine, pointed at docs.

Agent-driven and with no shell command of its own — it reuses the findings
machinery entirely, which is exactly why it was cheap to add.

Two things distinguish it from review:

- **Findings may target files outside the diff.** That is the whole point: the
  diff changed code, and the doc that should have changed did not. So the
  changed-file constraint relaxes to a *documentation allowlist*. It does not
  become unconstrained — a "docs" finding against ``src/auth.py`` is still
  rejected, because that is a review finding wearing a docs hat.
- **``require_changelog`` is owned by code.** Whether a changelog was touched is
  a mechanical fact, and mechanical facts should not depend on the agent
  remembering a rule.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from ..diff import path_matches
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
    """Every tracked-looking file matching any documentation pattern."""
    seen: set[str] = set()
    for path in sorted(worktree_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(worktree_path).as_posix()
        if rel.startswith(".git/"):
            continue
        if any(_matches(rel, pattern) for pattern in patterns):
            if rel not in seen:
                seen.add(rel)
                yield rel


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
    is a mechanical rule, and mechanical rules are exactly what an agent forgets
    on the twentieth run.
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
