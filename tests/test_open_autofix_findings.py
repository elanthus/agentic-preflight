"""An open non-blocking ``auto_fix`` finding must be answered before review is left (#343).

Such a finding does not make its stage red, but the attestation refuses evidence
that still holds it. Earlier releases let the run continue to merge-back, which
then failed with an internal error and left the run in ``MERGEBACK_PENDING``,
where ``respond`` is not legal.
"""

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_preflight.envelope import ExitCode
from tests.conftest import commit_all, configure_base, git, write
from tests.driver import ScriptedAgent, findings_json

CONFIG = "[commands]\nlint = 'true'\ntest = 'true'\n"

MEDIUM_AUTO_FIX = [
    {
        "path": "src/app.py",
        "line": 1,
        "severity": "medium",
        "action": "auto_fix",
        "title": "the loud flag is ignored",
    }
]

DOCS_AUTO_FIX = [
    {
        "path": "README.md",
        "severity": "medium",
        "action": "auto_fix",
        "title": "the loud flag is undocumented",
    }
]


def _accept(agent, finding_id="F001"):
    return agent.run(
        "respond",
        "--id",
        finding_id,
        "--action",
        "accepted",
        "--reason",
        "other",
        "--note",
        "The flag is intentionally quiet until it is stable.",
    )


def _docs_findings(tmp_path, items):
    path = tmp_path / "docs-findings.json"
    path.write_text(json.dumps({"findings": items}))
    return str(path)


def _review_green_with_open_finding(repo, tmp_path, config=CONFIG):
    configure_base(repo, config)
    agent = ScriptedAgent(repo)
    agent.run("start")
    agent.run("context")
    env = agent.run("submit-findings", "--file", findings_json(tmp_path, MEDIUM_AUTO_FIX))
    assert env["state"] == "REVIEW_GREEN"
    return agent


def _assert_names_open_finding(env, finding_id="F001"):
    assert env["error"]["code"] == "findings_unresolved"
    assert finding_id in env["error"]["message"]
    assert [f["id"] for f in env["blocking"]] == [finding_id]
    assert env["next"]["command"].startswith(f"agentic-preflight respond --id {finding_id}")


def test_an_open_auto_fix_finding_stops_the_run_at_review(feature_repo, tmp_path):
    agent = _review_green_with_open_finding(feature_repo, tmp_path)

    status = agent.run("status")
    assert status["state"] == "REVIEW_GREEN"
    assert status["next"]["command"].startswith("agentic-preflight respond --id F001")

    refused = agent.run("context", "--section", "docs", expect=ExitCode.PRECONDITION)
    _assert_names_open_finding(refused)
    assert refused["state"] == "REVIEW_GREEN"
    assert agent.run("status")["state"] == "REVIEW_GREEN"

    accepted = _accept(agent)
    assert accepted["next"]["command"] == "agentic-preflight context --section docs"
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"


def test_disabled_docs_are_not_skipped_while_a_finding_is_open(feature_repo, tmp_path):
    agent = _review_green_with_open_finding(
        feature_repo, tmp_path, config="[docs]\nenabled = false\n\n" + CONFIG
    )

    env = agent.run("verify")
    assert env["state"] == "REVIEW_GREEN"
    assert env["next"]["command"].startswith("agentic-preflight respond --id F001")
    assert agent.run("status")["state"] == "REVIEW_GREEN"

    accepted = _accept(agent)
    assert accepted["state"] == "DOCS_GREEN"


def test_an_open_docs_finding_stops_lint(feature_repo, tmp_path):
    configure_base(feature_repo, CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, DOCS_AUTO_FIX))

    status = agent.run("status")
    assert status["state"] == "DOCS_GREEN"
    assert status["next"]["command"].startswith("agentic-preflight respond --id F001")

    refused = agent.run("stage", "run", "lint", expect=ExitCode.PRECONDITION)
    _assert_names_open_finding(refused)
    assert refused["state"] == "DOCS_GREEN"

    _accept(agent)
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"


HIGH_AUTO_FIX = {
    "path": "src/app.py",
    "line": 2,
    "severity": "high",
    "action": "auto_fix",
    "title": "the greeting is wrong",
}


