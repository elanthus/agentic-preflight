"""Exact-head attestation reuse after synchronizing the base."""

import hashlib

from agentic_preflight import attestation, config
from tests.conftest import commit_all, git, write
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


def test_start_preserves_green_when_the_attested_head_already_contains_the_fresh_base(
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
    assert env["state"] == "VERIFIED"
    assert env["next"]["command"] == "agentic-preflight gate"
    assert env["data"]["attestation_reused"] is True
    assert "reused_from_sha" not in env["data"]
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head


def test_start_requires_a_fresh_run_after_a_history_only_rebase(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    main = git("rev-parse", "main", cwd=feature_repo)
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", main, "-m", "empty upstream", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert "attestation_reused" not in env["data"]

    new_head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert new_head != old_head
    assert attestation.read(feature_repo, new_head) is None


def test_exact_attestation_requires_the_fresh_base_to_be_an_ancestor(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    target = git("rev-parse", "HEAD", cwd=feature_repo)
    resolved_config_digest = config.config_digest(
        config.load_config(feature_repo).model_dump(mode="json")
    )
    agent.run("abort", "--force")

    git("switch", "main", cwd=feature_repo)
    main = git("rev-parse", "HEAD", cwd=feature_repo)
    main_tree = git("rev-parse", "HEAD^{tree}", cwd=feature_repo)
    base = git("commit-tree", main_tree, "-p", main, "-m", "divergent fresh base", cwd=feature_repo)
    git("update-ref", "refs/heads/main", base, main, cwd=feature_repo)

    assert (
        attestation.reuse_exact(
            feature_repo,
            sha=target,
            base_sha=base,
            branch="feature/x",
            base_ref="main",
            intent="exercise the requested behavior safely",
            config_digest=resolved_config_digest,
        )
        is None
    )


def test_a_different_user_intent_forces_a_fresh_review(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    env = ScriptedAgent(feature_repo).run("start", "--intent", "review a different objective")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head


def test_a_different_effective_config_forces_a_fresh_review(feature_repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
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
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    assert git("rev-parse", "HEAD", cwd=feature_repo) == head
