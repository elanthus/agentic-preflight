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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import gitx
from ..diff import literal_scope, match_scope, path_matches
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
    size: int
    touched_by_diff: bool
    blob_id: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "touched_by_diff": self.touched_by_diff,
        }


def _iter_candidates(worktree_path: Path, patterns: tuple[str, ...] | list[str]):
    """Matching HEAD blobs in the historical Path order, independent of checkout."""
    candidates = _listed_candidates(worktree_path, patterns)
    for rel in sorted(candidates, key=lambda rel: worktree_path / rel):
        if any(_matches(rel, pattern) for pattern in patterns):
            yield rel, candidates[rel]


def _listed_candidates(
    worktree_path: Path, patterns: tuple[str, ...] | list[str]
) -> dict[str, gitx.TreeBlob]:
    """Committed files at ``HEAD`` that could match a pattern, without listing the whole tree.

    The listing only narrows: the caller still matches every path, so the result
    is the same as filtering a full listing. A bare-name pattern with a wildcard
    can match any top-level file, which a top-level listing covers. Any other
    pattern either names one literal path or matches beneath a literal scope.
    A pattern that can match at any depth falls back to the full listing.
    """
    scopes: set[str] = set()
    top_level = False
    for pattern in patterns:
        if "/" in pattern:
            scope = match_scope(pattern)
            if scope is None:
                return gitx.tree_blobs(worktree_path)
            scopes.add(scope)
        elif literal_scope(pattern) == pattern:
            scopes.add(pattern)
        elif pattern not in {"", ".", ".."}:
            # A wildcard or a backslash; no committed path equals the names skipped.
            top_level = True
    found = gitx.tree_blobs(worktree_path, scopes=sorted(scopes))
    if top_level:
        found.update(gitx.tree_blobs(worktree_path, recursive=False))
    return found


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
    for rel, blob in _iter_candidates(worktree_path, patterns):
        entries.append(
            DocEntry(
                path=rel,
                size=blob.size,
                blob_id=blob.object_id,
                touched_by_diff=rel in changed,
            )
        )
    return entries


def allowlist(inventory: list[DocEntry]) -> set[str]:
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


# Configuration consumed by hosted CI systems. Keep this deliberately narrower
# than every file under ``.github``: actions, scripts, and application manifests
# can contain executable software and still deserve tests.
STANDARD_CI_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".circleci/*.yml",
    ".circleci/*.yaml",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/ci/*.yml",
    ".gitlab/ci/*.yaml",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    "azure-pipelines/*.yml",
    "azure-pipelines/*.yaml",
    "bitbucket-pipelines.yml",
    "bitbucket-pipelines.yaml",
    ".buildkite/*.yml",
    ".buildkite/*.yaml",
    ".travis.yml",
    ".travis.yaml",
    "appveyor.yml",
    "appveyor.yaml",
)

DOCUMENTATION_FILE_PATTERNS: tuple[str, ...] = (
    "**/*.md",
    "**/*.rst",
    "**/*.adoc",
)


def tests_are_not_applicable(
    changed_files: list[str], *, extra_doc_paths: list[str] | None = None
) -> bool:
    """Return true when every changed path is documentation or CI configuration."""
    if not changed_files:
        return False
    doc_surface = (*STANDARD_DOC_PATTERNS, *(extra_doc_paths or []))

    def qualifies(path: str) -> bool:
        if any(_matches(path, pattern) for pattern in STANDARD_CI_PATTERNS):
            return True
        if any(_matches(path, pattern) for pattern in DOCUMENTATION_FILE_PATTERNS):
            return True
        # Documentation-review eligibility is broader than test-skip eligibility.
        # In particular, docs/examples and configured globs can contain software.
        if PurePosixPath(path).suffix == ".txt":
            return any(_matches(path, pattern) for pattern in doc_surface)
        return path in {"README", "CONTRIBUTING", "CHANGELOG"}

    return all(qualifies(path) for path in changed_files)
