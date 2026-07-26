"""Mechanical classification of changes that cannot affect software behaviour."""

from __future__ import annotations

from ..diff import path_matches
from .docs import STANDARD_DOC_PATTERNS

# Configuration consumed by hosted CI systems. Keep this deliberately narrower
# than every file under ``.github``: actions, scripts, and application manifests
# can contain executable software and still deserve tests.
STANDARD_CI_PATTERNS: tuple[str, ...] = (
    ".github/workflows/**",
    ".circleci/**",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".gitlab/ci/**",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    "azure-pipelines/**",
    "bitbucket-pipelines.yml",
    "bitbucket-pipelines.yaml",
    ".buildkite/**",
    ".travis.yml",
    ".travis.yaml",
    "appveyor.yml",
    "appveyor.yaml",
    "Jenkinsfile",
    "Jenkinsfile.*",
)

DOCUMENTATION_FILE_PATTERNS: tuple[str, ...] = (
    "**/*.md",
    "**/*.mdx",
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
    patterns = (
        *STANDARD_DOC_PATTERNS,
        *DOCUMENTATION_FILE_PATTERNS,
        *(extra_doc_paths or []),
        *STANDARD_CI_PATTERNS,
    )
    return all(any(_matches(path, pattern) for pattern in patterns) for path in changed_files)
