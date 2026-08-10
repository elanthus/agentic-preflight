from pathlib import Path

import pytest

from agentic_preflight import attestation, gitx
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


def test_sync_preserves_a_locally_ahead_attestation_history(feature_repo: Path, bare_remote: Path):
    base_sha = git("rev-parse", "main", cwd=feature_repo)
    git(
        "notes", f"--ref={attestation.NOTES_REF}", "add", "-m", "remote", base_sha, cwd=feature_repo
    )
    git("push", "origin", attestation.NOTES_REF, cwd=feature_repo)

    head_sha = git("rev-parse", "HEAD", cwd=feature_repo)
    git("notes", f"--ref={attestation.NOTES_REF}", "add", "-m", "local", head_sha, cwd=feature_repo)

    result = synchronize(feature_repo, feature_repo, base_ref="main")

    assert result.remote == "origin"
    assert git("notes", f"--ref={attestation.NOTES_REF}", "show", base_sha, cwd=feature_repo) == (
        "remote"
    )
    assert git("notes", f"--ref={attestation.NOTES_REF}", "show", head_sha, cwd=feature_repo) == (
        "local"
    )


def test_sync_merges_disjoint_local_and_remote_attestations(
    feature_repo: Path, bare_remote: Path, tmp_path: Path
):
    base_sha = git("rev-parse", "main", cwd=feature_repo)
    git("notes", f"--ref={attestation.NOTES_REF}", "add", "-m", "base", base_sha, cwd=feature_repo)
    git("push", "origin", attestation.NOTES_REF, cwd=feature_repo)

    local_head = git("rev-parse", "HEAD", cwd=feature_repo)
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-m",
        "local",
        local_head,
        cwd=feature_repo,
    )

    peer = tmp_path / "peer"
    git("clone", str(bare_remote), str(peer), cwd=tmp_path)
    git(
        "fetch",
        "origin",
        f"{attestation.NOTES_REF}:{attestation.NOTES_REF}",
        cwd=peer,
    )
    write(peer, "upstream.txt", "concurrent base change\n")
    upstream = commit_all(peer, "advance base concurrently")
    git("notes", f"--ref={attestation.NOTES_REF}", "add", "-m", "remote", upstream, cwd=peer)
    git("push", "origin", "main", attestation.NOTES_REF, cwd=peer)

    synchronize(feature_repo, feature_repo, base_ref="main")

    assert git("notes", f"--ref={attestation.NOTES_REF}", "show", local_head, cwd=feature_repo) == (
        "local"
    )
    assert git("notes", f"--ref={attestation.NOTES_REF}", "show", upstream, cwd=feature_repo) == (
        "remote"
    )


def test_sync_refuses_conflicting_attestations_for_the_same_commit(
    feature_repo: Path, bare_remote: Path, tmp_path: Path
):
    base_sha = git("rev-parse", "main", cwd=feature_repo)
    git("notes", f"--ref={attestation.NOTES_REF}", "add", "-m", "base", base_sha, cwd=feature_repo)
    git("push", "origin", attestation.NOTES_REF, cwd=feature_repo)

    peer = tmp_path / "peer-conflict"
    git("clone", str(bare_remote), str(peer), cwd=tmp_path)
    git(
        "fetch",
        "origin",
        f"{attestation.NOTES_REF}:{attestation.NOTES_REF}",
        cwd=peer,
    )
    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-f",
        "-m",
        "remote replacement",
        base_sha,
        cwd=peer,
    )
    git("push", "origin", attestation.NOTES_REF, cwd=peer)

    git(
        "notes",
        f"--ref={attestation.NOTES_REF}",
        "add",
        "-f",
        "-m",
        "local replacement",
        base_sha,
        cwd=feature_repo,
    )
    git("config", "notes.mergeStrategy", "union", cwd=feature_repo)

    with pytest.raises(gitx.GitError, match="notes"):
        synchronize(feature_repo, feature_repo, base_ref="main")

    assert git("notes", f"--ref={attestation.NOTES_REF}", "show", base_sha, cwd=feature_repo) == (
        "local replacement"
    )


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
