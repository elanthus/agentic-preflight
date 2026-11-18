"""The file listing behind the documentation inventory.

``build_inventory`` feeds the allowlist, the agent's context, and
``doc_surface_sha256``. It lists the files committed at ``HEAD``, so ignored,
generated, and untracked files stay out. Ordinary committed files whose checkout
bytes match Git keep the historical entries and Path order. ``_rglob_inventory``
is the original walk, kept as an oracle for those ordinary-file fixtures.
Sparse checkouts, symlinks, and converted content are tested against blobs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_preflight.fingerprints import config_digest, doc_surface
from agentic_preflight.stages import docs
from tests.conftest import commit_all, git, requires_git_symlinks, write

CHANGED = ["README.md", "docs/a/b.md", "node_modules/pkg/README.md", "src/app.py"]


def _rglob_inventory(root: Path, changed: list[str], extra: list[str] | None) -> list[dict]:
    """The inventory the full walk built, restricted to committed files."""
    committed = set(git("ls-files", "-z", cwd=root).split("\0"))
    patterns = list(docs.STANDARD_DOC_PATTERNS) + list(extra or [])
    entries: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in committed:
            continue
        if any(docs._matches(rel, pattern) for pattern in patterns):
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


def _paths(root: Path, extra: list[str] | None = None) -> list[str]:
    return [entry["path"] for entry in _inventory(root, [], extra)]


def _repo(root: Path) -> Path:
    """A committed doc surface beside ignored, generated, and untracked trees."""
    git("init", "-q", "-b", "main", cwd=root)
    committed = {
        ".gitignore": "docs/_build/\n.claude/worktrees/\n.venv/\n",
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
        ".github/instructions/review.md": "review\n",
        ".github/workflows/ci.yml": "on: push\n",
        "node_modules/pkg/README.md": "pkg readme\n",
    }
    for rel, content in committed.items():
        write(root, rel, content)
    commit_all(root, "docs")
    # Everything below stays out of HEAD.
    write(root, "docs/_build/index.md", "generated\n")
    write(root, ".claude/worktrees/other/README.md", "other readme\n")
    write(root, ".venv/lib/site-packages/mod/README.md", "venv readme\n")
    write(root, "docs/draft.md", "untracked\n")
    write(root, "CONTRIBUTING.md", "untracked root doc\n")
    return root


@pytest.mark.parametrize(
    "extra",
    [
        None,
        ["**/*.md"],
        ["**/*"],
        ["*"],
        ["site/"],
        ["/Guide/", "/guide/", "/src/"],
        ["*/README.md"],
        ["[Dd]ocs/**", "node_modules/*/docs/**"],
        ["../outside/**", "./docs/**", "docs", ""],
        ["docs/a/**", "Guide/upper.md", "notes.txt", "site/*.md", "missing/**"],
        ["no*tes.txt", ".", "..", "src/README.md"],
    ],
)
def test_inventory_matches_the_full_walk_over_committed_files(tmp_path, extra):
    """Committed files keep every inventory field and their order."""
    root = _repo(tmp_path)

    expected = _rglob_inventory(root, CHANGED, extra)

    assert expected, "the oracle found nothing, so the comparison would be vacuous"
    assert _inventory(root, CHANGED, extra) == expected


def test_ignored_generated_and_untracked_files_stay_out(tmp_path):
    root = _repo(tmp_path)

    paths = _paths(root, ["**/*.md"])

    assert "docs/a.md" in paths
    for absent in (
        "docs/_build/index.md",
        ".claude/worktrees/other/README.md",
        ".venv/lib/site-packages/mod/README.md",
        "docs/draft.md",
        "CONTRIBUTING.md",
    ):
        assert absent not in paths


def test_an_ignored_build_leaves_the_surface_digest_unchanged(tmp_path):
    root = _repo(tmp_path)
    before = config_digest(doc_surface(root, docs.build_inventory(root, CHANGED)))

    write(root, "docs/_build/html/index.md", "rebuilt\n")
    write(root, "docs/_build/index.md", "regenerated with different bytes\n")

    assert config_digest(doc_surface(root, docs.build_inventory(root, CHANGED))) == before


def test_staged_changes_do_not_change_the_committed_inventory(tmp_path):
    """The inventory reads ``HEAD``, not the index."""
    root = _repo(tmp_path)
    write(root, "docs/staged.md", "staged, not committed\n")
    git("add", "docs/staged.md", cwd=root)
    git("rm", "--cached", "-q", "docs/a.md", cwd=root)

    paths = _paths(root, ["**/*.md"])

    assert "docs/staged.md" not in paths
    assert "docs/a.md" in paths


def test_submodule_contents_stay_out(tmp_path):
    (tmp_path / "outer").mkdir()
    root = _repo(tmp_path / "outer")
    inner = root / "vendor" / "lib"
    inner.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=inner)
    write(inner, "README.md", "vendored\n")
    commit_all(inner, "vendored")
    # Adding a nested repository records a gitlink, the tree entry a submodule uses.
    git("add", "vendor/lib", cwd=root)
    git("commit", "-q", "-m", "vendor", cwd=root)

    assert "vendor/lib/README.md" not in _paths(root, ["vendor/**"])


def test_a_committed_file_missing_from_disk_stays_in_the_surface(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "a.md").unlink()

    paths = _paths(root)

    assert "docs/a.md" in paths
    assert "docs/Z.md" in paths


@requires_git_symlinks
def test_committed_symlinks_count_without_following_their_targets(tmp_path):
    root = _repo(tmp_path)
    (root / "docs" / "linked.md").symlink_to("../README.md")
    (root / "docs" / "broken.md").symlink_to("missing.md")
    (root / "docs" / "dirlink").symlink_to("a", target_is_directory=True)
    commit_all(root, "links")

    paths = _paths(root, ["**/*"])

    assert "docs/linked.md" in paths
    assert "docs/broken.md" in paths
    assert "docs/dirlink" in paths
    assert "docs/dirlink/b.md" not in paths


@pytest.mark.parametrize(
    "variable",
    ["GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"],
)
def test_git_pathspec_environment_does_not_change_the_inventory(tmp_path, monkeypatch, variable):
    """The scoped listing ignores Git's global pathspec switches."""
    root = _repo(tmp_path)
    extra = ["site/*.md", "notes.txt"]
    expected = _rglob_inventory(root, CHANGED, extra)

    monkeypatch.setenv(variable, "1")

    assert _inventory(root, CHANGED, extra) == expected


