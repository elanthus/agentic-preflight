"""Portable provenance must work without the producer's object database."""

import subprocess

import pytest

from agentic_preflight import attestation, evidence_transport, hook
from tests.conftest import git
from tests.driver import ScriptedAgent
from tests.test_evidence_refresh import _finish, _prepare, _restack


@pytest.fixture
def published(feature_repo, bare_remote, tmp_path):
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
    agent.run("stage", "run", "lint")
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
    return runner, bare_remote, original, value, feature_repo


def _fetch_hosted_evidence(runner, sha):
    git(
        "fetch",
        "--no-tags",
        "--",
        "origin",
        sha,
        "+refs/notes/agentic-preflight:refs/notes/agentic-preflight",
        "+refs/agentic-preflight/evidence/*:refs/agentic-preflight/evidence/*",
        cwd=runner,
    )


def test_fresh_hosted_checkout_fetches_unreachable_originals(published):
    runner, _, original, value, _ = published
    _fetch_hosted_evidence(runner, value.sha)
    result = ScriptedAgent(runner).run("verify", value.sha)
    assert result["data"]["verified"] is True
    assert git("rev-parse", original.sha + "^{commit}", cwd=runner) == original.sha


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
def test_hosted_provenance_failure_is_not_retried(published, bad):
    runner, remote, original, value, _ = published
    ref = evidence_transport.ref_for(original.sha)
    if bad == "missing":
        git("update-ref", "-d", ref, cwd=remote)
    else:
        git("update-ref", ref, value.merge_base_sha, cwd=remote)
    _fetch_hosted_evidence(runner, value.sha)
    result = ScriptedAgent(runner).run("verify", value.sha, expect=2)
    assert result["data"]["reason"] == "git_failure"


@pytest.mark.parametrize("allow_force_push", [False, True])
@pytest.mark.parametrize("attested", [False, True])
@pytest.mark.parametrize("ancestor", [False, True])
@pytest.mark.parametrize(
    "destination",
    ["correct", "unchanged", "wrong_suffix", "branch", "replace", "replace_named", "delete"],
)
def test_hook_skips_evidence_destinations_but_gates_branch_pushes(
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
    assert decision.allowed is (destination != "branch" or attested)


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


def test_historical_verified_ci_snapshot_remains_verifiable(feature_repo, tmp_path):
    import json

    from agentic_preflight.digests import json_digest

    _prepare(feature_repo)
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    payload = value.model_dump(mode="json")
    payload.update(publication_ready_at=None, test_delegation=None)
    policy = {
        "test_authority": "local",
        "required_jobs": [],
        "workflow_path": ".github/workflows/preflight-tests.yml",
    }
    payload["config_snapshot"]["ci"] = policy
    payload["config_sha256"] = json_digest(payload["config_snapshot"])
    for item in payload["evidence"].values():
        origin = item["origin"]
        origin["config_snapshot"]["ci"] = policy
        origin["config_sha256"] = json_digest(origin["config_snapshot"])
        item["origin_sha256"] = json_digest(origin)
    historical = attestation.decode(json.dumps(payload))
    assert (
        attestation.verify_value(feature_repo, historical, value.sha, purpose="publish")
        == historical
    )


def test_retired_ci_configuration_runs_local_tests(feature_repo, tmp_path):
    from tests.conftest import commit_all

    _prepare(feature_repo)
    config = feature_repo / ".agentic-preflight.toml"
    config.write_text(config.read_text() + "\n[ci]\ntest_authority = 'github_actions'\n")
    commit_all(feature_repo, "retired ci configuration")
    agent = ScriptedAgent(feature_repo)
    agent.run("init")
    agent.run("start")
    _finish(agent, tmp_path)
    value = attestation.verify(feature_repo, "HEAD")
    assert value.outcome == "verified"
    assert (
        value.stages[next(stage for stage in value.stages if stage.value == "test")].status
        == "green"
    )
    result = agent.run("ci", "status", expect=6)
    assert result["error"]["code"] == "usage_error"
