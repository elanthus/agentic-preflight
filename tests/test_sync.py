import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation, gitx
from agentic_preflight import sync
from agentic_preflight.envelope import ExitCode
from agentic_preflight.sync import SyncConflict, synchronize
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _pause_interactive_rebase(repo: Path, tmp_path: Path) -> Path:
    """Stop a real interactive rebase at edit without dirtying the checkout."""
    write(repo, "first.txt", "first feature commit\n")
    commit_all(repo, "first feature commit")
    write(repo, "second.txt", "second feature commit\n")
    commit_all(repo, "second feature commit")

    editor = tmp_path / "mark_first_commit_for_edit.py"
    editor.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[1])\n"
        "todo = path.read_text(encoding='utf-8')\n"
        "path.write_text(todo.replace('pick ', 'edit ', 1), encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": f"{shlex.quote(sys.executable)} {shlex.quote(str(editor))}",
    }
    result = subprocess.run(
        ["git", "rebase", "-i", "main"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rebase_dir = Path(git("rev-parse", "--git-path", "rebase-merge", cwd=repo))
    if not rebase_dir.is_absolute():
        rebase_dir = repo / rebase_dir
    assert rebase_dir.is_dir()
    assert git("status", "--porcelain", cwd=repo) == ""

    # Move the base after the pause so the old implementation gets past its
    # ancestry shortcut, attempts another rebase, and exposes the destructive
    # unconditional --abort behavior this test guards.
    base_worktree = tmp_path / "advanced-main"
    git("worktree", "add", str(base_worktree), "main", cwd=repo)
    write(base_worktree, "upstream.txt", "main advanced while the user was paused\n")
    commit_all(base_worktree, "advance main during paused rebase")
    return rebase_dir


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
    assert (worktree_path / "base.txt").read_text(encoding="utf-8") == "new upstream content\n"


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


def test_rebase_refuses_to_abort_the_users_paused_interactive_rebase(
    feature_repo: Path, tmp_path: Path
):
    rebase_dir = _pause_interactive_rebase(feature_repo, tmp_path)
    before_head = git("rev-parse", "HEAD", cwd=feature_repo)
    before_status = git("status", "--porcelain", cwd=feature_repo)

    caught: Exception | None = None
    try:
        sync.rebase_onto(feature_repo, "main")
    except Exception as exc:  # the assertion below identifies the public refusal
        caught = exc

    assert rebase_dir.is_dir(), "rebase_onto aborted the user's paused interactive rebase"
    assert type(caught).__name__ == "OperationInProgress"
    assert getattr(caught, "operation", None) == "rebase"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before_head
    assert git("status", "--porcelain", cwd=feature_repo) == before_status

    refused = ScriptedAgent(feature_repo).run("start", expect=ExitCode.PRECONDITION)

    assert refused["error"]["code"] == "operation_in_progress"
    assert refused["data"] == {"operation": "rebase", "path": str(feature_repo)}
    assert refused["next"]["command"] == "git status"
    assert rebase_dir.is_dir()
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before_head
    assert git("status", "--porcelain", cwd=feature_repo) == before_status
