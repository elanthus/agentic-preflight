import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight import fileperms, gitx, worktree
from tests.conftest import git, write


def assert_owner_only(path):
    """Assert the platform's expression of "readable by the owner and nobody else".

    On Windows the mode bits are meaningless, so the DACL is read back with
    ``icacls``: exactly one principal must appear, and it must be this user.
    """
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    listing = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    granted = [
        line.split(":", 1)[0].strip()
        for line in listing.splitlines()
        if ":" in line and not line.startswith("Successfully")
    ]
    # The first line carries the path before the principal; drop it.
    granted = [entry.rsplit(str(path), 1)[-1].strip() or entry for entry in granted]
    assert len(granted) == 1, f"expected a single ACE, got: {listing}"
    assert "(I)" not in listing, f"inherited permissions survived: {listing}"


@pytest.fixture
def wt(feature_repo, tmp_path):
    """A live worktree for the feature branch head."""
    head = gitx.rev_parse(feature_repo, "HEAD")
    return worktree.create(
        feature_repo,
        path=tmp_path / "wt" / "r_abc123",
        branch="ap/r_abc123",
        head_sha=head,
    )


def test_create_checks_out_the_head_sha_on_its_own_branch(feature_repo, wt):
    assert wt.exists()
    assert gitx.current_branch(wt) == "ap/r_abc123"
    assert gitx.rev_parse(wt, "HEAD") == gitx.rev_parse(feature_repo, "HEAD")


def test_create_leaves_the_users_tree_untouched(feature_repo, wt):
    assert gitx.current_branch(feature_repo) == "feature/x"
    assert gitx.is_clean(feature_repo) is True


def test_create_does_not_clobber_an_existing_branch_name(feature_repo, tmp_path):
    git("branch", "ap/taken", cwd=feature_repo)
    head = gitx.rev_parse(feature_repo, "HEAD")
    with pytest.raises(worktree.WorktreeError):
        worktree.create(
            feature_repo,
            path=tmp_path / "wt" / "taken",
            branch="ap/taken",
            head_sha=head,
        )


def test_configured_root_must_remain_outside_the_repository(feature_repo):
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.resolve_root(feature_repo, str(feature_repo / ".worktrees"))
    assert "outside" in str(exc.value)


# -- copied-file containment (secret-leak class) ----------------------------


def test_a_gitignored_env_file_is_copied_and_stays_invisible_to_git(feature_repo, wt):
    write(feature_repo, ".env", "SECRET=hunter2\n")

    copied = worktree.copy_files(feature_repo, wt, [".env"])

    assert copied == [".env"]
    assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=hunter2\n"
    # The whole point: git must not see it, or `git add -A` would sweep it up.
    assert git("status", "--porcelain", cwd=wt) == ""


def test_a_non_ignored_file_is_refused_and_not_copied(feature_repo, wt):
    """Guard 1: never copy a secret-bearing file that git would happily track."""
    write(feature_repo, "secrets.txt", "SECRET=hunter2\n")

    with pytest.raises(worktree.CopyRefused) as exc:
        worktree.copy_files(feature_repo, wt, ["secrets.txt"])

    assert "secrets.txt" in str(exc.value)
    assert "gitignore" in str(exc.value).lower()
    assert not (wt / "secrets.txt").exists()


def test_ignore_status_is_judged_in_the_worktree_not_the_users_tree(feature_repo, tmp_path):
    """An uncommitted ignore rule does not protect the worktree, where the
    dangerous `git add -A` actually runs."""
    head = gitx.rev_parse(feature_repo, "HEAD")
    wt = worktree.create(
        feature_repo,
        path=tmp_path / "wt" / "r_late",
        branch="ap/r_late",
        head_sha=head,
    )
    # Ignored in the user's tree, but never committed — so not ignored at head_sha.
    write(feature_repo, ".gitignore", ".env\nlate.txt\n")
    write(feature_repo, "late.txt", "SECRET=1\n")

    with pytest.raises(worktree.CopyRefused):
        worktree.copy_files(feature_repo, wt, ["late.txt"])


def test_copies_are_written_owner_only(feature_repo, wt):
    """The same guarantee on both platforms, asserted the way each expresses it."""
    write(feature_repo, ".env", "SECRET=hunter2\n")

    worktree.copy_files(feature_repo, wt, [".env"])

    assert_owner_only(wt / ".env")


