import stat

import pytest

from agentic_cli import gitx, worktree
from tests.conftest import git, write


@pytest.fixture
def wt(feature_repo, tmp_path):
    """A live worktree for the feature branch head."""
    head = gitx.rev_parse(feature_repo, "HEAD")
    return worktree.create(
        feature_repo,
        path=tmp_path / "wt" / "r_abc123",
        branch="ac/r_abc123",
        head_sha=head,
    )


def test_create_checks_out_the_head_sha_on_its_own_branch(feature_repo, wt):
    assert wt.exists()
    assert gitx.current_branch(wt) == "ac/r_abc123"
    assert gitx.rev_parse(wt, "HEAD") == gitx.rev_parse(feature_repo, "HEAD")


def test_create_leaves_the_users_tree_untouched(feature_repo, wt):
    assert gitx.current_branch(feature_repo) == "feature/x"
    assert gitx.is_clean(feature_repo) is True


def test_create_does_not_clobber_an_existing_branch_name(feature_repo, tmp_path):
    git("branch", "ac/taken", cwd=feature_repo)
    head = gitx.rev_parse(feature_repo, "HEAD")
    with pytest.raises(worktree.WorktreeError):
        worktree.create(
            feature_repo,
            path=tmp_path / "wt" / "taken",
            branch="ac/taken",
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
    assert (wt / ".env").read_text() == "SECRET=hunter2\n"
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
        branch="ac/r_late",
        head_sha=head,
    )
    # Ignored in the user's tree, but never committed — so not ignored at head_sha.
    write(feature_repo, ".gitignore", ".env\nlate.txt\n")
    write(feature_repo, "late.txt", "SECRET=1\n")

    with pytest.raises(worktree.CopyRefused):
        worktree.copy_files(feature_repo, wt, ["late.txt"])


def test_copies_are_written_owner_only(feature_repo, wt):
    write(feature_repo, ".env", "SECRET=hunter2\n")

    worktree.copy_files(feature_repo, wt, [".env"])

    mode = stat.S_IMODE((wt / ".env").stat().st_mode)
    assert mode == 0o600


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


def test_remove_deletes_the_worktree_and_its_copies(feature_repo, wt):
    write(feature_repo, ".env", "SECRET=hunter2\n")
    worktree.copy_files(feature_repo, wt, [".env"])

    worktree.remove(feature_repo, wt, branch="ac/r_abc123")

    assert not wt.exists()
    assert "ac/r_abc123" not in git("branch", "--list", cwd=feature_repo)


def test_setup_command_runs_inside_the_worktree(feature_repo, wt):
    result = worktree.run_setup(wt, "pwd > setup_ran.txt")
    assert result.returncode == 0
    assert str(wt) in (wt / "setup_ran.txt").read_text()


def test_a_failing_setup_command_reports_rather_than_raising(feature_repo, wt):
    result = worktree.run_setup(wt, "exit 7")
    assert result.returncode == 7
