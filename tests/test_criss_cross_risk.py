"""Criss-cross history must not hide a change from local classification.

Each scenario builds the same history. The base merged a side branch that changed a
high-risk file. The feature branch forks from the earlier main commit and merges that
side branch with ``-s ours``, so Git has two merge bases. It picks the main commit, and
``base...head`` omits the side-branch file even though merging the head into the base
reverts it. Synchronization must keep that revert: a rebase would drop the merge.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentic_preflight import gitx, refresh_validation, sync
from agentic_preflight.approval import evaluate
from agentic_preflight.models import AttestedStage, ReviewCoverage, Stage
from agentic_preflight.store import Store
from tests.attestation_helpers import make_attestation
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _criss_cross(repo, monkeypatch, *, edit_notes=True, mode="in_place"):
    """Return ``(base, head)`` for a head whose merge into base hides a revert."""
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    write(
        repo,
        ".agentic-preflight.toml",
        f"[policy]\nhigh_risk_paths = ['src/**']\n[commands]\nlint = {command}\ntest = {command}\n"
        f"[worktree]\nmode = '{mode}'\n",
    )
    write(repo, "src/security.py", "CHECK = 'on'\n")
    commit_all(repo, "configure")
    # ``git merge-base`` without ``--all`` prints the newest of the best merge bases.
    # Dating ``fork`` one day after ``side`` makes Git choose ``fork`` whatever date
    # the fixture starts from.
    side_date = datetime.fromisoformat(git("log", "-1", "--format=%cI", cwd=repo))
    monkeypatch.setenv("GIT_COMMITTER_DATE", side_date.isoformat())
    git("switch", "-c", "side", cwd=repo)
    write(repo, "src/security.py", "CHECK = 'on'\nSTRICT = 'on'\n")
    side = commit_all(repo, "tighten security")
    git("switch", "main", cwd=repo)
    write(repo, "docs/notes.md", "one\n")
    monkeypatch.setenv("GIT_COMMITTER_DATE", (side_date + timedelta(days=1)).isoformat())
    fork = commit_all(repo, "advance main")
    git("merge", "--no-ff", "--no-edit", "side", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo)
    git("switch", "-c", "feature/hidden", fork, cwd=repo)
    if edit_notes:
        write(repo, "docs/notes.md", "two\n")
        commit_all(repo, "edit notes")
    git("merge", "-s", "ours", "--no-edit", side, cwd=repo)
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    head = git("rev-parse", "HEAD", cwd=repo)
    assert set(git("merge-base", "--all", base, head, cwd=repo).split()) == {fork, side}
    assert gitx.merge_base(repo, base, head) == fork, "Git chose a different base"
    assert "src/security.py" not in gitx.changed_files(repo, base, head)
    assert "src/security.py" in gitx.merge_result_files(repo, base, head)
    return base, head


def test_classified_files_include_paths_only_the_merge_result_changes(tmp_repo, monkeypatch):
    base, head = _criss_cross(tmp_repo, monkeypatch)
    assert gitx.classified_files(tmp_repo, base, head) == ["docs/notes.md", "src/security.py"]


def test_sync_merges_the_base_when_a_rebase_would_change_what_the_branch_lands(
    tmp_repo, monkeypatch
):
    base, head = _criss_cross(tmp_repo, monkeypatch)
    landed = gitx.merge_tree(tmp_repo, base, head)

    result = sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    after = git("rev-parse", "HEAD", cwd=tmp_repo)
    assert result.merged is True
    assert result.head_after == after
    assert gitx.tree_sha(tmp_repo, after) == landed
    # Merging keeps the user's commits rather than rewriting them.
    assert git("rev-parse", f"{after}^1", cwd=tmp_repo) == head
    assert git("rev-parse", f"{after}^2", cwd=tmp_repo) == base
    assert gitx.changed_files(tmp_repo, base, after) == ["docs/notes.md", "src/security.py"]


def test_sync_fallback_merge_is_not_blocked_by_commit_hooks(tmp_repo, monkeypatch):
    base, head = _criss_cross(tmp_repo, monkeypatch)
    hooks = Path(git("rev-parse", "--git-path", "hooks", cwd=tmp_repo))
    if not hooks.is_absolute():
        hooks = tmp_repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("pre-merge-commit", "commit-msg"):
        hook = hooks / name
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

    result = sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert result.merged is True
    assert gitx.tree_sha(tmp_repo, "HEAD") == gitx.merge_tree(tmp_repo, base, head)


@pytest.mark.parametrize("backend", ["apply", "merge"])
def test_sync_never_overwrites_an_ignored_file(tmp_repo, backend):
    """A directory rename would move a branch file onto an ignored local file.

    The merge backend's rebase moves it; the apply backend keeps it in place, so the
    fallback merge would move it instead. Either way, refuse before changing anything.
    """
    git("config", "rebase.backend", backend, cwd=tmp_repo)
    git("config", "merge.directoryRenames", "true", cwd=tmp_repo)
    write(tmp_repo, ".gitignore", ".env\nconfig.local\n")
    commit_all(tmp_repo, "ignore local config")
    git("switch", "-c", "feature/config", cwd=tmp_repo)
    write(tmp_repo, "src/config.local", "TRACKED = 1\n")
    git("add", "-f", "src/config.local", cwd=tmp_repo)
    git("commit", "-m", "track config", cwd=tmp_repo)
    head = git("rev-parse", "HEAD", cwd=tmp_repo)
    git("switch", "main", cwd=tmp_repo)
    git("mv", "src", "lib", cwd=tmp_repo)
    base = commit_all(tmp_repo, "rename src to lib")
    git("switch", "feature/config", cwd=tmp_repo)
    write(tmp_repo, "lib/config.local", "LOCAL SECRET\n")
    landed = gitx.merge_tree(tmp_repo, base, head)
    assert landed is not None
    assert "lib/config.local" in git("ls-tree", "-r", "--name-only", landed, cwd=tmp_repo)

    with pytest.raises(sync.SyncObstructed) as caught:
        sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert caught.value.conflicting_files == ["lib/config.local"]
    assert (tmp_repo / "lib/config.local").read_text(encoding="utf-8") == "LOCAL SECRET\n"
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == head
    assert gitx.operation_in_progress(tmp_repo) is None


@pytest.mark.parametrize("options", ["--no-commit", "--no-commit --squash"])
def test_sync_fallback_merge_commits_despite_branch_merge_options(tmp_repo, monkeypatch, options):
    base, head = _criss_cross(tmp_repo, monkeypatch)
    git("config", "branch.feature/hidden.mergeOptions", options, cwd=tmp_repo)

    result = sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert result.merged is True
    assert gitx.operation_in_progress(tmp_repo) is None
    assert gitx.is_ancestor(tmp_repo, base, "HEAD")
    assert gitx.is_ancestor(tmp_repo, head, "HEAD")


def test_sync_stops_before_mutating_when_the_landed_tree_cannot_be_computed(tmp_repo, monkeypatch):
    _, head = _criss_cross(tmp_repo, monkeypatch)

    def failing_merge_tree(*_args, **_kwargs):
        raise gitx.GitError(["merge-tree"], 128, "fatal: unable to write object")

    monkeypatch.setattr(gitx, "merge_tree", failing_merge_tree)
    with pytest.raises(gitx.GitError):
        sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert git("rev-parse", "HEAD", cwd=tmp_repo) == head


def test_sync_rebases_unrelated_history_without_a_landed_tree(tmp_repo):
    git("switch", "--orphan", "feature/unrelated", cwd=tmp_repo)
    write(tmp_repo, "other.txt", "unrelated\n")
    commit_all(tmp_repo, "unrelated root")

    result = sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert result.rebased is True
    assert result.merged is False
    assert gitx.is_ancestor(tmp_repo, "main", "HEAD")


def test_sync_merges_when_a_rebase_would_skip_a_change_the_base_already_has(tmp_repo):
    """A rebase skips the branch commit the base already has, then replays its revert."""
    original = (tmp_repo / "src/app.py").read_text(encoding="utf-8")
    git("switch", "-c", "feature/undo", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", original + "FLAG = 1\n")
    commit_all(tmp_repo, "add flag")
    write(tmp_repo, "src/app.py", original)
    head = commit_all(tmp_repo, "remove flag")
    git("switch", "main", cwd=tmp_repo)
    write(tmp_repo, "src/app.py", original + "FLAG = 1\n")
    base = commit_all(tmp_repo, "main adds flag")
    git("switch", "feature/undo", cwd=tmp_repo)
    # The branch nets to nothing, so merging it keeps the base's flag.
    assert gitx.merge_tree(tmp_repo, base, head) == gitx.tree_sha(tmp_repo, base)

    result = sync.synchronize(tmp_repo, tmp_repo, base_ref="main")

    assert result.merged is True
    assert gitx.tree_sha(tmp_repo, "HEAD") == gitx.tree_sha(tmp_repo, base)


def test_start_validates_a_branch_whose_only_change_is_hidden_by_criss_cross_history(
    tmp_repo, monkeypatch
):
    base, head = _criss_cross(tmp_repo, monkeypatch, edit_notes=False)
    assert gitx.changed_files(tmp_repo, base, head) == []

    data = ScriptedAgent(tmp_repo).run("start")["data"]

    assert data["sync"]["merged"] is True
    assert data["changed_files"] == ["src/security.py"]
    assert data["risk"]["level"] == "high"
    assert gitx.is_ancestor(tmp_repo, head, "HEAD")


def test_refresh_verification_classifies_the_merge_result_for_a_test_skip(tmp_repo, monkeypatch):
    base, head = _criss_cross(tmp_repo, monkeypatch)
    stages = {
        Stage.REVIEW: AttestedStage(
            status="green",
            executor="in_harness",
            coverage=ReviewCoverage(
                manifest="d" * 64, head_sha=head, total_units=1, clean_units=["U0001"]
            ),
        ),
        Stage.DOCS: AttestedStage(status="green"),
        Stage.LINT: AttestedStage(
            status="green", command="lint", exit_code=0, output_sha256="b" * 64
        ),
        Stage.TEST: AttestedStage(status="skipped", reason="documentation only"),
    }
    value = make_attestation(
        stages=stages,
        sha=head,
        tree_sha=gitx.tree_sha(tmp_repo, head),
        merge_base_sha=base,
        branch="feature/hidden",
    )

    with pytest.raises(ValueError, match="test skip is not applicable"):
        refresh_validation.verify_evidence(tmp_repo, value)


@pytest.mark.parametrize("mode", ["in_place", "strict"])
def test_local_gate_risk_matches_the_hosted_verdict_after_synchronization(
    tmp_repo, bare_remote, tmp_path, monkeypatch, mode
):
    """Synchronization keeps what the branch would land, with the base tip as an ancestor.

    A rebase would drop the ``-s ours`` merge and silently restore the base's change, so
    synchronization merges the base instead. The published head still reverts the
    high-risk file, and the local gate and hosted check both classify that revert.
    """
    base, original = _criss_cross(tmp_repo, monkeypatch, mode=mode)
    git("push", "origin", "main", cwd=tmp_repo)
    agent = ScriptedAgent(tmp_repo)
    started = agent.run("start")
    agent.run("context")
    payload = tmp_path / "findings.json"
    payload.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("context", "--section", "docs")
    payload.write_text('{"findings":[]}')
    agent.run("submit-findings", "--file", str(payload))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    gate = agent.run("gate")["data"]
    run = Store(tmp_repo / ".git" / "agentic-preflight").load_run(started["run_id"])
    head = git("rev-parse", "HEAD", cwd=tmp_repo)

    assert run.head_sha == head
    assert run.merge_base_sha == base
    assert gitx.is_ancestor(tmp_repo, base, head)
    assert gitx.is_ancestor(tmp_repo, original, head)
    assert gitx.tree_sha(tmp_repo, head) == gitx.merge_tree(tmp_repo, base, original)
    assert git("show", f"{head}:src/security.py", cwd=tmp_repo) == "CHECK = 'on'"
    assert run.changed_files == gitx.classified_files(tmp_repo, base, head)
    hosted = evaluate(
        tmp_repo, base_sha=base, head_sha=head, reviews=[], pull_request_author="author"
    )
    assert hosted["changed_files"] == run.changed_files == ["docs/notes.md", "src/security.py"]
    assert hosted["risk_level"] == gate["risk"]["level"] == "high"
