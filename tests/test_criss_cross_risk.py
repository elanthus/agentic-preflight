"""Criss-cross history must not hide a change from local classification.

Each scenario builds the same history. The base merged a side branch that changed a
high-risk file. The feature branch forks from the earlier main commit and merges that
side branch with ``-s ours``, so Git has two merge bases. It picks the main commit, and
``base...head`` omits the side-branch file even though merging the head into the base
reverts it.
"""

import json
import sys
from datetime import datetime, timedelta

import pytest

from agentic_preflight import gitx, refresh_validation
from agentic_preflight.approval import evaluate
from agentic_preflight.envelope import ExitCode
from agentic_preflight.models import AttestedStage, ReviewCoverage, Stage
from agentic_preflight.store import Store
from tests.attestation_helpers import make_attestation
from tests.conftest import commit_all, git, write
from tests.driver import ScriptedAgent


def _criss_cross(repo, monkeypatch, *, edit_notes=True):
    """Return ``(base, head)`` for a head whose merge into base hides a revert."""
    command = json.dumps(f'"{sys.executable}" -c "print(1)"')
    write(
        repo,
        ".agentic-preflight.toml",
        f"[policy]\nhigh_risk_paths = ['src/**']\n[commands]\nlint = {command}\ntest = {command}\n",
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


def test_start_refuses_a_branch_whose_only_change_is_hidden_by_criss_cross_history(
    tmp_repo, monkeypatch
):
    base, head = _criss_cross(tmp_repo, monkeypatch, edit_notes=False)
    assert gitx.changed_files(tmp_repo, base, head) == []

    result = ScriptedAgent(tmp_repo).run("start", expect=ExitCode.PRECONDITION)

    assert result["error"]["code"] == "empty_diff"
    assert result["data"]["merge_result_files"] == ["src/security.py"]
    assert "git revert" in result["next"]["instruction"]
    # The refusal happens before synchronization, so the branch is left untouched.
    assert git("rev-parse", "HEAD", cwd=tmp_repo) == head


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


def test_local_gate_risk_matches_the_hosted_verdict_after_synchronization(
    tmp_repo, bare_remote, tmp_path, monkeypatch
):
    """Synchronization rebases onto the base tip, so the gated head has one merge base.

    The rebase drops the ``-s ours`` merge, so the published head keeps the base's
    high-risk change, and the local gate and hosted check classify the same paths.
    """
    base, _ = _criss_cross(tmp_repo, monkeypatch)
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
    # Only documentation reaches the gated head, so green lint skips the test stage.
    assert agent.run("stage", "run", "lint")["state"] == "TEST_GREEN"
    agent.run("mergeback")
    gate = agent.run("gate")["data"]
    run = Store(tmp_repo / ".git" / "agentic-preflight").load_run(started["run_id"])
    head = git("rev-parse", "HEAD", cwd=tmp_repo)

    assert run.head_sha == head
    assert run.merge_base_sha == base
    assert gitx.is_ancestor(tmp_repo, base, head)
    assert git("show", f"{head}:src/security.py", cwd=tmp_repo) == "CHECK = 'on'\nSTRICT = 'on'"
    assert run.changed_files == gitx.classified_files(tmp_repo, base, head)
    hosted = evaluate(
        tmp_repo, base_sha=base, head_sha=head, reviews=[], pull_request_author="author"
    )
    assert hosted["changed_files"] == run.changed_files == ["docs/notes.md"]
    assert hosted["risk_level"] == gate["risk"]["level"]
