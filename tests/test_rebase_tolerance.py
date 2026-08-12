"""Green reuse across history-only rebases."""

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


def test_start_preserves_green_across_a_tree_and_merge_equivalent_rebase(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    old_tree = git("rev-parse", "HEAD^{tree}", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    main = git("rev-parse", "main", cwd=feature_repo)
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", main, "-m", "empty upstream", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "VERIFIED"
    assert env["next"]["command"] == "agentic-preflight gate"
    assert env["data"]["attestation_reused"] is True
    assert env["data"]["reused_from_sha"] == old_head

    new_head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert new_head != old_head
    assert git("rev-parse", "HEAD^{tree}", cwd=feature_repo) == old_tree
    reused = attestation.verify(feature_repo, new_head)
    assert reused.merge_base_sha == new_main


def test_identical_tree_is_rejected_when_ancestry_changes_the_merge_outcome(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    source_tree = git("rev-parse", "HEAD^{tree}", cwd=feature_repo)
    agent.run("abort", "--force")

    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "README.md", "# changed upstream\n")
    base = commit_all(feature_repo, "change the merge outcome")
    target = git(
        "commit-tree",
        source_tree,
        "-p",
        base,
        "-m",
        "same snapshot, new ancestry",
        cwd=feature_repo,
    )

    assert (
        attestation.reuse_for_rebase(
            feature_repo,
            sha=target,
            base_sha=base,
            branch="feature/x",
            base_ref="main",
            intent="exercise the requested behavior safely",
            config_digest=config.config_digest(
                config.load_config(feature_repo).model_dump(mode="json")
            ),
        )
        is None
    )
    assert attestation.read(feature_repo, target) is None


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
        attestation.reuse_for_rebase(
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
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    main = git("rev-parse", "main", cwd=feature_repo)
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", main, "-m", "empty upstream", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("start", "--intent", "review a different objective")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    new_head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert new_head != old_head
    assert attestation.read(feature_repo, new_head) is None


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
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")
    agent.run("gc")

    user_config.write_text("[docs]\nenabled = true\n")
    main = git("rev-parse", "main", cwd=feature_repo)
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", main, "-m", "empty upstream", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run("start")
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    new_head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert new_head != old_head
    assert attestation.read(feature_repo, new_head) is None
