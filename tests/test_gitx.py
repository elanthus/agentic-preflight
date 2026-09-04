import os
import subprocess

import pytest

from agentic_preflight import gitx
from agentic_preflight.stages import command as command_plan
from tests.conftest import (
    commit_all,
    git,
    require_type_change,
    requires_git_symlinks,
    write,
)


def test_current_branch_reads_the_checked_out_branch(feature_repo):
    assert gitx.current_branch(feature_repo) == "feature/x"


def test_operation_in_progress_is_absent_in_an_idle_checkout(tmp_repo):
    assert gitx.operation_in_progress(tmp_repo) is None


def test_operation_in_progress_detects_a_real_merge(tmp_repo):
    git("switch", "-c", "merge-side", cwd=tmp_repo)
    write(tmp_repo, "side.txt", "side\n")
    commit_all(tmp_repo, "add side file")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "main.txt", "main\n")
    commit_all(tmp_repo, "add main file")
    git("merge", "--no-commit", "merge-side", cwd=tmp_repo)

    assert gitx.operation_in_progress(tmp_repo) == "merge"


def test_rev_parse_resolves_a_ref_to_a_full_sha(tmp_repo):
    sha = gitx.rev_parse(tmp_repo, "HEAD")
    assert len(sha) == 40
    assert sha == git("rev-parse", "HEAD", cwd=tmp_repo)


def test_merge_base_finds_the_fork_point(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    assert base == git("rev-parse", "main", cwd=feature_repo)


def test_merge_tree_uses_the_legacy_fallback_for_invalid_write_tree_object(tmp_repo, monkeypatch):
    tree = "a" * 40
    calls: list[list[str]] = []

    monkeypatch.setattr(
        gitx,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=128,
            stdout="",
            stderr="fatal: Not a valid object name --write-tree\n",
        ),
    )
    monkeypatch.setattr(gitx, "merge_base", lambda *_args: "base")

    def legacy_run(args, **_kwargs):
        calls.append(args)
        if args[1] == "read-tree":
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{tree}\n", stderr="")

    monkeypatch.setattr(gitx.subprocess, "run", legacy_run)

    assert gitx.merge_tree(tmp_repo, "left", "right") == tree
    assert [call[1] for call in calls] == ["read-tree", "write-tree"]


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("git version 2.34.1\n", (2, 34)),
        ("git version 2.46.2.windows.1\n", (2, 46)),
        ("git version 2.39.3 (Apple Git-146)\n", (2, 39)),
        ("git version 2.51.0\n", (2, 51)),
    ],
)
def test_the_git_version_is_read_from_real_world_version_strings(
    tmp_repo, monkeypatch, reported, expected
):
    monkeypatch.setattr(
        gitx,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=reported, stderr=""
        ),
    )

    assert gitx.version(tmp_repo) == expected


def test_an_unreadable_git_version_is_reported_as_unknown(tmp_repo, monkeypatch):
    monkeypatch.setattr(
        gitx,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not git"
        ),
    )

    assert gitx.version(tmp_repo) is None


def test_a_pre_2_38_git_never_invokes_the_write_tree_flag(tmp_repo, monkeypatch):
    """The regression this guards: git 2.30-2.37 rejects ``--write-tree`` on stderr
    while exiting *zero*, so there is no failure to detect. Asking the version
    first is what keeps those releases on the interface they actually have."""
    invoked: list[tuple[str, ...]] = []

    def record(cwd, *args, **_kwargs):
        invoked.append(args)
        if args == ("--version",):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="git version 2.34.1\n", stderr=""
            )
        raise AssertionError(f"unexpected git invocation: {args}")

    monkeypatch.setattr(gitx, "run", record)
    monkeypatch.setattr(gitx, "_merge_tree_via_index", lambda *_args: "b" * 40)

    assert gitx.merge_tree(tmp_repo, "left", "right") == "b" * 40
    assert invoked == [("--version",)]


def test_a_modern_git_that_still_rejects_the_flag_falls_back(tmp_repo, monkeypatch):
    """Exit zero with no tree is the shape that previously returned "no clean
    merge" for a merge that may have been perfectly clean."""

    def respond(cwd, *args, **_kwargs):
        if args == ("--version",):
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="git version 2.46.0\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="fatal: unknown rev --write-tree\n"
        )

    monkeypatch.setattr(gitx, "run", respond)
    monkeypatch.setattr(gitx, "_merge_tree_via_index", lambda *_args: "c" * 40)

    assert gitx.merge_tree(tmp_repo, "left", "right") == "c" * 40


def test_the_index_fallback_agrees_with_this_git_on_a_clean_merge(feature_repo):
    """Exercises the fallback against the real binary, whichever interface this
    machine's git would otherwise have used."""
    base = gitx.merge_base(feature_repo, "main", "HEAD")

    assert gitx._merge_tree_via_index(feature_repo, base, "HEAD") == gitx.tree_sha(
        feature_repo, "HEAD"
    )


