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
    commands = gate["data"]["push_commands"]
    assert len(commands) == 2
    assert evidence_transport.ref_for(original.sha) in commands[0]
    assert attestation.NOTES_REF in commands[1]
    dry = agent.run("push", "--confirm", gate["data"]["token"], "--dry-run")
    assert dry["data"]["would_push"] == " && ".join(commands)
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
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", original.sha], cwd=runner, capture_output=True
        ).returncode
        != 0
    )
    return runner, bare_remote, original, value, policy, feature_repo


@pytest.mark.parametrize("published", [False], indirect=True)
def test_fresh_hosted_checkout_fetches_unreachable_originals(published):
    runner, _, original, value, _, _ = published
    result = ScriptedAgent(runner).run(
        "hosted-check",
        value.sha,
        "--base",
        value.merge_base_sha,
        "--source-remote",
        "origin",
        "--head-ref",
        "refs/heads/feature/x",
    )
    assert result["data"]["verified"] is True
    assert original.sha in result["data"]["availability"]["attempts"][0]["fetched_evidence_commits"]
    assert git("rev-parse", original.sha + "^{commit}", cwd=runner) == original.sha
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=runner)


@pytest.mark.parametrize("bad_ref", [False, True])
@pytest.mark.parametrize("published", [True], indirect=True)
def test_ci_consumer_fetches_originals_from_contributor_refs(published, monkeypatch, bad_ref):
    runner, remote, original, value, policy, producer = published
    # Build the API candidate in the producer, where all objects already exist.
    api = remote_for(producer, policy, value)
    monkeypatch.setattr(
        api,
        "scoped",
        lambda repository, **kwargs: SimpleNamespace(repository=repository, note=api.note),
    )
    candidate, cfg, _ = ci_merge.snapshot(api, 86)
    if bad_ref:
        # A wrong descendant ref still imports the expected ancestor object.
        descendant = git(
            "commit-tree",
            original.tree_sha,
            "-p",
            original.sha,
            "-m",
            "wrong ref tip",
            cwd=producer,
        )
        git("fetch", str(producer), descendant, cwd=remote)
        git("update-ref", evidence_transport.ref_for(original.sha), descendant, cwd=remote)
    invoke = ci_merge.gitx.run
    fetched = []

    def local_transport(repo, *args, **kwargs):
        if "fetch" in args:
            fetched.append(args)
            args = tuple(
                str(remote) if arg.startswith("https://github.com/") else arg for arg in args
            )
        return invoke(repo, *args, **kwargs)

    monkeypatch.setattr(ci_merge.gitx, "run", local_transport)
    if bad_ref:
        with pytest.raises(ValueError, match="Fetched evidence ref names a different commit"):
            ci_merge.published_attestation(runner, api, candidate, cfg)
    else:
        assert ci_merge.published_attestation(runner, api, candidate, cfg) == value
    assert not git("for-each-ref", "refs/agentic-preflight/ci-evidence", cwd=runner)
    assert any(
        any(arg.startswith(evidence_transport.ref_for(original.sha) + ":") for arg in args)
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
        "hosted-check",
        value.sha,
        "--base",
        value.merge_base_sha,
        "--source-remote",
        "origin",
        "--head-ref",
        "refs/heads/feature/x",
        expect=2,
    )
    assert result["data"]["reason"] == ("git_failure" if bad == "missing" else "commit_mismatch")
    assert len(result["data"]["availability"]["attempts"]) == 1
    assert not git("for-each-ref", "refs/agentic-preflight/availability", cwd=runner)


@pytest.mark.parametrize("allow_force_push", [False, True])
@pytest.mark.parametrize("attested", [False, True])
@pytest.mark.parametrize("ancestor", [False, True])
@pytest.mark.parametrize(
    "destination",
    ["correct", "unchanged", "wrong_suffix", "branch", "replace", "replace_named", "delete"],
)
def test_hook_exempts_only_immutable_evidence_destinations(
    destination, allow_force_push, attested, ancestor
):
    sha = "a" * 40
    source = evidence_transport.ref_for(sha)
    target = source
    old = "0" * 40
    if destination == "unchanged":
        old = sha
    elif destination == "wrong_suffix":
        target = evidence_transport.ref_for("b" * 40)
    elif destination == "branch":
        target = "refs/heads/main"
    elif destination == "replace":
        old = "b" * 40
    elif destination == "replace_named":
        old = sha
        sha = "b" * 40
    elif destination == "delete":
        old = sha
        sha = "0" * 40
    decision = hook.evaluate(
        [hook.RefUpdate(source, sha, target, old)],
        is_ancestor=lambda *_: ancestor,
        has_attestation=lambda _: attested,
        allow_force_push=allow_force_push,
    )
    assert decision.allowed is (
        destination in {"correct", "unchanged"} or (destination == "branch" and attested)
    )


def test_evidence_replacement_is_blocked_even_from_notes_source():
    sha = "a" * 40
    old = "b" * 40
    decision = hook.evaluate(
        [hook.RefUpdate(attestation.NOTES_REF, sha, evidence_transport.ref_for(old), old)],
        is_ancestor=lambda *_: True,
        has_attestation=lambda _: True,
        allow_force_push=True,
    )
    assert decision.allowed is False
    assert decision.reason == "evidence replacement"


@pytest.mark.parametrize("change", ["delete", "replace", "malformed", "unrelated"])
def test_notes_sync_preserves_selected_evidence(feature_repo, bare_remote, tmp_path, change):
    _prepare(feature_repo)
    git("fetch", str(feature_repo), "main:refs/heads/main", cwd=bare_remote)
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    token = agent.run("gate")["data"]["token"]
    git(
        "fetch",
        str(feature_repo),
        f"{attestation.NOTES_REF}:{attestation.NOTES_REF}",
        cwd=bare_remote,
    )
    git("config", "user.name", "Remote writer", cwd=bare_remote)
    git("config", "user.email", "writer@example.test", cwd=bare_remote)
    if change == "delete":
        git("notes", f"--ref={attestation.NOTES_REF}", "remove", value.sha, cwd=bare_remote)
    else:
        target = value.merge_base_sha if change == "unrelated" else value.sha
        note = (
            "{malformed"
            if change == "malformed"
            else attestation.encode(value.model_copy(update={"run_id": "r_other"}))
        )
        git(
            "notes",
            f"--ref={attestation.NOTES_REF}",
            "add",
            "-f",
            "-m",
            note,
            target,
            cwd=bare_remote,
        )
    result = agent.run("push", "--confirm", token, expect=0 if change == "unrelated" else 2)
    if change == "unrelated":
        assert result["data"]["pushed"] is True
    else:
        assert "Attestation changed during notes synchronization" in result["error"]["message"]
        assert not git(
            "for-each-ref", "refs/heads/feature/x", evidence_transport.REF_PREFIX, cwd=bare_remote
        )
