"""The walk behind the documentation inventory.

``build_inventory`` feeds ``doc_surface_sha256``, so a cheaper walk must give
exactly the inventory the original full ``rglob`` walk gave: the same entries,
in the same order. ``_rglob_inventory`` is that original walk, kept as the
oracle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_preflight.stages import docs
from tests.conftest import SYMLINKS_AVAILABLE, requires_posix_permissions, write

CHANGED = ["README.md", "docs/a/b.md", "node_modules/pkg/README.md", "src/app.py"]


def _rglob_inventory(root: Path, changed: list[str], extra: list[str] | None) -> list[dict]:
    """The inventory as built before the walk was pruned."""
    patterns = list(docs.STANDARD_DOC_PATTERNS) + list(extra or [])
    entries: list[dict] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        if any(docs._matches(rel, pattern) for pattern in patterns) and rel not in seen:
            seen.add(rel)
            entries.append(
                {
                    "path": rel,
                    "size": (root / rel).stat().st_size,
                    "touched_by_diff": rel in changed,
                }
            )
    return entries


def _inventory(root: Path, changed: list[str], extra: list[str] | None) -> list[dict]:
    return [entry.as_dict() for entry in docs.build_inventory(root, changed, extra)]


def _tree(root: Path, *, git_as_file: bool = False) -> Path:
    """A worktree whose doc surface sits beside large-looking ignored trees."""
    files = {
        "README.md": "# readme\n",
        "CHANGELOG.md": "# changes\n",
        "AGENTS.md": "agents\n",
        "PRODUCT.md": "product\n",
        ".hidden.md": "hidden\n",
        "notes.txt": "notes\n",
        # Path order and string order disagree here: Path sorts docs/a/b.md
        # before docs/a-b.md because it compares parts, not characters.
        "docs/a.md": "a\n",
        "docs/a-b.md": "a-b\n",
        "docs/a/b.md": "b\n",
        "docs/a/c/d.md": "d\n",
        "docs/Z.md": "z\n",
        "docs/été.md": "unicode\n",
        "Guide/upper.md": "case\n",
        "site/index.md": "site\n",
        "nested/site/deep.md": "deep\n",
        "src/README.md": "src readme\n",
        "src/app.py": "pass\n",
        ".claude/rules/style.md": "rule\n",
        ".claude/settings.json": "{}\n",
        ".claude/worktrees/other/README.md": "other readme\n",
        ".claude/worktrees/other/docs/stale.md": "stale\n",
        ".github/instructions/review.md": "review\n",
        ".github/workflows/ci.yml": "on: push\n",
        "node_modules/pkg/README.md": "pkg readme\n",
        "node_modules/pkg/docs/api.md": "api\n",
        "node_modules/pkg/index.js": "",
        ".venv/lib/site-packages/mod/README.md": "venv readme\n",
        "vendor/lib/README.md": "vendored\n",
        "vendor/lib/.git/HEAD": "ref: refs/heads/main\n",
    }
    for rel, content in files.items():
        write(root, rel, content)
    if git_as_file:
        write(root, ".git", "gitdir: /elsewhere/.git/worktrees/x\n")
    else:
        write(root, ".git/HEAD", "ref: refs/heads/main\n")
        write(root, ".git/info/exclude", "node_modules/\n")
    if SYMLINKS_AVAILABLE:
        (root / "README-link.md").symlink_to("README.md")
        (root / "docs/linked.md").symlink_to("../README.md")
        (root / "docs/broken.md").symlink_to("missing.md")
        (root / "docs/dirlink").symlink_to(root / "node_modules/pkg", target_is_directory=True)
    return root


@pytest.mark.parametrize("git_as_file", [False, True], ids=["git-dir", "git-file"])
@pytest.mark.parametrize(
    "extra",
    [
        None,
        ["**/*.md"],
        ["**/*"],
        ["*"],
        ["site/"],
        ["/Guide/", "/guide/", "/src/"],
        ["vendor/**"],
        ["*/README.md"],
        ["[Dd]ocs/**", "node_modules/*/docs/**"],
        ["../outside/**", "./docs/**", "docs", ""],
    ],
)
def test_inventory_matches_the_full_rglob_walk(tmp_path, extra, git_as_file):
    """Pruning the walk leaves every inventory field and its order unchanged."""
    root = _tree(tmp_path, git_as_file=git_as_file)

    expected = _rglob_inventory(root, CHANGED, extra)

    assert expected, "the oracle found nothing, so the comparison would be vacuous"
    assert _inventory(root, CHANGED, extra) == expected


@requires_posix_permissions
def test_inventory_skips_an_unreadable_directory_as_rglob_does(tmp_path):
    root = _tree(tmp_path)
    write(root, "docs/private/secret.md", "secret\n")
    private = root / "docs" / "private"
    private.chmod(0)
    try:
        assert _inventory(root, CHANGED, None) == _rglob_inventory(root, CHANGED, None)
    finally:
        private.chmod(0o755)


def _scanned_directories(root: Path, monkeypatch, extra: list[str] | None) -> set[str]:
    scanned: set[str] = set()
    real_scandir = os.scandir

    def recording_scandir(path):
        scanned.add(Path(path).relative_to(root).as_posix())
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(docs.os, "scandir", recording_scandir)
        docs.build_inventory(root, [], extra)
    return scanned


def test_the_walk_skips_trees_no_documentation_pattern_can_reach(tmp_path, monkeypatch):
    root = _tree(tmp_path)

    scanned = _scanned_directories(root, monkeypatch, None)

    assert {".", "docs", "docs/a/c", ".claude/rules", ".github/instructions"} <= scanned
    for skipped in (".git", "node_modules", ".venv", "src", "vendor", ".claude/worktrees"):
        assert skipped not in scanned


def test_a_pattern_that_can_match_anywhere_still_walks_ignored_trees(tmp_path, monkeypatch):
    root = _tree(tmp_path)

    scanned = _scanned_directories(root, monkeypatch, ["**/*.md"])

    assert {"node_modules/pkg/docs", ".venv/lib/site-packages/mod", "vendor/lib/.git"} <= scanned
    assert ".git" not in scanned
