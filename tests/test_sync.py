import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_preflight import attestation, gitx, sync
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
    assert result.rebased is True
    assert result.merged is False
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
        sync.sync_onto(feature_repo, "main")
    except Exception as exc:  # noqa: BLE001 - identifies the BASE failure by assertion below
        caught = exc

    assert rebase_dir.is_dir(), "sync_onto aborted the user's paused interactive rebase"
    assert type(caught).__name__ == "OperationInProgress"
    assert getattr(caught, "operation", None) == "rebase"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before_head
    assert git("status", "--porcelain", cwd=feature_repo) == before_status

    agent = ScriptedAgent(feature_repo)
    refused = agent.run("start", expect=ExitCode.PRECONDITION)

    assert refused["error"]["code"] == "operation_in_progress"
    assert refused["data"] == {"operation": "rebase", "path": str(feature_repo)}
    assert refused["next"]["command"] == "git status"
    assert rebase_dir.is_dir()
    assert git("rev-parse", "HEAD", cwd=feature_repo) == before_head
    assert git("status", "--porcelain", cwd=feature_repo) == before_status

    git("rebase", "--abort", cwd=feature_repo)
    assert agent.run("start")["state"] == "REVIEW_AWAITING_FINDINGS"


@pytest.mark.parametrize("remote", [None, "origin"])
def test_sync_result_serialization_preserves_nullable_remote(remote):
    result = sync.SyncResult(remote, "origin/main", "a" * 40, "b" * 40, "c" * 40, True)
    serialized = result.as_dict()
    assert serialized == {
        "remote": remote,
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "head_before": "b" * 40,
        "head_after": "c" * 40,
        "rebased": True,
        "merged": False,
    }
    serialized["remote"] = "changed"
    assert result.remote == remote


def test_non_conflict_rebase_failure_is_reported_with_git_stderr(tmp_repo, tmp_path):
    git("switch", "-c", "feat", cwd=tmp_repo)
    write(tmp_repo, "feature.txt", "feature\n")
    commit_all(tmp_repo, "feature")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "upstream.txt", "upstream\n")
    commit_all(tmp_repo, "upstream")
    git("switch", "feat", cwd=tmp_repo)
    before = gitx.rev_parse(tmp_repo, "HEAD")

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-rebase"
    hook.write_text("#!/bin/sh\necho 'pre-rebase hook refused' >&2\nexit 1\n")
    hook.chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=tmp_repo)

    with pytest.raises(sync.SyncRebaseFailed) as caught:
        sync.sync_onto(tmp_repo, "main")

    assert caught.value.conflicting_files == []
    assert "pre-rebase hook refused" in caught.value.stderr
    assert "pre-rebase hook refused" in str(caught.value)
    assert "without a conflict" in str(caught.value)
    assert gitx.rev_parse(tmp_repo, "HEAD") == before
    assert gitx.operation_in_progress(tmp_repo) is None
    assert git("symbolic-ref", "HEAD", cwd=tmp_repo) == "refs/heads/feat"


def test_signing_failure_during_rebase_leaves_no_rebase_in_progress(tmp_repo):
    git("switch", "-c", "feat", cwd=tmp_repo)
    write(tmp_repo, "feature.txt", "feature\n")
    commit_all(tmp_repo, "feature")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "upstream.txt", "upstream\n")
    commit_all(tmp_repo, "upstream")
    git("switch", "feat", cwd=tmp_repo)
    before = gitx.rev_parse(tmp_repo, "HEAD")
    git("config", "commit.gpgsign", "true", cwd=tmp_repo)
    git("config", "gpg.program", "false", cwd=tmp_repo)

    with pytest.raises(sync.SyncRebaseFailed) as caught:
        sync.sync_onto(tmp_repo, "main")

    assert caught.value.conflicting_files == []
    assert caught.value.stderr.strip()
    assert gitx.operation_in_progress(tmp_repo) is None
    assert gitx.rev_parse(tmp_repo, "HEAD") == before
    assert git("symbolic-ref", "HEAD", cwd=tmp_repo) == "refs/heads/feat"


def _diverge(repo: Path) -> str:
    """Put ``feat`` one commit ahead of the old ``main``, advance ``main``, check out ``feat``.

    Returns the advanced ``main`` commit.
    """
    git("switch", "-c", "feat", cwd=repo)
    write(repo, "feature.txt", "feature\n")
    commit_all(repo, "feature")
    git("switch", "main", cwd=repo)
    write(repo, "upstream.txt", "upstream\n")
    base = commit_all(repo, "upstream")
    git("switch", "feat", cwd=repo)
    return base


