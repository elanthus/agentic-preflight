"""Exact-head attestation reuse after synchronizing the base."""

import hashlib

from agentic_preflight import attestation, config
from tests.conftest import commit_all, git, set_home, write
from tests.driver import ScriptedAgent


def _green_run(repo, tmp_path):
    write(
        repo,
        ".agentic-preflight.toml",
        "[docs]\nenabled = false\n\n[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(repo, "configure agentic-preflight")
    agent = ScriptedAgent(repo)
    agent.run("start")
    agent.run("context")
    findings = tmp_path / "findings.json"
    findings.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}\n')
    agent.run("submit-findings", "--file", str(findings))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    return agent


def test_attestation_uses_dedicated_intent_and_config_bindings(feature_repo, tmp_path):
    _green_run(feature_repo, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    expected_config = config.config_digest(config.load_config(feature_repo).model_dump(mode="json"))

    assert (
        value.intent_sha256 == hashlib.sha256(b"exercise the requested behavior safely").hexdigest()
    )
    assert value.config_sha256 == expected_config
    assert value.findings_summary == {}


def test_start_rechecks_refresh_evidence_when_attested_head_contains_fresh_base(
    feature_repo, tmp_path
):
    agent = _green_run(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    old_main = git("rev-parse", "main", cwd=feature_repo)
    fresh_base = git("rev-parse", "HEAD^", cwd=feature_repo)
    git("update-ref", "refs/heads/main", fresh_base, old_main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert "attestation_reused" not in env["data"]
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head


def test_a_different_user_intent_forces_a_fresh_review(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    env = ScriptedAgent(feature_repo).run("start", "--intent", "review a different objective")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head


def test_a_different_docs_config_reuses_review_but_forces_fresh_docs(
    feature_repo, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    user_config = home / ".config" / "agentic-preflight" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("[docs]\nenabled = false\n")
    write(
        feature_repo,
        ".agentic-preflight.toml",
        "[commands]\nlint = 'true'\ntest = 'true'\n",
    )
    commit_all(feature_repo, "configure agentic-preflight")

    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    agent.run("context")
    findings = tmp_path / "config-findings.json"
    findings.write_text('{"coverage":{"manifest":"$context","examined":"all"},"findings":[]}\n')
    agent.run("submit-findings", "--file", str(findings))
    agent.run("stage", "run", "lint")
    agent.run("stage", "run", "test")
    agent.run("mergeback")
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    user_config.write_text("[docs]\nenabled = true\n")
    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "REVIEW_GREEN"
    assert env["next"]["command"] == "agentic-preflight context --section docs"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head


def test_unchanged_attested_commit_imports_green_through_refresh(
    feature_repo, tmp_path, monkeypatch
):
    from agentic_preflight.machine import Action
    from agentic_preflight.models import Stage
    from agentic_preflight.runs import _evidence_install
    from agentic_preflight.store import Store
    from tests.test_evidence_refresh import _finish, _prepare

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD")
    assert original.outcome == "verified"
    agent.run("abort", "--force")
    actions = []
    apply = _evidence_install._apply

    def tracked(doc, action):
        actions.append(action)
        apply(doc, action)

    monkeypatch.setattr(_evidence_install, "_apply", tracked)
    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "TEST_GREEN"
    assert set(env["data"]["applicability"]) == {stage.value for stage in Stage}
    assert all(
        result["disposition"] == "reusable" for result in env["data"]["applicability"].values()
    )
    assert actions == [
        Action.SUBMIT_CLEAN,
        Action.BEGIN_DOCS,
        Action.SUBMIT_CLEAN,
        Action.RUN_LINT,
        Action.LINT_PASSED,
        Action.RUN_TEST,
        Action.TEST_PASSED,
    ]
    run = Store(feature_repo / ".git" / "agentic-preflight").load_run(env["run_id"])
    for stage in Stage:
        record = run.stages[stage]
        result = original.stages[stage]
        for field in ("status", "command", "reason", "exit_code", "output_sha256"):
            assert getattr(record, field) == getattr(result, field)
        assert record.head_sha == original.sha
        assert original.evidence is not None
        assert run.evidence[stage].origin == original.evidence[stage].origin
        assert record.executor == result.executor
        assert record.finished_at == original.evidence[stage].origin.finished_at.isoformat()
    assert run.review_coverage is not None