def test_a_copy_that_cannot_be_restricted_is_refused_and_removed(feature_repo, wt, monkeypatch):
    """A secret copied but left readable is the exact outcome this must never reach."""
    write(feature_repo, ".env", "SECRET=hunter2\n")

    def refuse(path):
        raise fileperms.PermissionRestrictionError(path, "simulated ACL failure")

    monkeypatch.setattr(worktree.fileperms, "restrict_to_owner", refuse)

    with pytest.raises(worktree.CopyRefused) as caught:
        worktree.copy_files(feature_repo, wt, [".env"])

    assert "simulated ACL failure" in str(caught.value)
    assert not (wt / ".env").exists()


def test_a_missing_copy_file_is_skipped_silently(feature_repo, wt):
    assert worktree.copy_files(feature_repo, wt, [".env"]) == []


def test_a_copy_directory_gets_a_helpful_setup_command_error(feature_repo, wt):
    write(feature_repo, "cache/artifact", "cached\n")
    with pytest.raises(worktree.CopyRefused) as exc:
        worktree.copy_files(feature_repo, wt, ["cache"])
    assert "setup_command" in str(exc.value)


def test_commit_content_invariant_rejects_a_commit_touching_a_copied_path(feature_repo, wt):
    """Guard 2, independent of guard 1: a .gitignore edited mid-run must not
    open the hole. Verified against commit *content*, not against ignore rules."""
    write(wt, ".env", "SECRET=hunter2\n")
    git("add", "-f", ".env", cwd=wt)
    git("commit", "-m", "oops, committed the env file", cwd=wt)
    bad_sha = gitx.rev_parse(wt, "HEAD")

    with pytest.raises(worktree.CopiedFileInCommit) as exc:
        worktree.assert_commit_is_clean_of(wt, bad_sha, [".env"])
    assert ".env" in str(exc.value)


def test_commit_content_invariant_passes_an_innocent_commit(feature_repo, wt):
    write(wt, "src/app.py", "def greet(name, loud=False):\n    return 'hi'\n")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "real fix", cwd=wt)
    sha = gitx.rev_parse(wt, "HEAD")

    worktree.assert_commit_is_clean_of(wt, sha, [".env"])  # must not raise


def test_cumulative_diff_invariant_rejects_a_copied_path(feature_repo):
    base = gitx.rev_parse(feature_repo, "main")
    write(feature_repo, ".env", "SECRET=hunter2\n")
    git("add", "-f", ".env", cwd=feature_repo)
    git("commit", "-m", "force-add copied file", cwd=feature_repo)
    head = gitx.rev_parse(feature_repo, "HEAD")

    with pytest.raises(worktree.CopiedFileInCommit, match="resolved diff"):
        worktree.assert_diff_is_clean_of(feature_repo, base, head, [".env"])


def test_resolved_source_must_still_ignore_copied_paths(feature_repo):
    write(feature_repo, ".gitignore", "")

    with pytest.raises(worktree.CopyRefused, match="no longer ignores"):
        worktree.assert_paths_are_ignored(feature_repo, [".env"])


def test_remove_deletes_the_worktree_and_its_copies(feature_repo, wt):
    write(feature_repo, ".env", "SECRET=hunter2\n")
    worktree.copy_files(feature_repo, wt, [".env"])

    worktree.remove(feature_repo, wt, branch="ap/r_abc123")

    assert not wt.exists()
    assert "ap/r_abc123" not in git("branch", "--list", cwd=feature_repo)


def test_setup_command_runs_inside_the_worktree(feature_repo, wt):
    """``git rev-parse`` rather than ``pwd``: the shell builtin reports a POSIX
    path under Git Bash on Windows, which no comparison with ``wt`` can survive."""
    result = worktree.run_setup(wt, "git rev-parse --show-toplevel > setup_ran.txt")

    assert result.returncode == 0
    reported = (wt / "setup_ran.txt").read_text(encoding="utf-8").strip()
    assert Path(reported) == Path(wt)


def test_a_failing_setup_command_reports_rather_than_raising(feature_repo, wt):
    result = worktree.run_setup(wt, "exit 7")
    assert result.returncode == 7