def test_sync_rebase_leaves_a_stacked_branch_alone_under_update_refs(tmp_repo):
    git("switch", "-c", "part1", cwd=tmp_repo)
    write(tmp_repo, "part1.txt", "part one\n")
    part1 = commit_all(tmp_repo, "part one")
    _diverge(tmp_repo)
    git("config", "rebase.updateRefs", "true", cwd=tmp_repo)

    before, after, merged = sync.sync_onto(tmp_repo, "main")

    assert after != before
    assert merged is False
    assert git("rev-parse", "part1", cwd=tmp_repo) == part1


def test_strict_start_leaves_a_stacked_branch_alone_under_update_refs(feature_repo):
    """``rebase.updateRefs`` lives in repository config, which the suite does not isolate."""
    write(feature_repo, ".agentic-preflight.toml", "[worktree]\nmode = 'strict'\n")
    commit_all(feature_repo, "use strict worktrees")
    git("branch", "part1", cwd=feature_repo)
    part1 = git("rev-parse", "part1", cwd=feature_repo)
    write(feature_repo, "part2.txt", "part two\n")
    commit_all(feature_repo, "part two")
    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "upstream.txt", "upstream\n")
    base = commit_all(feature_repo, "advance main")
    git("switch", "feature/x", cwd=feature_repo)
    git("config", "rebase.updateRefs", "true", cwd=feature_repo)

    started = ScriptedAgent(feature_repo).run("start")

    worktree_path = Path(started["data"]["worktree_path"])
    assert gitx.is_ancestor(worktree_path, base, "HEAD"), "start did not rebase"
    assert git("rev-parse", "part1", cwd=feature_repo) == part1
    assert len(git("reflog", "part1", cwd=feature_repo).splitlines()) == 1


def test_sync_rebase_ignores_rebase_autosquash(tmp_repo):
    """A guard: Git applies ``rebase.autoSquash`` only to interactive rebases today."""
    _diverge(tmp_repo)
    write(tmp_repo, "feature.txt", "feature, amended\n")
    commit_all(tmp_repo, "fixup! feature")
    git("config", "rebase.autoSquash", "true", cwd=tmp_repo)

    sync.sync_onto(tmp_repo, "main")

    assert git("log", "--format=%s", "main..HEAD", cwd=tmp_repo).splitlines() == [
        "fixup! feature",
        "feature",
    ]


