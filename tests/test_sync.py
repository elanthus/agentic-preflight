from pathlib import Path

import pytest

from agentic_preflight import gitx
from agentic_preflight.sync import SyncConflict, synchronize
from tests.conftest import commit_all, git, write


def test_sync_fetches_and_rebases_the_worktree_onto_fresh_origin(
    feature_repo: Path, bare_remote: Path, tmp_path: Path
):
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "base.txt", "new upstream content\n")
    upstream = commit_all(feature_repo, "advance main")
    git("push", "origin", "main", cwd=feature_repo)
    git("switch", "feature/x", cwd=feature_repo)

    worktree_path = tmp_path / "sync-worktree"
    git("worktree", "add", "--detach", str(worktree_path), "HEAD", cwd=feature_repo)
    git("switch", "-c", "sync-test", cwd=worktree_path)

    result = synchronize(feature_repo, worktree_path, base_ref="main")

    assert result.remote == "origin"
    assert result.base_sha == upstream
    assert gitx.is_ancestor(worktree_path, upstream, "HEAD")
    assert (worktree_path / "base.txt").read_text() == "new upstream content\n"


def test_sync_uses_the_local_base_when_origin_is_absent(feature_repo: Path):
    result = synchronize(feature_repo, feature_repo, base_ref="main")

    assert result.remote is None
    assert result.base_ref == "main"
    assert result.base_sha == git("rev-parse", "main", cwd=feature_repo)


def test_sync_aborts_and_reports_conflicts(feature_repo: Path, bare_remote: Path, tmp_path: Path):
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "src/app.py", "upstream\n")
    commit_all(feature_repo, "change app upstream")
    git("push", "origin", "main", cwd=feature_repo)
    git("switch", "feature/x", cwd=feature_repo)
    write(feature_repo, "src/app.py", "feature\n")
    commit_all(feature_repo, "change app on feature")

    worktree_path = tmp_path / "conflict-worktree"
    git("worktree", "add", "--detach", str(worktree_path), "HEAD", cwd=feature_repo)
    git("switch", "-c", "sync-conflict", cwd=worktree_path)
    before = git("rev-parse", "HEAD", cwd=worktree_path)

    with pytest.raises(SyncConflict) as exc:
        synchronize(feature_repo, worktree_path, base_ref="main")

    assert exc.value.conflicting_files == ["src/app.py"]
    assert git("rev-parse", "HEAD", cwd=worktree_path) == before
    assert not (worktree_path / ".git" / "rebase-merge").exists()