def test_merge_tree_rejects_object_ids_outside_the_sha1_schema(tmp_repo, monkeypatch):
    tree = "a" * 64
    monkeypatch.setattr(
        gitx,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{tree}\n", stderr=""
        ),
    )

    assert gitx.merge_tree(tmp_repo, "left", "right") is None


def test_changed_files_lists_only_files_touched_by_the_branch(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    assert gitx.changed_files(feature_repo, base, "HEAD") == ["src/app.py"]


def test_changed_files_preserves_non_ascii_paths(tmp_repo):
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/non-ascii", cwd=tmp_repo)
    write(tmp_repo, "café.txt", "changed\n")
    commit_all(tmp_repo, "add non-ascii path")

    assert gitx.changed_files(tmp_repo, base) == ["café.txt"]


def test_diff_text_survives_non_utf8_file_content(tmp_repo):
    """Changed files are repository data, and repositories contain latin-1.

    Git emits the patch bytes as they are; a strict UTF-8 decode turns one
    legacy-encoded changed file into a traceback for the whole diff.
    """
    (tmp_repo / "legacy.txt").write_bytes(b"caf\xe9 before\n")
    commit_all(tmp_repo, "add legacy-encoded file")
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/legacy-encoding", cwd=tmp_repo)
    (tmp_repo / "legacy.txt").write_bytes(b"caf\xe9 after\n")
    commit_all(tmp_repo, "change legacy-encoded file")

    diff = gitx.diff_text(tmp_repo, base)

    assert "legacy.txt" in diff
    assert "\\xe9" in diff


def test_diff_text_contains_the_change(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    diff = gitx.diff_text(feature_repo, base, "HEAD")
    assert "loud=False" in diff
    assert "src/app.py" in diff


def test_diff_text_by_path_matches_individual_patches(feature_repo):
    base = gitx.merge_base(feature_repo, "main", "HEAD")
    write(feature_repo, "README.md", "# changed\n")
    commit_all(feature_repo, "change readme")

    paths = gitx.changed_files(feature_repo, base)
    batched = gitx.diff_text_by_path(feature_repo, base, "HEAD", paths)

    assert list(batched) == paths
    assert batched == {
        path: gitx.diff_text_for_path(feature_repo, base, "HEAD", path) for path in paths
    }


def test_diff_text_by_path_handles_renames_binary_and_pathspec_characters(tmp_repo):
    shared = "".join(f"shared line {index}\n" for index in range(20))
    write(tmp_repo, "literal[1].txt", shared + "before\n")
    write(tmp_repo, "brackets[1].txt", "before\n")
    (tmp_repo / "image.bin").write_bytes(b"before\0")
    commit_all(tmp_repo, "add unusual files")
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/unusual", cwd=tmp_repo)
    git("mv", "literal[1].txt", "renamed file.txt", cwd=tmp_repo)
    write(tmp_repo, "renamed file.txt", shared + "after\n")
    write(tmp_repo, "brackets[1].txt", "after\n")
    (tmp_repo / "image.bin").write_bytes(b"after\0")
    commit_all(tmp_repo, "rename text and update binary")

    paths = gitx.changed_files(tmp_repo, base)
    batched = gitx.diff_text_by_path(tmp_repo, base, "HEAD", paths)

    assert list(batched) == paths
    assert batched == {
        path: gitx.diff_text_for_path(tmp_repo, base, "HEAD", path) for path in paths
    }
    assert "brackets[1].txt" in batched["brackets[1].txt"]
    assert "renamed file.txt" in batched["renamed file.txt"]
    assert "Binary files" in batched["image.bin"]


@requires_git_symlinks
def test_diff_text_by_path_handles_non_ascii_and_file_to_symlink_changes(tmp_repo):
    write(tmp_repo, "plain.txt", "before\n")
    write(tmp_repo, "café.txt", "before\n")
    write(tmp_repo, "kind.txt", "before\n")
    commit_all(tmp_repo, "add mixed files")
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/mixed-diff", cwd=tmp_repo)
    write(tmp_repo, "plain.txt", "after\n")
    write(tmp_repo, "café.txt", "after\n")
    (tmp_repo / "kind.txt").unlink()
    (tmp_repo / "kind.txt").symlink_to("plain.txt")
    commit_all(tmp_repo, "change mixed files")
    require_type_change(tmp_repo, base, "HEAD", "kind.txt")

    paths = gitx.changed_files(tmp_repo, base)
    batched = gitx.diff_text_by_path(tmp_repo, base, "HEAD", paths)

    assert set(batched) == {"plain.txt", "café.txt", "kind.txt"}
    assert batched == {
        path: gitx.diff_text_for_path(tmp_repo, base, "HEAD", path) for path in paths
    }
    assert batched["kind.txt"].count("diff --git ") == 2


@requires_git_symlinks
def test_diff_text_by_path_handles_symlink_to_file_changes(tmp_repo):
    write(tmp_repo, "target.txt", "target\n")
    (tmp_repo / "kind.txt").symlink_to("target.txt")
    commit_all(tmp_repo, "add symlink")
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/symlink-to-file", cwd=tmp_repo)
    (tmp_repo / "kind.txt").unlink()
    write(tmp_repo, "kind.txt", "regular file\n")
    commit_all(tmp_repo, "replace symlink with file")
    require_type_change(tmp_repo, base, "HEAD", "kind.txt")

    batched = gitx.diff_text_by_path(tmp_repo, base, "HEAD", ["kind.txt"])

    assert batched["kind.txt"] == gitx.diff_text_for_path(tmp_repo, base, "HEAD", "kind.txt")
    assert batched["kind.txt"].count("diff --git ") == 2


def test_diff_text_by_path_batches_many_files(tmp_repo, monkeypatch):
    base = gitx.rev_parse(tmp_repo, "HEAD")
    git("switch", "-c", "feature/many-files", cwd=tmp_repo)
    for index in range(300):
        write(tmp_repo, f"many/{index:03d}.txt", "changed\n")
    commit_all(tmp_repo, "add many files")

    calls = 0
    real_run = gitx.run

    def recording_run(cwd, *args, **kwargs):
        nonlocal calls
        calls += 1
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(gitx, "run", recording_run)
    paths = [f"many/{index:03d}.txt" for index in range(300)]

    assert len(gitx.diff_text_by_path(tmp_repo, base, "HEAD", paths)) == 300
    assert calls == 2


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


def test_commit_files_preserves_non_ascii_paths(tmp_repo):
    """Under default ``core.quotePath`` git C-quotes ``café.txt`` into
    ``"caf\\303\\251.txt"`` — a path that exists nowhere — unless asked for
    NUL-delimited output, the same way ``changed_files`` already asks."""
    git("switch", "-c", "feature/non-ascii-commit", cwd=tmp_repo)
    write(tmp_repo, "café.txt", "changed\n")
    sha = commit_all(tmp_repo, "add non-ascii path")

    assert gitx.commit_files(tmp_repo, sha) == ["café.txt"]


def test_git_is_invoked_through_an_absolute_executable_path(tmp_repo, monkeypatch):
    """Handed a bare name, Windows' ``CreateProcess`` searches the parent's
    current directory before PATH — for this tool, the repository under
    validation. A repo-committed ``git.exe`` must never be the git that
    validates the repository that ships it; an absolute path leaves no search."""
    recorded: list[str] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        recorded.append(argv[0])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitx.subprocess, "run", recording_run)

    gitx.current_branch(tmp_repo)

    assert recorded
    assert all(os.path.isabs(program) for program in recorded)


def test_a_missing_git_raises_rather_than_falling_back_to_a_bare_name(tmp_repo, monkeypatch):
    """A bare-name fallback would hand ``CreateProcess`` its current-directory
    search back — the exact hole PATH-only resolution exists to close."""
    monkeypatch.setattr(gitx, "_RESOLVED_GIT", None)
    monkeypatch.setattr(gitx, "resolve_on_path", lambda program: None)

    with pytest.raises(FileNotFoundError, match="git"):
        gitx.current_branch(tmp_repo)


def test_the_git_executable_is_resolved_on_path_exactly_once(tmp_repo, monkeypatch):
    calls: list[str] = []
    real_resolve = command_plan.resolve_on_path

    def counting_resolve(program: str) -> str | None:
        calls.append(program)
        return real_resolve(program)

    monkeypatch.setattr(gitx, "_RESOLVED_GIT", None, raising=False)
    monkeypatch.setattr(gitx, "resolve_on_path", counting_resolve, raising=False)

    gitx.current_branch(tmp_repo)
    gitx.rev_parse(tmp_repo, "HEAD")

    assert calls == ["git"]


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


def test_patch_ids_distinguish_raw_bytes_from_their_escape_text(tmp_repo):
    """The patch reaches ``git patch-id`` as the bytes git emitted. Decoding it
    first would fold the raw byte 0xE9 and the literal four characters ``\\xe9``
    into one string, giving two different changes the same patch identity."""
    write(tmp_repo, "data.txt", "before\n")
    commit_all(tmp_repo, "seed")
    base = gitx.current_branch(tmp_repo)
    git("switch", "-c", "raw-byte", cwd=tmp_repo)
    (tmp_repo / "data.txt").write_bytes(b"caf\xe9\n")
    raw_sha = commit_all(tmp_repo, "raw byte")
    git("switch", "-c", "escape-text", base, cwd=tmp_repo)
    (tmp_repo / "data.txt").write_bytes(b"caf\\xe9\n")
    escaped_sha = commit_all(tmp_repo, "escape text")

    assert gitx.commit_patch_id(tmp_repo, raw_sha) != gitx.commit_patch_id(tmp_repo, escaped_sha)


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