def test_sync_rebase_ignores_rebase_autostash(tmp_repo):
    """Without autostash Git refuses to rebase a dirty checkout, and so must the sync."""
    _diverge(tmp_repo)
    before = gitx.rev_parse(tmp_repo, "HEAD")
    write(tmp_repo, "feature.txt", "uncommitted\n")
    git("config", "rebase.autoStash", "true", cwd=tmp_repo)

    with pytest.raises(sync.SyncRebaseFailed):
        sync.sync_onto(tmp_repo, "main")

    assert gitx.rev_parse(tmp_repo, "HEAD") == before
    assert (tmp_repo / "feature.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert git("stash", "list", cwd=tmp_repo) == ""


def test_sync_rebase_ignores_rebase_rebase_merges(tmp_repo):
    """The fallback-merge logic relies on the rebase dropping merge commits."""
    base = _diverge(tmp_repo)
    git("switch", "-c", "side", "feat~1", cwd=tmp_repo)
    write(tmp_repo, "side.txt", "side\n")
    commit_all(tmp_repo, "side")
    git("switch", "feat", cwd=tmp_repo)
    git("merge", "--no-ff", "--no-edit", "side", cwd=tmp_repo)
    git("config", "rebase.rebaseMerges", "true", cwd=tmp_repo)

    _before, after, merged = sync.sync_onto(tmp_repo, "main")

    assert merged is False
    assert git("rev-list", "--merges", f"{base}..{after}", cwd=tmp_repo) == ""


def test_sync_rebase_ignores_pull_twohead(tmp_repo):
    """The rebase reads ``pull.twohead`` as its default strategy; ``ours`` drops the branch."""
    base = _diverge(tmp_repo)
    git("config", "pull.twohead", "ours", cwd=tmp_repo)

    _before, after, merged = sync.sync_onto(tmp_repo, "main")

    assert merged is False
    assert git("log", "--format=%s", f"{base}..{after}", cwd=tmp_repo) == "feature"
    assert git("show", f"{after}:feature.txt", cwd=tmp_repo) == "feature"


# -- obstructed-path detection ------------------------------------------------


def _fake_git(monkeypatch, outputs: dict[str, list[str]]) -> None:
    """Answer ``gitx.run`` from canned output, keyed by the Git subcommand."""
    queues = {command: list(values) for command, values in outputs.items()}

    def fake_run(cwd, *args, **kwargs):
        command = next(arg for arg in args if not arg.startswith("-"))
        return subprocess.CompletedProcess(["git", *args], 0, queues[command].pop(0), "")

    monkeypatch.setattr(gitx, "run", fake_run)


def test_obstructed_paths_keeps_first_seen_order_across_trees(tmp_path, monkeypatch):
    for name in ("b", "a", "e"):
        (tmp_path / name).write_text("local\n", encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("local\n", encoding="utf-8")
    _fake_git(
        monkeypatch,
        {
            "diff": ["b\0a\0c\0", "a\0d\0b\0e\0"],
            # Git lists untracked files sorted; the result must still follow the trees.
            "ls-files": ["a\0b\0d/x\0"],
        },
    )

    assert sync._obstructed_paths(tmp_path, "before", ["one", "two"]) == ["b", "a", "d"]


class _CountingPath(str):
    """A path that counts equality tests, the cost a list membership test pays per element."""

    budget = 0
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        if type(self).comparisons > type(self).budget:
            raise AssertionError("compared paths more than linearly often")
        return str.__eq__(self, other)

    __hash__ = str.__hash__


class _CountingOutput(str):
    def split(self, sep=None, maxsplit=-1):  # type: ignore[override]
        return [_CountingPath(part) for part in str.split(self, sep, maxsplit)]


def test_obstructed_paths_deduplicates_in_linear_comparisons(tmp_path, monkeypatch):
    count = 100_000
    paths = [f"dir/file-{index}.txt" for index in range(count)]
    # The second tree repeats every path of the first and adds as many again.
    first = _CountingOutput("\0".join(paths) + "\0")
    second = _CountingOutput("\0".join(paths + [f"new/{path}" for path in paths]) + "\0")
    monkeypatch.setattr(_CountingPath, "comparisons", 0)
    monkeypatch.setattr(_CountingPath, "budget", 3 * count)
    _fake_git(monkeypatch, {"diff": [first, second], "ls-files": []})
    _no_local_files(monkeypatch)

    # No path exists locally, so nothing is obstructed and Git is not asked again.
    assert sync._obstructed_paths(tmp_path, "before", ["one", "two"]) == []
    # Each repeated path is compared once against its first occurrence. Fewer comparisons
    # mean the counting paths were bypassed and the budget checked nothing.
    assert _CountingPath.comparisons >= count


def _no_local_files(monkeypatch) -> None:
    monkeypatch.setattr(sync.Path, "exists", lambda self, **kwargs: False)
    monkeypatch.setattr(sync.Path, "is_symlink", lambda self: False)


def _every_path_exists(monkeypatch) -> None:
    monkeypatch.setattr(sync.Path, "exists", lambda self, **kwargs: True)


def _reference_filter(candidates: list[str], untracked: list[str]) -> list[str]:
    """The original quadratic filter, kept as the specification for the linear one."""
    return [
        path
        for path in candidates
        if any(found == path or found.startswith(f"{path}/") for found in untracked)
    ]


def test_obstructed_paths_filter_matches_the_reference_on_edge_cases(tmp_path, monkeypatch):
    candidates = ["a", "a/", "ab", "a/b", "b", "c//d", "c", "c/", "x/y", "x", "y", "z/w"]
    untracked = ["a/b", "ab", "c//d/e", "x/y", "y/", "z/w/v/u"]
    _fake_git(
        monkeypatch,
        {"diff": ["\0".join(candidates) + "\0"], "ls-files": ["\0".join(untracked) + "\0"]},
    )
    _every_path_exists(monkeypatch)

    expected = _reference_filter(candidates, untracked)
    assert expected == ["a", "ab", "a/b", "c//d", "c", "c/", "x/y", "x", "y", "z/w"]
    assert sync._obstructed_paths(tmp_path, "before", ["one"]) == expected


def test_obstructed_paths_filters_untracked_files_in_linear_comparisons(tmp_path, monkeypatch):
    count = 100_000
    directories = [f"dir-{index}" for index in range(count)]
    # Every other added directory holds an untracked file; the rest hold nothing.
    untracked = [f"{directory}/file.txt" for directory in directories[::2]]
    added = _CountingOutput("\0".join(directories) + "\0")
    listed = _CountingOutput("\0".join(untracked) + "\0")
    monkeypatch.setattr(_CountingPath, "comparisons", 0)
    monkeypatch.setattr(_CountingPath, "budget", 3 * count)
    _fake_git(monkeypatch, {"diff": [added], "ls-files": [listed]})
    _every_path_exists(monkeypatch)

    assert sync._obstructed_paths(tmp_path, "before", ["one"]) == directories[::2]
    # Each obstructed directory is compared once when it is found among the matches.
    assert _CountingPath.comparisons >= count // 2
