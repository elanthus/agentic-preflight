import pytest

from agentic_preflight import gitx
from tests.conftest import commit_all, git, write


def test_current_branch_reads_the_checked_out_branch(feature_repo):
    assert gitx.current_branch(feature_repo) == "feature/x"


def test_rev_parse_resolves_a_ref_to_a_full_sha(tmp_repo):
    sha = gitx.rev_parse(tmp_repo, "HEAD")
    assert len(sha) == 40
    assert sha == git("rev-parse", "HEAD", cwd=tmp_repo)


def test_merge_base_finds_the_fork_point(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    assert base == git("rev-parse", "main", cwd=feature_repo)


def test_changed_files_lists_only_files_touched_by_the_branch(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    assert gitx.changed_files(feature_repo, base, "HEAD") == ["src/app.py"]


def test_diff_text_contains_the_change(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    diff = gitx.diff_text(feature_repo, base, "HEAD")
    assert "loud=False" in diff
    assert "src/app.py" in diff


def test_is_clean_is_true_for_an_untouched_tree(tmp_repo):
    assert gitx.is_clean(tmp_repo) is True


def test_is_clean_is_false_with_an_unstaged_edit(tmp_repo):
    write(tmp_repo, "src/app.py", "changed\n")
    assert gitx.is_clean(tmp_repo) is False


def test_is_clean_is_false_with_an_untracked_file(tmp_repo):
    """An untracked file matters: `git add -A` in a worktree would sweep it in."""
    write(tmp_repo, "stray.txt", "junk\n")
    assert gitx.is_clean(tmp_repo) is False


def test_git_common_dir_is_absolute(tmp_repo):
    common = gitx.git_common_dir(tmp_repo)
    assert common.is_absolute()
    assert common.name == ".git"


def test_is_ignored_reports_gitignore_status(tmp_repo):
    write(tmp_repo, ".gitignore", ".env\n")
    write(tmp_repo, ".env", "SECRET=1\n")
    write(tmp_repo, "tracked.txt", "hello\n")
    assert gitx.is_ignored(tmp_repo, ".env") is True
    assert gitx.is_ignored(tmp_repo, "tracked.txt") is False


def test_commit_exists_distinguishes_real_from_invented_shas(tmp_repo):
    sha = gitx.rev_parse(tmp_repo, "HEAD")
    assert gitx.commit_exists(tmp_repo, sha) is True
    assert gitx.commit_exists(tmp_repo, "0" * 40) is False


def test_commit_touches_checks_the_claim(feature_repo):
    sha = gitx.rev_parse(feature_repo, "HEAD")
    assert gitx.commit_touches(feature_repo, sha, "src/app.py") is True
    assert gitx.commit_touches(feature_repo, sha, "README.md") is False


def test_commit_files_lists_the_changed_set(feature_repo):
    sha = gitx.rev_parse(feature_repo, "HEAD")
    assert gitx.commit_files(feature_repo, sha) == ["src/app.py"]


def test_is_ancestor_reflects_history(feature_repo):
    base = git("rev-parse", "main", cwd=feature_repo)
    head = gitx.rev_parse(feature_repo, "HEAD")
    assert gitx.is_ancestor(feature_repo, base, head) is True
    assert gitx.is_ancestor(feature_repo, head, base) is False


def test_stable_patch_id_matches_a_cherry_pick_with_a_different_sha(tmp_repo):
    git("switch", "-c", "fix", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", "def greet(name):\n    return f'hello {name}'\n")
    original = commit_all(tmp_repo, "improve greeting")

    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "README.md", "# demo\n\nUnrelated history.\n")
    commit_all(tmp_repo, "update readme")
    git("cherry-pick", original, cwd=tmp_repo)
    picked = git("rev-parse", "HEAD", cwd=tmp_repo)

    assert original != picked
    assert gitx.commit_patch_id(tmp_repo, original) == gitx.commit_patch_id(tmp_repo, picked)


def test_tree_sha_is_stable_for_identical_content(tmp_repo):
    first = gitx.tree_sha(tmp_repo, "HEAD")
    write(tmp_repo, "extra.txt", "x\n")
    commit_all(tmp_repo, "add extra")
    git("rm", "-q", "extra.txt", cwd=tmp_repo)
    commit_all(tmp_repo, "remove extra")
    assert gitx.tree_sha(tmp_repo, "HEAD") == first


def test_a_failing_git_command_raises_with_stderr_attached(tmp_repo):
    with pytest.raises(gitx.GitError) as exc:
        gitx.rev_parse(tmp_repo, "no-such-ref")
    assert "no-such-ref" in str(exc.value)
