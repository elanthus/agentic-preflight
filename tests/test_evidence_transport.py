"""Portable provenance must work without the producer's object database."""

import subprocess
from types import SimpleNamespace

import pytest

from agentic_preflight import attestation, ci_merge, evidence_transport, hook
from tests.conftest import git
from tests.driver import ScriptedAgent
from tests.test_ci_lifecycle import configure, remote_for
from tests.test_evidence_refresh import _finish, _prepare, _restack


@pytest.fixture
def published(request, feature_repo, bare_remote, tmp_path):
    delegated = request.param
    policy = configure(feature_repo) if delegated else None
    if not delegated:
        _prepare(feature_repo)
    git("fetch", str(feature_repo), "main:refs/heads/main", cwd=bare_remote)
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    agent.run("start")
    _finish(agent, tmp_path)
    original = attestation.verify(feature_repo, "HEAD", purpose="publish")
    agent.run("abort", "--force")
    _restack(feature_repo)
    git("fetch", str(feature_repo), "main:refs/heads/main", cwd=bare_remote)
    agent = ScriptedAgent(feature_repo)
    agent.run("start")
    if delegated:
        agent.run("stage", "run", "test")
    agent.run("mergeback")
    value = attestation.verify(feature_repo, "HEAD", purpose="publish")
    assert value.sha != original.sha
    gate = agent.run("gate")
    assert evidence_transport.ref_for(original.sha) in gate["data"]["refspec"]
    dry = agent.run("push", "--confirm", gate["data"]["token"], "--dry-run")
    assert evidence_transport.ref_for(original.sha) in dry["data"]["would_push"]
    agent.run("push", "--confirm", gate["data"]["token"])
    agent.run("finish")
    agent.run("gc")
    for sha in evidence_transport.commits(value):
        ref = evidence_transport.ref_for(sha)
        assert git("rev-parse", ref, cwd=bare_remote) == sha
        assert git("rev-parse", ref, cwd=feature_repo) == sha
    runner = tmp_path / "fresh-runner"
    git("init", str(runner), cwd=tmp_path)
    git("remote", "add", "origin", str(bare_remote), cwd=runner)
    git("fetch", "origin", "main:refs/remotes/origin/main", cwd=runner)
    git("checkout", "--detach", "origin/main", cwd=runner)
    assert subprocess.run(
        ["git", "cat-file", "-e", original.sha], cwd=runner, capture_output=True
    ).returncode != 0
    return runner, bare_remote, original, value, policy, feature_repo


@pytest.mark.parametrize("published", [False], indirect=True)
def test_fresh_hosted_checkout_fetches_unreachable_originals(published):
    runner, _, original, value, _, _ = published
    result = ScriptedAgent(runner).run(
        "hosted-check", value.sha, "--base", value.merge_base_sha,
        "--source-remote", "origin", "--head-ref", "refs/heads/feature/x",
    )
    assert result["data"]["verified"] is True
    assert original.sha in result["data"]["availability"]["attempts"][0]["fetched_evidence_commits"]
    assert git("rev-parse", original.sha + "^{commit}", cwd=runner) == original.sha
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=runner)


@pytest.mark.parametrize("published", [True], indirect=True)
def test_ci_consumer_fetches_originals_from_contributor_refs(published, monkeypatch):
    runner, remote, original, value, policy, producer = published
    # Build the API candidate in the producer, where all objects already exist.
    api = remote_for(producer, policy, value)
    monkeypatch.setattr(
        api, "scoped",
        lambda repository, **kwargs: SimpleNamespace(repository=repository, note=api.note),
    )
    candidate, cfg, _ = ci_merge.snapshot(api, 86)
    invoke = ci_merge.gitx.run
    fetched = []

    def local_transport(repo, *args, **kwargs):
        if "fetch" in args:
            fetched.append(args)
            args = tuple(str(remote) if arg.startswith("https://github.com/") else arg for arg in args)
        return invoke(repo, *args, **kwargs)

    monkeypatch.setattr(ci_merge.gitx, "run", local_transport)
    assert ci_merge.published_attestation(runner, api, candidate, cfg) == value
    assert any(
        evidence_transport.ref_for(original.sha) in args
        and "https://github.com/contributor/fork.git" in args
        for args in fetched
    )


def test_rejected_evidence_ref_prevents_partial_publication(feature_repo, bare_remote, tmp_path):
    _prepare(feature_repo)
    git("fetch", str(feature_repo), "main:refs/heads/main", cwd=bare_remote)
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    agent.run("start")
    _finish(agent, tmp_path)
    gate = agent.run("gate")
    git("config", "receive.hideRefs", evidence_transport.REF_PREFIX, cwd=bare_remote)
    rejected = agent.run("push", "--confirm", gate["data"]["token"], expect=1)
    assert not git(
        "for-each-ref", "refs/heads/feature/x", attestation.NOTES_REF, cwd=bare_remote
    ), rejected
    assert agent.run("status")["state"] == "AWAITING_PUSH_CONFIRM"


@pytest.mark.parametrize("bad", ["missing", "wrong_commit"])
@pytest.mark.parametrize("published", [False], indirect=True)
def test_hosted_provenance_failure_is_not_retried(published, bad):
    runner, remote, original, value, _, _ = published
    ref = evidence_transport.ref_for(original.sha)
    if bad == "missing":
        git("update-ref", "-d", ref, cwd=remote)
    else:
        git("update-ref", ref, value.merge_base_sha, cwd=remote)
    result = ScriptedAgent(runner).run(
        "hosted-check", value.sha, "--base", value.merge_base_sha,
        "--source-remote", "origin", "--head-ref", "refs/heads/feature/x", expect=2,
    )
    assert result["data"]["reason"] == ("git_failure" if bad == "missing" else "commit_mismatch")
    assert len(result["data"]["availability"]["attempts"]) == 1
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=runner)


@pytest.mark.parametrize("destination", ["correct", "wrong_suffix", "branch", "replace"])
def test_hook_exempts_only_immutable_evidence_destinations(destination):
    sha = "a" * 40
    source = evidence_transport.ref_for(sha)
    target = source
    old = "0" * 40
    if destination == "wrong_suffix":
        target = evidence_transport.ref_for("b" * 40)
    elif destination == "branch":
        target = "refs/heads/main"
    elif destination == "replace":
        old = "b" * 40
    decision = hook.evaluate(
        [hook.RefUpdate(source, sha, target, old)],
        is_ancestor=lambda *_: False, has_attestation=lambda _: False,
    )
    assert decision.allowed is (destination == "correct")
