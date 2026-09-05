"""Mechanical classification of changes that cannot affect software behaviour."""

from __future__ import annotations

from pathlib import PurePosixPath

from ..diff import path_matches
from .docs import STANDARD_DOC_PATTERNS

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


def _matches(path: str, pattern: str) -> bool:
    if "/" not in pattern:
        return "/" not in path and path_matches(path, pattern)
    return path_matches(path, pattern)


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