def _legacy_test_green(repo, tmp_path, monkeypatch, config, *, with_fix=False):
    """Reach TEST_GREEN with an open finding, as earlier releases allowed.

    ``with_fix`` also records a fix commit, so isolated merge-back cherry-picks it.
    """
    from agentic_preflight import findings

    configure_base(repo, config)
    agent = ScriptedAgent(repo)
    with monkeypatch.context() as legacy:
        # Earlier releases did not look for open auto-fix findings after review.
        legacy.setattr(findings, "actionable", lambda items: [])
        worktree = agent.run("start")["data"]["worktree_path"]
        agent.run("context")
        submitted = [HIGH_AUTO_FIX, *MEDIUM_AUTO_FIX] if with_fix else MEDIUM_AUTO_FIX
        agent.run("submit-findings", "--file", findings_json(tmp_path, submitted))
        if with_fix:
            write(worktree, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
            sha = commit_all(Path(worktree), "apply the fix")
            agent.run("respond", "--id", "F001", "--action", "fixed", "--commit", sha)
            agent.run("verify")
            agent.run("context")
            agent.run("submit-findings", "--file", findings_json(tmp_path, []))
        agent.run("context", "--section", "docs")
        agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
        agent.run("stage", "run", "lint")
        assert agent.run("stage", "run", "test")["state"] == "TEST_GREEN"
    return agent


def _stuck_in_pending_mergeback(repo, tmp_path, monkeypatch, config, *, with_fix=False):
    """Reproduce a run that an earlier release left in MERGEBACK_PENDING."""
    mergeback = importlib.import_module("agentic_preflight.runs.mergeback")

    agent = _legacy_test_green(repo, tmp_path, monkeypatch, config, with_fix=with_fix)
    with monkeypatch.context() as legacy:
        legacy.setattr(mergeback, "unresolved_findings", lambda session, items: [])
        failed = agent.run("mergeback", expect=ExitCode.ERROR)
    assert failed["error"]["code"] == "internal_error"
    assert agent.run("status")["state"] == "MERGEBACK_PENDING"
    return agent


def test_mergeback_refuses_an_open_finding_before_any_effect(feature_repo, tmp_path, monkeypatch):
    agent = _legacy_test_green(
        feature_repo,
        tmp_path,
        monkeypatch,
        CONFIG + "\n[worktree]\nmode = 'reusable'\n",
        with_fix=True,
    )
    source = git("rev-parse", "HEAD", cwd=feature_repo)
    before = agent.run("status")["data"]
    assert before["validation_restarts"] == 1

    mergeback = importlib.import_module("agentic_preflight.runs.mergeback")
    with monkeypatch.context() as differing:
        # Make registration observable: it would rewrite the replay list and log it.
        differing.setattr(mergeback, "derive_fix_commits", lambda session, run: [])
        refused = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    _assert_names_open_finding(refused, "F002")
    assert refused["state"] == "REVIEW_GREEN"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == source
    status = agent.run("status")
    assert status["data"]["mergeback_attempt"] is None
    assert status["data"]["validation_restarts"] == 1
    assert status["data"]["fix_commits"] == before["fix_commits"]
    events = [event["event"] for event in agent.run("events")["data"]["events"]]
    assert "findings_reopened" in events
    assert "validated_fix_commits_registered" not in events

    _accept(agent, "F002")
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    subjects = git("log", "--format=%s", "main..HEAD", cwd=feature_repo).splitlines()
    assert subjects.count("apply the fix") == 1


def test_an_in_place_run_stuck_in_pending_mergeback_recovers(feature_repo, tmp_path, monkeypatch):
    agent = _stuck_in_pending_mergeback(feature_repo, tmp_path, monkeypatch, CONFIG)

    recovered = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    _assert_names_open_finding(recovered)
    assert recovered["state"] == "REVIEW_GREEN"

    _accept(agent)
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"


@pytest.mark.parametrize("mode", ["reusable", "strict"])
def test_an_isolated_run_stuck_after_its_cherry_pick_recovers(
    feature_repo, tmp_path, monkeypatch, mode
):
    # One restart is already spent by the fix; recovery must not spend the last one.
    agent = _stuck_in_pending_mergeback(
        feature_repo,
        tmp_path,
        monkeypatch,
        CONFIG + f"\n[stage]\nmax_restarts = 2\n\n[worktree]\nmode = '{mode}'\n",
        with_fix=True,
    )
    assert agent.run("status")["data"]["validation_restarts"] == 1
    picked = git("rev-parse", "HEAD", cwd=feature_repo)
    assert git("log", "-1", "--format=%s", cwd=feature_repo) == "apply the fix"

    recovered = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert recovered["error"]["code"] == "findings_unresolved"
    assert "F002" in recovered["error"]["message"]
    assert [f["id"] for f in recovered["blocking"]] == ["F002"]
    assert recovered["state"] == "REVIEW_AWAITING_FINDINGS"
    assert recovered["next"]["command"] == "agentic-preflight context"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == picked
    status = agent.run("status")
    assert status["data"]["validation_restarts"] == 1
    assert status["data"]["mergeback_attempt"] is None
    stored = {finding["id"]: finding for finding in status["data"]["findings"]}
    assert stored["F002"]["status"] == "open"

    agent.run("context")
    reviewed = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert reviewed["state"] == "REVIEW_GREEN"
    assert reviewed["next"]["command"].startswith("agentic-preflight respond --id F002")
    answered = _accept(agent, "F002")
    assert answered["data"]["finding"]["status"] == "accepted"
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
    subjects = git("log", "--format=%s", "main..HEAD", cwd=feature_repo).splitlines()
    assert subjects.count("apply the fix") == 1


@pytest.mark.parametrize("dirty_validation", [False, True])
def test_a_legacy_conflict_retry_with_an_open_finding_returns_to_review(
    feature_repo, tmp_path, monkeypatch, dirty_validation
):
    mergeback = importlib.import_module("agentic_preflight.runs.mergeback")
    agent = _legacy_test_green(
        feature_repo,
        tmp_path,
        monkeypatch,
        CONFIG + "\n[worktree]\nmode = 'reusable'\n",
        with_fix=True,
    )
    run_id = agent.run("status")["run_id"]
    fix_sha = git("rev-parse", "HEAD", cwd=Path(agent.run("status")["data"]["worktree_path"]))

    # Diverge the source on the same file. Re-point the recorded source head so
    # the staleness guard does not fire first, as test_mergeback's conflict tests do.
    write(feature_repo, "src/app.py", "BRANCH CONTENT\n")
    new_head = commit_all(feature_repo, "branch rewrite")
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=feature_repo)
    run_path = Path(common) / "agentic-preflight" / "runs" / run_id / "run.json"
    doc = json.loads(run_path.read_text(encoding="utf-8"))
    doc["source_head_sha"] = new_head
    run_path.write_text(json.dumps(doc))
    with monkeypatch.context() as legacy:
        legacy.setattr(mergeback, "unresolved_findings", lambda session, items: [])
        conflict = agent.run("mergeback", expect=ExitCode.NEEDS_HUMAN)
    assert conflict["state"] == "MERGEBACK_CONFLICT"

    picked = subprocess.run(
        ["git", "cherry-pick", fix_sha], cwd=feature_repo, capture_output=True, text=True
    )
    assert picked.returncode != 0
    write(feature_repo, "src/app.py", "def greet(name, loud=False):\n    return 'FIXED'\n")
    git("add", "src/app.py", cwd=feature_repo)
    git("cherry-pick", "--continue", cwd=feature_repo)
    restarts = agent.run("status")["data"]["validation_restarts"]

    if dirty_validation:
        # The recovery resets the validation checkout, so it must not discard work there.
        worktree = Path(agent.run("status")["data"]["worktree_path"])
        edited = write(worktree, "README.md", "uncommitted validation edit\n")
        refused = agent.run("mergeback", expect=ExitCode.PRECONDITION)
        assert refused["error"]["code"] == "dirty_tree"
        assert edited.read_text() == "uncommitted validation edit\n"
        git("checkout", "--", "README.md", cwd=worktree)

    recovered = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert recovered["error"]["code"] == "findings_unresolved"
    assert [f["id"] for f in recovered["blocking"]] == ["F002"]
    assert recovered["state"] == "REVIEW_AWAITING_FINDINGS"
    assert agent.run("status")["data"]["validation_restarts"] == restarts