@requires_git_symlinks
def test_symlink_surface_hashes_link_text_and_never_reads_its_target(tmp_path, tmp_path_factory):
    import hashlib

    root = _repo(tmp_path)
    target = tmp_path_factory.mktemp("outside-doc") / "target"
    target.write_text("private outside content")
    (root / "docs/external.md").symlink_to(target)
    # Git for Windows stores a link's text with forward slashes.
    link_text = target.as_posix()
    commit_all(root, "external link")
    before = doc_surface(root, docs.build_inventory(root, []))
    entry = next(item for item in before if item["path"] == "docs/external.md")
    assert entry["size"] == len(link_text.encode())
    assert entry["content_sha256"] == hashlib.sha256(link_text.encode()).hexdigest()
    target.unlink()
    assert doc_surface(root, docs.build_inventory(root, [])) == before


def test_surface_uses_committed_bytes_despite_checkout_conversion(tmp_path):
    import hashlib

    root = _repo(tmp_path)
    write(root, ".gitattributes", "docs/eol.md text eol=crlf\n")
    write(root, "docs/eol.md", "one\ntwo\n")
    pointer = (
        b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 100\n"
    )
    (root / "docs/pointer.md").write_bytes(pointer)
    commit_all(root, "stored docs")
    (root / "docs/eol.md").unlink()
    git("checkout", "--", "docs/eol.md", cwd=root)
    assert (root / "docs/eol.md").read_bytes() == b"one\r\ntwo\r\n"
    # A smudge filter can leave arbitrary large content behind a committed pointer.
    (root / "docs/pointer.md").write_bytes(b"smudged checkout content" * 100)
    entries = {item["path"]: item for item in doc_surface(root, docs.build_inventory(root, []))}
    for path, content in (("docs/eol.md", b"one\ntwo\n"), ("docs/pointer.md", pointer)):
        assert entries[path]["size"] == len(content)
        assert entries[path]["content_sha256"] == hashlib.sha256(content).hexdigest()
