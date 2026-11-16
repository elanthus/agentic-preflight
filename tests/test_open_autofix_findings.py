"""An open non-blocking ``auto_fix`` finding must be answered before review is left (#343).

Such a finding does not make its stage red, but the attestation refuses evidence
that still holds it. Earlier releases let the run continue to merge-back, which
then failed with an internal error and left the run in ``MERGEBACK_PENDING``,
where ``respond`` is not legal.
"""

import importlib
import json
from pathlib import Path

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
        feature_repo, tmp_path, monkeypatch, CONFIG + "\n[worktree]\nmode = 'reusable'\n"
    )
    source = git("rev-parse", "HEAD", cwd=feature_repo)

    refused = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    _assert_names_open_finding(refused)
    assert refused["state"] == "REVIEW_GREEN"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == source
    status = agent.run("status")
    assert status["data"]["mergeback_attempt"] is None
    assert status["data"]["validation_restarts"] == 0

    _accept(agent)
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"


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


def test_an_isolated_run_stuck_after_its_cherry_pick_recovers(feature_repo, tmp_path, monkeypatch):
    agent = _stuck_in_pending_mergeback(
        feature_repo,
        tmp_path,
        monkeypatch,
        CONFIG + "\n[worktree]\nmode = 'reusable'\n",
        with_fix=True,
    )
    picked = git("rev-parse", "HEAD", cwd=feature_repo)
    assert git("log", "-1", "--format=%s", cwd=feature_repo) == "apply the fix"

    recovered = agent.run("mergeback", expect=ExitCode.PRECONDITION)
    assert recovered["error"]["code"] == "findings_unresolved"
    assert "F002" in recovered["error"]["message"]
    assert [f["id"] for f in recovered["blocking"]] == ["F002"]
    assert recovered["state"] == "REVIEW_AWAITING_FINDINGS"
    assert recovered["next"]["command"] == "agentic-preflight context"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == picked

    agent.run("context")
    reviewed = agent.run("submit-findings", "--file", findings_json(tmp_path, []))
    assert reviewed["state"] == "REVIEW_GREEN"
    assert reviewed["next"]["command"].startswith("agentic-preflight respond --id F002")
    _accept(agent, "F002")
    agent.run("context", "--section", "docs")
    agent.run("submit-findings", "--file", _docs_findings(tmp_path, []))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    assert agent.run("mergeback")["state"] == "VERIFIED"
