from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_preflight import gitx, grounding_sources
from tests.conftest import commit_all, git


def write(repo: Path, relpath: str, content: str) -> None:
    # These tests assert committed blob bytes, so disable platform newline
    # translation when creating the fixtures (including explicit CRLF inputs).
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def test_hundreds_of_documents_use_two_processes_and_preserve_exact_text(tmp_repo):
    for i in range(300):
        write(tmp_repo, f"docs/page-{i:03}.md", f"src/widget.py reference {i}\n")
    write(tmp_repo, "docs/odd name.md", "spaced path\n")
    commit_all(tmp_repo, "many grounding documents")
    with patch("subprocess.run", wraps=gitx.subprocess.run) as processes:
        texts, omitted, _ = grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))
    assert processes.call_count == 2
    assert len(texts) == 301
    assert texts["docs/page-299.md"] == "src/widget.py reference 299\n"
    assert texts["docs/odd name.md"] == "spaced path\n"
    assert omitted == {}


def test_source_limits_are_applied_before_content_read(tmp_repo, monkeypatch):
    monkeypatch.setattr(grounding_sources, "MAX_SOURCE_BYTES", 10)
    monkeypatch.setattr(grounding_sources, "MAX_READ_BYTES", 12)
    write(tmp_repo, "docs/a.md", "12345678")
    write(tmp_repo, "docs/b.md", "12345678")
    write(tmp_repo, "docs/c.md", "too large for the per-source limit")
    write(tmp_repo, "docs/d.png", "image")
    write(tmp_repo, "docs/e.md", "x\0y")
    commit_all(tmp_repo, "bounded sources")
    with patch.object(gitx, "read_blobs", wraps=gitx.read_blobs) as read:
        texts, omitted, paths = grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))
    assert len(read.call_args.args[1]) == 2
    assert texts == {"docs/a.md": "12345678"}
    assert omitted == {"binary": 2, "oversized": 1, "read_budget": 1}
    assert len(paths) == 5


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_snapshot_uses_committed_blobs(tmp_repo, newline):
    write(tmp_repo, "docs/a.md", f"committed{newline}")
    commit_all(tmp_repo, "committed source")
    write(tmp_repo, "docs/a.md", "staged\n")
    write(tmp_repo, "docs/new.md", "not committed\n")
    git("add", ".", cwd=tmp_repo)
    texts, omitted, _ = grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))
    assert texts == {"docs/a.md": f"committed{newline}"}
    assert omitted == {}


def test_symlink_blob_is_omitted_without_reading_its_target(tmp_repo):
    # Construct a Git symlink directly so this also runs on Windows without
    # filesystem symlink privileges.
    write(tmp_repo, "target.txt", "../private.txt")
    oid = git("hash-object", "-w", "target.txt", cwd=tmp_repo)
    git("update-index", "--add", "--cacheinfo", f"120000,{oid},docs/link.md", cwd=tmp_repo)
    git("commit", "-m", "add symlink blob", cwd=tmp_repo)
    texts, omitted, _ = grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))
    assert texts == {}
    assert omitted == {"non_regular": 1}


def test_source_count_limit_and_head_change_are_deterministic(tmp_repo, monkeypatch):
    monkeypatch.setattr(grounding_sources, "MAX_SOURCES", 1)
    write(tmp_repo, "docs/a.md", "original\n")
    write(tmp_repo, "docs/b.md", "omitted\n")
    commit_all(tmp_repo, "first source snapshot")
    first = grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))
    assert first[0] == {"docs/a.md": "original\n"}
    assert first[1] == {"read_budget": 1}
    assert grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/")) == first
    write(tmp_repo, "docs/a.md", "changed\n")
    commit_all(tmp_repo, "update source snapshot")
    assert grounding_sources.load(tmp_repo, lambda p: p.startswith("docs/"))[0] == {
        "docs/a.md": "changed\n",
    }