def test_reused_docs_evidence_does_not_carry_the_run_past_an_open_finding(feature_repo, tmp_path):
    configure_base(feature_repo, CONFIG)
    user_config = Path.home() / ".config" / "agentic-preflight" / "config.toml"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text("[review]\nmax_findings = 40\n")
    first = ScriptedAgent(feature_repo)
    first.run("start")
    first.run("context")
    first.run("submit-findings", "--file", findings_json(tmp_path, []))
    first.run("context", "--section", "docs")
    first.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    first.run("stage", "run", "lint")
    first.run("stage", "run", "test")
    assert first.run("mergeback")["state"] == "VERIFIED"
    first.run("abort", "--reason", "other")

    # A review-only setting changes, so review runs again while docs stay reusable.
    user_config.write_text("[review]\nmax_findings = 30\n")
    second = ScriptedAgent(feature_repo)
    second.run("start")
    second.run("context")
    env = second.run("submit-findings", "--file", findings_json(tmp_path, MEDIUM_AUTO_FIX))
    assert env["state"] == "REVIEW_GREEN"

    status = second.run("status")
    assert status["data"]["applicability"]["docs"]["disposition"] == "reusable"
    assert status["state"] == "REVIEW_GREEN"
    assert status["next"]["command"].startswith("agentic-preflight respond --id F001")
    _accept(second)
    assert second.run("status")["state"] == "DOCS_GREEN"


def test_respond_points_at_an_open_finding_from_another_stage(feature_repo, tmp_path, monkeypatch):
    from agentic_preflight import findings

    configure_base(feature_repo, CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, MEDIUM_AUTO_FIX))
    with monkeypatch.context() as legacy:
        legacy.setattr(findings, "actionable", lambda items: [])
        agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, DOCS_AUTO_FIX))

    answered = _accept(agent, "F002")
    assert answered["state"] == "DOCS_GREEN"
    assert answered["next"]["command"].startswith("agentic-preflight respond --id F001")


def test_status_and_verify_point_at_a_review_finding_from_docs_green(
    feature_repo, tmp_path, monkeypatch
):
    from agentic_preflight import findings

    configure_base(feature_repo, CONFIG)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    agent.run("submit-findings", "--file", findings_json(tmp_path, MEDIUM_AUTO_FIX))
    with monkeypatch.context() as legacy:
        # A run that an earlier release let into documentation with the finding open.
        legacy.setattr(findings, "actionable", lambda items: [])
        agent.run("context", "--section", "docs")
    assert agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))["state"] == (
        "DOCS_GREEN"
    )

    for command in ("status", "verify"):
        env = agent.run(command)
        assert env["state"] == "DOCS_GREEN"
        assert env["next"]["command"].startswith("agentic-preflight respond --id F001")
