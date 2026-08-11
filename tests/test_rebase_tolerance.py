"""Green reuse across history-only rebases."""

from agentic_preflight import attestation
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
    findings.write_text('{"findings": []}\n')
    agent.run("submit-findings", "--file", str(findings))
    agent.run("stage", "run", "test")
    agent.run("stage", "run", "lint")
    agent.run("mergeback")
    return agent


def test_start_preserves_green_across_a_tree_and_merge_equivalent_rebase(
    feature_repo, tmp_path
):
    agent = _green_run(feature_repo, tmp_path)
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    old_tree = git("rev-parse", "HEAD^{tree}", cwd=feature_repo)
    agent.run("abort", "--force")

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


def test_identical_tree_is_rejected_when_ancestry_changes_the_merge_outcome(
    feature_repo, tmp_path
):
    agent = _green_run(feature_repo, tmp_path)
    source = git("rev-parse", "HEAD", cwd=feature_repo)
    source_tree = git("rev-parse", "HEAD^{tree}", cwd=feature_repo)
    source_run_id = attestation.verify(feature_repo, source).run_id
    agent.run("abort", "--force")

    git("switch", "main", cwd=feature_repo)
    write(feature_repo, "README.md", "# changed upstream\n")
    base = commit_all(feature_repo, "change the merge outcome")
    target = git(
        "commit-tree", source_tree, "-p", base, "-m", "same snapshot, new ancestry", cwd=feature_repo
    )

    assert attestation.reuse_for_rebase(
        feature_repo,
        sha=target,
        base_sha=base,
        branch="feature/x",
        base_ref="main",
        eligible_run_ids={source_run_id},
    ) is None
    assert attestation.read(feature_repo, target) is None


def test_a_different_user_intent_forces_a_fresh_review(feature_repo, tmp_path):
    agent = _green_run(feature_repo, tmp_path)
    old_head = git("rev-parse", "HEAD", cwd=feature_repo)
    agent.run("abort", "--force")

    main = git("rev-parse", "main", cwd=feature_repo)
    main_tree = git("rev-parse", "main^{tree}", cwd=feature_repo)
    new_main = git("commit-tree", main_tree, "-p", main, "-m", "empty upstream", cwd=feature_repo)
    git("update-ref", "refs/heads/main", new_main, main, cwd=feature_repo)

    env = ScriptedAgent(feature_repo).run(
        "start", "--intent", "review a different objective"
    )
    assert env["state"] == "REVIEW_AWAITING_FINDINGS"
    new_head = git("rev-parse", "HEAD", cwd=feature_repo)
    assert new_head != old_head
    assert attestation.read(feature_repo, new_head) is None
